"""Concurrency, disconnect, total duration, and graceful shutdown."""

from __future__ import annotations

import asyncio
import json
import signal
import time
import urllib.error
import urllib.request

from tests.integration.http_util import exchange, multipart, problem, read_response, request_head
from tests.integration.server_proc import RunningServer


async def _open(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port, limit=2**20)


def _partial(port: int) -> tuple[bytes, bytes]:
    head = request_head(port, "bound", 5_000_000)
    body = (
        b'--bound\r\nContent-Disposition: form-data; name="files"; filename="a.txt"\r\n\r\npartial'
    )
    return head, body


def test_concurrency_limit_returns_overloaded(make_server: object) -> None:
    server: RunningServer = make_server(  # type: ignore[operator]
        MAX_CONCURRENT_REQUESTS="1",
        IDLE_TIMEOUT_SECONDS="30",
    )
    head, partial = _partial(server.port)
    boundary, small = multipart([("files", "ok.txt", b"ok")])
    second = request_head(server.port, boundary, len(small))

    async def run() -> object:
        reader, writer = await _open(server.port)
        writer.write(head + partial)
        await writer.drain()
        await asyncio.sleep(0.3)
        try:
            started = time.monotonic()
            result = await exchange(server.port, second, small)
            return time.monotonic() - started, result
        finally:
            writer.close()
            reader.feed_eof()

    elapsed, result = asyncio.run(run())
    assert elapsed < 2
    assert result.status == 503
    assert result.headers["retry-after"] == "5"
    assert problem(result)["code"] == "overloaded"


def test_client_disconnect_releases_the_slot(make_server: object) -> None:
    server: RunningServer = make_server(  # type: ignore[operator]
        MAX_CONCURRENT_REQUESTS="1",
        IDLE_TIMEOUT_SECONDS="30",
    )
    head, partial = _partial(server.port)

    async def run() -> None:
        _reader, writer = await _open(server.port)
        writer.write(head + partial)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()
        await writer.wait_closed()

    asyncio.run(run())
    boundary, body = multipart([("files", "ok.txt", b"ok")])
    head = request_head(server.port, boundary, len(body))
    result = asyncio.run(asyncio.wait_for(exchange(server.port, head, body), timeout=5))
    assert result.status == 200
    assert result.complete
    logs = server.zip_logs()
    assert any(line["outcome"] == "client_disconnected" for line in logs)


def test_total_duration_before_streaming(make_server: object) -> None:
    server: RunningServer = make_server(  # type: ignore[operator]
        MAX_REQUEST_DURATION_SECONDS="1",
        IDLE_TIMEOUT_SECONDS="30",
    )
    head = request_head(server.port, "b", 1_000_000)

    async def run() -> object:
        reader, writer = await _open(server.port)
        writer.write(head)
        await writer.drain()
        try:
            return await asyncio.wait_for(read_response(reader), timeout=5)
        finally:
            writer.close()

    result = asyncio.run(run())
    assert result.status == 408  # type: ignore[attr-defined]
    assert problem(result)["code"] == "request-timeout"  # type: ignore[arg-type]


def test_graceful_shutdown_rejects_new_work_and_exits(make_server: object) -> None:
    server: RunningServer = make_server(  # type: ignore[operator]
        SHUTDOWN_GRACE_SECONDS="2",
        IDLE_TIMEOUT_SECONDS="30",
    )
    head, partial = _partial(server.port)

    async def hold() -> None:
        reader, writer = await _open(server.port)
        writer.write(head + partial)
        await writer.drain()
        try:
            await reader.read()
        finally:
            writer.close()

    async def run() -> None:
        task = asyncio.create_task(hold())
        await asyncio.sleep(0.3)
        server.proc.send_signal(signal.SIGTERM)
        await asyncio.sleep(0.2)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/health/ready",
                timeout=2,
            ) as response:
                raise AssertionError(f"ready stayed up: {response.status}")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            assert json.loads(exc.read()) == {"status": "shutting-down"}
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}/health/live",
            timeout=2,
        ) as response:
            assert json.loads(response.read()) == {"status": "ok"}
        boundary, body = multipart([("files", "a.txt", b"x")])
        request = request_head(server.port, boundary, len(body))
        result = await exchange(server.port, request, body)
        assert result.status == 503
        assert problem(result)["code"] == "shutting-down"
        assert "retry-after" not in result.headers
        await task

    asyncio.run(run())
    assert server.proc.wait(timeout=10) == 0


def test_too_many_files_aborts_after_streaming_begins(make_server: object) -> None:
    """MAX_FILES is reached only after the first file has started the response."""

    server: RunningServer = make_server(MAX_FILES="1", IDLE_TIMEOUT_SECONDS="30")  # type: ignore[operator]
    boundary, body = multipart(
        [
            ("files", "a.txt", b"one"),
            ("files", "b.txt", b"two"),
        ]
    )
    head = request_head(server.port, boundary, len(body))

    async def run() -> object:
        reader, writer = await _open(server.port)
        writer.write(head + body)
        await writer.drain()
        try:
            return await read_response(reader)
        finally:
            writer.close()

    result = asyncio.run(run())
    assert result.status == 200  # type: ignore[attr-defined]
    assert result.complete is False  # type: ignore[attr-defined]
    assert not result.raw.endswith(b"0\r\n\r\n")  # type: ignore[attr-defined]
    logs = [line for line in server.zip_logs() if line.get("outcome") == "aborted"]
    assert logs[-1]["code"] == "too-many-files"
    assert logs[-1]["status"] == 413
    assert logs[-1]["level"] == "WARNING"


def test_inflight_request_is_aborted_when_grace_expires(make_server: object) -> None:
    server: RunningServer = make_server(  # type: ignore[operator]
        SHUTDOWN_GRACE_SECONDS="1",
        IDLE_TIMEOUT_SECONDS="30",
    )
    head, partial = _partial(server.port)

    async def run() -> bytes:
        reader, writer = await _open(server.port)
        writer.write(head + partial)
        await writer.drain()
        await asyncio.sleep(0.2)
        server.proc.send_signal(signal.SIGTERM)

        async def watch() -> bytes:
            chunks: list[bytes] = []
            while True:
                piece = await reader.read(64 * 1024)
                if not piece:
                    return b"".join(chunks)
                chunks.append(piece)

        try:
            return await asyncio.wait_for(watch(), timeout=8)
        finally:
            writer.close()

    data = asyncio.run(run())
    assert b"HTTP/1.1 200" in data
    assert server.proc.wait(timeout=10) == 0
    aborted = [line for line in server.zip_logs() if line.get("outcome") == "aborted"]
    assert aborted
    assert aborted[-1]["code"] == "shutting-down"
