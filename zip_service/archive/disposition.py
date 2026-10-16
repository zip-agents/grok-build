"""Content-Disposition for the archive download name (RFC 6266 / RFC 8187)."""

from __future__ import annotations

# RFC 5987 attr-char, excluding alpha and digit which are handled separately.
_ATTR_CHAR = set("!#$&+-.^_`|~")


def content_disposition(archive_name: str) -> str:
    """Build the header value for a validated archive name."""

    fallback = "".join(
        "_" if ord(character) > 127 or character in {'"', "\\"} else character
        for character in archive_name
    )
    encoded = _rfc8187(archive_name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _rfc8187(value: str) -> str:
    pieces: list[str] = []
    for byte in value.encode("utf-8"):
        character = chr(byte)
        # ``isalnum`` is Unicode-aware, so it must not see bytes above 7-bit ASCII.
        if character.isascii() and (character.isalnum() or character in _ATTR_CHAR):
            pieces.append(character)
        else:
            pieces.append(f"%{byte:02X}")
    return "".join(pieces)
