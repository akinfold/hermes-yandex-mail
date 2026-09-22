"""Shared fixtures: a scriptable stand-in for an imaplib connection.

The unit suite never opens a socket. ``FakeIMAP`` mimics just enough of
:class:`imaplib.IMAP4` — the call signatures, the ``(typ, data)`` replies, and
the exception type — for the client to be exercised end to end, and records
every command so tests can assert on the wire traffic.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
from typing import Any

import pytest

DEFAULT_CAPABILITIES = ("IMAP4REV1", "UIDPLUS", "MOVE", "LITERAL+")

#: Marks a FETCH reply the test did not script, so the fake answers it from the
#: mailbox it models instead of echoing one canned response at every read.
FROM_MAILBOX = object()

LIST_LINES = [
    b'(\\HasNoChildren \\Unmarked \\Drafts) "|" Drafts',
    b'(\\HasNoChildren \\Marked \\NoInferiors) "|" INBOX',
    b'(\\HasNoChildren \\Unmarked) "|" Outbox',
    b'(\\HasNoChildren \\Unmarked \\Sent) "|" Sent',
    b'(\\HasNoChildren \\Unmarked \\Junk) "|" Spam',
    b'(\\HasNoChildren \\Marked \\Trash) "|" Trash',
    b'(\\HasNoChildren) "|" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"',
]

HEADERS = (
    b"Subject: =?utf-8?B?0J/RgNC40LLQtdGCLCDQvNC40YA=?=\r\n"
    b"From: =?utf-8?B?0K/QvdC00LXQutGB?= <noreply@id.yandex.ru>\r\n"
    b"To: hermesplugins@yandex.ru\r\n"
    b"Cc: second@example.org\r\n"
    b"Date: Sun, 26 Jul 2026 01:40:20 +0300\r\n"
    b"Message-ID: <abc123@yandex.ru>\r\n"
    b"\r\n"
)

PLAIN_MESSAGE = (
    b"Subject: Plain report\r\n"
    b"From: Sender <sender@example.org>\r\n"
    b"To: hermesplugins@yandex.ru\r\n"
    b"Date: Sun, 26 Jul 2026 01:40:20 +0300\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hello there.\r\nSecond line.\r\n"
)


def fetch_summary_response(
    uid: int = 8,
    headers: bytes = HEADERS,
    flags: bytes = rb"\Seen",
    size: int = 30761,
) -> list[Any]:
    """A FETCH reply shaped the way imaplib hands it back."""
    info = (
        b"1 (UID %d FLAGS (%s) RFC822.SIZE %d "
        b"BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] {%d}"
        % (uid, flags, size, len(headers))
    )
    return [(info, headers), b")"]


def fetch_body_response(
    uid: int = 8, raw: bytes = PLAIN_MESSAGE, flags: bytes = rb"\Seen"
) -> list[Any]:
    info = b"1 (UID %d FLAGS (%s) BODY[] {%d}" % (uid, flags, len(raw))
    return [(info, raw), b")"]


class FakeIMAP:
    """A scriptable imaplib.IMAP4 look-alike."""

    def __init__(
        self,
        capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES,
        login_error: str | None = None,
        list_data: list[Any] | None = None,
        existing_uids: set[str] | None = None,
    ) -> None:
        self.capabilities = capabilities
        #: UIDs the mailbox actually holds. The client probes existence with a
        #: bare ``UID FETCH <set> (UID)`` before it mutates anything (RFC 3501
        #: §6.4.8 makes a non-existent UID a silent no-op, not an error), so a
        #: fixture that answered that probe from a static script would let the
        #: very bug this check exists for slip through. Set it to make a UID
        #: vanish the way a message deleted in another client would.
        self.existing_uids = {"5", "6", "8", "9"} if existing_uids is None else existing_uids
        self.calls: list[tuple[Any, ...]] = []
        self.login_error = login_error
        self.logged_out = False
        self.logout_error: Exception | None = None
        #: command name -> (typ, data), or a callable taking the raw args.
        self.responses: dict[str, Any] = {
            "LIST": ("OK", list_data if list_data is not None else list(LIST_LINES)),
            "SELECT": ("OK", [b"1"]),
            "STATUS": ("OK", [b"INBOX (MESSAGES 3 UNSEEN 2)"]),
            "SEARCH": ("OK", [b"5 6 8"]),
            "FETCH": FROM_MAILBOX,
            "STORE": ("OK", [b"1 (UID 8 FLAGS (\\Seen))"]),
            "COPY": ("OK", [b"[COPYUID 1 8 12] Completed"]),
            "MOVE": ("OK", [b"[COPYUID 1 8 12] Completed"]),
            "EXPUNGE": ("OK", [b"1"]),
            "APPEND": ("OK", [b"[APPENDUID 1469770579 42] APPEND completed"]),
            # Read via conn.response(...) after a SELECT/EXAMINE, not a tagged
            # command reply — but scripted the same way as everything else here.
            "UIDNEXT": ("OK", [b"9"]),
        }

    # -- imaplib surface ----------------------------------------------------

    def login(self, user: str, password: str) -> tuple[str, list]:
        self.calls.append(("login", user))
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        return "OK", [b"LOGIN Completed."]

    def logout(self) -> tuple[str, list]:
        self.calls.append(("logout",))
        if self.logout_error:
            raise self.logout_error
        self.logged_out = True
        return "BYE", [b"LOGOUT completed"]

    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list]:
        self.calls.append(("list", directory, pattern))
        return self._reply("LIST")

    def select(self, mailbox: bytes | str = "INBOX", readonly: bool = False) -> tuple[str, list]:
        self.calls.append(("select", mailbox, readonly))
        return self._reply("SELECT")

    def status(self, mailbox: bytes | str, names: str) -> tuple[str, list]:
        self.calls.append(("status", mailbox, names))
        return self._reply("STATUS")

    def uid(self, command: str, *args: Any) -> tuple[str, list]:
        self.calls.append(("uid", command.upper(), *args))
        if command.upper() == "FETCH":
            if "BODYSTRUCTURE" in args[1]:
                return self.responses.get(
                    "STRUCTURE",
                    (
                        "OK",
                        [
                            (
                                f'1 (UID {args[0]} BODYSTRUCTURE ("TEXT" "PLAIN" '
                                '("CHARSET" "UTF-8") NIL NIL "8BIT" 1024 1))'
                            ).encode()
                        ],
                    ),
                )
            if re.search(r"BODY\.PEEK\[[1-9]", args[1]):
                return self._part_reply(args)
            reply = self._fetch_reply(args)
            if reply is not None:
                return reply
        return self._reply(command.upper())

    def _fetch_reply(self, args: tuple[Any, ...]) -> tuple[str, list] | None:
        """Answer a FETCH from the modelled mailbox, unless a test scripted one.

        Both the bare ``(UID)`` existence probe and the fuller summary read
        the client uses as a move's existence check must reflect
        ``existing_uids`` — answering either from a canned response would let
        a missing message look present, which is the whole bug the probe
        exists to catch.
        """
        if args[-1:] == ("(UID)",):
            return "OK", self._existence_reply(str(args[0]))
        if self.responses.get("FETCH") is not FROM_MAILBOX:
            return None
        uids = [u for u in str(args[0]).split(",") if u in self.existing_uids]
        data: list[Any] = []
        for uid in uids:
            data += fetch_summary_response(uid=int(uid))
        return "OK", data

    def _existence_reply(self, uid_set: str) -> list[Any]:
        """What the server answers a bare ``UID FETCH <set> (UID)``: one line
        per UID that exists, and simply nothing for the ones that do not."""
        requested = [u for u in uid_set.split(",") if u]
        return [
            b"%d (UID %s)" % (index + 1, uid.encode())
            for index, uid in enumerate(requested)
            if uid in self.existing_uids
        ]

    def _part_reply(self, args: tuple[Any, ...]) -> tuple[str, list]:
        reply = self._reply("FETCH")
        raw = next((item[1] for item in reply[1] if isinstance(item, tuple)), b"")
        body = raw.partition(b"\r\n\r\n")[2]
        match = re.search(r"BODY\.PEEK\[([\d.]+)\]<(\d+)\.(\d+)>", args[1])
        assert match is not None
        part_id, start_text, size_text = match.groups()
        start, size = int(start_text), int(size_text)
        part = body[start : start + size]
        return "OK", [
            (f"1 (UID {args[0]} BODY[{part_id}]<{start}> {{{len(part)}}}".encode(), part),
            b")",
        ]

    def append(self, mailbox: Any, flags: Any, date_time: Any, message: bytes) -> tuple[str, list]:
        self.calls.append(("append", mailbox, flags, date_time, message))
        return self._reply("APPEND")

    def response(self, name: str) -> tuple[str, list]:
        """Mimic imaplib.IMAP4.response: an untagged response captured after a
        command, e.g. ``UIDVALIDITY`` after SELECT. Unscripted names come back
        empty, the same as a server that did not send that response."""
        self.calls.append(("response", name))
        return self._reply(name)

    # -- scripting ----------------------------------------------------------

    def _reply(self, command: str) -> tuple[str, list]:
        reply = self.responses.get(command, ("OK", []))
        if isinstance(reply, list) and reply:
            # A queue of replies for successive calls to the same command —
            # e.g. a move's pre- and post-move FETCH need different answers.
            # Pop one per call; once only one is left, keep returning it, so
            # a test does not have to predict exactly how many calls happen.
            reply = reply[0] if len(reply) == 1 else reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply()
        return reply

    def command_names(self) -> list[str]:
        """The UID commands issued, in order — handy for ordering assertions."""
        return [call[1] for call in self.calls if call[0] == "uid"]


@pytest.fixture
def fake_imap() -> FakeIMAP:
    return FakeIMAP()


class FakeSMTPError(smtplib.SMTPServerDisconnected):
    """What smtplib raises when the socket dies mid-conversation."""


class FakeSMTP:
    """A scriptable stand-in for :class:`smtplib.SMTP_SSL`.

    Models the calls the client actually makes, one at a time, so the
    pre-DATA / post-DATA distinction is decided by *where the bytes got to*
    rather than by a phase label the test hands to the code. A fixture that
    let the code declare which phase it was in would make those two tests
    self-fulfilling: they would pass whichever branch the implementation took.
    """

    def __init__(
        self,
        *,
        fail_at: str | None = None,
        auth_error: bool = False,
        mail_code: int = 250,
        rcpt_codes: dict[str, tuple[int, bytes]] | None = None,
        data_code: int = 354,
        final_code: int = 250,
        final_text: bytes = b"2.0.0 Ok: queued",
    ) -> None:
        #: "connect" | "mail" | "rcpt" | "putcmd" | "data" | "write" | "final"
        self.fail_at = fail_at
        self.auth_error = auth_error
        self.mail_code = mail_code
        self.rcpt_codes = rcpt_codes or {}
        self.data_code = data_code
        #: The reply to end-of-data — the server's verdict on the message.
        #: Scriptable in both halves, because what the code must do with it
        #: depends on the class of the code *and* on what the text says.
        self.final_code = final_code
        self.final_text = final_text
        self.calls: list[tuple[str, Any]] = []
        self.written: bytes = b""
        self._next_reply: tuple[int, bytes] | None = None

    # -- connection ---------------------------------------------------------

    def ehlo_or_helo_if_needed(self) -> None:
        self.calls.append(("ehlo", None))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user))
        if self.auth_error:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Error: authentication failed")

    def quit(self) -> None:
        self.calls.append(("quit", None))

    def close(self) -> None:
        self.calls.append(("close", None))

    def rset(self) -> None:
        self.calls.append(("rset", None))

    # -- transaction --------------------------------------------------------

    def mail(self, sender: str, options: Any = ()) -> tuple[int, bytes]:
        self.calls.append(("mail", sender))
        if self.fail_at == "mail":
            raise FakeSMTPError("Server not connected")
        return self.mail_code, b"2.1.0 Ok"

    def rcpt(self, address: str, options: Any = ()) -> tuple[int, bytes]:
        self.calls.append(("rcpt", address))
        if self.fail_at == "rcpt":
            raise FakeSMTPError("Server not connected")
        return self.rcpt_codes.get(address, (250, b"2.1.5 Ok"))

    def putcmd(self, cmd: str, args: str = "") -> None:
        self.calls.append(("putcmd", cmd))
        if self.fail_at == "putcmd":
            raise FakeSMTPError("Server not connected")
        if self.fail_at == "data":
            self._next_reply = (451, b"4.3.0 Temporary failure")
        else:
            self._next_reply = (self.data_code, b"2.0.0 End data with <CR><LF>.<CR><LF>")

    def getreply(self) -> tuple[int, bytes]:
        reply, self._next_reply = self._next_reply, None
        if reply is not None:
            self.calls.append(("getreply", reply[0]))
            return reply
        # The closing reply, after the payload.
        if self.fail_at == "final":
            raise FakeSMTPError("Server not connected")
        self.calls.append(("getreply", self.final_code))
        return self.final_code, self.final_text

    def send(self, payload: bytes) -> None:
        self.calls.append(("send", len(payload)))
        if self.fail_at == "write":
            # Half the payload reached the socket: this is the case where the
            # message may or may not have been delivered.
            self.written = payload[: len(payload) // 2]
            raise FakeSMTPError("Server not connected")
        self.written = payload

    @property
    def command_names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def fake_smtp() -> FakeSMTP:
    return FakeSMTP()
