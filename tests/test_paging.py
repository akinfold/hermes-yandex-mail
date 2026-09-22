"""Page decoded MIME content without fetching other message parts."""

import base64
import json
import re

import pytest

from hermes_yandex_mail import tool
from hermes_yandex_mail.imap import MailError, YandexIMAPClient

from .conftest import FakeIMAP


class MimeIMAP(FakeIMAP):
    def __init__(self, text="Hello, world!", attachment=b"attachment", encoding="BASE64"):
        super().__init__()
        self.text = text
        raw = text.encode("utf-8")
        if encoding == "BASE64":
            raw = base64.encodebytes(raw)
        self.parts = {"1": raw, "2": base64.encodebytes(attachment)}
        self.structure = (
            b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "'
            + encoding.encode()
            + b'" '
            + str(len(raw)).encode()
            + b" 1 NIL NIL)"
            + b'("APPLICATION" "OCTET-STREAM" NIL NIL NIL "BASE64" '
            + str(len(self.parts["2"])).encode()
            + b' NIL ("ATTACHMENT" ("FILENAME" "test.bin")))'
        )

    def uid(self, command, *args):
        if command == "FETCH" and "BODYSTRUCTURE" in args[1]:
            self.calls.append(("uid", command, *args))
            return "OK", [b"1 (UID 8 BODYSTRUCTURE (" + self.structure + b' "MIXED"))']
        match = (
            re.search(r"BODY.PEEK\[([\d.]+)\]<([0-9]+)\.([0-9]+)>", args[1])
            if command == "FETCH"
            else None
        )
        if match:
            self.calls.append(("uid", command, *args))
            part, start, size = match.groups()
            raw = self.parts[part][int(start) : int(start) + int(size)]
            info = f"1 (UID 8 BODY[{part}]<{start}> {{{len(raw)}}}".encode()
            return "OK", [(info, raw), b")"]
        return super().uid(command, *args)


@pytest.fixture
def mailbox(monkeypatch):
    fake = MimeIMAP()
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake),
    )
    return fake


def test_text_page_does_not_download_a_large_attachment(mailbox):
    mailbox.parts["2"] = b"unused"
    mailbox.structure = mailbox.structure.replace(b"17 NIL", b"99999999 NIL")
    result = json.loads(
        tool.handle_read({"uid": "8", "folder": "INBOX", "offset": 2, "max_chars": 4})
    )["message"]
    assert result["body"] == "llo,"
    assert result["next_offset"] == 6
    assert result["eof"] is False
    assert result["attachments"][0]["part_id"] == "2"
    assert result["attachments"][0]["encoded_size"] == 99999999
    assert not any(
        "BODY.PEEK[2]" in str(call) or "BODY.PEEK[]" in str(call) for call in mailbox.calls
    )


def test_text_continuation_has_no_missing_or_repeated_characters(mailbox):
    expected = "A🙂Б\r\nlast line"
    mailbox.parts["1"] = base64.encodebytes(expected.encode())
    offset, pages = 0, []
    for _ in range(30):
        page = json.loads(
            tool.handle_read({"uid": "8", "folder": "INBOX", "offset": offset, "max_chars": 2})
        )["message"]
        pages.append(page["body"])
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert "".join(pages) == expected.replace("\r\n", "\n")


@pytest.mark.parametrize("offset", [-1, "bad", True])
def test_invalid_offsets_are_errors(mailbox, offset):
    result = json.loads(tool.handle_read({"uid": "8", "folder": "INBOX", "offset": offset}))
    assert "error" in result


def test_missing_mime_metadata_is_an_error_not_a_full_message_fallback(mailbox):
    mailbox.structure = b""
    result = json.loads(tool.handle_read({"uid": "8", "folder": "INBOX"}))
    assert "error" in result
    assert not any("BODY.PEEK[]" in str(c) for c in mailbox.calls)


def test_html_and_multiple_text_parts_are_paged_after_conversion(mailbox):
    mailbox.structure = (
        b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "8BIT" 99 1)'
        b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "8BIT" 99 1)'
    )
    mailbox.parts = {"1": b"<b>hello</b>", "2": b"<b>world</b>"}
    page = json.loads(
        tool.handle_read({"uid": "8", "folder": "INBOX", "offset": 3, "max_chars": 5})
    )["message"]
    assert page["body"] == "lo\nwo"
    assert page["body_from_html"] is True
    assert page["next_offset"] == 8


def test_quoted_body_data_and_quoted_empty_eof_are_valid():
    class QuotedServer(MimeIMAP):
        def uid(self, command, *args):
            if "BODY.PEEK[1]" in args[1]:
                offset = 65536 if "<65536." in args[1] else 0
                body = b"x" * 65536 if offset == 0 else b""
                return "OK", [f'1 (UID 8 BODY[1]<{offset}> "'.encode() + body + b'")']
            return super().uid(command, *args)

    with YandexIMAPClient(
        "fixture", "fixture", connection_factory=lambda *_: QuotedServer()
    ) as client:
        assert b"".join(client.iter_part("INBOX", "8", "1")) == b"x" * 65536


def test_malformed_part_response_is_a_mail_error():
    class BrokenServer(MimeIMAP):
        def uid(self, command, *args):
            return "OK", [b"("]

    with (
        YandexIMAPClient(
            "fixture", "fixture", connection_factory=lambda *_: BrokenServer()
        ) as client,
        pytest.raises(MailError),
    ):
        list(client.iter_part("INBOX", "8", "1"))
