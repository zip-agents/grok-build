"""ASGI application: zip endpoint, health, and problem responses."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from zip_service.config import Settings, load_settings
from zip_service.constants import REQUEST_ID_PATTERN
from zip_service.endpoint import ZipEndpoint
from zip_service.gate import Gate
from zip_service.jsonlog import configure_logging, emit, request_id_var
from zip_service.problems import problem_response

_REQUEST_ID_RE = re.compile(REQUEST_ID_PATTERN)

_HTTP_CODES = {
    404: "not-found",
    405: "method-not-allowed",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Pass ``settings`` in tests; otherwise load them."""

    resolved = settings if settings is not None else load_settings()
    gate = Gate(resolved.max_concurrent_requests)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Uvicorn may adjust loggers during startup. Put JSON lines back.
        configure_logging(app.state.settings.log_level)
        emit("INFO", "server started", port=app.state.settings.port)
        yield
        emit("INFO", "server stopped")

    app = FastAPI(
        title="zip-service",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.gate = gate

    @app.get("/health/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        if request.app.state.gate.shutting_down:
            return JSONResponse({"status": "shutting-down"}, status_code=503)
        return JSONResponse({"status": "ready"})

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
    return app


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
