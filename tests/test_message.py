"""MIME parsing: header decoding, body selection, attachments, HTML stripping."""

from __future__ import annotations

from hermes_yandex_mail.message import (
    addresses,
    decode_header_value,
    extract_body,
    header_date_iso,
    html_to_text,
    parse_message_bytes,
)

MULTIPART = (
    b"Subject: =?utf-8?B?0J7RgtGH0LXRgg==?=\r\n"
    b"From: Sender <sender@example.org>\r\n"
    b'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
    b"\r\n"
    b"--BOUND\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Plain part.\r\n"
    b"--BOUND\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>HTML part</p>\r\n"
    b"--BOUND\r\n"
    b'Content-Type: application/pdf; name="report.pdf"\r\n'
    b'Content-Disposition: attachment; filename="report.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"aGVsbG8gcGRm\r\n"
    b"--BOUND--\r\n"
)

HTML_ONLY = (
    b"Subject: Newsletter\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<html><head><style>p{color:red}</style></head><body>"
    b"<p>Hello&nbsp;&amp; welcome</p><br><div>Second</div>"
    b"<script>alert(1)</script></body></html>\r\n"
)


def test_decode_header_value_handles_encoded_words():
    assert decode_header_value("=?utf-8?B?0J/RgNC40LLQtdGC?=") == "Привет"


def test_decode_header_value_empty_and_plain():
    assert decode_header_value(None) == ""
    assert decode_header_value("Plain subject") == "Plain subject"


def test_decode_header_value_survives_broken_input():
    assert decode_header_value("=?utf-8?Q?bad") == "=?utf-8?Q?bad"


def test_addresses_splits_and_decodes():
    raw = "=?utf-8?B?0K/QvdC00LXQutGB?= <noreply@id.yandex.ru>, plain@example.org"
    assert addresses(raw) == ["Яндекс <noreply@id.yandex.ru>", "plain@example.org"]


def test_addresses_empty():
    assert addresses("") == []
    assert addresses(None) == []


def test_header_date_iso():
    assert header_date_iso("Sun, 26 Jul 2026 01:40:20 +0300").startswith("2026-07-26T01:40:20")
    assert header_date_iso("not a date") == ""
    assert header_date_iso(None) == ""


def test_html_to_text_drops_scripts_and_unescapes():
    text = html_to_text(HTML_ONLY.split(b"\r\n\r\n", 1)[1].decode())
    assert "alert(1)" not in text
    assert "color:red" not in text
    assert "Hello & welcome" in text
    assert "Second" in text


def test_extract_body_prefers_plain_and_lists_attachments():
    body = extract_body(parse_message_bytes(MULTIPART))
    assert body.text == "Plain part."
    assert body.is_html is False
    assert [(a.filename, a.content_type, a.size) for a in body.attachments] == [
        ("report.pdf", "application/pdf", 9)
    ]


def test_extract_body_falls_back_to_html():
    body = extract_body(parse_message_bytes(HTML_ONLY))
    assert body.is_html is True
    assert "Hello & welcome" in body.text
    assert body.attachments == []


def test_extract_body_truncates_and_says_so():
    body = extract_body(parse_message_bytes(MULTIPART), max_chars=5)
    assert body.text == "Plain"
    assert body.truncated is True


def test_extract_body_of_a_message_without_text():
    raw = b"Subject: Only image\r\nContent-Type: image/png\r\n\r\nbinary\r\n"
    body = extract_body(parse_message_bytes(raw))
    assert body.text == ""
    assert body.attachments[0].content_type == "image/png"


def test_extract_body_with_unknown_charset():
    raw = (
        b"Subject: Odd\r\nContent-Type: text/plain; charset=nonexistent-charset\r\n"
        b"\r\nplain text\r\n"
    )
    assert extract_body(parse_message_bytes(raw)).text == "plain text"
