"""JSON lines on stdout. Nothing else is written there."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_RESERVED = {
    "timestamp",
    "level",
    "message",
}


def utc_timestamp(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    return current.strftime("%Y-%m-%dT%H:%M:%S.") + f"{current.microsecond:06d}Z"


def emit(level: str, message: str, **fields: Any) -> None:
    """Write one JSON object to stdout."""

    payload: dict[str, Any] = {
        "timestamp": utc_timestamp(),
        "level": level,
        "message": message,
    }
    request_id = fields.pop("request_id", None)
    if request_id is None:
        request_id = request_id_var.get()
    if request_id is not None:
        payload["request_id"] = request_id
    for key, value in fields.items():
        if key in _RESERVED:
            continue
        payload[key] = value
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


class JsonLogHandler(logging.Handler):
    """Format third-party log records as JSON lines on stdout."""

    def emit(self, record: logging.LogRecord) -> None:
        fields: dict[str, Any] = {}
        if record.exc_info:
            fields["error"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        emit(record.levelname, record.getMessage(), **fields)


class _DropIntentionalCloseFilter(logging.Filter):
    """Hide uvicorn's note when we close a zip response on purpose.

    A mid-stream failure returns without the terminating chunk so the
    connection drops. Uvicorn logs that as an error; the completion line
    already records the outcome.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "without completing response" not in record.getMessage()


def configure_logging(level: str) -> None:
    """Send every log record through the JSON handler. Access logs stay off."""

    handler = JsonLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_DropIntentionalCloseFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level)
        logger.addFilter(_DropIntentionalCloseFilter())
    logging.captureWarnings(True)
