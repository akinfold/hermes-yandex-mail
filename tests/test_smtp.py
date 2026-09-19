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


def test_a_socket_error_during_the_envelope_is_nothing_sent(fake_smtp):
    """A timeout surfaces as OSError, not as an smtplib exception."""

    def timeout(_address, options=()):
        raise TimeoutError("timed out")

    fake_smtp.rcpt = timeout  # type: ignore[method-assign]
    with client(fake_smtp) as smtp, pytest.raises(SendError, match="dropped before"):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert fake_smtp.written == b""


def test_one_connection_serves_the_whole_submission(fake_smtp):
    smtp = client(fake_smtp)
    assert smtp.connect() is smtp.connect()
    assert fake_smtp.command_names.count("login") == 1
    smtp.close()


def test_a_drop_between_the_last_recipient_and_data_is_nothing_sent(fake_smtp):
    """The exact boundary the 'written' flag exists to sit on."""
    fake_smtp.fail_at = "putcmd"
    with client(fake_smtp) as smtp, pytest.raises(SendError, match="dropped before"):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert fake_smtp.written == b""


def test_refusals_survive_an_unconfirmed_delivery(fake_smtp):
    """The two are independent, and an agent told 'unconfirmed' must not retry —
    so a refusal dropped here is one no later turn can ever surface."""
    fake_smtp.rcpt_codes = {"typo@exmaple.org": (550, b"5.1.1 no such user")}
    fake_smtp.fail_at = "write"
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["bob@example.org", "typo@exmaple.org"], PAYLOAD)
    assert result.confirmed is False
    assert result.accepted == ("bob@example.org",)
    assert result.refused["typo@exmaple.org"]["code"] == 550


def test_a_forwarding_mailbox_answering_251_is_accepted(fake_smtp):
    """RFC 5321 §3.3: 251 is 'will forward', not a refusal."""
    fake_smtp.rcpt_codes = {"alias@example.org": (251, b"2.1.5 User not local; will forward")}
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["alias@example.org"], PAYLOAD)
    assert result.accepted == ("alias@example.org",)
    assert result.refused == {}


def test_a_refused_login_does_not_leave_the_tls_connection_open(fake_smtp):
    """A wrong app password is the likeliest first-run failure of all."""
    fake_smtp.auth_error = True
    with client(fake_smtp) as smtp, pytest.raises(SendError):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert "close" in fake_smtp.command_names


def test_an_unreachable_server_keeps_the_reason_it_gave():
    """Certificate failure, DNS failure and a refused port must not read alike."""

    def refuse(host: str, port: int):
        raise ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate")

    smtp = YandexSMTPClient(login="me@yandex.ru", password="x", connection_factory=refuse)
    with pytest.raises(SendError, match="certificate verify failed"):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)


def test_the_connection_timeout_is_thirty_seconds():
    """Pinned by value: 'equals DEFAULT_TIMEOUT' passes for any number at all."""
    assert smtp_module.DEFAULT_TIMEOUT == 30
