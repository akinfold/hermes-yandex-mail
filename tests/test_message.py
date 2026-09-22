"""Header decoding: encoded words, address lists, dates."""

from __future__ import annotations

import base64

from hermes_yandex_mail.message import (
    addresses,
    bare_addresses,
    decode_header_value,
    header_date_iso,
    parse_message_bytes,
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


def test_an_encoded_word_nested_in_a_display_name_cannot_become_an_address():
    """The header is already decoded by the parser's policy, so decoding the whole
    thing again before splitting turned one sender into two — and the attacker's
    address came first, which is exactly what ``from_address`` reports."""
    inner = "=?utf-8?B?" + base64.b64encode(b"x@evil.org,").decode() + "?="
    outer = "=?utf-8?B?" + base64.b64encode(inner.encode()).decode() + "?="
    parsed = parse_message_bytes(f"From: {outer} <billing@real-bank.example>\r\n\r\n".encode())
    assert bare_addresses(parsed.get("From")) == ["billing@real-bank.example"]
    assert addresses(parsed.get("From"))[0].endswith("<billing@real-bank.example>")


def test_a_display_name_that_is_itself_an_address_stays_a_display_name():
    parsed = parse_message_bytes(b'From: "me@victim.org" <attacker@evil.example>\r\n\r\n')
    assert bare_addresses(parsed.get("From")) == ["attacker@evil.example"]
