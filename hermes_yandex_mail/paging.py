"""Streaming transfer/charset decoding and bounded pages; no persistent mail cache."""

from __future__ import annotations

import base64
import codecs
import quopri
from html.parser import HTMLParser


def decoded_chunks(chunks, encoding):
    encoding = encoding.lower()
    if encoding in {"7bit", "8bit", "binary"}:
        yield from chunks
    elif encoding == "base64":
        yield from _base64_chunks(chunks)
    elif encoding == "quoted-printable":
        yield from _quoted_chunks(chunks)
    else:
        raise ValueError(f"Unsupported MIME transfer encoding: {encoding}.")


def _base64_chunks(chunks):
    pending = b""
    ended = False
    for chunk in chunks:
        pending += b"".join(chunk.split())
        if ended and pending:
            raise ValueError("Data after base64 padding.")
        length = len(pending) // 4 * 4
        if length:
            block, pending = pending[:length], pending[length:]
            yield base64.b64decode(block, validate=True)
            ended = b"=" in block
    if pending:
        raise ValueError("Incomplete base64 MIME content.")


def _quoted_chunks(chunks):
    pending = b""
    for chunk in chunks:
        pending += chunk
        last = pending.rfind(b"=")
        end = last if last >= 0 and len(pending) - last < 3 else len(pending)
        yield quopri.decodestring(pending[:end])
        pending = pending[end:]
    if pending:
        yield quopri.decodestring(pending)


class TextHTMLParser(HTMLParser):
    """Stream readable HTML text without executing or fetching embedded content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.output = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag == "br" and not self.hidden:
            self.output.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.output.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.output.append(data)

    def drain(self):
        result = "".join(self.output)
        self.output.clear()
        if len(self.rawdata) > 65536:
            raise ValueError("HTML token exceeds the 64 KiB parsing limit.")
        return result


def _text_decoder(charset):
    try:
        b"".decode(charset)  # Reject binary transformation codecs used as a charset.
        return codecs.getincrementaldecoder(charset)(errors="replace")
    except LookupError:
        return codecs.getincrementaldecoder("utf-8")(errors="replace")


def text_chunks(chunks, charset, html=False):
    decoder = _text_decoder(charset)
    parser = TextHTMLParser() if html else None
    pending = ""
    for chunk in chunks:
        text = pending + decoder.decode(chunk)
        if len(decoder.getstate()[0]) > 65536:
            raise ValueError("Charset decoder state exceeds the 64 KiB buffering limit.")
        pending = "\r" if text.endswith("\r") else ""
        text = text[:-1] if pending else text
        text = text.replace("\r\n", "\n")
        if parser:
            parser.feed(text)
            text = parser.drain()
        yield text
    tail = pending + decoder.decode(b"", final=True)
    if parser:
        parser.feed(tail)
        parser.close()
        tail = parser.drain()
    yield tail


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
