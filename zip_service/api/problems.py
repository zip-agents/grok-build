"""RFC 9457 problem responses.

Title and detail strings are fixed so clients can match on ``code`` and the
human text stays stable. ``no-files`` matches the contract example.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from starlette.responses import Response

from zip_service.runtime.jsonlog import request_id_var


@dataclass(frozen=True, slots=True)
class Problem:
    status: int
    title: str
    detail: str


PROBLEMS: dict[str, Problem] = {
    "malformed-request": Problem(
        400,
        "Malformed request",
        "The request was not valid multipart/form-data.",
    ),
    "no-files": Problem(
        400,
        "No files were uploaded",
        "The request contained no file parts with a non-empty filename.",
    ),
    "unexpected-file-field": Problem(
        400,
        "Unexpected file field",
        'File parts must use the field name "files".',
    ),
    "not-found": Problem(
        404,
        "Not found",
        "No resource exists at the requested path.",
    ),
    "method-not-allowed": Problem(
        405,
        "Method not allowed",
        "This method is not allowed for the requested path.",
    ),
    "request-timeout": Problem(
        408,
        "Request timeout",
        "The request made no progress for too long, or exceeded the maximum duration.",
    ),
    "too-many-files": Problem(
        413,
        "Too many files",
        "The request included more files than the server allows.",
    ),
    "unsupported-media-type": Problem(
        415,
        "Unsupported media type",
        "Content-Type must be multipart/form-data.",
    ),
    "invalid-archive-name": Problem(
        422,
        "Invalid archive name",
        "The filename field is not a valid archive name.",
    ),
    "internal-error": Problem(
        500,
        "Internal error",
        "The server could not complete the request.",
    ),
    "overloaded": Problem(
        503,
        "Service overloaded",
        "Too many zip requests are in progress.",
    ),
    "shutting-down": Problem(
        503,
        "Server shutting down",
        "The server is shutting down and is not accepting new zip requests.",
    ),
}

# Status we would have sent for a failure, including ones that abort mid-stream.
ABORT_STATUS: dict[str, int] = {code: problem.status for code, problem in PROBLEMS.items()}


def problem_body(code: str, request_id: str | None = None) -> bytes:
    problem = PROBLEMS[code]
    identifier = request_id if request_id is not None else (request_id_var.get() or "")
    payload = {
        "type": f"urn:zip-service:problem:{code}",
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "code": code,
        "request_id": identifier,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def problem_response(
    code: str,
    *,
    extra_headers: dict[str, str] | None = None,
    request_id: str | None = None,
) -> Response:
    """Error response sent only before the zip status line has gone out.

    ``Connection: close`` is set because the request body may be unread.
    Leaving it on the connection would desynchronise a keep-alive client.
    """

    problem = PROBLEMS[code]
    identifier = request_id if request_id is not None else (request_id_var.get() or "")
    headers = {
        "x-request-id": identifier,
        "connection": "close",
    }
    if extra_headers:
        headers.update(extra_headers)
    return Response(
        content=problem_body(code, identifier),
        status_code=problem.status,
        headers=headers,
        media_type="application/problem+json",
    )
