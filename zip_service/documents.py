"""Markdown served to the docs portal.

The allow-list is fixed at startup. Request paths are never joined onto the
filesystem, so a docs URL cannot name a file outside this list.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from starlette.responses import FileResponse, JSONResponse

_SLUG_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_PATH_RE = re.compile(r"^/docs/[A-Za-z0-9._-]{1,64}\.md$")
_MAX_BYTES = 1024 * 1024
_MAX_DOCUMENTS = 20
_SERVICE = "zip-service"
_CONTRACT_VERSION = "1.1.0"
OPENAPI_PATH = "/docs/openapi.json"

# Slug, title, filename. Order is the manifest order.
_CATALOG = (
    ("readme", "README", "README.md"),
    ("agents", "AGENTS", "AGENTS.md"),
)


class DocumentError(Exception):
    """A baked document is missing, too large, or not on the allow-list."""


@dataclass(frozen=True, slots=True)
class Document:
    slug: str
    title: str
    url_path: str
    file_path: Path


@dataclass(frozen=True, slots=True)
class Catalog:
    version: str
    documents: tuple[Document, ...]

    def manifest(self) -> dict[str, object]:
        return {
            "service": _SERVICE,
            "version": self.version,
            "revision": revision(),
            "contract_version": _CONTRACT_VERSION,
            "documents": [
                {"slug": item.slug, "title": item.title, "path": item.url_path}
                for item in self.documents
            ],
            "openapi": OPENAPI_PATH,
        }


def document_response(document: Document) -> FileResponse:
    """Serve one allow-listed file. The path was chosen when the catalog was built."""

    return FileResponse(
        document.file_path,
        media_type="text/markdown",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


def revision() -> str:
    """Commit from ``GIT_SHA``. Unset and empty are ``unknown``."""

    value = os.environ.get("GIT_SHA")
    if not value:
        return "unknown"
    return value


def load_catalog(root: Path | None = None) -> Catalog:
    """Read the fixed document list from ``root``.

    ``root`` defaults to the parent of this package: the repo root in a
    checkout, and ``/app`` in the image.
    """

    base = (Path(__file__).resolve().parent.parent if root is None else root).resolve()
    return Catalog(_version(base / "pyproject.toml"), _documents(base, _CATALOG))


def manifest_response(catalog: Catalog) -> JSONResponse:
    return JSONResponse(catalog.manifest())


def _documents(base: Path, entries: tuple[tuple[str, str, str], ...]) -> tuple[Document, ...]:
    if len(entries) > _MAX_DOCUMENTS:
        raise DocumentError(f"at most {_MAX_DOCUMENTS} documents are allowed")
    documents: list[Document] = []
    for slug, title, filename in entries:
        url_path = f"/docs/{filename}"
        if _SLUG_RE.fullmatch(slug) is None:
            raise DocumentError(f"invalid document slug: {slug}")
        if len(title) > 80 or title == "":
            raise DocumentError(f"invalid document title: {title}")
        if _PATH_RE.fullmatch(url_path) is None:
            raise DocumentError(f"invalid document path: {url_path}")
        file_path = (base / filename).resolve()
        if file_path.parent != base:
            raise DocumentError(f"document escapes the image: {filename}")
        if not file_path.is_file():
            raise DocumentError(f"document is not in the image: {filename}")
        if file_path.stat().st_size > _MAX_BYTES:
            raise DocumentError(f"document exceeds 1 MiB: {filename}")
        documents.append(Document(slug, title, url_path, file_path))
    return tuple(documents)


def _version(path: Path) -> str:
    if not path.is_file():
        raise DocumentError("pyproject.toml is not in the image")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise DocumentError("pyproject.toml is not valid TOML") from exc
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or version == "":
        raise DocumentError("project.version is missing from pyproject.toml")
    return version
