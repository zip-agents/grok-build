"""Error responses that can be returned before streaming begins."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from tests.integration.http_util import exchange, multipart, problem, request_head
from tests.integration.server_proc import RunningServer


def _get(
    port: int,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            header_map = {key.lower(): value for key, value in response.headers.items()}
            return response.status, header_map, response.read()
    except urllib.error.HTTPError as exc:
        header_map = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, header_map, exc.read()


def _post(
    server: RunningServer,
    *,
    body: bytes = b"",
    boundary: str = "b",
    content_type: str | None = None,
    extra: dict[str, str] | None = None,
    length: int | None = None,
) -> object:
    head = request_head(
        server.port,
        boundary,
        len(body) if length is None else length,
        content_type=content_type,
        extra=extra,
    )
    return asyncio.run(exchange(server.port, head, body))


def test_unsupported_media_type(server: RunningServer) -> None:
    result = _post(server, content_type="text/plain", body=b"hi")
    assert result.status == 415  # type: ignore[attr-defined]
    assert problem(result)["code"] == "unsupported-media-type"  # type: ignore[arg-type]
    assert result.headers["content-type"] == "application/problem+json"  # type: ignore[attr-defined]


def test_missing_boundary_and_garbage_body(server: RunningServer) -> None:
    missing = _post(server, content_type="multipart/form-data", body=b"--x--\r\n")
    assert problem(missing)["code"] == "malformed-request"  # type: ignore[arg-type]
    garbage = _post(server, body=b"this is not a body")
    assert garbage.status == 400  # type: ignore[attr-defined]
    assert problem(garbage)["code"] == "malformed-request"  # type: ignore[arg-type]


def test_no_files_and_unexpected_field(server: RunningServer) -> None:
    boundary, body = multipart([("note", None, b"hello")])
    result = _post(server, body=body, boundary=boundary)
    payload = problem(result)  # type: ignore[arg-type]
    assert result.status == 400  # type: ignore[attr-defined]
    assert payload["code"] == "no-files"
    assert payload["detail"] == "The request contained no file parts with a non-empty filename."
    assert list(payload) == ["type", "title", "status", "detail", "code", "request_id"]

    boundary, other = multipart([("other", "a.txt", b"x")])
    unexpected = _post(server, body=other, boundary=boundary)
    assert problem(unexpected)["code"] == "unexpected-file-field"  # type: ignore[arg-type]


def test_invalid_archive_names(server: RunningServer) -> None:
    boundary, slashed = multipart([("filename", None, b"a/b"), ("files", "a.txt", b"x")])
    slashed_result = _post(server, body=slashed, boundary=boundary)
    assert problem(slashed_result)["code"] == "invalid-archive-name"  # type: ignore[arg-type]

    boundary, twice = multipart(
        [
            ("filename", None, b"one"),
            ("filename", None, b"two"),
            ("files", "a.txt", b"x"),
        ]
    )
    assert problem(_post(server, body=twice, boundary=boundary))["code"] == "invalid-archive-name"  # type: ignore[arg-type]

    boundary, huge = multipart([("filename", None, b"a" * 1025)])
    assert problem(_post(server, body=huge, boundary=boundary))["code"] == "invalid-archive-name"  # type: ignore[arg-type]

    boundary, binary = multipart([("filename", None, b"\xff")])
    assert problem(_post(server, body=binary, boundary=boundary))["code"] == "invalid-archive-name"  # type: ignore[arg-type]


def test_not_found_and_method_not_allowed(server: RunningServer) -> None:
    status, headers, body = _get(server.port, "/no/such")
    payload = json.loads(body)
    assert status == 404
    assert payload["code"] == "not-found"
    assert headers["content-type"] == "application/problem+json"
    assert headers["x-request-id"]

    status, headers, body = _get(server.port, "/health/live/")
    assert status == 404
    assert json.loads(body)["code"] == "not-found"

    status, headers, body = _get(server.port, "/api/v1/zip")
    assert status == 405
    assert json.loads(body)["code"] == "method-not-allowed"
    assert "POST" in headers["allow"]

    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/health/live",
        method="POST",
        data=b"",
    )
    try:
        urllib.request.urlopen(request, timeout=5)
        raise AssertionError("POST /health/live should fail")
    except urllib.error.HTTPError as exc:
        assert exc.code == 405
        assert json.loads(exc.read())["code"] == "method-not-allowed"


def test_request_id_is_echoed_or_generated(server: RunningServer) -> None:
    status, headers, _body = _get(server.port, "/health/live", {"X-Request-ID": "abc.DEF_123"})
    assert status == 200
    assert headers["x-request-id"] == "abc.DEF_123"
    _status, headers, _body = _get(server.port, "/health/live", {"X-Request-ID": "has space"})
    assert headers["x-request-id"] != "has space"
    assert len(headers["x-request-id"]) == 36


def test_idle_timeout_before_streaming(make_server: object) -> None:
    server = make_server(IDLE_TIMEOUT_SECONDS="1", MAX_REQUEST_DURATION_SECONDS="30")  # type: ignore[operator]
    head = request_head(server.port, "b", 1_000_000)

    async def run() -> object:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port, limit=2**20)
        writer.write(head)
        await writer.drain()
        try:
            return await asyncio.wait_for(read_only(reader), timeout=5)
        finally:
            writer.close()

    async def read_only(reader: asyncio.StreamReader) -> object:
        from tests.integration.http_util import read_response

        return await read_response(reader)

    result = asyncio.run(run())
    assert result.status == 408  # type: ignore[attr-defined]
    assert problem(result)["code"] == "request-timeout"  # type: ignore[arg-type]


def test_internal_error_hides_details(make_server: object) -> None:
    server = make_server(ZIP_SERVICE_TEST_HOOKS="1")  # type: ignore[operator]
    _, body = multipart([("files", "a.txt", b"hello")])
    result = _post(server, body=body, extra={"X-Zip-Test-Fault": "before-stream"})
    assert result.status == 500  # type: ignore[attr-defined]
    text = result.body.decode()  # type: ignore[attr-defined]
    assert problem(result)["code"] == "internal-error"  # type: ignore[arg-type]
    assert "Traceback" not in text
    assert "injected" not in text


def test_bad_configuration_exits_nonzero(tmp_path: object) -> None:
    env = os.environ.copy()
    env["PORT"] = "nope"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "zip_service"],
        cwd=tmp_path,  # type: ignore[arg-type]
        env=env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert b"PORT" in completed.stderr
    assert completed.stdout == b""
