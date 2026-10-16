import unicodedata

import pytest

from zip_service.names import (
    EntryNamer,
    InvalidArchiveName,
    resolve_archive_name,
    sanitize_entry_name,
)


def test_absent_or_blank_archive_name_uses_the_default() -> None:
    assert resolve_archive_name(None) == "zipped_files.zip"
    assert resolve_archive_name("   ") == "zipped_files.zip"
    assert resolve_archive_name("\t\n") == "zipped_files.zip"


def test_zip_suffix_is_appended_unless_already_present() -> None:
    assert resolve_archive_name("report") == "report.zip"
    assert resolve_archive_name("  quarterly report  ") == "quarterly report.zip"
    assert resolve_archive_name("report.ZIP") == "report.ZIP"
    assert resolve_archive_name("report.Zip") == "report.Zip"


def test_archive_name_rejects_controls_and_separators() -> None:
    for raw in ("a/b", "a\\b", "a\nb", "a\x00b", "a\x7fb"):
        with pytest.raises(InvalidArchiveName):
            resolve_archive_name(raw)


def test_archive_name_is_normalised_to_nfc() -> None:
    decomposed = "cafe\u0301"
    assert resolve_archive_name(decomposed) == "café.zip"
    assert unicodedata.is_normalized("NFC", resolve_archive_name(decomposed))


def test_archive_name_rejects_more_than_255_utf8_bytes() -> None:
    stem = "é" * 127  # 254 bytes, plus ".zip" would be 258
    with pytest.raises(InvalidArchiveName):
        resolve_archive_name(stem)
    assert len(resolve_archive_name("a" * 251).encode()) == 255


def test_entry_names_are_flattened_and_cleaned() -> None:
    assert sanitize_entry_name("dir\\sub/report.txt") == "report.txt"
    assert sanitize_entry_name("a\x00b\x1fc") == "abc"
    assert sanitize_entry_name("  café.txt  ") == "café.txt"
    assert sanitize_entry_name("") == "file"
    assert sanitize_entry_name(".") == "file"
    assert sanitize_entry_name("..") == "file"
    assert sanitize_entry_name("foo/..") == "file"
    assert sanitize_entry_name("   ") == "file"


def test_entry_truncation_preserves_a_short_extension() -> None:
    stem = "a" * 300
    name = sanitize_entry_name(f"{stem}.txt")
    assert name.endswith(".txt")
    assert len(name.encode()) <= 255

    long_ext = "." + ("b" * 17)
    whole = sanitize_entry_name(("c" * 300) + long_ext)
    assert not whole.endswith(long_ext)
    assert len(whole.encode()) <= 255


def test_entry_truncation_stops_on_a_character_boundary() -> None:
    name = sanitize_entry_name("é" * 200 + ".txt")
    name.encode("utf-8")
    assert name.endswith(".txt")
    assert len(name.encode()) <= 255


def test_dedup_inserts_the_lowest_free_number_before_the_extension() -> None:
    namer = EntryNamer()
    assert namer.allocate("report.txt") == "report.txt"
    assert namer.allocate("report.txt") == "report (1).txt"
    assert namer.allocate("Report.TXT") == "Report (2).TXT"
    assert namer.allocate("README") == "README"
    assert namer.allocate("readme") == "readme (1)"
    assert namer.allocate("archive.tar.gz") == "archive.tar.gz"
    assert namer.allocate("archive.tar.gz") == "archive.tar (1).gz"
    assert namer.allocate(".env") == ".env"
    assert namer.allocate(".env") == ".env (1)"


def test_generated_dedup_names_are_checked_for_collisions() -> None:
    namer = EntryNamer()
    assert namer.allocate("a.txt") == "a.txt"
    assert namer.allocate("a (1).txt") == "a (1).txt"
    assert namer.allocate("a.txt") == "a (2).txt"


def test_casefold_treats_sharp_s_as_a_collision() -> None:
    namer = EntryNamer()
    assert namer.allocate("straße.txt") == "straße.txt"
    assert namer.allocate("STRASSE.txt") == "STRASSE (1).txt"
