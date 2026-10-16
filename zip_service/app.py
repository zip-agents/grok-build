"""ASGI application: zip endpoint, health, and problem responses."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from zip_service.api.endpoint import ZipEndpoint
from zip_service.api.gate import Gate
from zip_service.api.openapi import install_openapi
from zip_service.api.problems import problem_response
from zip_service.config import Settings, load_settings
from zip_service.documents import (
    OPENAPI_PATH,
    Catalog,
    Document,
    document_response,
    load_catalog,
    manifest_response,
)
from zip_service.runtime.constants import REQUEST_ID_PATTERN
from zip_service.runtime.jsonlog import configure_logging, emit, request_id_var

_REQUEST_ID_RE = re.compile(REQUEST_ID_PATTERN)

_HTTP_CODES = {
    404: "not-found",
    405: "method-not-allowed",
}

_OPENAPI_TAGS = [
    {"name": "health", "description": "Process probes. They do not take a concurrency slot."},
    {"name": "docs", "description": "Markdown and the manifest fetched by the docs portal."},
    {"name": "zip", "description": "Streaming ZIP uploads, as this process implements them."},
]

_MARKDOWN_HEADERS = {
    "Cache-Control": {
        "description": "Always no-cache.",
        "schema": {"type": "string", "const": "no-cache"},
    },
    "X-Content-Type-Options": {
        "schema": {"type": "string", "const": "nosniff"},
    },
}


class MarkdownResponse(Response):
    """Media type used only so OpenAPI describes the baked markdown."""

    media_type = "text/markdown"


class HealthOk(BaseModel):
    status: Literal["ok"]


class HealthReady(BaseModel):
    status: Literal["ready"]


class HealthShuttingDown(BaseModel):
    status: Literal["shutting-down"]


class ManifestDocument(BaseModel):
    slug: str
    title: str
    path: str


class DocsManifest(BaseModel):
    service: str
    version: str
    revision: str
    contract_version: str
    documents: list[ManifestDocument]
    openapi: str


def create_app(settings: Settings | None = None, *, docs_root: Path | None = None) -> FastAPI:
    """Build the application. Pass ``settings`` in tests; otherwise load them."""

    resolved = settings if settings is not None else load_settings()
    gate = Gate(resolved.max_concurrent_requests)
    catalog = load_catalog(docs_root)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Uvicorn may adjust loggers during startup. Put JSON lines back.
        configure_logging(app.state.settings.log_level)
        emit("INFO", "server started", port=app.state.settings.port)
        yield
        emit("INFO", "server stopped")

    app = FastAPI(
        title="zip-service",
        version=catalog.version,
        description=(
            "Streaming multipart-to-ZIP API as this process implements it. "
            "No servers and no security: the gateway adds those."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_PATH,
        root_path_in_servers=False,
        openapi_tags=_OPENAPI_TAGS,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.gate = gate

    @app.get(
        "/health/live",
        tags=["health"],
        summary="Liveness",
        response_model=HealthOk,
    )
    async def live() -> JSONResponse:
        """The process is running."""
        return JSONResponse({"status": "ok"})

    @app.get(
        "/health/ready",
        tags=["health"],
        summary="Readiness",
        response_model=HealthReady,
        responses={
            503: {
                "model": HealthShuttingDown,
                "description": (
                    "The process is shutting down. The body is "
                    '{"status": "shutting-down"}, not a problem document.'
                ),
            }
        },
    )
    async def ready(request: Request) -> JSONResponse:
        """The process can accept new zip requests."""
        if request.app.state.gate.shutting_down:
            return JSONResponse({"status": "shutting-down"}, status_code=503)
        return JSONResponse({"status": "ready"})

    _register_docs(app, catalog)

    app.router.routes.insert(
        0,
        Route(
            "/api/v1/zip",
            endpoint=ZipEndpoint(resolved, gate),
            methods=["POST"],
            include_in_schema=False,
        ),
    )
    app.add_exception_handler(HTTPException, _http_exception)
    app.add_exception_handler(Exception, _unhandled)
    app.add_middleware(RequestIdMiddleware)
    install_openapi(app, resolved)
    return app


def _register_docs(app: FastAPI, catalog: Catalog) -> None:
    """Register the manifest and each allow-listed document. No path parameters."""

    @app.get(
        "/docs/manifest.json",
        tags=["docs"],
        summary="Documentation manifest",
        response_model=DocsManifest,
        include_in_schema=True,
    )
    async def docs_manifest() -> JSONResponse:
        """Baked markdown paths and the OpenAPI document for this process."""
        return manifest_response(catalog)

    for document in catalog.documents:
        _register_document(app, document)


def _register_document(app: FastAPI, document: Document) -> None:
    async def serve() -> FileResponse:
        """Markdown copied into the image at build time."""
        return document_response(document)

    serve.__name__ = "docs_" + document.slug
    app.add_api_route(
        document.url_path,
        serve,
        methods=["GET"],
        include_in_schema=True,
        tags=["docs"],
        summary=document.title,
        response_class=MarkdownResponse,
        responses={200: {"headers": _MARKDOWN_HEADERS}},
    )


def _request_id(request: Request) -> str:
    value = request.scope.get("zip.request_id")
    if isinstance(value, str):
        return value
    return ""


async def _http_exception(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    code = _HTTP_CODES.get(exc.status_code, "internal-error")
    extra = {key: value for key, value in (exc.headers or {}).items()}
    identifier = _request_id(request)
    emit(
        "INFO",
        "request rejected",
        request_id=identifier,
        method=request.method,
        path=request.url.path,
        status=exc.status_code,
        code=code,
    )
    return problem_response(code, extra_headers=extra or None, request_id=identifier)


async def _unhandled(request: Request, exc: Exception) -> Response:
    identifier = _request_id(request)
    emit(
        "ERROR",
        "unhandled error",
        request_id=identifier,
        error_type=type(exc).__name__,
        method=request.method,
        path=request.url.path,
    )
    return problem_response("internal-error", request_id=identifier)


class RequestIdMiddleware:
    """Echo a client request id when it is safe, otherwise a UUIDv4.

    The id is stored on the ASGI scope because exception handlers run after
    the middleware's context variable has been reset.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = _incoming_request_id(scope)
        scope["zip.request_id"] = request_id
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw_headers = message.get("headers", [])
                headers = list(raw_headers)
                if not any(key == b"x-request-id" for key, _value in headers):
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self._app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


def _incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name != b"x-request-id":
            continue
        text = str(value.decode("latin-1"))
        if _REQUEST_ID_RE.fullmatch(text):
            return text
        break
    return str(uuid.uuid4())
