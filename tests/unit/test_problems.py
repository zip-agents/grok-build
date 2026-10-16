import json

from zip_service.api.problems import ABORT_STATUS, PROBLEMS, problem_body


def test_no_files_body_matches_the_contract_shape() -> None:
    body = json.loads(problem_body("no-files", "3f0c"))
    assert list(body) == ["type", "title", "status", "detail", "code", "request_id"]
    assert body == {
        "type": "urn:zip-service:problem:no-files",
        "title": "No files were uploaded",
        "status": 400,
        "detail": "The request contained no file parts with a non-empty filename.",
        "code": "no-files",
        "request_id": "3f0c",
    }


def test_every_catalogued_code_has_a_stable_status() -> None:
    assert set(PROBLEMS) == set(ABORT_STATUS)
    assert PROBLEMS["too-many-files"].status == 413
    assert PROBLEMS["overloaded"].status == 503
    assert PROBLEMS["shutting-down"].status == 503
    assert PROBLEMS["internal-error"].status == 500
    encoded = problem_body("internal-error", "abc").decode()
    assert "Traceback" not in encoded
    assert "internal-error" in encoded
