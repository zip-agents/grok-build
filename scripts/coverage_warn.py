"""Warn when new lines in zip_service are not covered.

The process exits 0 when it finds uncovered lines. CI prints GitHub
annotations and the job stays green. A missing report or a git error exits 1.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", type=Path, required=True)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--diff", type=Path)
    args = parser.parse_args(argv)
    if (args.repo is None) == (args.diff is None):
        parser.error("pass exactly one of --repo or --diff")

    if not args.xml.is_file():
        print(f"coverage report not found: {args.xml}", file=sys.stderr)
        return 1

    if args.diff is not None:
        diff_text = args.diff.read_text(encoding="utf-8")
    else:
        diff_text, code = diff_from_git(args.repo)
        if code != 0:
            return code

    added = added_lines(diff_text)
    measured = measured_lines(args.xml)
    missing = uncovered_additions(added, measured)
    emit(missing)
    return 0


def diff_from_git(repo: Path) -> tuple[str, int]:
    base, code = resolve_base(repo)
    if code != 0:
        return "", code
    tracked = git(repo, ["diff", "-U0", "--no-color", base, "--", "zip_service"])
    if tracked.returncode > 1:
        print(tracked.stderr, file=sys.stderr, end="")
        return "", tracked.returncode
    parts = [tracked.stdout]
    untracked = git(repo, ["ls-files", "--others", "--exclude-standard", "--", "zip_service"])
    if untracked.returncode != 0:
        print(untracked.stderr, file=sys.stderr, end="")
        return "", untracked.returncode
    for path in untracked.stdout.splitlines():
        if not path.endswith(".py"):
            continue
        created = git(repo, ["diff", "--no-index", "--no-color", "-U0", "/dev/null", path])
        if created.returncode > 1:
            print(created.stderr, file=sys.stderr, end="")
            return "", created.returncode
        parts.append(created.stdout)
    return "\n".join(parts), 0


def resolve_base(repo: Path) -> tuple[str, int]:
    raw = os.environ.get("COVERAGE_DIFF_BASE", "").strip()
    if raw and set(raw) != {"0"}:
        return raw, 0
    merge = git(repo, ["merge-base", "HEAD", "origin/main"])
    if merge.returncode == 0 and merge.stdout.strip():
        return merge.stdout.strip(), 0
    head = git(repo, ["rev-parse", "HEAD"])
    if head.returncode != 0:
        print(head.stderr, file=sys.stderr, end="")
        return "", head.returncode
    return head.stdout.strip(), 0


def git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--no-optional-locks", "-c", "safe.directory=*", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Map a zip_service Python path to added ``(line number, text)`` pairs."""

    found: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    new_line: int | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path = None
            new_line = None
            continue
        if raw.startswith("+++ "):
            path = _added_path(raw[4:].strip())
            new_line = None
            if path is not None:
                found.setdefault(path, [])
            continue
        if path is None:
            continue
        if raw.startswith("@@"):
            new_line = _new_start(raw)
            continue
        if new_line is None:
            continue
        if raw.startswith("+"):
            found[path].append((new_line, raw[1:]))
            new_line += 1
            continue
        if raw.startswith("-") or raw.startswith("\\"):
            continue
        new_line += 1
    return found


def _added_path(spec: str) -> str | None:
    if spec.startswith('"') and spec.endswith('"'):
        spec = spec[1:-1]
    if spec.startswith("b/"):
        spec = spec[2:]
    if spec == "/dev/null" or not spec.startswith("zip_service/") or not spec.endswith(".py"):
        return None
    return spec


def _new_start(hunk: str) -> int | None:
    parts = hunk.split()
    if len(parts) < 3 or not parts[2].startswith("+"):
        return None
    start = parts[2][1:].split(",", 1)[0]
    return int(start)


def measured_lines(xml_path: Path) -> dict[str, dict[int, int]]:
    """Map a repo path to ``line -> hits`` for files coverage measured.

    Coverage writes filenames relative to ``<source>``. ``zip_service`` as the
    source means ``api/endpoint.py`` is ``zip_service/api/endpoint.py``.
    """

    root = ET.parse(xml_path).getroot()
    sources = [node.text for node in root.findall("./sources/source") if node.text]
    measured: dict[str, dict[int, int]] = {}
    for node in root.findall(".//class"):
        filename = node.get("filename")
        if filename is None:
            continue
        path = _repo_path(filename, sources)
        hits = measured.setdefault(path, {})
        for line in node.findall("./lines/line"):
            number = line.get("number")
            count = line.get("hits")
            if number is None or count is None:
                continue
            hits[int(number)] = max(hits.get(int(number), 0), int(count))
    return measured


def _repo_path(filename: str, sources: list[str]) -> str:
    direct = _normalize(filename)
    if direct.startswith("zip_service/"):
        return direct
    for source in sources:
        joined = _normalize(str(Path(source) / filename))
        if joined.startswith("zip_service/"):
            return joined
    return direct


def _normalize(path: str) -> str:
    path = path.replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    marker = "/zip_service/"
    at = path.find(marker)
    if at != -1:
        return path[at + 1 :]
    return path


def uncovered_additions(
    added: dict[str, list[tuple[int, str]]],
    measured: dict[str, dict[int, int]],
) -> list[tuple[str, int]]:
    missing: list[tuple[str, int]] = []
    for path in sorted(added):
        known = measured.get(path)
        for number, text in added[path]:
            if known is None:
                if _counts_without_a_report(text):
                    missing.append((path, number))
                continue
            if known.get(number) == 0:
                missing.append((path, number))
    return missing


def _counts_without_a_report(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("#")


def emit(missing: list[tuple[str, int]]) -> None:
    if not missing:
        print("New lines in zip_service are covered.")
        return
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    shown = 0
    for path, number in missing:
        if actions and shown < 50:
            print(
                f"::warning file={path},line={number},title=coverage::"
                "New line is not covered by tests"
            )
            shown += 1
        else:
            print(f"{path}:{number}: new line is not covered")
    print(f"{len(missing)} new line(s) in zip_service are not covered")


if __name__ == "__main__":
    sys.exit(main())
