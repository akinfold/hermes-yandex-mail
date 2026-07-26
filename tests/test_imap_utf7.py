"""Modified UTF-7 (RFC 3501 §5.1.3) round-trips, including Yandex' folders."""

from __future__ import annotations

import pytest

from hermes_yandex_mail import imap_utf7


@pytest.mark.parametrize(
    ("text", "encoded"),
    [
        ("INBOX", b"INBOX"),
        ("Sent", b"Sent"),
        ("Отправленные", b"&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"),
        ("Спам", b"&BCEEPwQwBDw-"),
        ("Черновики", b"&BCcENQRABD0EPgQyBDgEOgQ4-"),
        # A '/' inside the base64 run is written as ',', but a literal '/' in the
        # name is printable ASCII and stays as it is.
        ("Проекты/2026", b"&BB8EQAQ+BDUEOgRCBEs-/2026"),
        ("R&D", b"R&-D"),
        ("Papa & Mama", b"Papa &- Mama"),
        ("", b""),
    ],
)
def test_encode(text, encoded):
    assert imap_utf7.encode(text) == encoded


@pytest.mark.parametrize(
    "text",
    [
        "INBOX",
        "Отправленные",
        "Спам",
        "Удалённые",
        "Черновики",
        "R&D",
        "Проекты/2026",
        "mixed Кириллица and latin",
        "emoji 📬 folder",
    ],
)
def test_round_trip(text):
    assert imap_utf7.decode(imap_utf7.encode(text)) == text


def test_decode_accepts_str_and_bytes():
    assert imap_utf7.decode("&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-") == "Отправленные"
    assert imap_utf7.decode(b"INBOX") == "INBOX"


def test_decode_ampersand_escape():
    assert imap_utf7.decode(b"R&-D") == "R&D"


def test_decode_unterminated_shift_is_kept_verbatim():
    assert imap_utf7.decode(b"Broken&BB4EQg") == "Broken&BB4EQg"


def test_decode_invalid_base64_is_kept_verbatim():
    assert imap_utf7.decode(b"Odd&!!!-name") == "Odd&!!!-name"
