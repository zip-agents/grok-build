"""OpenAPI description of POST /api/v1/zip.

The route itself is a plain Starlette ``Route``. FastAPI never sees it, so
this module builds the operation and the application inserts it into the
generated document. Error examples come from ``problem_body`` so the title,
detail, and status cannot drift from the bytes the service sends.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from zip_service.api.problems import PROBLEMS, problem_body
from zip_service.config import Settings
from zip_service.runtime.constants import (
    FILENAME_FIELD_MAX_BYTES,
    REQUEST_ID_PATTERN,
    RETRY_AFTER_SECONDS,
)

# Not HTTP responses on POST /api/v1/zip before the status line.
# not-found and method-not-allowed belong to unknown paths and methods.
# too-many-files is only raised after streaming has started: MAX_FILES is at
# least 1, and the status line is sent with the first non-skipped file.
_NOT_A_ZIP_RESPONSE = frozenset({"not-found", "method-not-allowed", "too-many-files"})

_METHODS = frozenset({"get", "post", "put", "delete", "patch", "head", "options", "trace"})

_ZIP_DESCRIPTION = (
    "Accepts multipart/form-data and streams a ZIP archive as it is compressed. "
    "Send the optional filename field before any file part. File parts use the "
    "field name files, in the order they belong in the archive. A file part with "
    "an empty filename is skipped. "
    "The status line is sent when the first non-skipped file starts, and only "
    "after the archive name is valid. After that, a failure does not return a "
    "problem document: the server stops writing, omits the ZIP end records and "
    "the terminating HTTP chunk, and closes the connection. The client receives "
    "a truncated archive. A limit that can only be crossed after that point, "
    "including too many files, ends the same way and is not listed here."
)

_REQUEST_ID_REF = "#/components/headers/X-Request-ID"
_PROBLEM_REF = "#/components/schemas/Problem"


def zip_http_codes() -> tuple[str, ...]:
    """Problem codes a zip request can receive as an HTTP response."""

    return tuple(code for code in PROBLEMS if code not in _NOT_A_ZIP_RESPONSE)


def zip_operation(settings: Settings) -> dict[str, Any]:
    """The ``post`` operation for ``/api/v1/zip``."""

    return {
        "tags": ["zip"],
        "operationId": "streamZip",
        "summary": "Stream uploaded files as a ZIP archive.",
        "description": _ZIP_DESCRIPTION,
        "requestBody": _request_body(settings),
        "responses": _responses(),
    }


def install_openapi(app: FastAPI, settings: Settings) -> None:
    """Serve FastAPI's document, then add the zip operation. Cached on the app."""

    def openapi() -> dict[str, Any]:
        cached = app.openapi_schema
        if cached is not None:
            return cached
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        schema.pop("servers", None)
        schema.pop("security", None)
        components = schema.setdefault("components", {})
        components.setdefault("schemas", {})["Problem"] = _problem_schema()
        components.setdefault("headers", {})["X-Request-ID"] = _request_id_header()
        zip_path = schema.setdefault("paths", {}).setdefault("/api/v1/zip", {})
        zip_path["post"] = zip_operation(settings)
        _strip_security(schema)
        _attach_request_id(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def _request_body(settings: Settings) -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "maxLength": FILENAME_FIELD_MAX_BYTES,
                            "description": (
                                "Optional archive name. Send it before the first file "
                                "part. The part is at most 1024 raw bytes. Empty or "
                                "whitespace becomes zipped_files.zip, and .zip is "
                                "appended when it is missing. Controls, slashes, "
                                "invalid UTF-8, or more than 255 UTF-8 bytes after "
                                "NFC is 422 invalid-archive-name."
                            ),
                            "examples": ["quarterly report"],
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "string", "format": "binary"},
                            "minItems": 1,
                            "maxItems": settings.max_files,
                            "description": (
                                "One part per file, field name files, in archive order. "
                                "A part with an empty filename is skipped. maxItems is "
                                "this process's MAX_FILES. Passing it aborts the "
                                "connection and is not a 413 response."
                            ),
                        },
                    },
                    "required": ["files"],
                },
                "encoding": {"files": {"contentType": "application/octet-stream"}},
            }
        },
    }


def _responses() -> dict[str, Any]:
    grouped: dict[int, list[str]] = {}
    for code in zip_http_codes():
        grouped.setdefault(PROBLEMS[code].status, []).append(code)
    responses: dict[str, Any] = {"200": _success_response()}
    for status, codes in grouped.items():
        responses[str(status)] = _problem_response(codes)
    return responses


def _success_response() -> dict[str, Any]:
    return {
        "description": (
            "A ZIP archive, streamed as it is compressed. There is no "
            "Content-Length. The terminating chunk is sent only for a complete archive."
        ),
        "headers": {
            "Content-Disposition": {
                "description": (
                    "attachment, with an ASCII filename and an RFC 5987 filename*. "
                    "Non-ASCII, quotes, and backslashes in the ASCII name become "
                    "underscores."
                ),
                "schema": {"type": "string"},
            },
            "Cache-Control": {
                "description": "Always no-store.",
                "schema": {"type": "string", "const": "no-store"},
            },
            "X-Content-Type-Options": {
                "schema": {"type": "string", "const": "nosniff"},
            },
            "X-Request-ID": {"$ref": _REQUEST_ID_REF},
        },
        "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
    }


def _problem_response(codes: list[str]) -> dict[str, Any]:
    examples: dict[str, Any] = {}
    for code in codes:
        body = json.loads(problem_body(code, "example-request"))
        examples[code] = {"summary": body["title"], "value": body}
    headers: dict[str, Any] = {
        "X-Request-ID": {"$ref": _REQUEST_ID_REF},
        "Connection": {
            "description": "close, because the request body may be unread.",
            "schema": {"type": "string", "const": "close"},
        },
    }
    if "overloaded" in codes:
        headers["Retry-After"] = {
            "description": "Seconds to wait. Sent for overloaded, not for shutting-down.",
            "schema": {"type": "string", "const": RETRY_AFTER_SECONDS},
        }
    return {
        "description": "Problem codes: " + ", ".join(codes) + ".",
        "headers": headers,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": _PROBLEM_REF},
                "examples": examples,
            }
        },
    }


def _problem_schema() -> dict[str, Any]:
    sample = json.loads(problem_body("no-files", "example-request"))
    properties: dict[str, Any] = {}
    for key, value in sample.items():
        if isinstance(value, int):
            properties[key] = {"type": "integer"}
        else:
            properties[key] = {"type": "string"}
    return {"type": "object", "properties": properties, "required": list(sample)}


def _request_id_header() -> dict[str, Any]:
    return {
        "description": (
            "The first request X-Request-ID when it matches the pattern, "
            "otherwise a new UUID. The value is not trimmed."
        ),
        "required": True,
        "schema": {"type": "string", "pattern": REQUEST_ID_PATTERN},
    }


def _strip_security(schema: dict[str, Any]) -> None:
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method in _METHODS:
                operation.pop("security", None)


def _attach_request_id(schema: dict[str, Any]) -> None:
    ref = {"$ref": _REQUEST_ID_REF}
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method in _METHODS:
                for response in operation["responses"].values():
                    response.setdefault("headers", {}).setdefault("X-Request-ID", ref)
