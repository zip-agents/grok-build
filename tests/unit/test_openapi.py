"""OpenAPI document: paths, the zip operation, and routes that stay off."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.routing import APIRoute
from starlette.routing import Route

from zip_service.api.endpoint import ZipEndpoint
from zip_service.api.openapi import zip_http_codes, zip_operation
from zip_service.api.problems import PROBLEMS
from zip_service.app import create_app
from zip_service.config import Settings

_EXPECTED_ROUTES = {
    ("/health/live", "get"),
    ("/health/ready", "get"),
    ("/docs/manifest.json", "get"),
    ("/docs/README.md", "get"),
    ("/docs/AGENTS.md", "get"),
    ("/api/v1/zip", "post"),
}

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}

# Independent of zip_http_codes(), so a hand-written example list cannot match by accident.
_MID_STREAM_ONLY = frozenset({"too-many-files"})
_NOT_ZIP = frozenset({"not-found", "method-not-allowed"})


def test_document_lists_the_implemented_routes() -> None:
    app = create_app(Settings())
    schema = app.openapi()
    assert app.openapi() is schema
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["version"] == "1.2.0"
    assert "servers" not in schema
    assert "security" not in schema
    assert len(json.dumps(schema).encode()) <= 1024 * 1024
    found = {
        (path, method)
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in _HTTP_METHODS and isinstance(operation, dict)
    }
    assert found == _EXPECTED_ROUTES
    for operation in _operations(schema):
        assert "security" not in operation
    assert "HTTPValidationError" not in schema["components"]["schemas"]


def test_zip_route_stays_a_starlette_route() -> None:
    app = create_app(Settings())
    matches = [
        route for route in app.router.routes if getattr(route, "path", None) == "/api/v1/zip"
    ]
    assert len(matches) == 1
    route = matches[0]
    assert type(route) is Route
    assert not isinstance(route, APIRoute)
    assert isinstance(route.endpoint, ZipEndpoint)
    assert route.methods == {"POST"}


def test_zip_request_body_lists_filename_then_files() -> None:
    settings = Settings(max_files=7)
    operation = zip_operation(settings)
    assert operation["tags"] == ["zip"]
    assert operation["operationId"] == "streamZip"
    assert operation["summary"]
    description = operation["description"]
    assert "filename field before any file part" in description
    assert "truncated archive" in description
    assert "terminating HTTP chunk" in description
    body = operation["requestBody"]["content"]["multipart/form-data"]
    properties = body["schema"]["properties"]
    assert list(properties) == ["filename", "files"]
    filename = properties["filename"]
    assert filename["type"] == "string"
    assert filename["maxLength"] == 1024
    assert filename["examples"] == ["quarterly report"]
    files = properties["files"]
    assert files["type"] == "array"
    assert files["items"] == {"type": "string", "format": "binary"}
    assert files["minItems"] == 1
    assert files["maxItems"] == 7
    assert body["schema"]["required"] == ["files"]
    assert body["encoding"] == {"files": {"contentType": "application/octet-stream"}}
    success = operation["responses"]["200"]
    assert success["content"]["application/zip"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    for name in (
        "Content-Disposition",
        "Cache-Control",
        "X-Content-Type-Options",
        "X-Request-ID",
    ):
        assert name in success["headers"]


def test_zip_problem_codes_match_the_table() -> None:
    schema = create_app(Settings()).openapi()
    operation = schema["paths"]["/api/v1/zip"]["post"]
    expected = {code for code in PROBLEMS if code not in _NOT_ZIP and code not in _MID_STREAM_ONLY}
    assert set(zip_http_codes()) == expected
    documented: set[str] = set()
    for status, response in operation["responses"].items():
        content = response.get("content", {})
        if "application/problem+json" not in content:
            continue
        examples = content["application/problem+json"]["examples"]
        assert set(examples) <= expected
        for code, example in examples.items():
            problem = PROBLEMS[code]
            value = example["value"]
            assert value["title"] == problem.title
            assert value["detail"] == problem.detail
            assert value["status"] == problem.status
            assert value["status"] == int(status)
            assert code not in documented
            documented.add(code)
        assert response["description"]
        for code in examples:
            assert code in response["description"]
    assert documented == expected
    assert "413" not in operation["responses"]
    assert "404" not in operation["responses"]
    assert "405" not in operation["responses"]
    overloaded = operation["responses"]["503"]["headers"]["Retry-After"]
    assert overloaded["schema"]["const"] == "5"
    problem_422 = operation["responses"]["422"]["content"]
    assert "application/problem+json" in problem_422
    assert "application/json" not in problem_422


def test_health_and_docs_are_not_json_validation_errors() -> None:
    schema = create_app(Settings()).openapi()
    live = schema["paths"]["/health/live"]["get"]
    ready = schema["paths"]["/health/ready"]["get"]
    readme = schema["paths"]["/docs/README.md"]["get"]
    assert live["tags"] == ["health"]
    assert live["summary"] == "Liveness"
    assert "422" not in live["responses"]
    assert "503" in ready["responses"]
    assert ready["responses"]["503"]["content"]["application/json"]
    assert "422" not in ready["responses"]
    markdown = readme["responses"]["200"]["content"]
    assert "text/markdown" in markdown
    assert "application/json" not in markdown
    assert readme["tags"] == ["docs"]
    assert "422" not in readme["responses"]
    manifest = schema["paths"]["/docs/manifest.json"]["get"]
    assert "application/json" in manifest["responses"]["200"]["content"]
    assert "422" not in manifest["responses"]


def test_every_ref_resolves() -> None:
    schema = create_app(Settings()).openapi()
    refs = list(_refs(schema))
    assert refs
    for ref in refs:
        assert ref.startswith("#/")
        _resolve(schema, ref)


def test_framework_docs_stay_off() -> None:
    app = create_app(Settings())
    for path in ("/redoc", "/openapi.json", "/docs"):
        status, headers, body = asyncio.run(_invoke(app, path))
        assert status == 404
        assert json.loads(body)["code"] == "not-found"
        assert headers["content-type"] == "application/problem+json"
    status, headers, body = asyncio.run(_invoke(app, "/docs/openapi.json"))
    assert status == 200
    assert headers["content-type"] == "application/json"
    served = json.loads(body)
    assert "/api/v1/zip" in served["paths"]
    assert "servers" not in served
    assert "security" not in served


def _operations(schema: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in schema["paths"].values():
        for method, operation in item.items():
            if method in _HTTP_METHODS and isinstance(operation, dict):
                found.append(operation)
    return found


def _refs(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.append(ref)
        for value in node.values():
            found.extend(_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_refs(item))
    return found


def _resolve(document: dict[str, Any], ref: str) -> object:
    cursor: object = document
    for part in ref[2:].split("/"):
        assert isinstance(cursor, dict)
        cursor = cursor[part.replace("~1", "/").replace("~0", "~")]
    return cursor


async def _invoke(app: object, path: str) -> tuple[int, dict[str, str], bytes]:
    body = bytearray()
    status = 0
    raw_headers: list[tuple[bytes, bytes]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])  # type: ignore[arg-type]
            raw_headers.extend(message.get("headers", []))  # type: ignore[arg-type]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))  # type: ignore[arg-type]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 9),
        "server": ("127.0.0.1", 80),
    }
    await app(scope, receive, send)  # type: ignore[operator]
    headers = {key.decode().lower(): value.decode() for key, value in raw_headers}
    return status, headers, bytes(body)


def test_max_items_follows_the_process_settings() -> None:
    operation = create_app(Settings(max_files=3)).openapi()["paths"]["/api/v1/zip"]["post"]
    files = operation["requestBody"]["content"]["multipart/form-data"]["schema"]["properties"]
    assert files["files"]["maxItems"] == 3
