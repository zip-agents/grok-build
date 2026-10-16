import pytest

from zip_service.api.multipart import (
    Finished,
    MediaTypeError,
    MultipartDecoder,
    MultipartError,
    PartData,
    PartEnd,
    PartStart,
    classify_content_type,
)


def _push_all(decoder: MultipartDecoder, payload: bytes, size: int = 1) -> list[object]:
    events: list[object] = []
    offset = 0
    while True:
        event = decoder.next()
        if event is not None and event.__class__.__name__ != "_Need":
            events.append(event)
            if isinstance(event, Finished):
                return events
            continue
        if offset >= len(payload):
            decoder.finish()
            continue
        chunk = payload[offset : offset + size]
        offset += len(chunk)
        decoder.push(chunk)


def _body(boundary: str = "bound") -> bytes:
    return (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="filename"\r\n'
        "\r\n"
        "report\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="a.txt"\r\n'
        "\r\n"
        "hello\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="b.txt"; '
        "filename*=UTF-8''caf%C3%A9.txt\r\n"
        "\r\n"
        "x\r\n"
        f"--{boundary}--\r\n"
    ).encode()


def test_content_type_classification() -> None:
    assert classify_content_type("Multipart/Form-Data; Boundary=abc") == "abc"
    assert classify_content_type('multipart/form-data; boundary="a b"') == "a b"
    with pytest.raises(MediaTypeError) as missing:
        classify_content_type(None)
    assert missing.value.code == "unsupported-media-type"
    with pytest.raises(MediaTypeError) as plain:
        classify_content_type("text/plain")
    assert plain.value.code == "unsupported-media-type"
    with pytest.raises(MediaTypeError) as no_boundary:
        classify_content_type("multipart/form-data")
    assert no_boundary.value.code == "malformed-request"


def test_one_byte_feed_parses_fields_and_prefers_filename_star() -> None:
    decoder = MultipartDecoder("bound")
    events = _push_all(decoder, _body(), size=1)
    assert isinstance(events[0], PartStart)
    assert events[0].name == "filename"
    assert events[0].filename is None
    text = b"".join(event.data for event in events if isinstance(event, PartData))
    assert text == b"reporthellox"
    files = [
        event for event in events if isinstance(event, PartStart) and event.filename is not None
    ]
    assert files[0].filename == "a.txt"
    assert files[1].filename == "café.txt"
    assert isinstance(events[-1], Finished)
    assert any(isinstance(event, PartEnd) for event in events)


def test_false_boundary_inside_file_bytes_is_data() -> None:
    payload = (
        b"--bound\r\n"
        b'Content-Disposition: form-data; name="files"; filename="a.bin"\r\n'
        b"\r\n"
        b"before\r\n--boundary-not\r\n"
        b"after\r\n"
        b"--bound--\r\n"
    )
    decoder = MultipartDecoder("bound")
    events = _push_all(decoder, payload, size=3)
    data = b"".join(event.data for event in events if isinstance(event, PartData))
    assert data == b"before\r\n--boundary-not\r\nafter"


def test_empty_filename_is_a_file_part() -> None:
    payload = (
        b'--b\r\nContent-Disposition: form-data; name="files"; filename=""\r\n\r\n\r\n--b--\r\n'
    )
    events = _push_all(MultipartDecoder("b"), payload, size=5)
    starts = [event for event in events if isinstance(event, PartStart)]
    assert starts[0].filename == ""
    assert starts[0].name == "files"


def test_truncated_body_is_an_error() -> None:
    decoder = MultipartDecoder("bound")
    decoder.push(
        b'--bound\r\nContent-Disposition: form-data; name="files"; filename="a"\r\n\r\nno-end'
    )
    decoder.finish()
    with pytest.raises(MultipartError):
        while True:
            event = decoder.next()
            if event.__class__.__name__ == "_Need":
                break


def test_oversized_part_headers_are_rejected() -> None:
    decoder = MultipartDecoder("b")
    decoder.push(b"--b\r\n" + (b"X-Fill: " + b"a" * 70000) + b"\r\n\r\n")
    decoder.finish()
    with pytest.raises(MultipartError):
        while decoder.next().__class__.__name__ != "_Need":
            pass
        decoder.next()
