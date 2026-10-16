"""OpenAPI operation for POST /api/v1/zip, built beside the route."""

from __future__ import annotations

from zip_service.api.openapi import zip_http_codes, zip_operation
from zip_service.api.problems import PROBLEMS
from zip_service.config import Settings

# Independent of zip_http_codes(), so a hand-written example list cannot match by accident.
_MID_STREAM_ONLY = frozenset({"too-many-files"})
_NOT_ZIP = frozenset({"not-found", "method-not-allowed"})


def test_zip_request_body_lists_filename_then_files() -> None:
    settings = Settings(max_files=7)
    operation = zip_operation(settings)
    assert operation["tags"] == ["zip"]
    assert operation["operationId"] == "streamZip"
    assert operation["summary"]
    description = operation["description"]
    assert "filename field before any file part" in description
    assert "truncated archive" in description
    assert "terminating HTTP chunk" in description
    body = operation["requestBody"]["content"]["multipart/form-data"]
    properties = body["schema"]["properties"]
    assert list(properties) == ["filename", "files"]
    filename = properties["filename"]
    assert filename["type"] == "string"
    assert filename["maxLength"] == 1024
    assert filename["examples"] == ["quarterly report"]
    files = properties["files"]
    assert files["type"] == "array"
    assert files["items"] == {"type": "string", "format": "binary"}
    assert files["minItems"] == 1
    assert files["maxItems"] == 7
    assert body["schema"]["required"] == ["files"]
    assert body["encoding"] == {"files": {"contentType": "application/octet-stream"}}
    success = operation["responses"]["200"]
    assert success["content"]["application/zip"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    for name in (
        "Content-Disposition",
        "Cache-Control",
        "X-Content-Type-Options",
        "X-Request-ID",
    ):
        assert name in success["headers"]


def test_zip_problem_codes_match_the_table() -> None:
    operation = zip_operation(Settings())
    expected = {code for code in PROBLEMS if code not in _NOT_ZIP and code not in _MID_STREAM_ONLY}
    assert set(zip_http_codes()) == expected
    documented: set[str] = set()
    for status, response in operation["responses"].items():
        content = response.get("content", {})
        if "application/problem+json" not in content:
            continue
        examples = content["application/problem+json"]["examples"]
        assert set(examples) <= expected
        for code, example in examples.items():
            problem = PROBLEMS[code]
            value = example["value"]
            assert value["title"] == problem.title
            assert value["detail"] == problem.detail
            assert value["status"] == problem.status
            assert value["status"] == int(status)
            assert code not in documented
            documented.add(code)
        assert response["description"]
        for code in examples:
            assert code in response["description"]
    assert documented == expected
    assert "413" not in operation["responses"]
    assert "404" not in operation["responses"]
    assert "405" not in operation["responses"]
    overloaded = operation["responses"]["503"]["headers"]["Retry-After"]
    assert overloaded["schema"]["const"] == "5"
    problem_422 = operation["responses"]["422"]["content"]
    assert "application/problem+json" in problem_422
    assert "application/json" not in problem_422
