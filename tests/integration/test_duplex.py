"""Half-duplex, full-duplex, idle abort, and a truncated mid-stream archive."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile

import pytest

from tests.integration.http_util import multipart, read_response, request_head
from tests.integration.server_proc import RunningServer


async def _open(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port, limit=2**20)


def test_half_duplex_succeeds_when_output_fits(make_server: object) -> None:
    server: RunningServer = make_server(OUTPUT_BUFFER_MAX_BYTES=str(256 * 1024))  # type: ignore[operator]
    boundary, body = multipart([("files", "note.txt", b"hello" * 1000)])
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
    assert result.complete  # type: ignore[attr-defined]
    archive = zipfile.ZipFile(io.BytesIO(result.body))  # type: ignore[attr-defined]
    assert archive.read("note.txt") == b"hello" * 1000


def test_full_duplex_larger_than_the_buffer(make_server: object) -> None:
    server: RunningServer = make_server(OUTPUT_BUFFER_MAX_BYTES="65536")  # type: ignore[operator]
    payload = bytes((index * 17) % 256 for index in range(4 * 1024 * 1024))
    boundary, body = multipart([("files", "blob.bin", payload)])
    head = request_head(server.port, boundary, len(body))

    async def run() -> object:
        reader, writer = await _open(server.port)

        async def produce() -> None:
            writer.write(head)
            view = memoryview(body)
            for offset in range(0, len(view), 32 * 1024):
                writer.write(view[offset : offset + 32 * 1024])
                await writer.drain()

        async def consume() -> object:
            return await read_response(reader)

        try:
            _produced, result = await asyncio.gather(produce(), consume())
            return result
        finally:
            writer.close()

    result = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert result.complete  # type: ignore[attr-defined]
    archive = zipfile.ZipFile(io.BytesIO(result.body))  # type: ignore[attr-defined]
    assert archive.testzip() is None
    assert archive.read("blob.bin") == payload


def test_stalled_client_is_aborted_after_idle_timeout(make_server: object) -> None:
    server: RunningServer = make_server(IDLE_TIMEOUT_SECONDS="1")  # type: ignore[operator]
    partial = (
        b'--bound\r\nContent-Disposition: form-data; name="files"; filename="a.txt"\r\n\r\n'
        b"hello-partial"
    )
    head = request_head(server.port, "bound", 5_000_000)

    async def run() -> tuple[float, bytes]:
        reader, writer = await _open(server.port)
        writer.write(head + partial)
        await writer.drain()
        started = time.monotonic()

        async def watch() -> bytes:
            chunks: list[bytes] = []
            while True:
                piece = await reader.read(64 * 1024)
                if not piece:
                    return b"".join(chunks)
                chunks.append(piece)

        try:
            data = await asyncio.wait_for(watch(), timeout=8)
        finally:
            writer.close()
        return time.monotonic() - started, data

    elapsed, data = asyncio.run(run())
    assert elapsed >= 0.9
    assert b"HTTP/1.1 200" in data
    assert not data.endswith(b"0\r\n\r\n")
    assert b"PK\x05\x06" not in data


def test_mid_stream_failure_truncates_the_archive(server: RunningServer) -> None:
    boundary, body = multipart(
        [
            ("files", "a.txt", b"hello"),
            ("other", "b.txt", b"nope"),
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
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(result.body)).testzip()  # type: ignore[attr-defined]
    logs = [line for line in server.zip_logs() if line.get("outcome") == "aborted"]
    assert logs[-1]["code"] == "unexpected-file-field"
    assert logs[-1]["status"] == 400


def test_mid_stream_internal_fault_closes(make_server: object) -> None:
    server: RunningServer = make_server(ZIP_SERVICE_TEST_HOOKS="1")  # type: ignore[operator]
    boundary, body = multipart([("files", "a.txt", b"hello")])
    head = request_head(
        server.port,
        boundary,
        len(body),
        extra={"X-Zip-Test-Fault": "mid-stream"},
    )

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
    finished = server.zip_logs()
    assert finished[-1]["outcome"] == "aborted"
    assert finished[-1]["code"] == "internal-error"
    assert finished[-1]["level"] == "ERROR"
