"""The SMTP wire: what actually goes down the socket, and when.

The client drives the transaction by hand instead of calling
``smtplib.send_message``. These tests hold that decision in place: they check
the bytes (a line that is a single dot must not end the message early) and they
check the one distinction the hand-driven version exists to make — whether any
byte of the payload had gone out when things went wrong.
"""

from __future__ import annotations

import smtplib
import ssl

import pytest

from hermes_yandex_mail import smtp as smtp_module
from hermes_yandex_mail.smtp import SendError, YandexSMTPClient

from .conftest import FakeSMTP


def client(fake: FakeSMTP) -> YandexSMTPClient:
    return YandexSMTPClient(
        login="me@yandex.ru", password="x", connection_factory=lambda host, port: fake
    )


PAYLOAD = b"Subject: x\r\n\r\nbody\r\n"


def test_the_transaction_runs_in_order(fake_smtp):
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert result.accepted == ("bob@example.org",)
    assert result.confirmed is True
    assert fake_smtp.command_names == [
        "ehlo",
        "login",
        "mail",
        "rcpt",
        "putcmd",
        "getreply",
        "send",
        "getreply",
        "quit",
    ]


def test_a_line_that_is_a_single_dot_cannot_end_the_message_early(fake_smtp):
    """Without dot-stuffing the recipient silently receives a truncated message."""
    with client(fake_smtp) as smtp:
        smtp.send("me@yandex.ru", ["bob@example.org"], b"Subject: x\r\n\r\na\r\n.\r\nb\r\n")
    assert b"\r\n..\r\n" in fake_smtp.written
    assert fake_smtp.written.endswith(b"\r\n.\r\n")


def test_the_payload_is_terminated_even_without_a_trailing_newline(fake_smtp):
    with client(fake_smtp) as smtp:
        smtp.send("me@yandex.ru", ["bob@example.org"], b"Subject: x\r\n\r\nbody")
    assert fake_smtp.written.endswith(b"body\r\n.\r\n")


def test_recipients_are_reported_in_the_spelling_that_went_on_the_wire(fake_smtp):
    """Two addresses can name one mailbox; neither may be collapsed or dropped."""
    fake_smtp.rcpt_codes = {"Me@ya.ru": (550, b"5.1.1 no")}
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["Bob@Example.ORG", "Me@ya.ru"], PAYLOAD)
    assert result.accepted == ("Bob@Example.ORG",)
    assert set(result.refused) == {"Me@ya.ru"}


def test_a_refusal_is_decoded_and_bounded(fake_smtp):
    fake_smtp.rcpt_codes = {"bob@example.org": (550, b"5.7.1 " + b"\xff long " * 100)}
    with client(fake_smtp) as smtp, pytest.raises(SendError):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)


def test_nothing_is_written_when_the_sender_is_rejected(fake_smtp):
    fake_smtp.mail_code = 550
    with client(fake_smtp) as smtp, pytest.raises(SendError, match="Nothing was sent"):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert fake_smtp.written == b""
    assert "rset" in fake_smtp.command_names


def test_a_refused_data_command_is_nothing_sent(fake_smtp):
    fake_smtp.fail_at = "data"
    with client(fake_smtp) as smtp, pytest.raises(SendError, match="Nothing was sent"):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert fake_smtp.written == b""


def test_a_drop_while_writing_is_never_reported_as_nothing_sent(fake_smtp):
    fake_smtp.fail_at = "write"
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert result.confirmed is False
    assert result.accepted == ("bob@example.org",)


def test_saying_goodbye_can_never_fail_the_call(fake_smtp):
    def broken_quit() -> None:
        raise smtplib.SMTPServerDisconnected("already gone")

    fake_smtp.quit = broken_quit  # type: ignore[method-assign]
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert result.confirmed is True


def test_a_refused_login_names_the_likely_cause(fake_smtp):
    fake_smtp.auth_error = True
    with client(fake_smtp) as smtp, pytest.raises(SendError, match="app password"):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)


def test_an_unreachable_server_says_nothing_was_sent():
    def refuse(host: str, port: int):
        raise OSError("connection refused")

    smtp = YandexSMTPClient(login="me@yandex.ru", password="x", connection_factory=refuse)
    with pytest.raises(SendError, match="Nothing was sent"):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)


def test_the_default_connection_verifies_the_certificate_and_times_out(monkeypatch):
    """The 0.2.1 lesson: an unauthenticated connection carries the app password."""
    captured: dict[str, object] = {}

    class Recorder:
        def __init__(self, host, port, context=None, timeout=None):
            captured.update(host=host, port=port, context=context, timeout=timeout)

    monkeypatch.setattr(smtplib, "SMTP_SSL", Recorder)
    smtp_module._default_connection("smtp.yandex.ru", 465)
    context = captured["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert captured["timeout"] == smtp_module.DEFAULT_TIMEOUT
