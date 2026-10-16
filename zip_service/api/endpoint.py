"""POST /api/v1/zip.

The upload is parsed on the event loop. Compression runs on one thread per
request. Compressed bytes sit in a bounded buffer; the socket is written by a
second task. When that buffer is full the parser stops reading, which is the
backpressure from contract section 4.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime

from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from zip_service.api.gate import Gate
from zip_service.api.multipart import (
    AsyncMultipart,
    MediaTypeError,
    MultipartError,
    Part,
    classify_content_type,
)
from zip_service.api.problems import ABORT_STATUS, PROBLEMS, problem_body
from zip_service.archive.disposition import content_disposition
from zip_service.archive.names import EntryNamer, InvalidArchiveName, resolve_archive_name
from zip_service.archive.zipstream import ZipWorker
from zip_service.config import Settings
from zip_service.runtime.buffer import BufferClosed, OutputBuffer
from zip_service.runtime.constants import FILENAME_FIELD_MAX_BYTES, RETRY_AFTER_SECONDS
from zip_service.runtime.jsonlog import emit

_POLL_SECONDS = 0.1

# Outcomes the contract asks for on the single completion line.
_CLIENT_CODES = frozenset(
    {
        "malformed-request",
        "no-files",
        "unexpected-file-field",
        "request-timeout",
        "too-many-files",
        "unsupported-media-type",
        "invalid-archive-name",
        "client-disconnected",
    }
)


class RequestRejected(Exception):
    """A normal HTTP error, sent before the zip status line."""

    def __init__(self, code: str, headers: dict[str, str] | None = None) -> None:
        self.code = code
        self.headers = headers
        super().__init__(code)


class StreamAbort(Exception):
    """Streaming has started. The connection must close without a final chunk."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Tracker:
    def __init__(self, gate: Gate, idle_seconds: float, total_seconds: float) -> None:
        self.gate = gate
        now = time.monotonic()
        self.start = now
        self.last = now
        self.idle_seconds = idle_seconds
        self.total_seconds = total_seconds
        self.bytes_in = 0
        self.bytes_out = 0
        self.files = 0
        self.streaming = False
        self.clean = False
        self.held = False
        self.worker: ZipWorker | None = None
        self.writer: asyncio.Task[None] | None = None

    def note_in(self, count: int) -> None:
        if count <= 0:
            return
        self.bytes_in += count
        self.last = time.monotonic()

    def note_out(self, count: int) -> None:
        if count <= 0:
            return
        self.bytes_out += count
        self.last = time.monotonic()

    def failure_code(self) -> str | None:
        if self.gate.force_code:
            return self.gate.force_code
        now = time.monotonic()
        if now - self.start >= self.total_seconds:
            return "request-timeout"
        if now - self.last >= self.idle_seconds:
            return "request-timeout"
        return None


class _Completion:
    def __init__(self, tracker: _Tracker, method: str, path: str) -> None:
        self._tracker = tracker
        self._method = method
        self._path = path
        self._done = False

    def emit(
        self,
        *,
        status: int,
        outcome: str,
        code: str | None = None,
        error_type: str | None = None,
    ) -> None:
        if self._done:
            return
        self._done = True
        fields: dict[str, object] = {
            "method": self._method,
            "path": self._path,
            "status": status,
            "duration_ms": round((time.monotonic() - self._tracker.start) * 1000),
            "bytes_in": self._tracker.bytes_in,
            "bytes_out": self._tracker.bytes_out,
            "files_count": self._tracker.files,
            "outcome": outcome,
        }
        if outcome != "completed":
            fields["code"] = code or "internal-error"
        if error_type is not None:
            fields["error_type"] = error_type
        emit(_level(outcome, code), "zip request finished", **fields)


def _level(outcome: str, code: str | None) -> str:
    if outcome == "completed":
        return "INFO"
    if code == "internal-error":
        return "ERROR"
    if code == "shutting-down":
        return "INFO"
    if code in _CLIENT_CODES or code == "overloaded":
        return "WARNING"
    return "ERROR"


class ZipEndpoint:
    """Raw ASGI endpoint. A function would be wrapped as ``(request) -> response``."""

    def __init__(self, settings: Settings, gate: Gate) -> None:
        self._settings = settings
        self._gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_id = _scope_request_id(scope)
        tracker = _Tracker(
            self._gate,
            float(self._settings.idle_timeout_seconds),
            float(self._settings.max_request_duration_seconds),
        )
        completion = _Completion(tracker, str(scope.get("method", "")), str(scope.get("path", "")))
        try:
            await self._run(scope, receive, send, tracker, completion, request_id)
        except RequestRejected as exc:
            completion.emit(status=ABORT_STATUS[exc.code], outcome="rejected", code=exc.code)
            await _send_problem(send, exc.code, request_id, exc.headers)
        except ClientDisconnect:
            completion.emit(
                status=200 if tracker.streaming else 0,
                outcome="client_disconnected",
                code="client-disconnected",
            )
        except MultipartError:
            if tracker.streaming:
                completion.emit(status=400, outcome="aborted", code="malformed-request")
            else:
                completion.emit(status=400, outcome="rejected", code="malformed-request")
                with contextlib.suppress(Exception):
                    await _send_problem(send, "malformed-request", request_id)
        except StreamAbort as exc:
            completion.emit(status=ABORT_STATUS[exc.code], outcome="aborted", code=exc.code)
        except asyncio.CancelledError:
            if self._gate.force_code:
                completion.emit(status=503, outcome="aborted", code="shutting-down")
            else:
                completion.emit(
                    status=200 if tracker.streaming else 0,
                    outcome="client_disconnected",
                    code="client-disconnected",
                )
            raise
        except Exception as exc:
            if tracker.streaming:
                completion.emit(
                    status=500,
                    outcome="aborted",
                    code="internal-error",
                    error_type=type(exc).__name__,
                )
            else:
                completion.emit(
                    status=500,
                    outcome="rejected",
                    code="internal-error",
                    error_type=type(exc).__name__,
                )
                with contextlib.suppress(Exception):
                    await _send_problem(send, "internal-error", request_id)
        finally:
            await _stop(tracker)
            if tracker.held:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._gate.release())
                tracker.held = False

    async def _run(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        tracker: _Tracker,
        completion: _Completion,
        request_id: str,
    ) -> None:
        if self._gate.shutting_down:
            raise RequestRejected("shutting-down")

        content_type = _header(scope, b"content-type")
        try:
            boundary = classify_content_type(content_type)
        except MediaTypeError as exc:
            raise RequestRejected(exc.code) from exc

        refusal = await self._gate.try_acquire()
        if refusal == "overloaded":
            raise RequestRejected(refusal, {"retry-after": RETRY_AFTER_SECONDS})
        if refusal is not None:
            raise RequestRejected(refusal)
        tracker.held = True
        current = asyncio.current_task()
        if current is not None:
            self._gate.track(current)

        if _test_fault(scope) == "before-stream":
            raise RuntimeError("injected before streaming")

        received_at = datetime.now(UTC)
        archive_value: str | None = None
        filename_fields = 0
        namer = EntryNamer()
        writer_box: list[asyncio.Task[None] | None] = [None]

        async def chunks() -> AsyncIterator[bytes]:
            while True:
                message = await _wait(receive(), tracker, writer_box[0])
                if message["type"] == "http.disconnect":
                    raise ClientDisconnect
                body = message.get("body", b"")
                if not isinstance(body, bytes):
                    body = bytes(body)
                tracker.note_in(len(body))
                if body:
                    yield body
                if not message.get("more_body", False):
                    return

        parser = AsyncMultipart(chunks(), boundary)
        fault = _test_fault(scope)

        while True:
            part = await parser.next_part()
            if part is None:
                break
            if part.is_file and part.filename == "":
                await part.discard()
                continue
            if part.is_file:
                await self._accept_file(
                    part,
                    tracker,
                    namer,
                    archive_value,
                    received_at,
                    writer_box,
                    send,
                    request_id,
                    fault,
                )
                continue
            if tracker.streaming:
                emit(
                    "WARNING",
                    "ignoring field part received after streaming began",
                    field=part.name or "",
                )
                await part.discard()
                continue
            if part.name == "filename":
                filename_fields += 1
                if filename_fields > 1:
                    raise RequestRejected("invalid-archive-name")
                raw = await part.read_limited(FILENAME_FIELD_MAX_BYTES)
                if raw is None:
                    raise RequestRejected("invalid-archive-name")
                try:
                    archive_value = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RequestRejected("invalid-archive-name") from exc
                try:
                    resolve_archive_name(archive_value)
                except InvalidArchiveName as exc:
                    raise RequestRejected("invalid-archive-name") from exc
                continue
            await part.discard()

        if not tracker.streaming:
            raise RequestRejected("no-files")

        worker = tracker.worker
        assert worker is not None
        await _enqueue(worker, None, tracker, writer_box[0], finish=True)
        await parser.drain()
        writer_task = writer_box[0]
        assert writer_task is not None
        await writer_task
        await _send_body(send, b"", more=False, tracker=tracker, writer_task=None)
        tracker.clean = True
        completion.emit(status=200, outcome="completed")

    async def _accept_file(
        self,
        part: Part,
        tracker: _Tracker,
        namer: EntryNamer,
        archive_value: str | None,
        received_at: datetime,
        writer_box: list[asyncio.Task[None] | None],
        send: Send,
        request_id: str,
        fault: str | None,
    ) -> None:
        if part.name != "files":
            if tracker.streaming:
                raise StreamAbort("unexpected-file-field")
            raise RequestRejected("unexpected-file-field")
        if tracker.files >= self._settings.max_files:
            if tracker.streaming:
                raise StreamAbort("too-many-files")
            raise RequestRejected("too-many-files")
        if not tracker.streaming:
            try:
                archive_name = resolve_archive_name(archive_value)
            except InvalidArchiveName as exc:
                raise RequestRejected("invalid-archive-name") from exc
            await self._start_streaming(
                archive_name,
                tracker,
                received_at,
                writer_box,
                send,
                request_id,
            )
            if fault == "mid-stream":
                raise StreamAbort("internal-error")
        worker = tracker.worker
        assert worker is not None
        entry = namer.allocate(part.filename or "file")
        await _enqueue(worker, ("start", entry), tracker, writer_box[0])
        tracker.files += 1
        async for chunk in part.iter_chunks():
            await _enqueue(worker, ("data", chunk), tracker, writer_box[0])
        await _enqueue(worker, ("end", None), tracker, writer_box[0])

    async def _start_streaming(
        self,
        archive_name: str,
        tracker: _Tracker,
        received_at: datetime,
        writer_box: list[asyncio.Task[None] | None],
        send: Send,
        request_id: str,
    ) -> None:
        # Mark streaming before the status line so a failure while sending
        # headers cannot fall back to a second HTTP response.
        tracker.streaming = True
        output = OutputBuffer(self._settings.output_buffer_max_bytes)
        output.bind(asyncio.get_running_loop())
        worker = ZipWorker(output, self._settings.zip_compression_level, received_at)
        tracker.worker = worker
        worker.start()
        disposition = content_disposition(archive_name).encode("ascii")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/zip"),
                    (b"content-disposition", disposition),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-request-id", request_id.encode("ascii")),
                ],
            }
        )

        async def writer() -> None:
            while True:
                try:
                    data = await _wait(output.read(), tracker, None)
                except BufferClosed as exc:
                    raise _abort_from_buffer(tracker, worker) from exc
                if data is None:
                    return
                await _send_body(send, data, more=True, tracker=tracker, writer_task=None)
                tracker.note_out(len(data))

        writer_box[0] = asyncio.create_task(writer())
        tracker.writer = writer_box[0]


def _abort_from_buffer(tracker: _Tracker, worker: ZipWorker) -> StreamAbort:
    if worker.error is not None:
        return StreamAbort("internal-error")
    return StreamAbort(tracker.failure_code() or "internal-error")


async def _enqueue(
    worker: ZipWorker,
    item: tuple[str, object] | None,
    tracker: _Tracker,
    writer_task: asyncio.Task[None] | None,
    *,
    finish: bool = False,
) -> None:
    while True:
        _raise_limited(tracker)
        _raise_writer(writer_task)
        try:
            queued = worker.try_finish() if finish else worker.try_put(item)
        except BufferClosed as exc:
            raise _abort_from_buffer(tracker, worker) from exc
        if queued:
            return
        await asyncio.sleep(_POLL_SECONDS)


def _raise_limited(tracker: _Tracker) -> None:
    code = tracker.failure_code()
    if code is None:
        return
    if tracker.streaming:
        raise StreamAbort(code)
    raise RequestRejected(code)


def _raise_writer(writer_task: asyncio.Task[None] | None) -> None:
    if writer_task is None or not writer_task.done() or writer_task.cancelled():
        return
    exc = writer_task.exception()
    if exc is not None:
        raise exc


async def _wait[T](
    awaitable: Awaitable[T],
    tracker: _Tracker,
    writer_task: asyncio.Task[None] | None,
) -> T:
    task: asyncio.Task[T] = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            _raise_limited(tracker)
            _raise_writer(writer_task)
            await asyncio.wait({task}, timeout=_POLL_SECONDS)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _send_body(
    send: Send,
    data: bytes,
    *,
    more: bool,
    tracker: _Tracker,
    writer_task: asyncio.Task[None] | None,
) -> None:
    task = asyncio.ensure_future(
        send({"type": "http.response.body", "body": data, "more_body": more})
    )
    try:
        while not task.done():
            _raise_limited(tracker)
            _raise_writer(writer_task)
            await asyncio.wait({task}, timeout=_POLL_SECONDS)
        task.result()
    except (BrokenPipeError, ConnectionResetError, ConnectionError) as exc:
        raise ClientDisconnect from exc
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _send_problem(
    send: Send,
    code: str,
    request_id: str,
    extra: dict[str, str] | None = None,
) -> None:
    body = problem_body(code, request_id)
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/problem+json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-request-id", request_id.encode("ascii")),
        (b"connection", b"close"),
    ]
    if extra:
        for key, value in extra.items():
            headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    await send(
        {
            "type": "http.response.start",
            "status": PROBLEMS[code].status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _stop(tracker: _Tracker) -> None:
    writer = tracker.writer
    worker = tracker.worker
    if not tracker.clean and worker is not None:
        worker.abort()
    if writer is not None and not writer.done():
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await writer
    if worker is not None:
        worker.join(timeout=2)


def _test_fault(scope: Scope) -> str | None:
    if os.environ.get("ZIP_SERVICE_TEST_HOOKS") != "1":
        return None
    raw = _header(scope, b"x-zip-test-fault")
    if raw in {"before-stream", "mid-stream"}:
        return raw
    return None


def _header(scope: Scope, name: bytes) -> str | None:
    headers = scope.get("headers", [])
    for key, value in headers:
        if key == name:
            return str(value.decode("latin-1"))
    return None


def _scope_request_id(scope: Scope) -> str:
    value = scope.get("zip.request_id")
    if isinstance(value, str):
        return value
    return ""
