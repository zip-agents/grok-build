"""Small HTTP/1.1 client for integration tests.

httpx would buffer or hide the chunked framing these tests need to see.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass


@dataclass
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes
    complete: bool
    raw: bytes


def multipart(
    parts: list[tuple[str, str | None, bytes]],
    boundary: str = "----zipbound",
) -> tuple[str, bytes]:
    """Build a multipart body. ``filename`` None means a field part."""

    chunks: list[bytes] = []
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        head = f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode()
        chunks.append(head + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(chunks)


def request_head(
    port: int,
    boundary: str,
    body_length: int,
    *,
    method: str = "POST",
    path: str = "/api/v1/zip",
    content_type: str | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    if content_type is None:
        content_type = f"multipart/form-data; boundary={boundary}"
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        f"Content-Type: {content_type}",
        f"Content-Length: {body_length}",
        "Connection: close",
    ]
    if extra:
        for key, value in extra.items():
            lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def exchange(
    port: int,
    head: bytes,
    body: bytes = b"",
    *,
    timeout: float = 10,
) -> HTTPResult:
    """Send a whole request, then read the response. Half-duplex on purpose."""

    async def run() -> HTTPResult:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=2**20)
        try:
            writer.write(head + body)
            await writer.drain()
            return await read_response(reader)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                await writer.wait_closed()

    return await asyncio.wait_for(run(), timeout=timeout)


async def read_response(reader: asyncio.StreamReader) -> HTTPResult:
    raw = bytearray()

    async def read_exactly(count: int) -> bytes:
        data = await reader.readexactly(count)
        raw.extend(data)
        return data

    async def read_line() -> bytes:
        line = await reader.readline()
        raw.extend(line)
        if not line:
            raise asyncio.IncompleteReadError(partial=b"", expected=1)
        return line

    try:
        status_line = await read_line()
    except asyncio.IncompleteReadError:
        return HTTPResult(0, {}, b"", False, bytes(raw))
    parts = status_line.split()
    status = int(parts[1]) if len(parts) > 1 else 0
    headers: dict[str, str] = {}
    while True:
        line = await read_line()
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("iso-8859-1")
        name, value = text.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    if "content-length" in headers:
        try:
            body = await read_exactly(int(headers["content-length"]))
        except asyncio.IncompleteReadError as exc:
            return HTTPResult(status, headers, bytes(exc.partial), False, bytes(raw))
        return HTTPResult(status, headers, body, True, bytes(raw))

    chunks: list[bytes] = []
    complete = False
    try:
        while True:
            size_line = await read_line()
            size_token = size_line.split(b";", 1)[0].strip()
            size = int(size_token, 16)
            if size == 0:
                while True:
                    extra = await read_line()
                    if extra in (b"\r\n", b"\n"):
                        break
                complete = True
                break
            chunks.append(await read_exactly(size))
            crlf = await read_exactly(2)
            if crlf != b"\r\n":
                break
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
        complete = False
    return HTTPResult(status, headers, b"".join(chunks), complete, bytes(raw))


def problem(result: HTTPResult) -> dict[str, object]:
    return json.loads(result.body.decode())
