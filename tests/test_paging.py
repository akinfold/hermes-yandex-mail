"""Page decoded MIME content without fetching other message parts."""

import base64
import json
import re

import pytest

from hermes_yandex_mail import tool
from hermes_yandex_mail.imap import MailError, YandexIMAPClient

from .conftest import FakeIMAP

#: BODYSTRUCTURE exactly as imap.yandex.ru returned it for messages planted in
#: the test account. Hand-written fixtures are kinder than the server: Yandex
#: joins RFC 2231 continuations itself and returns the value still
#: percent-encoded under the plain "filename" key, and it describes a forwarded
#: message's own parts inside the message/rfc822 part.
YANDEX_CYRILLIC_ATTACHMENT = (
    b'11 (UID 8 BODYSTRUCTURE (("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 15 1 NIL NIL'
    b' NIL NIL)("application" "pdf" NIL NIL NIL "base64" 410528 NIL ("attachment" ("filename"'
    b" \"utf-8''%D0%9E%D1%82%D1%87%D1%91%D1%82%20%D0%B7%D0%B0%20%D1%81%D0%B5%D0%BD%D1%82%D1%8F"
    b'%D0%B1%D1%80%D1%8C.pdf")) NIL NIL) "mixed" ("boundary" "===============0250679736585718576'
    b'==") NIL NIL NIL))'
)
YANDEX_RFC2047_ATTACHMENT = (
    b'12 (UID 8 BODYSTRUCTURE (("text" "plain" ("charset" "utf-8") NIL NIL "7BIT" 4 1 NIL NIL'
    b' NIL NIL)("application" "pdf" ("name" "=?utf-8?B?0J7RgtGH0ZHRgi5wZGY=?=") NIL NIL "base64"'
    b' 20 NIL ("attachment" ("filename" "=?utf-8?B?0J7RgtGH0ZHRgi5wZGY=?=")) NIL NIL) "mixed"'
    b' ("boundary" "B") NIL NIL NIL))'
)
YANDEX_FORWARDED = (
    b'13 (UID 8 BODYSTRUCTURE (("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 13 1 NIL NIL'
    b' NIL NIL)("message" "rfc822" NIL NIL NIL "8bit" 579 (NIL "[hermes-yandex-mail e2e]'
    b' d137a1693556 inner" ((NIL NIL "hermesplugins" "yandex.ru")) ((NIL NIL "hermesplugins"'
    b' "yandex.ru")) ((NIL NIL "hermesplugins" "yandex.ru")) ((NIL NIL "hermesplugins"'
    b' "yandex.ru")) NIL NIL NIL NIL) (("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 26 1'
    b' NIL NIL NIL NIL)("text" "html" ("charset" "utf-8") NIL NIL "7bit" 30 1 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "===============4329491408176734700==") NIL NIL NIL) 21 NIL'
    b' ("attachment" ("filename" "")) NIL NIL) "mixed" ("boundary"'
    b' "===============8185087172575757081==") NIL NIL NIL))'
)


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

        self.response = None

    def uid(self, command, *args):
        if command == "FETCH" and "BODYSTRUCTURE" in args[1]:
            self.calls.append(("uid", command, *args))
            if self.response is not None:
                return "OK", [self.response]
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
    assert result["attachments"][0]["size"] == 99999999 * 57 // 78
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


@pytest.mark.parametrize("offset,body", [("7", "world!"), (7, "world!"), (-1, "Hello, world!")])
def test_offset_is_read_the_way_search_reads_it(mailbox, offset, body):
    result = json.loads(tool.handle_read({"uid": "8", "folder": "INBOX", "offset": offset}))
    assert result["message"]["body"] == body


def test_an_offset_that_is_not_a_number_is_an_error(mailbox):
    result = json.loads(tool.handle_read({"uid": "8", "folder": "INBOX", "offset": "bad"}))
    assert "'offset' must be a number" in result["error"]


def test_a_missing_message_is_reported_as_missing(mailbox):
    mailbox.response = b"* OK nothing"
    result = json.loads(tool.handle_read({"uid": "8", "folder": "INBOX"}))
    assert result["error"] == "Message 8 not found in INBOX."


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


def _read(mailbox, response, parts, **args):
    mailbox.response = response
    mailbox.parts = parts
    return json.loads(tool.handle_read({"uid": "8", "folder": "INBOX", **args}))["message"]


def test_a_cyrillic_attachment_name_from_yandex_is_decoded(mailbox):
    message = _read(mailbox, YANDEX_CYRILLIC_ATTACHMENT, {"1": b"See attached.\r\n"})
    [attachment] = message["attachments"]
    assert attachment["filename"] == "Отчёт за сентябрь.pdf"
    assert attachment["part_id"] == "2"
    assert abs(attachment["size"] - 300_000) < 4  # the planted file was 300 000 bytes


def test_an_encoded_word_attachment_name_from_yandex_is_decoded(mailbox):
    message = _read(mailbox, YANDEX_RFC2047_ATTACHMENT, {"1": b"body"})
    assert [a["filename"] for a in message["attachments"]] == ["Отчёт.pdf"]


def test_the_text_of_a_forwarded_message_is_part_of_the_body(mailbox):
    parts = {
        "1": b"Outer text.\r\n",
        "2.1": b"Forwarded text survives.\r\n",
        "2.2": b"<p>Forwarded <b>html</b></p>",
    }
    message = _read(mailbox, YANDEX_FORWARDED, parts)
    assert message["body"] == "Outer text.\nForwarded text survives."
    assert message["attachments"] == [
        {"part_id": "2", "filename": "(unnamed)", "content_type": "message/rfc822", "size": 579}
    ]
    assert not any("BODY.PEEK[2]<" in str(c) or "BODY.PEEK[2.2]" in str(c) for c in mailbox.calls)


def test_a_forwarded_single_part_message_is_numbered_n_1(mailbox):
    inner = b'("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 9 1 NIL NIL NIL NIL)'
    envelope = b"(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL)"
    rfc822 = b'("message" "rfc822" NIL NIL NIL "7bit" 99 ' + envelope + b" " + inner + b" 3)"
    response = b'1 (UID 8 BODYSTRUCTURE (("text" "plain" NIL NIL NIL "7bit" 5 1)' + rfc822
    message = _read(mailbox, response + b' "mixed"))', {"1": b"outer", "2.1": b"inner"})
    assert message["body"] == "outer\ninner"


def test_the_body_reads_as_it_did_before_paging(mailbox):
    """Plain text wins and every part is stripped; HTML is the fallback."""
    both = (
        b'1 (UID 8 BODYSTRUCTURE ((("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 20 1)'
        b'("text" "html" ("charset" "utf-8") NIL NIL "7bit" 20 1) "alternative")'
        b'("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 20 1) "mixed"))'
    )
    parts = {"1.1": b"\r\n  Plain part.  \r\n\r\n", "1.2": b"<p>HTML</p>", "2": b" \r\n "}
    message = _read(mailbox, both, parts)
    assert message["body"] == "Plain part."
    assert message["body_from_html"] is False


def test_a_message_without_text_has_an_empty_body_and_lists_its_part(mailbox):
    response = b'1 (UID 8 BODYSTRUCTURE ("image" "png" NIL NIL NIL "base64" 78 NIL NIL NIL NIL))'
    message = _read(mailbox, response, {})
    assert message["body"] == ""
    assert message["eof"] is True
    assert message["attachments"][0]["content_type"] == "image/png"
    assert message["attachments"][0]["size"] == 57


def test_an_unknown_charset_is_read_as_utf8(mailbox):
    response = (
        b'1 (UID 8 BODYSTRUCTURE ("text" "plain" ("charset" "nonexistent-charset") NIL NIL'
        b' "8bit" 10 1 NIL NIL NIL NIL))'
    )
    assert _read(mailbox, response, {"1": b"plain text\r\n"})["body"] == "plain text"


def test_base64_without_padding_still_reads(mailbox):
    response = (
        b'1 (UID 8 BODYSTRUCTURE ("text" "plain" ("charset" "utf-8") NIL NIL "base64" 15 1 NIL'
        b" NIL NIL NIL))"
    )
    assert _read(mailbox, response, {"1": b"SGVsbG8gd29ybGQ"})["body"] == "Hello world"


@pytest.mark.parametrize("part_id", ["", "0", "01", "1.0", "2.MIME", "1 2", "2\r\nX NOOP"])
def test_a_malformed_part_id_never_reaches_the_server(part_id):
    fake = MimeIMAP()
    with (
        YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake) as client,
        pytest.raises(MailError, match="Invalid MIME part_id"),
    ):
        list(client.iter_part("INBOX", "8", part_id))
    assert not any(call[0] in {"select", "uid"} for call in fake.calls)


def test_a_server_that_sends_more_than_it_was_asked_for_is_refused():
    class Oversharing(MimeIMAP):
        def uid(self, command, *args):
            if "BODY.PEEK[1]" in args[1]:
                body = b"x" * 65537
                return "OK", [(b"1 (UID 8 BODY[1]<0> {65537}", body), b")"]
            return super().uid(command, *args)

    with (
        YandexIMAPClient(
            "fixture", "fixture", connection_factory=lambda *_: Oversharing()
        ) as client,
        pytest.raises(MailError, match="chunk size limit"),
    ):
        list(client.iter_part("INBOX", "8", "1"))


def test_a_reply_without_bodystructure_is_an_error(mailbox):
    mailbox.response = b"1 (UID 8 BODYSTRUCTURE NIL)"
    result = json.loads(tool.handle_read({"uid": "8", "folder": "INBOX"}))
    assert result["error"] == "Cannot parse MIME structure: Server did not return BODYSTRUCTURE."


@pytest.mark.parametrize(
    "reply,error",
    [
        (b"1 (UID 9 FLAGS ())", "Message 8 not found in INBOX."),
        (b"1 (UID 8 BODY[2]<0> NIL)", "Server did not return the requested MIME part range."),
    ],
)
def test_a_part_the_server_does_not_return_is_an_error(reply, error):
    class Vanishing(MimeIMAP):
        def uid(self, command, *args):
            if "BODY.PEEK[1]" in args[1]:
                return "OK", [reply]
            return super().uid(command, *args)

    with (
        YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: Vanishing()) as client,
        pytest.raises(MailError) as caught,
    ):
        list(client.iter_part("INBOX", "8", "1"))
    assert str(caught.value) == error
