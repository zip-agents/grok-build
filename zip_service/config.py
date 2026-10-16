"""Environment configuration.

Real environment variables win over a ``.env`` file in the working directory.
Invalid configuration exits the process before it listens.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_INT_RE = re.compile(r"[+-]?\d+")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


class ConfigError(Exception):
    """Raised when configuration is missing, malformed, or out of range."""


@dataclass(frozen=True, slots=True)
class Settings:
    port: int = 8000
    log_level: str = "INFO"
    zip_compression_level: int = 6
    shutdown_grace_seconds: int = 25
    max_files: int = 500
    max_concurrent_requests: int = 32
    idle_timeout_seconds: int = 30
    max_request_duration_seconds: int = 900
    output_buffer_max_bytes: int = 16 * 1024 * 1024


def load_settings(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Settings:
    """Load settings from ``environ`` and, when present, a ``.env`` file.

    When ``environ`` is omitted, ``os.environ`` is used and the ``.env`` file
    is read from the working directory if ``dotenv_path`` is also omitted.
    An empty string is a set value and is validated, not treated as missing.
    """

    if environ is None:
        environ = os.environ
        if dotenv_path is None:
            dotenv_path = Path.cwd() / ".env"

    file_values: dict[str, str] = {}
    if dotenv_path is not None and dotenv_path.is_file():
        file_values = parse_dotenv(dotenv_path.read_text(encoding="utf-8"))

    def raw(name: str) -> str | None:
        if name in environ:
            return environ[name]
        if name in file_values:
            return file_values[name]
        return None

    return Settings(
        port=_parse_int("PORT", raw("PORT"), 1, 65535, default=8000),
        log_level=_parse_log_level(raw("LOG_LEVEL")),
        zip_compression_level=_parse_int(
            "ZIP_COMPRESSION_LEVEL", raw("ZIP_COMPRESSION_LEVEL"), 0, 9, default=6
        ),
        shutdown_grace_seconds=_parse_int(
            "SHUTDOWN_GRACE_SECONDS",
            raw("SHUTDOWN_GRACE_SECONDS"),
            0,
            None,
            default=25,
        ),
        max_files=_parse_int("MAX_FILES", raw("MAX_FILES"), 1, None, default=500),
        max_concurrent_requests=_parse_int(
            "MAX_CONCURRENT_REQUESTS",
            raw("MAX_CONCURRENT_REQUESTS"),
            1,
            None,
            default=32,
        ),
        idle_timeout_seconds=_parse_int(
            "IDLE_TIMEOUT_SECONDS", raw("IDLE_TIMEOUT_SECONDS"), 1, None, default=30
        ),
        max_request_duration_seconds=_parse_int(
            "MAX_REQUEST_DURATION_SECONDS",
            raw("MAX_REQUEST_DURATION_SECONDS"),
            1,
            None,
            default=900,
        ),
        output_buffer_max_bytes=_parse_int(
            "OUTPUT_BUFFER_MAX_BYTES",
            raw("OUTPUT_BUFFER_MAX_BYTES"),
            1,
            None,
            default=16 * 1024 * 1024,
        ),
    )


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse a subset of dotenv syntax.

    Blank lines and ``#`` comments are ignored. ``export `` is optional.
    Matching single or double quotes around a value are removed. There is no
    interpolation and no multi-line value support.
    """

    values: dict[str, str] = {}
    for lineno, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ConfigError(f".env line {lineno} is missing '='")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_NAME_RE.fullmatch(key):
            raise ConfigError(f".env line {lineno} has an invalid variable name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _parse_log_level(raw: str | None) -> str:
    if raw is None:
        return "INFO"
    level = raw.strip().upper()
    if level not in _LOG_LEVELS:
        allowed = ", ".join(sorted(_LOG_LEVELS))
        raise ConfigError(f"LOG_LEVEL must be one of {allowed}, got {raw!r}")
    return level


def _parse_int(
    name: str,
    raw: str | None,
    minimum: int,
    maximum: int | None,
    *,
    default: int,
) -> int:
    if raw is None:
        return default
    text = raw.strip()
    if not _INT_RE.fullmatch(text):
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    value = int(text, 10)
    if value < minimum or (maximum is not None and value > maximum):
        upper = "none" if maximum is None else str(maximum)
        raise ConfigError(f"{name} must be between {minimum} and {upper}, got {value}")
    return value
