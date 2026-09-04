"""Bounded parsing of IMAP BODYSTRUCTURE (RFC 3501, section 7.4.2)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message

from .message import decode_header_value


class StructureParser:
    """Parse IMAP lists, quoted strings, atoms, and length-prefixed literals."""

    def __init__(self, data: bytes, literal_bytes: bool = False):
        if len(data) > 1024 * 1024:
            raise ValueError("MIME structure exceeds the 1 MiB metadata limit.")
        self.data = data
        self.pos = 0
        self.literal_bytes = literal_bytes

    def value(self, depth: int = 0):
        if depth > 40:
            raise ValueError("MIME structure is too deeply nested.")
        self.space()
        if self.pos >= len(self.data):
            raise ValueError("Incomplete MIME structure.")
        char = self.data[self.pos : self.pos + 1]
        if char == b"(":
            return self.list_value(depth)
        if char == b'"':
            return self.quoted()
        if char == b"{":
            return self.literal()
        match = re.match(rb"[^\s()]+", self.data[self.pos :])
        if match is None:
            raise ValueError("Invalid MIME structure token.")
        self.pos += len(match[0])
        return None if match[0].upper() == b"NIL" else match[0].decode("utf-8", "replace")

    def space(self):
        while self.pos < len(self.data) and self.data[self.pos] in b" \t\r\n":
            self.pos += 1

    def list_value(self, depth):
        self.pos += 1
        items = []
        while True:
            self.space()
            if self.data[self.pos : self.pos + 1] == b")":
                self.pos += 1
                return items
            items.append(self.value(depth + 1))

    def quoted(self):
        self.pos += 1
        out = bytearray()
        while self.pos < len(self.data):
            char = self.data[self.pos]
            self.pos += 1
            if char == 34:
                return bytes(out) if self.literal_bytes else out.decode("utf-8", "replace")
            if char == 92 and self.pos < len(self.data):
                char = self.data[self.pos]
                self.pos += 1
            out.append(char)
        raise ValueError("Unterminated MIME string.")

    def literal(self):
        match = re.match(rb"\{([0-9]+)\}\r\n", self.data[self.pos :])
        if match is None:
            raise ValueError("Invalid MIME literal.")
        self.pos += len(match[0])
        end = self.pos + int(match[1])
        if end > len(self.data):
            raise ValueError("Incomplete MIME literal.")
        value = self.data[self.pos : end]
        self.pos = end
        return value if self.literal_bytes else value.decode("utf-8", "replace")


def response_fields(data: list, uid: str, literal_bytes: bool = False) -> dict:
    """Find the requested UID, including replies split around IMAP literals."""
    chunks = []
    for item in data:
        if isinstance(item, tuple):
            chunks.append(item[0] + b"\r\n" + item[1])
        elif isinstance(item, bytes):
            chunks.append(item)
    parser = StructureParser(b" ".join(chunks), literal_bytes)
    found = {}
    while parser.pos < len(parser.data):
        value = parser.value()
        if isinstance(value, list):
            fields = {str(k).upper(): v for k, v in zip(value[::2], value[1::2], strict=False)}
            if fields.get("UID") == uid:
                found.update(fields)
        parser.space()
    if not found:
        raise ValueError(f"Message {uid} was not returned.")
    return found


def _parameters(values) -> dict[str, str]:
    if not isinstance(values, list):
        return {}
    return {str(k).lower(): str(v) for k, v in zip(values[::2], values[1::2], strict=False)}


def _attached(disposition) -> bool:
    return (
        bool(disposition)
        and isinstance(disposition, list)
        and str(disposition[0]).lower() == "attachment"
    )


@dataclass(frozen=True)
class MimePart:
    part_id: str
    content_type: str
    encoding: str
    encoded_size: int
    charset: str = "utf-8"
    filename: str = ""
    attachment: bool = False

    def attachment_info(self):
        return {
            "part_id": self.part_id,
            "filename": self.filename or "(unnamed)",
            "content_type": self.content_type,
            "size": None,
            "encoded_size": self.encoded_size,
        }


def _filename(params, disposition) -> str:
    message = Message()
    message["Content-Type"] = "application/octet-stream"
    for key, value in _parameters(params).items():
        message.set_param(key, value)
    if isinstance(disposition, list) and len(disposition) > 1:
        message["Content-Disposition"] = str(disposition[0])
        for key, value in _parameters(disposition[1]).items():
            message.set_param(key, value, header="Content-Disposition")
    return decode_header_value(message.get_filename())


def _leaf(node: list, part_id: str, inherited_attachment: bool) -> MimePart:
    content_type = f"{node[0]}/{node[1]}".lower()
    extension = 8 if node[0].upper() == "TEXT" else 7
    if content_type == "message/rfc822":
        extension = 10
    disposition = node[extension + 1] if len(node) > extension + 1 else None
    filename = _filename(node[2], disposition)
    attached = _attached(disposition)
    size = int(node[6])
    if size < 0:
        raise ValueError("Invalid MIME part size.")
    return MimePart(
        part_id,
        content_type,
        str(node[5]).lower(),
        size,
        _parameters(node[2]).get("charset", "utf-8"),
        filename,
        inherited_attachment
        or attached
        or bool(filename)
        or content_type not in {"text/plain", "text/html"},
    )


def flatten_parts(
    node: list, prefix: str = "", inherited_attachment: bool = False
) -> list[MimePart]:
    """Number leaf sections; attached messages remain opaque downloadable parts."""
    if not node or not isinstance(node[0], list):
        return [_leaf(node, prefix or "1", inherited_attachment)]
    count = next((i for i, item in enumerate(node) if not isinstance(item, list)), len(node))
    disposition = node[count + 2] if len(node) > count + 2 else None
    attached = _attached(disposition)
    parts = []
    for i, child in enumerate(node[:count], 1):
        number = f"{prefix}.{i}" if prefix else str(i)
        parts.extend(flatten_parts(child, number, inherited_attachment or attached))
    return parts
