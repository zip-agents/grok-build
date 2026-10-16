"""Docs endpoints against a real server process."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tests.integration.http_util import HTTPResult, exchange
from tests.integration.server_proc import RunningServer

_ROOT = Path(__file__).resolve().parents[2]


def test_listed_documents_match_the_files_on_disk(make_server: object) -> None:
    server: RunningServer = make_server(GIT_SHA="3f2c9e1")  # type: ignore[operator]
    manifest = _get(server.port, "/docs/manifest.json")
    assert manifest.status == 200
    assert manifest.headers["content-type"] == "application/json"
    payload = json.loads(manifest.body)
    assert payload["service"] == "zip-service"
    assert payload["version"] == "1.2.0"
    assert payload["openapi"] == "/docs/openapi.json"
    assert payload["revision"] == "3f2c9e1"
    assert payload["contract_version"] == "1.1.0"
    assert [item["slug"] for item in payload["documents"]] == ["readme", "agents"]

    for item in payload["documents"]:
        result = _get(server.port, item["path"])
        assert result.status == 200
        assert result.headers["content-type"] == "text/markdown; charset=utf-8"
        assert result.headers["cache-control"] == "no-cache"
        assert result.headers["x-content-type-options"] == "nosniff"
        filename = item["path"].removeprefix("/docs/")
        assert result.body == (_ROOT / filename).read_bytes()

    missing = _get(server.port, "/docs/no-such.md")
    assert missing.status == 404
    assert json.loads(missing.body)["code"] == "not-found"
    assert missing.headers["content-type"] == "application/problem+json"


def test_openapi_document_lists_the_zip_route(make_server: object) -> None:
    server: RunningServer = make_server()  # type: ignore[operator]
    manifest = _get(server.port, "/docs/manifest.json")
    path = json.loads(manifest.body)["openapi"]
    document = _get(server.port, path)
    assert document.status == 200
    assert document.headers["content-type"] == "application/json"
    schema = json.loads(document.body)
    assert "/api/v1/zip" in schema["paths"]
    assert "post" in schema["paths"]["/api/v1/zip"]


def _get(port: int, path: str) -> HTTPResult:
    head = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    return asyncio.run(exchange(port, head.encode()))
