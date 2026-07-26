"""Modified UTF-7 for IMAP mailbox names (RFC 3501, section 5.1.3).

Yandex names most of its folders in Russian ("Отправленные", "Спам"), and IMAP
carries mailbox names in a modified UTF-7 where non-ASCII runs are BASE64 of
UTF-16BE between ``&`` and ``-``, with ``/`` written as ``,``. Python's built-in
``utf-7`` codec is the unmodified variant and cannot be used here.

No Hermes imports: this module is pure and unit-testable on its own.
"""

from __future__ import annotations

import base64

__all__ = ["decode", "encode"]

_PRINTABLE_START = "\x20"
_PRINTABLE_END = "\x7e"


def _encode_run(run: str) -> bytes:
    """Encode one non-ASCII run as ``&<modified base64>-``."""
    encoded = base64.b64encode(run.encode("utf-16-be")).rstrip(b"=").replace(b"/", b",")
    return b"&" + encoded + b"-"


def encode(name: str) -> bytes:
    """Encode a mailbox name to modified UTF-7 bytes."""
    out = bytearray()
    run: list[str] = []
    for char in name:
        if _PRINTABLE_START <= char <= _PRINTABLE_END:
            if run:
                out += _encode_run("".join(run))
                run = []
            out += b"&-" if char == "&" else char.encode("ascii")
        else:
            run.append(char)
    if run:
        out += _encode_run("".join(run))
    return bytes(out)


def _decode_run(chunk: str) -> str:
    """Decode one modified-BASE64 chunk (the text between ``&`` and ``-``)."""
    padded = chunk.replace(",", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        # validate=True: characters outside the alphabet must fail rather than be
        # silently dropped, otherwise a malformed name decodes to a plausible lie.
        return base64.b64decode(padded, validate=True).decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        # Not valid modified UTF-7 — hand the bytes back rather than lose them.
        return "&" + chunk + "-"


def decode(name: bytes | str) -> str:
    """Decode a modified UTF-7 mailbox name to text.

    Anything that does not decode is returned verbatim, so an unexpected server
    response degrades to a readable name instead of raising.
    """
    text = name.decode("utf-8", "replace") if isinstance(name, bytes) else name
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "&":
            out.append(char)
            index += 1
            continue
        end = text.find("-", index + 1)
        if end < 0:  # unterminated shift — treat the rest as literal
            out.append(text[index:])
            break
        chunk = text[index + 1 : end]
        out.append("&" if not chunk else _decode_run(chunk))
        index = end + 1
    return "".join(out)
