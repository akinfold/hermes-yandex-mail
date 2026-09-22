"""Message headers: RFC 5322 bytes in, decoded values out.

Everything here is pure and free of Hermes and IMAP imports, so it can be
unit-tested against fixture bytes. Headers are decoded from RFC 2047 encoded
words, never shown raw. The body is read part by part, without parsing the
whole message: see :mod:`.mime` and :mod:`.paging`.
"""

from __future__ import annotations

from email import message_from_bytes, policy
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

__all__ = [
    "addresses",
    "bare_addresses",
    "decode_header_value",
    "header_date_iso",
    "parse_message_bytes",
]


def decode_header_value(raw: str | None) -> str:
    """Decode an RFC 2047 header value ("=?utf-8?B?...?=") to plain text."""
    if not raw:
        return ""
    try:
        parts = decode_header(raw)
    except Exception:
        return str(raw).strip()
    out: list[str] = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _address_pairs(raw: str | None) -> list[tuple[str, str]]:
    """``(display name, address)`` pairs — split FIRST, decoded afterwards.

    The order is the whole point. :func:`parse_message_bytes` uses
    ``policy.default``, which has already decoded the header, so decoding the
    whole thing again before splitting — as an earlier version did — lets an
    encoded word *nested* inside a display name become a second address. A
    ``From`` of ``=?utf-8?B?…?= <billing@real-bank.example>`` whose display name
    decodes to ``x@evil.org,`` then yields two addresses where the sender wrote
    one, and the attacker's is the first of them: exactly the value
    ``from_address`` reports and a model is told to copy into a reply.

    Splitting before decoding means a display name can only ever stay a display
    name, however many layers of encoding are wrapped around it.
    """
    text = str(raw or "")
    if not text:
        return []
    return [(decode_header_value(name), addr) for name, addr in getaddresses([text])]


def addresses(raw: str | None) -> list[str]:
    """Split an address header into ``Name <addr>`` / ``addr`` strings."""
    out: list[str] = []
    for name, addr in _address_pairs(raw):
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr or name:
            out.append(addr or name)
    return out


def bare_addresses(raw: str | None) -> list[str]:
    """Just the addr-specs from an address header, without display names.

    :func:`addresses` renders ``Name <addr>`` for a human reader, which is the
    wrong thing to hand back to a model that may copy it into an argument: a
    display name containing an ``@`` parses as a second address. This is the
    form to copy.
    """
    return [addr for _name, addr in _address_pairs(raw) if addr]


def header_date_iso(raw: str | None) -> str:
    """Parse a ``Date:`` header into an ISO 8601 string, or "" if unparseable."""
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return ""


def parse_message_bytes(raw: bytes) -> Message:
    """Parse raw RFC 5322 bytes with the modern email policy."""
    return message_from_bytes(raw, policy=policy.default)
