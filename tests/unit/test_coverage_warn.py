"""The coverage diff warns, and does not fail, when new lines are uncovered."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "coverage_warn.py"


def _load():
    spec = importlib.util.spec_from_file_location("coverage_warn", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


warn = _load()


def test_added_lines_track_new_file_numbers() -> None:
    diff = """diff --git a/zip_service/app.py b/zip_service/app.py
--- a/zip_service/app.py
+++ b/zip_service/app.py
@@ -10,0 +11,2 @@
+kept = 1
+other = 2
@@ -20 +22,0 @@
-removed = 1
diff --git a/notes.md b/notes.md
+++ b/notes.md
@@ -0,0 +1 @@
+ignore
"""
    found = warn.added_lines(diff)
    assert found == {"zip_service/app.py": [(11, "kept = 1"), (12, "other = 2")]}


def test_uncovered_addition_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        """<?xml version="1.0" ?>
<coverage>
  <packages><package><classes>
    <class filename="zip_service/app.py">
      <lines>
        <line number="11" hits="1"/>
        <line number="12" hits="0"/>
      </lines>
    </class>
  </classes></package></packages>
</coverage>
""",
        encoding="utf-8",
    )
    diff = tmp_path / "diff.patch"
    diff.write_text(
        """diff --git a/zip_service/app.py b/zip_service/app.py
+++ b/zip_service/app.py
@@ -10,0 +11,3 @@
+covered = 1
+missed = 1
+
""",
        encoding="utf-8",
    )
    os.environ.pop("GITHUB_ACTIONS", None)
    code = warn.main(["--xml", str(xml), "--diff", str(diff)])
    captured = capsys.readouterr()
    assert code == 0
    assert "zip_service/app.py:12: new line is not covered" in captured.out
    assert "zip_service/app.py:11:" not in captured.out
    assert "1 new line(s) in zip_service are not covered" in captured.out


def test_file_missing_from_the_report_warns_on_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text("<coverage></coverage>", encoding="utf-8")
    diff = tmp_path / "diff.patch"
    diff.write_text(
        """diff --git a/zip_service/new.py b/zip_service/new.py
+++ b/zip_service/new.py
@@ -0,0 +1,2 @@
+# comment
+value = 1
""",
        encoding="utf-8",
    )
    code = warn.main(["--xml", str(xml), "--diff", str(diff)])
    captured = capsys.readouterr()
    assert code == 0
    assert "zip_service/new.py:2: new line is not covered" in captured.out
    assert "zip_service/new.py:1:" not in captured.out


def test_source_root_is_prefixed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        """<?xml version="1.0" ?>
<coverage>
  <sources><source>zip_service</source></sources>
  <packages><package><classes>
    <class filename="__main__.py"><lines>
      <line number="4" hits="1"/>
    </lines></class>
  </classes></package></packages>
</coverage>
""",
        encoding="utf-8",
    )
    diff = tmp_path / "diff.patch"
    diff.write_text("+++ b/zip_service/__main__.py\n@@ -3,0 +4 @@\n+ok = 1\n", encoding="utf-8")
    code = warn.main(["--xml", str(xml), "--diff", str(diff)])
    assert code == 0
    assert capsys.readouterr().out.strip() == "New lines in zip_service are covered."


def test_covered_diff_is_quiet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        """<?xml version="1.0" ?>
<coverage><packages><package><classes>
  <class filename="/app/zip_service/app.py"><lines>
    <line number="4" hits="2"/>
  </lines></class>
</classes></package></packages></coverage>
""",
        encoding="utf-8",
    )
    diff = tmp_path / "diff.patch"
    diff.write_text(
        """+++ b/zip_service/app.py
@@ -3,0 +4 @@
+ok = 1
""",
        encoding="utf-8",
    )
    code = warn.main(["--xml", str(xml), "--diff", str(diff)])
    assert code == 0
    assert capsys.readouterr().out.strip() == "New lines in zip_service are covered."


def test_missing_report_exits_nonzero(tmp_path: Path) -> None:
    code = warn.main(
        ["--xml", str(tmp_path / "missing.xml"), "--diff", str(tmp_path / "diff.patch")]
    )
    assert code == 1


def test_github_actions_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        """<coverage><packages><package><classes>
        <class filename="zip_service/app.py"><lines>
          <line number="1" hits="0"/>
        </lines></class>
        </classes></package></packages></coverage>""",
        encoding="utf-8",
    )
    diff = tmp_path / "diff.patch"
    diff.write_text("+++ b/zip_service/app.py\n@@ -0,0 +1 @@\n+x = 1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code = warn.main(["--xml", str(xml), "--diff", str(diff)])
    assert code == 0
    assert "::warning file=zip_service/app.py,line=1,title=coverage::" in capsys.readouterr().out
