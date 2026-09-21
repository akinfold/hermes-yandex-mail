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


# -- the verdict after end-of-data ------------------------------------------
#
# RFC 5321 §4.2: the reply to the closing dot is the server's answer about the
# whole message. A 250 is the server taking responsibility for it; a 4xx or 5xx
# is the server declining to, which means nothing was delivered to anybody. The
# two must not read alike, and neither may be confused with the third case —
# no reply at all — where what happened is genuinely unknown.


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (550, b"5.1.1 Mailbox unavailable"),
        (554, b"5.7.1 Message rejected under suspicion of SPAM"),
    ],
)
def test_a_permanent_rejection_after_the_body_is_not_a_delivery(fake_smtp, code, text):
    fake_smtp.final_code = code
    fake_smtp.final_text = text
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    message = str(excinfo.value)
    assert "Nothing was delivered" in message
    assert str(code) in message
    assert text.decode() in message
    assert "permanent" in message
    # The bytes really did go out; the point is that nothing came of them.
    assert fake_smtp.written != b""


def test_a_temporary_rejection_after_the_body_invites_a_later_retry(fake_smtp):
    """A 452 is the server saying 'not now', not 'not ever'."""
    fake_smtp.final_code = 452
    fake_smtp.final_text = b"4.2.2 Mailbox over quota"
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    message = str(excinfo.value)
    assert "452 4.2.2 Mailbox over quota" in message
    assert "temporary" in message
    assert "again later" in message
    assert "permanent" not in message


def test_a_421_after_the_body_is_a_refusal_not_an_unknown_outcome(fake_smtp):
    """The channel is closing, but the server still answered: it took nothing on."""
    fake_smtp.final_code = 421
    fake_smtp.final_text = b"4.7.0 Service not available, closing transmission channel"
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    message = str(excinfo.value)
    assert "421 4.7.0 Service not available, closing transmission channel" in message
    assert "temporary" in message


def test_a_multiline_rejection_is_quoted_back_on_one_line(fake_smtp):
    """smtplib joins a multiline reply with newlines; an error message is one line."""
    fake_smtp.final_code = 554
    fake_smtp.final_text = b"5.7.1 Message rejected.\nSee https://yandex.ru/support/mail\nfor why."
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    message = str(excinfo.value)
    assert "\n" not in message
    assert "5.7.1 Message rejected. See https://yandex.ru/support/mail for why." in message


def test_a_rejection_longer_than_the_bound_is_cut_to_it(fake_smtp):
    """Remote text is data from a machine we do not control: bounded, always.

    Asserted against a literal rather than against ``_clean``: computing the
    expectation with the code under test would accept any bound at all, since
    the first N characters stay a substring of an untruncated reply.
    """
    fake_smtp.final_code = 550
    fake_smtp.final_text = b"5.7.1 " + b"go away " * 100
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    message = str(excinfo.value)
    expected = ("5.7.1 " + "go away " * 100)[: smtp_module.MAX_SERVER_TEXT]
    assert expected in message
    assert "go away go" not in message.split(expected, 1)[1]


def test_server_text_exactly_at_the_bound_survives_intact(fake_smtp):
    """The boundary itself: one byte over is cut, the bound itself is not."""
    fits = b"5.7.1 " + b"a" * (smtp_module.MAX_SERVER_TEXT - 6)
    fake_smtp.final_code = 550
    fake_smtp.final_text = fits
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert fits.decode() in str(excinfo.value)

    fake_smtp.final_text = fits + b"a"
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert (fits + b"a").decode() not in str(excinfo.value)
    assert fits.decode() in str(excinfo.value)


def test_a_rejection_after_the_body_ends_the_way_the_send_path_ends(fake_smtp):
    """``tool._nothing_sent`` appends this sentence to anything lacking it.

    Spelling it here keeps it *after* the retry guidance instead of in front of
    it, and keeps exactly one copy of it. It is accurate: no recipient got
    anything, so trying again cannot produce a duplicate — which is the only
    thing that sentence is there to promise.
    """
    fake_smtp.final_code = 550
    fake_smtp.final_text = b"5.1.1 no"
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    message = str(excinfo.value)
    assert message.endswith("Nothing was sent.")
    assert message.count("Nothing was sent.") == 1


def test_a_rejection_after_the_body_undoes_the_recipients_already_accepted(fake_smtp):
    """One address was refused at RCPT, the rest accepted — and then the body
    was rejected. There is no partial delivery here to report."""
    fake_smtp.rcpt_codes = {"typo@exmaple.org": (550, b"5.1.1 no such user")}
    fake_smtp.final_code = 554
    fake_smtp.final_text = b"5.7.1 Message rejected"
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org", "typo@exmaple.org"], PAYLOAD)
    message = str(excinfo.value)
    assert "Nothing was delivered, to any recipient" in message
    # What the server already told us about a bad address is the reason a
    # retry of the same list would fail the same way, so it must survive.
    assert "typo@exmaple.org" in message
    assert "550" in message
    # ...while an address that WAS accepted must not appear, or the message
    # would read as a partial delivery.
    assert "bob@example.org" not in message


def test_a_rejection_after_the_body_sends_no_reset_and_still_quits(fake_smtp):
    """The end-of-data reply ends the transaction (RFC 5321 4.1.1.4).

    There is nothing to RSET, and ``close()`` still says goodbye. Pinned
    because the absence of a command is invisible to every other assertion.
    """
    fake_smtp.final_code = 550
    fake_smtp.final_text = b"5.1.1 no"
    with client(fake_smtp) as smtp, pytest.raises(SendError):
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert "rset" not in fake_smtp.command_names
    assert fake_smtp.command_names[-1] == "quit"


def test_no_reply_at_all_after_the_body_stays_unknown(fake_smtp):
    """The one case that is genuinely undecidable, and must stay that way."""
    fake_smtp.fail_at = "final"
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert result.confirmed is False
    assert result.accepted == ("bob@example.org",)


def test_a_reply_that_is_neither_acceptance_nor_refusal_stays_unknown(fake_smtp):
    """A 2xx that is not 250 may well mean the server took the message.

    Calling that a refusal would invite the retry that duplicates real mail, so
    the harmless direction is the unconfirmed one.
    """
    fake_smtp.final_code = 251
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert result.confirmed is False


def test_an_unparseable_reply_code_stays_unknown(fake_smtp):
    """``smtplib.SMTP.getreply`` answers -1 when the status line is not a code.

    That is not a refusal, and treating it as one would invite a retry of a
    message the server may have taken.
    """
    fake_smtp.final_code = -1
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert result.confirmed is False


def test_a_refusal_with_no_text_still_reads_as_a_sentence(fake_smtp):
    """Some servers answer a bare code. The message must not gain a stray gap."""
    fake_smtp.final_code = 550
    fake_smtp.final_text = b""
    with client(fake_smtp) as smtp, pytest.raises(SendError) as excinfo:
        smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    message = str(excinfo.value)
    assert "transmitted: 550. Nothing was delivered" in message


def test_a_timeout_waiting_for_the_verdict_stays_unknown(fake_smtp, monkeypatch):
    """A read timeout after the payload is the unknown case, not a refusal."""

    def timeout() -> tuple[int, bytes]:
        raise TimeoutError("timed out")

    with client(fake_smtp) as smtp:
        original = fake_smtp.getreply
        calls = {"n": 0}

        def getreply() -> tuple[int, bytes]:
            calls["n"] += 1
            return original() if calls["n"] == 1 else timeout()

        monkeypatch.setattr(fake_smtp, "getreply", getreply)
        result = smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert result.confirmed is False


def test_a_250_after_the_body_is_still_a_confirmed_delivery(fake_smtp):
    """The case nothing above may disturb."""
    with client(fake_smtp) as smtp:
        result = smtp.send("me@yandex.ru", ["bob@example.org"], PAYLOAD)
    assert result.confirmed is True
    assert result.accepted == ("bob@example.org",)
    assert result.refused == {}


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
