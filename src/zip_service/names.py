"""Archive names are validated. Entry names are sanitised and never rejected."""

from __future__ import annotations

import unicodedata

from zip_service.constants import (
    DEFAULT_ARCHIVE_NAME,
    ENTRY_NAME_MAX_BYTES,
    SHORT_EXTENSION_MAX_BYTES,
)

_CONTROLS = {chr(code) for code in range(0x20)} | {"\x7f"}


class InvalidArchiveName(Exception):
    """The filename field cannot be used as a download name."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def resolve_archive_name(raw: str | None) -> str:
    """Apply the archive-name rules in order.

    ``None`` means the filename field was absent. A present value is the
    decoded field text, which may be empty.
    """

    if raw is None:
        return DEFAULT_ARCHIVE_NAME
    trimmed = raw.strip()
    if trimmed == "":
        return DEFAULT_ARCHIVE_NAME
    if any(character in _CONTROLS or character in "/\\" for character in trimmed):
        raise InvalidArchiveName("control character or path separator")
    name = unicodedata.normalize("NFC", trimmed)
    if not name.lower().endswith(".zip"):
        name = f"{name}.zip"
    if len(name.encode("utf-8")) > ENTRY_NAME_MAX_BYTES:
        raise InvalidArchiveName("longer than 255 bytes")
    return name


def sanitize_entry_name(filename: str) -> str:
    """Sanitise one upload name. The result is not yet de-duplicated."""

    flattened = filename.replace("\\", "/")
    segment = flattened.rsplit("/", 1)[-1]
    cleaned = "".join(character for character in segment if character not in _CONTROLS)
    normalised = unicodedata.normalize("NFC", cleaned.strip())
    if normalised in {"", ".", ".."}:
        normalised = "file"
    return _truncate(normalised, ENTRY_NAME_MAX_BYTES)


class EntryNamer:
    """Hand out unique entry names for one request, in upload order."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def allocate(self, filename: str) -> str:
        candidate = sanitize_entry_name(filename)
        key = candidate.casefold()
        if key not in self._used:
            self._used.add(key)
            return candidate
        stem, extension = _split_extension(candidate)
        number = 1
        while True:
            renamed = f"{stem} ({number}){extension}"
            renamed_key = renamed.casefold()
            if renamed_key not in self._used:
                self._used.add(renamed_key)
                return renamed
            number += 1


def _split_extension(name: str) -> tuple[str, str]:
    """Return ``(stem, extension)`` where extension includes the dot.

    A leading dot does not start an extension, so ``.env`` has none and
    ``archive.tar.gz`` has ``.gz``.
    """

    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]


def _truncate(name: str, limit: int) -> str:
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, extension = _split_extension(name)
    extension_body = extension[1:] if extension else ""
    if extension and len(extension_body.encode("utf-8")) <= SHORT_EXTENSION_MAX_BYTES:
        budget = limit - len(extension.encode("utf-8"))
        if budget < 0:
            budget = 0
        return _truncate_to_bytes(stem, budget) + extension
    return _truncate_to_bytes(name, limit)


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""
