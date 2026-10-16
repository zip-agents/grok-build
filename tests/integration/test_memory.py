"""Memory stays bounded and uploads are not written to disk."""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from pathlib import Path

import pytest

from tests.integration.http_util import read_response, request_head
from tests.integration.server_proc import RunningServer

pytestmark = pytest.mark.slow


def _rss(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("VmRSS missing")


def _writable_files(pid: int) -> set[str]:
    found: set[str] = set()
    fd_dir = Path(f"/proc/{pid}/fd")
    for entry in fd_dir.iterdir():
        try:
            target = os.readlink(entry)
            info = Path(f"/proc/{pid}/fdinfo/{entry.name}").read_text(encoding="utf-8")
        except OSError:
            continue
        if not target.startswith("/"):
            continue
        flags = 0
        for line in info.splitlines():
            if line.startswith("flags:"):
                flags = int(line.split()[1], 8)
        access = flags & os.O_ACCMODE
        if access in (os.O_WRONLY, os.O_RDWR):
            found.add(target)
    return found


def test_large_upload_is_bounded_and_not_stored(make_server: object, tmp_path: Path) -> None:
    server: RunningServer = make_server(  # type: ignore[operator]
        OUTPUT_BUFFER_MAX_BYTES=str(1024 * 1024),
        MAX_CONCURRENT_REQUESTS="1",
    )
    total = 24 * 1024 * 1024
    boundary = "bound"
    prefix = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="blob.bin"\r\n\r\n'
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    body_length = len(prefix) + total + len(suffix)
    head = request_head(server.port, boundary, body_length)
    before = _writable_files(server.proc.pid)
    baseline = _rss(server.proc.pid)
    seen = {"rss": baseline, "files": set(before)}

    async def run() -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port, limit=2**20)

        async def sample() -> None:
            while True:
                seen["rss"] = max(int(seen["rss"]), _rss(server.proc.pid))
                seen["files"].update(_writable_files(server.proc.pid))  # type: ignore[attr-defined]
                await asyncio.sleep(0.05)

        sampler = asyncio.create_task(sample())
        try:
            writer.write(head + prefix)
            chunk = bytes((index * 31) % 256 for index in range(64 * 1024))
            sent = 0
            while sent < total:
                writer.write(chunk)
                sent += len(chunk)
                if sent % (1024 * 1024) == 0:
                    await writer.drain()
            writer.write(suffix)
            await writer.drain()
            result = await read_response(reader)
            assert result.status == 200
            assert result.complete
            archive = zipfile.ZipFile(io.BytesIO(result.body))
            assert archive.testzip() is None
            assert archive.getinfo("blob.bin").file_size == total
        finally:
            sampler.cancel()
            writer.close()

    asyncio.run(asyncio.wait_for(run(), timeout=60))
    growth = int(seen["rss"]) - baseline
    assert growth < 20 * 1024 * 1024, f"RSS grew by {growth} bytes"
    extra = set(seen["files"]) - before  # type: ignore[arg-type]
    interesting = {
        path for path in extra if not path.startswith("/dev/") and not path.startswith("/proc/")
    }
    assert interesting == set()
    assert list(tmp_path.iterdir()) == []
