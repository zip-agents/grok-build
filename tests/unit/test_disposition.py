from archive.disposition import content_disposition


def test_content_disposition_matches_the_contract_example() -> None:
    header = content_disposition("quarterly report.zip")
    assert header == (
        "attachment; filename=\"quarterly report.zip\"; filename*=UTF-8''quarterly%20report.zip"
    )


def test_ascii_fallback_replaces_non_ascii_quotes_and_backslashes() -> None:
    header = content_disposition('caf\u00e9 "q" \\ x.zip')
    assert 'filename="caf_ _q_ _ x.zip"' in header
    assert "filename*=UTF-8''caf%C3%A9%20%22q%22%20%5C%20x.zip" in header


def test_filename_star_uses_uppercase_percent_encoding() -> None:
    header = content_disposition("a b.zip")
    encoded = header.split("filename*=UTF-8''", 1)[1]
    assert encoded == "a%20b.zip"
    assert encoded == encoded.upper() or encoded == "a%20b.zip"
