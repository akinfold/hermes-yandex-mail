"""Decode MIME parts as they stream in, and cut pages from the result.

Nothing here keeps mail anywhere: every page is computed by decoding the
selected parts from their beginning.

Decoding is as forgiving as the email package that ``read_message`` relied on
before it paged. Base64 with missing padding, stray characters or several
concatenated blocks, an unknown charset, and an unknown transfer encoding all
still produce text: mail that was sent slightly wrong is still mail the user
wants to read.
"""

from __future__ import annotations

import base64
import codecs
import quopri
import re
from collections.abc import Iterable, Iterator

from .html_text import html_text

__all__ = ["body_text", "decoded_chunks", "take_page", "text_chunks"]

_BASE64_JUNK = re.compile(rb"[^A-Za-z0-9+/=]")
_TEXT_TOKEN = re.compile(r"\s+|\S+")
#: The most input a charset decoder may hold while waiting for a sequence to end.
_DECODER_LIMIT = 65536


def decoded_chunks(chunks: Iterable[bytes], encoding: str) -> Iterator[bytes]:
    """Undo the transfer encoding of one part, chunk by chunk."""
    encoding = encoding.lower()
    if encoding == "base64":
        yield from _base64_chunks(chunks)
    elif encoding == "quoted-printable":
        yield from _quoted_chunks(chunks)
    else:
        # 7bit, 8bit and binary need nothing, and an encoding nobody recognises
        # is passed through as is, as the email package does.
        yield from chunks


def _base64_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Decode base64, skipping stray characters and supplying missing padding.

    Padding ends a block, not the part, so blocks that were simply concatenated
    all decode.
    """
    pending = b""
    for chunk in chunks:
        data, pending = _base64_blocks(pending + _BASE64_JUNK.sub(b"", chunk))
        yield data
    yield _base64_block(pending)


def _base64_blocks(data: bytes) -> tuple[bytes, bytes]:
    """Decode every whole quad; return the decoded bytes and what is left over."""
    out = bytearray()
    while (pad := data.find(b"=")) >= 0:
        out += _base64_block(data[:pad])
        data = data[pad:].lstrip(b"=")
    whole = len(data) // 4 * 4
    out += base64.b64decode(data[:whole])
    return bytes(out), data[whole:]


def _base64_block(block: bytes) -> bytes:
    """Decode the end of a block, whether or not it was padded."""
    extra = len(block) % 4
    if extra == 1:
        block = block[:-1]  # a lone character does not carry a whole byte
    elif extra:
        block += b"=" * (4 - extra)
    return base64.b64decode(block)


def _quoted_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    pending = b""
    for chunk in chunks:
        pending += chunk
        last = pending.rfind(b"=")
        end = last if last >= 0 and len(pending) - last < 3 else len(pending)
        yield quopri.decodestring(pending[:end])
        pending = pending[end:]
    if pending:
        yield quopri.decodestring(pending)


def _text_decoder(charset: str) -> codecs.IncrementalDecoder:
    try:
        b"".decode(charset)  # Reject binary transformation codecs used as a charset.
        return codecs.getincrementaldecoder(charset)(errors="replace")
    except LookupError:
        return codecs.getincrementaldecoder("utf-8")(errors="replace")


def text_chunks(chunks: Iterable[bytes], charset: str) -> Iterator[str]:
    """Decode one part's bytes to text, with CRLF line ends turned into LF."""
    decoder = _text_decoder(charset)
    pending = ""
    for chunk in chunks:
        text = pending + decoder.decode(chunk)
        if len(decoder.getstate()[0]) > _DECODER_LIMIT:
            raise ValueError("Charset decoder state exceeds the 64 KiB buffering limit.")
        pending = "\r" if text.endswith("\r") else ""
        text = text[:-1] if pending else text
        yield text.replace("\r\n", "\n")
    yield (pending + decoder.decode(b"", final=True)).replace("\r\n", "\n")


def _stripped(chunks: Iterable[str]) -> Iterator[str]:
    """The text without leading or trailing whitespace, as ``str.strip`` gives it."""
    started, space = False, ""
    for chunk in chunks:
        out: list[str] = []
        for match in _TEXT_TOKEN.finditer(chunk):
            token = match.group()
            if not token[0].isspace():
                out += (space, token)
                started, space = True, ""
            elif started:
                space += token
        yield "".join(out)


def _plain_body(parts: Iterable[Iterable[str]]) -> Iterator[str]:
    """Every part stripped, the ones with text in them joined by newlines."""
    written = False
    for part in parts:
        started = False
        for chunk in _stripped(part):
            if not chunk:
                continue
            if written and not started:
                yield "\n"
            started = written = True
            yield chunk


def _html_parts(parts: Iterable[Iterable[str]]) -> Iterator[str]:
    for index, part in enumerate(parts):
        if index:
            yield "\n"
        yield from part


def body_text(parts: Iterable[Iterable[str]], html: bool) -> Iterator[str]:
    """The body as ``read_message`` shows it, from the text of its parts.

    Plain parts are stripped and joined with newlines; HTML parts are joined
    and converted to text together, as one document.
    """
    return html_text(_html_parts(parts)) if html else _plain_body(parts)


def take_page(chunks, offset, limit, empty):
    """Slice decoded characters or bytes with one extra unit to establish EOF."""
    remaining = limit + 1
    result = []
    for chunk in chunks:
        if offset >= len(chunk):
            offset -= len(chunk)
            continue
        piece = chunk[offset : offset + remaining]
        offset = 0
        result.append(piece)
        remaining -= len(piece)
        if remaining == 0:
            break
    value = empty.join(result)
    return value[:limit], len(value) <= limit
