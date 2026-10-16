"""Manifest shape, baked-file checks, and unknown doc names."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from zip_service.app import create_app
from zip_service.config import Settings
from zip_service.documents import DocumentError, _documents, load_catalog


def test_manifest_lists_readme_then_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "3f2c9e1")
    catalog = load_catalog()
    manifest = catalog.manifest()
    assert list(manifest) == [
        "service",
        "version",
        "revision",
        "contract_version",
        "documents",
    ]
    assert manifest["service"] == "zip-service"
    assert manifest["version"] == "1.1.0"
    assert manifest["revision"] == "3f2c9e1"
    assert manifest["contract_version"] == "1.1.0"
    documents = manifest["documents"]
    assert isinstance(documents, list)
    assert documents == [
        {"slug": "readme", "title": "README", "path": "/docs/README.md"},
        {"slug": "agents", "title": "AGENTS", "path": "/docs/AGENTS.md"},
    ]


def test_revision_is_unknown_when_git_sha_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert load_catalog().manifest()["revision"] == "unknown"
    monkeypatch.setenv("GIT_SHA", "")
    assert load_catalog().manifest()["revision"] == "unknown"


def test_unknown_document_is_not_found() -> None:
    app = create_app(Settings())
    status, headers, body = asyncio.run(_invoke(app, "/docs/no-such.md"))
    payload = json.loads(body)
    assert status == 404
    assert payload["code"] == "not-found"
    assert headers["content-type"] == "application/problem+json"

    status, _headers, body = asyncio.run(_invoke(app, "/docs/../pyproject.toml"))
    assert status == 404
    assert json.loads(body)["code"] == "not-found"

    status, headers, body = asyncio.run(_invoke(app, "/openapi.json"))
    assert status == 404
    assert json.loads(body)["code"] == "not-found"


def test_manifest_and_readme_are_served() -> None:
    app = create_app(Settings())
    status, headers, body = asyncio.run(_invoke(app, "/docs/manifest.json"))
    assert status == 200
    assert headers["content-type"] == "application/json"
    payload = json.loads(body)
    assert [item["slug"] for item in payload["documents"]] == ["readme", "agents"]

    status, headers, body = asyncio.run(_invoke(app, "/docs/README.md"))
    assert status == 200
    assert headers["content-type"] == "text/markdown; charset=utf-8"
    assert headers["cache-control"] == "no-cache"
    assert headers["x-content-type-options"] == "nosniff"
    readme = Path(__file__).resolve().parents[2] / "README.md"
    assert body == readme.read_bytes()


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        (("No", "Title", "README.md"), "slug"),
        (("ok", "", "README.md"), "title"),
        (("ok", "x" * 81, "README.md"), "title"),
        (("ok", "Title", "../secret.md"), "path"),
    ],
)
def test_catalog_entries_are_checked(
    tmp_path: Path,
    entry: tuple[str, str, str],
    match: str,
) -> None:
    with pytest.raises(DocumentError, match=match):
        _documents(tmp_path, (entry,))


def test_more_than_twenty_documents_is_rejected(tmp_path: Path) -> None:
    entries = tuple(("doc", "Doc", "README.md") for _ in range(21))
    with pytest.raises(DocumentError, match="20"):
        _documents(tmp_path, entries)


def test_symlink_outside_the_image_is_rejected(tmp_path: Path) -> None:
    image = tmp_path / "image"
    image.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (image / "link.md").symlink_to(outside)
    with pytest.raises(DocumentError, match="escapes"):
        _documents(image, (("link", "Link", "link.md"),))


def test_one_mebibyte_document_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_bytes(b"x" * (1024 * 1024))
    documents = _documents(tmp_path, (("readme", "README", "README.md"),))
    assert documents[0].url_path == "/docs/README.md"


def test_missing_or_invalid_version_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    with pytest.raises(DocumentError, match=r"pyproject\.toml"):
        load_catalog(tmp_path)

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'zip-service'\n", encoding="utf-8")
    with pytest.raises(DocumentError, match="version"):
        load_catalog(tmp_path)

    (tmp_path / "pyproject.toml").write_text("[project]\nversion = 1\n", encoding="utf-8")
    with pytest.raises(DocumentError, match="version"):
        load_catalog(tmp_path)

    (tmp_path / "pyproject.toml").write_text('project = "nope"\n', encoding="utf-8")
    with pytest.raises(DocumentError, match="version"):
        load_catalog(tmp_path)

    (tmp_path / "pyproject.toml").write_text("version = [\n", encoding="utf-8")
    with pytest.raises(DocumentError, match="not valid TOML"):
        load_catalog(tmp_path)


def test_main_exits_when_a_document_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from zip_service.__main__ import main

    monkeypatch.setattr("zip_service.__main__.load_settings", lambda: Settings())
    monkeypatch.setattr("zip_service.__main__.configure_logging", lambda _level: None)

    def fail(_settings: Settings) -> None:
        raise DocumentError("document is not in the image: README.md")

    monkeypatch.setattr("zip_service.__main__.create_app", fail)
    assert main() == 2
    assert "documentation error" in capsys.readouterr().err


def test_oversized_or_missing_document_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "1.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "README.md").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(DocumentError, match="1 MiB"):
        load_catalog(tmp_path)

    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").unlink()
    with pytest.raises(DocumentError, match=r"AGENTS\.md"):
        load_catalog(tmp_path)


async def _invoke(app: object, path: str) -> tuple[int, dict[str, str], bytes]:
    body = bytearray()
    status = 0
    raw_headers: list[tuple[bytes, bytes]] = []

    requested = False

    async def receive() -> dict[str, object]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])  # type: ignore[arg-type]
            raw_headers.extend(message.get("headers", []))  # type: ignore[arg-type]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))  # type: ignore[arg-type]

    # spec_version 2.5 makes FileResponse send the body and return. On 2.3 it
    # also waits for http.disconnect, and a receive() that never disconnects spins.
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 9),
        "server": ("127.0.0.1", 80),
    }
    await app(scope, receive, send)  # type: ignore[operator]
    headers = {key.decode().lower(): value.decode() for key, value in raw_headers}
    return status, headers, bytes(body)
