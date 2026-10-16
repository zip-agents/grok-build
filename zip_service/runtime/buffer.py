"""Bounded buffer between the zip worker and the socket writer.

When the buffer is full the worker stops, which stops the parser from reading
the upload. That is the backpressure described in the contract: a client that
is not reading cannot force the process to retain more than
``OUTPUT_BUFFER_MAX_BYTES`` of compressed output.
"""

from __future__ import annotations

import asyncio
import threading

from zip_service.runtime.constants import READ_CHUNK_BYTES


class BufferClosed(Exception):
    """The buffer was aborted. Writers and readers must stop."""


class OutputBuffer:
    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("output buffer must hold at least one byte")
        self._max = max_bytes
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._eof = False
        self._aborted = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._event = asyncio.Event()

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Block until ``data`` fits, or raise ``BufferClosed`` on abort.

        A single write may be larger than the buffer. It is sliced so a small
        configured buffer cannot deadlock.
        """

        if not data:
            return
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            with self._cond:
                while len(self._buf) >= self._max and not self._aborted:
                    self._cond.wait(timeout=0.2)
                if self._aborted:
                    raise BufferClosed
                room = self._max - len(self._buf)
                count = min(room, len(view) - offset)
                self._buf.extend(view[offset : offset + count])
                offset += count
                self._cond.notify_all()
            self._wake()

    def finish(self) -> None:
        """No further writes. ``read`` returns the remainder, then ``None``."""

        with self._cond:
            self._eof = True
            self._cond.notify_all()
        self._wake()

    def abort(self) -> None:
        """Drop buffered output and unblock every waiter."""

        with self._cond:
            self._aborted = True
            self._buf.clear()
            self._cond.notify_all()
        self._wake()

    async def read(self) -> bytes | None:
        """Return the next bytes, ``None`` at a clean end, or raise if aborted."""

        if self._event is None:
            raise RuntimeError("output buffer is not bound to an event loop")
        while True:
            with self._cond:
                if self._aborted:
                    raise BufferClosed
                if self._buf:
                    # Hand the socket one chunk at a time. Returning the whole
                    # buffer would hold a second copy while ``send`` blocks.
                    count = min(len(self._buf), READ_CHUNK_BYTES)
                    data = bytes(self._buf[:count])
                    del self._buf[:count]
                    self._cond.notify_all()
                    return data
                if self._eof:
                    return None
                self._event.clear()
            await self._event.wait()

    def _wake(self) -> None:
        event = self._event
        loop = self._loop
        if event is None or loop is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            return
