"""Streaming ZIP writer.

Entries are written with Zip64 local headers and data descriptors so a file
can be compressed as it arrives. The central directory is written only when
``close`` runs. ``abandon`` drops the archive without that record: ``ZipFile``
would otherwise write it from ``close`` or ``__del__``, and a client would
see a complete archive of a partial upload.
"""

from __future__ import annotations

import queue
import threading
import zipfile
from datetime import UTC, datetime
from typing import IO, Any, cast

from runtime.buffer import BufferClosed, OutputBuffer
from runtime.constants import INPUT_QUEUE_CHUNKS

_FINISH = object()
_ABORT = object()


def dos_datetime(when: datetime) -> tuple[int, int, int, int, int, int]:
    """UTC clock time as a ZIP DOS timestamp, rounded down to an even second."""

    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(UTC)
    second = when.second - (when.second % 2)
    return (when.year, when.month, when.day, when.hour, when.minute, second)


class _Sink:
    """Non-seekable file object. ``tell`` reports bytes accepted by the buffer."""

    def __init__(self, buffer: OutputBuffer) -> None:
        self._buffer = buffer
        self._pos = 0

    def write(self, data: bytes) -> int:
        self._buffer.write(data)
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return False

    def seek(self, offset: int, whence: int = 0) -> int:
        raise OSError("zip output is not seekable")

    def flush(self) -> None:
        return None

    def writable(self) -> bool:
        return True


class _Discard:
    def write(self, data: bytes) -> int:
        return len(data)

    def tell(self) -> int:
        return 0

    def flush(self) -> None:
        return None


class StreamingZip:
    def __init__(self, sink: IO[bytes], level: int, when: datetime) -> None:
        if level == 0:
            compression = zipfile.ZIP_STORED
            compresslevel: int | None = None
        else:
            compression = zipfile.ZIP_DEFLATED
            compresslevel = level
        self._zf = zipfile.ZipFile(
            sink,
            mode="w",
            compression=compression,
            allowZip64=True,
            compresslevel=compresslevel,
        )
        self._level = level
        self._stamp = dos_datetime(when)
        self._handle: IO[bytes] | None = None
        self._closed = False

    def start_file(self, name: str) -> None:
        info = zipfile.ZipInfo(filename=name, date_time=self._stamp)
        info.compress_type = zipfile.ZIP_STORED if self._level == 0 else zipfile.ZIP_DEFLATED
        # ZipFile.open does not copy compresslevel onto the ZipInfo it writes.
        cast(Any, info)._compresslevel = None if self._level == 0 else self._level
        # A non-zero mode stops ZipFile from substituting its default.
        info.external_attr = 0o644 << 16
        self._handle = self._zf.open(info, "w", force_zip64=True)

    def write(self, data: bytes) -> None:
        if self._handle is None:
            raise RuntimeError("write without an open zip entry")
        self._handle.write(data)

    def end_file(self) -> None:
        if self._handle is None:
            raise RuntimeError("end without an open zip entry")
        self._handle.close()
        self._handle = None

    def close(self) -> None:
        """Write the central directory and the end-of-central-directory record."""

        if self._closed:
            return
        self._closed = True
        if self._handle is not None:
            self.end_file()
        self._zf.close()

    def abandon(self) -> None:
        """Stop without writing the central directory."""

        if self._closed:
            return
        self._closed = True
        archive = self._zf
        # Finish a partial entry into a sink that is not the client buffer.
        archive.fp = _Discard()  # type: ignore[assignment]
        handle = self._handle
        self._handle = None
        if handle is not None:
            handle.close()
        archive._writing = False  # type: ignore[attr-defined]
        archive._didModify = False  # type: ignore[attr-defined]
        archive.fp = None


class ZipWorker:
    """Compresses entries on one thread so the event loop stays responsive."""

    def __init__(self, buffer: OutputBuffer, level: int, when: datetime) -> None:
        self._buffer = buffer
        self._queue: queue.Queue[object] = queue.Queue(maxsize=INPUT_QUEUE_CHUNKS)
        self._aborted = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="zip-worker",
            args=(level, when),
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def start_file(self, name: str) -> None:
        self._put(("start", name))

    def write(self, data: bytes) -> None:
        self._put(("data", data))

    def end_file(self) -> None:
        self._put(("end", None))

    def finish(self) -> None:
        """Ask the worker to close the archive and finish the buffer."""

        self._put(_FINISH)

    def try_finish(self) -> bool:
        """Non-blocking ``finish``. False when the input queue is full."""

        return self.try_put(_FINISH)

    def abort(self) -> None:
        self._aborted.set()
        self._buffer.abort()
        try:
            self._queue.put_nowait(_ABORT)
        except queue.Full:
            return

    def join(self, timeout: float = 5) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def error(self) -> BaseException | None:
        return self._error

    def try_put(self, item: object) -> bool:
        """Queue ``item`` without blocking. False when the queue is full.

        The event loop uses this so a full output buffer applies backpressure
        without stalling other requests. ``BufferClosed`` means the worker
        has been aborted.
        """

        if self._aborted.is_set():
            raise BufferClosed
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            return False
        return True

    def _put(self, item: object) -> None:
        while True:
            if self._aborted.is_set():
                raise BufferClosed
            try:
                self._queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def _run(self, level: int, when: datetime) -> None:
        writer = StreamingZip(cast(IO[bytes], _Sink(self._buffer)), level, when)
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    if self._aborted.is_set():
                        writer.abandon()
                        return
                    continue
                if item is _ABORT or self._aborted.is_set():
                    writer.abandon()
                    return
                if item is _FINISH:
                    writer.close()
                    self._buffer.finish()
                    return
                if not isinstance(item, tuple):
                    continue
                kind, payload = item
                if kind == "start":
                    if not isinstance(payload, str):
                        raise RuntimeError("zip entry name must be a string")
                    writer.start_file(payload)
                elif kind == "data":
                    if not isinstance(payload, bytes):
                        raise RuntimeError("zip entry data must be bytes")
                    writer.write(payload)
                elif kind == "end":
                    writer.end_file()
        except BufferClosed:
            writer.abandon()
        except Exception as exc:
            self._error = exc
            writer.abandon()
            self._buffer.abort()
