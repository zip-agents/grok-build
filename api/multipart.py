"""Streaming multipart/form-data parser.

The parser never accumulates a part body. It keeps at most one read chunk plus
a short boundary lookahead, and it caps preamble and part headers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from starlette.requests import ClientDisconnect

from runtime.constants import PART_HEADER_MAX_BYTES, PREAMBLE_MAX_BYTES, READ_CHUNK_BYTES

_BCHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'()+_,-./:=? ")


class MultipartError(Exception):
    """The body is not a well-formed multipart message."""


class MediaTypeError(Exception):
    """Content-Type is missing, not multipart, or has no usable boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Need:
    pass


NEED = _Need()


@dataclass(frozen=True, slots=True)
class PartStart:
    name: str | None
    filename: str | None


@dataclass(frozen=True, slots=True)
class PartData:
    data: bytes


@dataclass(frozen=True, slots=True)
class PartEnd:
    pass


@dataclass(frozen=True, slots=True)
class Finished:
    pass


type Event = PartStart | PartData | PartEnd | Finished | _Need


def classify_content_type(value: str | None) -> str:
    """Return the multipart boundary, or raise ``MediaTypeError``."""

    if value is None or not value.strip():
        raise MediaTypeError("unsupported-media-type")
    try:
        media, params = _parse_type_and_params(value)
    except MultipartError as exc:
        raise MediaTypeError("malformed-request") from exc
    if media != "multipart/form-data":
        raise MediaTypeError("unsupported-media-type")
    boundary = params.get("boundary")
    if boundary is None or not _valid_boundary(boundary):
        raise MediaTypeError("malformed-request")
    return boundary


class MultipartDecoder:
    """Pull parser. Call ``push`` when ``next`` returns ``NEED``."""

    def __init__(self, boundary: str) -> None:
        if not _valid_boundary(boundary):
            raise MultipartError("invalid boundary")
        raw = boundary.encode("ascii")
        self._open = b"--" + raw
        self._delim = b"\r\n--" + raw
        self._buf = bytearray()
        self._eof = False
        self._phase = "first"

    def push(self, data: bytes) -> None:
        if data:
            self._buf.extend(data)

    def finish(self) -> None:
        self._eof = True

    def next(self) -> Event:
        if self._phase == "first":
            return self._read_first()
        if self._phase == "headers":
            return self._read_headers()
        if self._phase == "body":
            return self._read_body()
        if self._phase == "boundary":
            return self._read_boundary_tail()
        return Finished()

    def _read_first(self) -> Event:
        token = self._open
        search_from = 0
        while True:
            idx = self._buf.find(token, search_from)
            if idx < 0:
                if len(self._buf) > PREAMBLE_MAX_BYTES + len(token):
                    raise MultipartError("missing initial boundary")
                if self._eof:
                    raise MultipartError("missing initial boundary")
                # Drop preamble we can no longer match, keeping a token tail.
                keep = len(token) + 2
                if len(self._buf) > keep:
                    del self._buf[:-keep]
                return NEED
            at_line_start = idx == 0 or (idx >= 2 and self._buf[idx - 2 : idx] == b"\r\n")
            if not at_line_start:
                search_from = idx + 1
                continue
            after = idx + len(token)
            if after >= len(self._buf):
                if self._eof:
                    raise MultipartError("truncated boundary")
                return NEED
            if not _is_boundary_terminator(self._buf[after]):
                search_from = idx + 1
                continue
            del self._buf[:after]
            self._phase = "boundary"
            return self._read_boundary_tail()

    def _read_headers(self) -> Event:
        marker = self._buf.find(b"\r\n\r\n")
        if marker < 0:
            if len(self._buf) > PART_HEADER_MAX_BYTES:
                raise MultipartError("part headers too large")
            if self._eof:
                raise MultipartError("truncated part headers")
            return NEED
        if marker > PART_HEADER_MAX_BYTES:
            raise MultipartError("part headers too large")
        block = bytes(self._buf[:marker])
        del self._buf[: marker + 4]
        name, filename = _headers_to_disposition(block)
        self._phase = "body"
        return PartStart(name=name, filename=filename)

    def _read_body(self) -> Event:
        delim = self._delim
        idx = self._buf.find(delim)
        if idx < 0:
            if self._eof:
                raise MultipartError("missing closing boundary")
            keep = len(delim) - 1
            available = len(self._buf) - keep
            if available <= 0:
                return NEED
            take = min(available, READ_CHUNK_BYTES)
            data = bytes(self._buf[:take])
            del self._buf[:take]
            return PartData(data)
        if idx > 0:
            take = min(idx, READ_CHUNK_BYTES)
            data = bytes(self._buf[:take])
            del self._buf[:take]
            return PartData(data)
        after = len(delim)
        if after >= len(self._buf):
            if self._eof:
                raise MultipartError("truncated boundary")
            return NEED
        if not _is_boundary_terminator(self._buf[after]):
            # ``\r\n--<boundary>`` was a prefix of ordinary body bytes.
            take = min(after + 1, READ_CHUNK_BYTES)
            data = bytes(self._buf[:take])
            del self._buf[:take]
            return PartData(data)
        del self._buf[:after]
        self._phase = "boundary"
        return PartEnd()

    def _read_boundary_tail(self) -> Event:
        index = 0
        size = len(self._buf)
        while index < size and self._buf[index] in (0x20, 0x09):
            index += 1
        if index >= size:
            if self._eof:
                raise MultipartError("truncated boundary")
            return NEED
        if self._buf[index] == 0x2D:
            if index + 1 >= size:
                if self._eof:
                    raise MultipartError("truncated closing boundary")
                return NEED
            if self._buf[index + 1] != 0x2D:
                raise MultipartError("malformed closing boundary")
            cursor = index + 2
            while cursor < size and self._buf[cursor] in (0x20, 0x09):
                cursor += 1
            if cursor >= size:
                if self._eof:
                    del self._buf[:cursor]
                    self._phase = "done"
                    return Finished()
                return NEED
            if self._buf[cursor : cursor + 2] == b"\r\n":
                cursor += 2
            elif size - cursor == 1 and self._buf[cursor] == 0x0D and not self._eof:
                return NEED
            elif not self._eof:
                raise MultipartError("malformed closing boundary")
            del self._buf[:cursor]
            self._phase = "done"
            return Finished()
        if self._buf[index : index + 2] == b"\r\n":
            del self._buf[: index + 2]
            self._phase = "headers"
            return self._read_headers()
        if size - index == 1 and self._buf[index] == 0x0D and not self._eof:
            return NEED
        raise MultipartError("malformed boundary")


class Part:
    """One multipart part. Its body can be read once."""

    def __init__(self, reader: AsyncMultipart, name: str | None, filename: str | None) -> None:
        self._reader = reader
        self.name = name
        self.filename = filename
        self.is_file = filename is not None
        self._consumed = False

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        if self._consumed:
            raise RuntimeError("part body already consumed")
        self._consumed = True
        while True:
            event = await self._reader._next_event()
            if isinstance(event, PartData):
                if event.data:
                    yield event.data
                continue
            if isinstance(event, PartEnd):
                return
            raise MultipartError("unexpected multipart event while reading a part")

    async def discard(self) -> None:
        async for _chunk in self.iter_chunks():
            pass

    async def read_limited(self, limit: int) -> bytes | None:
        """Return the body when it fits in ``limit`` bytes.

        Returns ``None`` without reading the rest when the body is larger.
        The connection must then be closed.
        """

        pieces: list[bytes] = []
        total = 0
        async for chunk in self.iter_chunks():
            total += len(chunk)
            if total > limit:
                return None
            pieces.append(chunk)
        return b"".join(pieces)


class AsyncMultipart:
    """Drive a ``MultipartDecoder`` from an async byte stream."""

    def __init__(
        self,
        source: AsyncIterator[bytes],
        boundary: str,
        on_bytes: Callable[[int], None] | None = None,
    ) -> None:
        self._source = source
        self._decoder = MultipartDecoder(boundary)
        self._on_bytes = on_bytes
        self._source_done = False
        self._finished = False

    async def next_part(self) -> Part | None:
        if self._finished:
            return None
        event = await self._next_event()
        if isinstance(event, Finished):
            self._finished = True
            return None
        if isinstance(event, PartStart):
            return Part(self, event.name, event.filename)
        raise MultipartError("unexpected multipart event between parts")

    async def drain(self) -> None:
        """Read and discard any epilogue so the HTTP body is fully consumed."""

        if self._source_done:
            return
        while True:
            try:
                chunk = await anext(self._source)
            except StopAsyncIteration:
                self._source_done = True
                return
            except ClientDisconnect:
                self._source_done = True
                raise
            if chunk and self._on_bytes is not None:
                self._on_bytes(len(chunk))

    async def _next_event(self) -> Event:
        while True:
            event = self._decoder.next()
            if event is not NEED:
                return event
            if self._source_done:
                raise MultipartError("truncated multipart body")
            await self._fill()

    async def _fill(self) -> None:
        try:
            chunk = await anext(self._source)
        except StopAsyncIteration:
            self._decoder.finish()
            self._source_done = True
            return
        except ClientDisconnect:
            self._source_done = True
            raise
        if chunk and self._on_bytes is not None:
            self._on_bytes(len(chunk))
        self._decoder.push(chunk)


def _valid_boundary(boundary: str) -> bool:
    return 1 <= len(boundary) <= 70 and all(character in _BCHARS for character in boundary)


def _is_boundary_terminator(byte: int) -> bool:
    # CRLF, the closing "--", or transport padding. Anything else means the
    # matched text is a prefix of ordinary body content.
    return byte in (0x20, 0x09, 0x0D, 0x2D)


def _parse_type_and_params(value: str) -> tuple[str, dict[str, str]]:
    segments = _split_semicolon(value)
    if not segments or not segments[0].strip():
        raise MultipartError("empty content type")
    media = segments[0].strip().lower()
    params: dict[str, str] = {}
    for segment in segments[1:]:
        piece = segment.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise MultipartError("malformed content-type parameter")
        key, raw_value = piece.split("=", 1)
        params[key.strip().lower()] = _unquote(raw_value.strip())
    return media, params


def _headers_to_disposition(block: bytes) -> tuple[str | None, str | None]:
    if not block:
        return None, None
    text = block.decode("latin-1")
    disposition: str | None = None
    current_name: str | None = None
    current_value: list[str] = []

    def finish_line() -> None:
        nonlocal disposition, current_name, current_value
        if current_name == "content-disposition" and disposition is None and current_value:
            disposition = " ".join(current_value)
        current_name = None
        current_value = []

    for line in text.split("\r\n"):
        if line.startswith((" ", "\t")) and current_name is not None:
            current_value.append(line.strip())
            continue
        finish_line()
        if not line:
            continue
        if ":" not in line:
            raise MultipartError("malformed part header")
        name, value = line.split(":", 1)
        current_name = name.strip().lower()
        current_value = [value.strip()]
    finish_line()
    if disposition is None:
        return None, None
    return _parse_content_disposition(disposition)


def _parse_content_disposition(value: str) -> tuple[str | None, str | None]:
    segments = _split_semicolon(value)
    params: dict[str, str] = {}
    extended: dict[str, str] = {}
    for segment in segments[1:]:
        piece = segment.strip()
        if not piece or "=" not in piece:
            continue
        key, raw_value = piece.split("=", 1)
        key = key.strip().lower()
        raw_value = raw_value.strip()
        if key.endswith("*"):
            extended[key[:-1]] = raw_value
        else:
            params[key] = _unquote(raw_value)
    name = params.get("name")
    if name is not None:
        name = _header_text(name)
    if "filename" in extended:
        try:
            filename: str | None = _decode_extended_value(extended["filename"])
        except ValueError:
            filename = _header_text(params["filename"]) if "filename" in params else ""
    elif "filename" in params:
        filename = _header_text(params["filename"])
    else:
        filename = None
    return name, filename


def _header_text(value: str) -> str:
    """Interpret a header parameter as UTF-8 when those bytes are valid.

    Header bytes are exposed as Latin-1 code points. Browsers send UTF-8 in
    ``filename``; clients that sent a single Latin-1 byte keep that character
    when it is not valid UTF-8.
    """

    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def _decode_extended_value(value: str) -> str:
    text = _unquote(value) if value.startswith('"') else value
    charset, separator, rest = text.partition("'")
    if not separator:
        raise ValueError("extended parameter is missing a charset")
    _language, separator, encoded = rest.partition("'")
    if not separator:
        raise ValueError("extended parameter is missing a language tag")
    raw = _percent_decode(encoded)
    normalised = charset.lower().replace("_", "-")
    if normalised in {"utf-8", "utf8"}:
        return raw.decode("utf-8")
    if normalised in {"us-ascii", "ascii"}:
        return raw.decode("ascii")
    if normalised in {"iso-8859-1", "latin-1"}:
        return raw.decode("latin-1")
    raise ValueError(f"unsupported charset {charset}")


def _percent_decode(text: str) -> bytes:
    output = bytearray()
    index = 0
    while index < len(text):
        character = text[index]
        if character == "%":
            hex_digits = text[index + 1 : index + 3]
            if len(hex_digits) != 2 or any(
                digit not in "0123456789abcdefABCDEF" for digit in hex_digits
            ):
                raise ValueError("bad percent-encoding")
            output.append(int(hex_digits, 16))
            index += 3
            continue
        encoded = character.encode("ascii")
        output.extend(encoded)
        index += 1
    return bytes(output)


def _unquote(value: str) -> str:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value
    characters: list[str] = []
    escaped = False
    for character in value[1:-1]:
        if escaped:
            characters.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        else:
            characters.append(character)
    if escaped:
        characters.append("\\")
    return "".join(characters)


def _split_semicolon(value: str) -> list[str]:
    segments: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    escaped = False
    for character in value:
        if escaped:
            buffer.append(character)
            escaped = False
            continue
        if character == "\\" and in_quotes:
            buffer.append(character)
            escaped = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            buffer.append(character)
            continue
        if character == ";" and not in_quotes:
            segments.append("".join(buffer))
            buffer = []
            continue
        buffer.append(character)
    segments.append("".join(buffer))
    return segments
