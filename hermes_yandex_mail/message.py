"""MIME parsing: RFC 5322 bytes in, plain data classes out.

Everything here is pure and free of Hermes and IMAP imports, so it can be
unit-tested against fixture bytes. The rules that matter for an agent-facing
mail tool:

* headers are decoded from RFC 2047 encoded words, never shown raw;
* the body is preferred as ``text/plain``, falling back to a stripped
  ``text/html`` so HTML-only newsletters are still readable;
* attachments are listed (name, type, size) but never decoded into the answer.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

__all__ = [
    "Attachment",
    "MessageBody",
    "addresses",
    "decode_header_value",
    "extract_body",
    "header_date_iso",
    "html_to_text",
    "parse_message_bytes",
]


@dataclass
class Attachment:
    """One attached part: what it is called, what it is, how big it is."""

    filename: str
    content_type: str
    size: int


@dataclass
class MessageBody:
    """The readable part of a message plus its attachment inventory."""

    text: str
    is_html: bool = False
    truncated: bool = False
    attachments: list[Attachment] = field(default_factory=list)


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


def addresses(raw: str | None) -> list[str]:
    """Split an address header into ``Name <addr>`` / ``addr`` strings."""
    decoded = decode_header_value(raw)
    if not decoded:
        return []
    out: list[str] = []
    for name, addr in getaddresses([decoded]):
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr or name:
            out.append(addr or name)
    return out


def header_date_iso(raw: str | None) -> str:
    """Parse a ``Date:`` header into an ISO 8601 string, or "" if unparseable."""
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return ""


_TAG_RE = re.compile(r"<[^>]+>")
_DROP_RE = re.compile(r"<(script|style|head)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(r"<(br|/p|/div|/tr|/h[1-6])[^>]*>", re.IGNORECASE)
_BLANK_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    """Reduce an HTML body to readable text.

    Deliberately regex-based rather than a parser: the input is untrusted mail,
    nothing here executes or expands anything, and it keeps the plugin
    dependency-free.
    """
    text = _DROP_RE.sub(" ", raw)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_module.unescape(text)
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RE.sub("\n\n", text).strip()


def parse_message_bytes(raw: bytes) -> Message:
    """Parse raw RFC 5322 bytes with the modern email policy."""
    return message_from_bytes(raw, policy=policy.default)


def _part_text(part: Message) -> str:
    """Decode one text part, tolerating a wrong or missing charset."""
    payload = part.get_payload(decode=True)
    if payload is None:
        content = part.get_payload()
        return content if isinstance(content, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def _is_attachment(part: Message) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    return disposition == "attachment" or bool(part.get_filename())


def _attachment_of(part: Message) -> Attachment:
    payload = part.get_payload(decode=True) or b""
    return Attachment(
        filename=decode_header_value(part.get_filename()) or "(unnamed)",
        content_type=part.get_content_type(),
        size=len(payload),
    )


def _collect_parts(msg: Message) -> tuple[list[str], list[str], list[Attachment]]:
    """Walk the MIME tree once, sorting parts into plain, html, and attachments."""
    plain: list[str] = []
    html: list[str] = []
    attachments: list[Attachment] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if _is_attachment(part):
            attachments.append(_attachment_of(part))
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_part_text(part))
        elif content_type == "text/html":
            html.append(_part_text(part))
        else:
            attachments.append(_attachment_of(part))
    return plain, html, attachments


def extract_body(msg: Message, max_chars: int = 20000) -> MessageBody:
    """Pick the most readable body of ``msg`` and list its attachments.

    ``text/plain`` wins; an HTML-only message is stripped to text and flagged
    with ``is_html``. The text is capped at ``max_chars`` and the cap is
    reported rather than hidden.
    """
    plain, html, attachments = _collect_parts(msg)
    if plain:
        text, is_html = "\n".join(p.strip() for p in plain if p.strip()), False
    elif html:
        text, is_html = html_to_text("\n".join(html)), True
    else:
        text, is_html = "", False
    text = text.replace("\r\n", "\n").strip()
    truncated = max_chars > 0 and len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return MessageBody(text=text, is_html=is_html, truncated=truncated, attachments=attachments)
