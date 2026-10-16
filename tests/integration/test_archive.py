"""Archive bytes, headers, and the tools that must accept them."""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from tests.integration.http_util import exchange, multipart, request_head
from tests.integration.server_proc import RunningServer

pytestmark = pytest.mark.filterwarnings("ignore")

UNZIP = shutil.which("unzip")
SEVEN = shutil.which("7z") or shutil.which("7zz")


def setup_module() -> None:
    if UNZIP is None or SEVEN is None:
        raise RuntimeError("unzip and 7z must be on PATH")


async def _zip(server: RunningServer, parts: list[tuple[str, str | None, bytes]]) -> bytes:
    boundary, body = multipart(parts)
    head = request_head(server.port, boundary, len(body))
    result = await exchange(server.port, head, body)
    assert result.status == 200, result.body[:300]
    assert result.complete, "response ended before the terminating chunk"
    assert result.headers["content-type"] == "application/zip"
    assert "content-length" not in result.headers
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["x-request-id"]
    return result.body


def test_contract_example(server: RunningServer, tmp_path: Path) -> None:
    import asyncio

    body = asyncio.run(
        _zip(
            server,
            [
                ("filename", None, b"quarterly report"),
                ("files", "a.txt", b"alpha"),
                ("files", "a.txt", b"beta"),
                ("files", "photo.jpg", b"\xff\xd8jpeg"),
            ],
        )
    )
    archive = zipfile.ZipFile(io.BytesIO(body))
    assert archive.testzip() is None
    assert archive.namelist() == ["a.txt", "a (1).txt", "photo.jpg"]
    assert archive.read("a.txt") == b"alpha"
    assert archive.read("a (1).txt") == b"beta"
    assert archive.read("photo.jpg") == b"\xff\xd8jpeg"
    # Every local header asks for Zip64.
    assert body[4:6] == (45).to_bytes(2, "little")

    path = tmp_path / "out.zip"
    path.write_bytes(body)
    unzip = subprocess.run([UNZIP or "unzip", "-t", str(path)], capture_output=True)
    assert unzip.returncode == 0, unzip.stderr.decode(errors="replace")
    seven = subprocess.run([SEVEN or "7z", "t", str(path)], capture_output=True)
    assert seven.returncode == 0, seven.stderr.decode(errors="replace")

    logs = server.wait_zip_logs()
    assert len(logs) == 1
    assert logs[0]["outcome"] == "completed"
    assert logs[0]["status"] == 200
    assert logs[0]["files_count"] == 3
    assert "code" not in logs[0]


def test_disposition_header_and_unicode_entry(server: RunningServer) -> None:
    import asyncio

    boundary, body = multipart(
        [
            ("filename", None, "café".encode()),
            ("files", "café.txt", "é".encode()),
        ]
    )
    head = request_head(server.port, boundary, len(body))
    result = asyncio.run(exchange(server.port, head, body))
    assert result.status == 200
    disposition = result.headers["content-disposition"]
    assert 'filename="caf_.zip"' in disposition
    assert "filename*=UTF-8''caf%C3%A9.zip" in disposition
    archive = zipfile.ZipFile(io.BytesIO(result.body))
    info = archive.infolist()[0]
    assert info.filename == "café.txt"
    assert info.flag_bits & 0x800
    assert archive.read(info) == "é".encode()


def test_zero_byte_file_and_skipped_empty_filename(server: RunningServer) -> None:
    import asyncio

    body = asyncio.run(
        _zip(
            server,
            [
                ("files", "", b""),
                ("files", "empty.txt", b""),
                ("note", None, b"ignored"),
            ],
        )
    )
    archive = zipfile.ZipFile(io.BytesIO(body))
    assert archive.namelist() == ["empty.txt"]
    assert archive.read("empty.txt") == b""
    warnings = [
        line
        for line in server.logs()
        if line.get("message") == "ignoring field part received after streaming began"
    ]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "WARNING"
    assert warnings[0]["field"] == "note"
    assert len(server.wait_zip_logs()) == 1


def test_stored_method_when_level_is_zero(make_server: object) -> None:
    import asyncio

    server = make_server(ZIP_COMPRESSION_LEVEL="0")  # type: ignore[operator]
    body = asyncio.run(_zip(server, [("files", "plain.txt", b"abc")]))
    info = zipfile.ZipFile(io.BytesIO(body)).getinfo("plain.txt")
    assert info.compress_type == zipfile.ZIP_STORED
