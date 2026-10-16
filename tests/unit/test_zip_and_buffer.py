import asyncio
import gc
import io
import threading
import zipfile
from datetime import UTC, datetime

import pytest

from zip_service.archive.zipstream import StreamingZip, ZipWorker, dos_datetime
from zip_service.runtime.buffer import BufferClosed, OutputBuffer


class _Mem:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0

    def write(self, data: bytes) -> int:
        self.buf += data
        self.pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self.pos

    def seekable(self) -> bool:
        return False

    def seek(self, offset: int, whence: int = 0) -> int:
        raise OSError("not seekable")

    def flush(self) -> None:
        return None


def _archive(level: int, name: str, payload: bytes) -> bytes:
    mem = _Mem()
    writer = StreamingZip(mem, level, datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC))  # type: ignore[arg-type]
    writer.start_file(name)
    writer.write(payload)
    writer.end_file()
    writer.close()
    return bytes(mem.buf)


def test_dos_timestamp_is_utc_and_even() -> None:
    stamp = dos_datetime(datetime(2024, 6, 1, 12, 0, 7, tzinfo=UTC))
    assert stamp == (2024, 6, 1, 12, 0, 6)


def test_streaming_zip_round_trip_and_zip64_local_header() -> None:
    payload = bytes(range(256)) + b"hello"
    data = _archive(6, "café.txt", payload)
    assert data[:4] == b"PK\x03\x04"
    assert int.from_bytes(data[4:6], "little") == 45
    flags = int.from_bytes(data[6:8], "little")
    assert flags & 0x800  # UTF-8 names
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.testzip() is None
    info = archive.infolist()[0]
    assert info.filename == "café.txt"
    assert archive.read(info) == payload
    assert info.date_time[:5] == (2024, 1, 2, 3, 4)


def test_level_zero_is_stored() -> None:
    data = _archive(0, "plain.txt", b"abc")
    info = zipfile.ZipFile(io.BytesIO(data)).infolist()[0]
    assert info.compress_type == zipfile.ZIP_STORED
    assert zipfile.ZipFile(io.BytesIO(data)).read("plain.txt") == b"abc"


def test_empty_file_is_a_valid_entry() -> None:
    data = _archive(6, "empty.txt", b"")
    assert zipfile.ZipFile(io.BytesIO(data)).read("empty.txt") == b""


def test_abandon_does_not_write_the_central_directory() -> None:
    mem = _Mem()
    writer = StreamingZip(mem, 6, datetime(2024, 1, 1, tzinfo=UTC))  # type: ignore[arg-type]
    writer.start_file("partial.txt")
    writer.write(b"abc" * 100)
    writer.abandon()
    del writer
    gc.collect()
    assert b"PK\x05\x06" not in bytes(mem.buf)
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(bytes(mem.buf))).testzip()


def test_worker_round_trip() -> None:
    async def run() -> bytes:
        loop = asyncio.get_running_loop()
        buffer = OutputBuffer(64 * 1024)
        buffer.bind(loop)
        worker = ZipWorker(buffer, 6, datetime(2024, 5, 5, 1, 2, 3, tzinfo=UTC))
        worker.start()
        worker.start_file("a.txt")
        worker.write(b"xyz")
        worker.end_file()
        worker.finish()
        pieces: list[bytes] = []
        while True:
            chunk = await buffer.read()
            if chunk is None:
                break
            pieces.append(chunk)
        worker.join()
        return b"".join(pieces)

    data = asyncio.run(run())
    assert zipfile.ZipFile(io.BytesIO(data)).read("a.txt") == b"xyz"


def test_output_buffer_slices_large_writes_and_abort() -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        buffer = OutputBuffer(8)
        buffer.bind(loop)
        errors: list[BaseException] = []

        def produce() -> None:
            try:
                buffer.write(b"abcdefghij")  # larger than the buffer
                buffer.finish()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread = threading.Thread(target=produce)
        thread.start()
        got = bytearray()
        while True:
            chunk = await buffer.read()
            if chunk is None:
                break
            assert len(chunk) <= 8
            got += chunk
        thread.join()
        assert errors == []
        assert got == b"abcdefghij"
        buffer.abort()
        with pytest.raises(BufferClosed):
            await buffer.read()

    asyncio.run(run())
