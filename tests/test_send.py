"""Sending: the gate in front of it, and the promise the result makes.

Two properties are load-bearing throughout. Nothing that can go wrong before
the payload is written may report anything other than "nothing was sent" — a
false failure is what makes an agent try again, and a retry here delivers a
second message to a real person. And nothing that goes wrong *after* it may
report an error at all.
"""

from __future__ import annotations

import base64
import json

import pytest

from hermes_yandex_mail import compose, config, tool
from hermes_yandex_mail.imap import YandexIMAPClient
from hermes_yandex_mail.smtp import YandexSMTPClient

from .conftest import FakeIMAP, FakeSMTP

ACCOUNT = "me@yandex.ru"
#: The Message-ID carried by the message ``FakeIMAP`` serves for every FETCH.
ANCHOR_ID = "<abc123@yandex.ru>"
REPLY_ARGS = {
    "reply_to_uid": "8",
    "reply_to_folder": "INBOX",
    "reply_to_message_id": ANCHOR_ID,
}


@pytest.fixture
def env(monkeypatch) -> dict[str, str]:
    values = {
        config.ENV_LOGIN: ACCOUNT,
        config.ENV_PASSWORD: "secret",
        config.ENV_ACTIONS: "all,send_message",
    }
    monkeypatch.setattr(config, "get_provider_env", lambda name: values.get(name, ""))
    return values


@pytest.fixture
def imap(monkeypatch) -> FakeIMAP:
    fake = FakeIMAP()
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login=ACCOUNT, password="secret", connection_factory=lambda host, port: fake
        ),
    )
    return fake


def wire_smtp(monkeypatch, fake: FakeSMTP) -> FakeSMTP:
    monkeypatch.setattr(
        tool,
        "build_smtp_client",
        lambda: YandexSMTPClient(
            login=ACCOUNT, password="secret", connection_factory=lambda host, port: fake
        ),
    )
    return fake


@pytest.fixture
def smtp(monkeypatch) -> FakeSMTP:
    return wire_smtp(monkeypatch, FakeSMTP())


def send(**args) -> dict:
    return json.loads(tool.handle_send(args))


def transmitted(fake: FakeSMTP) -> str:
    return fake.written.decode("utf-8", "replace")


# -- the happy path ---------------------------------------------------------


def test_a_plain_message_is_sent_and_filed(env, imap, smtp):
    result = send(to="bob@example.org", subject="Hello", body="Text.")
    assert "error" not in result, result
    assert result["sent"] is True
    assert result["delivery"] == "confirmed"
    assert result["recipients"] == ["bob@example.org"]
    assert result["from"] == ACCOUNT
    assert result["saved_to_sent"] is True
    assert result["sent_folder"] == "Sent"
    assert result["notes"] == []
    assert ("mail", ACCOUNT) in smtp.calls
    assert ("rcpt", "bob@example.org") in smtp.calls
    assert "Subject: Hello" in transmitted(smtp)


def test_the_copy_filed_in_sent_is_what_was_transmitted(env, imap, smtp):
    send(to="bob@example.org", subject="Hello", body="Text.")
    appended = [call for call in imap.calls if call[0] == "append"]
    assert appended, "no copy was filed"
    mailbox, flags, _when, raw = appended[0][1:]
    assert b"Sent" in mailbox
    assert flags == "(\\Seen)"
    # The bytes on the wire carry a trailing dot-stuffed terminator; the stored
    # copy is the message itself, which must otherwise be identical.
    assert raw in smtp.written


# -- the gate ---------------------------------------------------------------


def test_sending_is_refused_when_the_action_is_not_allowed(env, imap, smtp):
    env[config.ENV_ACTIONS] = "all"
    result = send(to="bob@example.org", subject="Hello", body="Text.")
    assert "not allowed" in result["error"]
    assert smtp.calls == []


def test_an_unsupported_argument_is_refused_rather_than_ignored(env, imap, smtp):
    """A model that believes it blind-copied someone must be told it did not."""
    for key in ("bcc", "Bcc", "from", "attachments", "html", "reply_to"):
        result = send(**{"to": "bob@example.org", "subject": "s", "body": "b", key: "x"})
        assert "does not support" in result["error"], key
        assert repr(key) in result["error"]
    assert smtp.calls == []


def test_a_recipient_outside_the_fence_never_reaches_a_socket(env, imap, smtp):
    env[config.ENV_SEND_TO] = "owner@yandex.ru"
    result = send(to="stranger@evil.example", subject="Hello", body="Text.")
    assert config.ENV_SEND_TO in result["error"]
    assert smtp.calls == []


def test_a_fence_that_parsed_to_nothing_refuses_every_recipient(env, imap, smtp):
    env[config.ENV_SEND_TO] = "not-an-address"
    result = send(to="bob@example.org", subject="Hello", body="Text.")
    assert config.ENV_SEND_TO in result["error"]
    assert smtp.calls == []


def test_a_smuggled_second_address_is_refused_before_connecting(env, imap, smtp):
    result = send(to="a@victim.org <b@evil.org>", subject="Hello", body="Text.")
    assert "plain address" in result["error"]
    assert smtp.calls == []


def test_a_login_that_is_not_an_address_refuses_instead_of_guessing(env, imap, smtp):
    env[config.ENV_LOGIN] = "akinfold"
    result = send(to="bob@example.org", subject="Hello", body="Text.")
    assert config.ENV_LOGIN in result["error"]
    assert smtp.calls == []


def test_missing_arguments_are_named(env, imap, smtp):
    assert "'to'" in send(subject="s", body="b")["error"]
    assert "'body'" in send(to="bob@example.org", subject="s")["error"]
    assert "'subject'" in send(to="bob@example.org", body="b")["error"]
    assert smtp.calls == []


# -- replies ----------------------------------------------------------------


def test_a_reply_threads_onto_the_message_it_names(env, imap, smtp):
    result = send(to="noreply@id.yandex.ru", body="Спасибо.", **REPLY_ARGS)
    assert "error" not in result, result
    assert result["in_reply_to"] == ANCHOR_ID
    assert result["subject"].startswith("Re: ")
    assert result["replied_to"]["uid"] == "8"
    assert f"In-Reply-To: {ANCHOR_ID}" in transmitted(smtp)


def test_replying_needs_the_uid_and_the_folder_together(env, imap, smtp):
    result = send(to="bob@example.org", body="b", reply_to_uid="8")
    assert "reply_to_folder" in result["error"]
    assert "Nothing was sent" in result["error"]
    assert smtp.calls == []


def test_a_missing_message_id_argument_is_named_after_the_message_is_read(env, imap, smtp):
    """Blank is allowed this far on purpose — see the no-Message-ID case below."""
    result = send(to="bob@example.org", body="b", reply_to_uid="8", reply_to_folder="INBOX")
    assert "reply_to_message_id" in result["error"]
    assert smtp.calls == []


def test_a_uid_that_now_holds_a_different_message_is_refused(env, imap, smtp):
    """A UID names a slot, not a message: the Message-ID is what proves identity."""
    result = send(
        to="bob@example.org",
        body="b",
        **{**REPLY_ARGS, "reply_to_message_id": "<the-one-i-read@example.org>"},
    )
    assert "not the one you read" in result["error"]
    assert smtp.calls == []


def test_the_original_is_flagged_answered(env, imap, smtp):
    send(to="noreply@id.yandex.ru", body="b", **REPLY_ARGS)
    stores = [call for call in imap.calls if call[0] == "uid" and call[1] == "STORE"]
    assert stores and "\\Answered" in stores[-1][-1]


def test_the_original_is_not_flagged_when_marking_is_not_allowed(env, imap, smtp):
    env[config.ENV_ACTIONS] = "read,send_message"
    result = send(to="noreply@id.yandex.ru", body="b", **REPLY_ARGS)
    assert result["sent"] is True
    assert result["marked_answered"] is False


def test_a_reply_reports_where_every_recipient_stands_in_the_thread(env, imap, smtp):
    result = send(to="noreply@id.yandex.ru,stranger@evil.example", body="b", **REPLY_ARGS)
    assert result["recipient_sources"] == {
        "noreply@id.yandex.ru": "from",
        "stranger@evil.example": "new",
    }
    assert any("stranger@evil.example" in note for note in result["notes"])


def test_the_reply_target_contributes_headers_but_never_a_recipient(env, imap, smtp):
    """The whole point: nothing in the original can add an envelope address."""
    send(to="bob@example.org", body="b", **REPLY_ARGS)
    rcpts = [value for name, value in smtp.calls if name == "rcpt"]
    assert rcpts == ["bob@example.org"]


# -- what the server does with it -------------------------------------------


def test_a_partly_refused_send_is_a_success_with_the_refusals_named(env, imap, monkeypatch):
    """Reporting this as an error would invite a retry, and a retry duplicates."""
    smtp = wire_smtp(
        monkeypatch,
        FakeSMTP(rcpt_codes={"typo@exmaple.org": (550, b"5.1.1 <typo> no such user")}),
    )
    result = send(to="bob@example.org,typo@exmaple.org", subject="s", body="b")
    assert result["sent"] is True
    assert result["recipients"] == ["bob@example.org"]
    assert result["refused"]["typo@exmaple.org"]["code"] == 550
    assert result["refused"]["typo@exmaple.org"]["status"] == "5.1.1"
    assert any("typo@exmaple.org" in note for note in result["notes"])
    assert smtp.written


def test_every_recipient_refused_means_nothing_was_sent(env, imap, monkeypatch):
    smtp = wire_smtp(monkeypatch, FakeSMTP(rcpt_codes={"bob@example.org": (550, b"5.1.1 no")}))
    result = send(to="bob@example.org", subject="s", body="b")
    assert "refused every recipient" in result["error"]
    assert smtp.written == b""


def test_a_drop_before_the_payload_is_reported_as_nothing_sent(env, imap, monkeypatch):
    for phase in ("mail", "rcpt", "data"):
        smtp = wire_smtp(monkeypatch, FakeSMTP(fail_at=phase))
        result = send(to="bob@example.org", subject="s", body="b")
        assert "Nothing was sent" in result["error"], phase
        assert smtp.written == b"", phase


def test_a_drop_after_the_payload_is_reported_as_sent_but_unconfirmed(env, imap, monkeypatch):
    """Whether it arrived is genuinely unknown, so it must not read as a failure."""
    smtp = wire_smtp(monkeypatch, FakeSMTP(fail_at="write"))
    result = send(to="bob@example.org", subject="s", body="b")
    assert result["sent"] is True
    assert result["delivery"] == "unconfirmed"
    assert any("second copy" in note for note in result["notes"])
    assert smtp.written != b""


def test_a_missing_final_confirmation_is_also_unconfirmed(env, imap, monkeypatch):
    wire_smtp(monkeypatch, FakeSMTP(fail_at="final"))
    result = send(to="bob@example.org", subject="s", body="b")
    assert result["sent"] is True
    assert result["delivery"] == "unconfirmed"


def test_a_rejected_final_code_does_not_become_nothing_sent(env, imap, monkeypatch):
    wire_smtp(monkeypatch, FakeSMTP(final_code=451))
    result = send(to="bob@example.org", subject="s", body="b")
    assert result["sent"] is True
    assert result["delivery"] == "unconfirmed"


def test_a_refused_login_says_so_and_sends_nothing(env, imap, monkeypatch):
    smtp = wire_smtp(monkeypatch, FakeSMTP(auth_error=True))
    result = send(to="bob@example.org", subject="s", body="b")
    assert "Nothing was sent" in result["error"]
    assert smtp.written == b""


# -- bookkeeping cannot undo a delivery -------------------------------------


def test_a_failure_filing_the_copy_never_turns_a_delivery_into_an_error(env, monkeypatch, smtp):
    broken = FakeIMAP()
    broken.responses["APPEND"] = OSError("disk full")
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login=ACCOUNT, password="secret", connection_factory=lambda host, port: broken
        ),
    )
    result = send(to="bob@example.org", subject="s", body="b")
    assert "error" not in result, result
    assert result["sent"] is True
    assert result["saved_to_sent"] is False
    assert any("follow-up" in note for note in result["notes"])


def test_an_account_without_a_sent_folder_still_sends(env, monkeypatch, smtp):
    bare = FakeIMAP(list_data=[b'(\\HasNoChildren \\Marked \\NoInferiors) "|" INBOX'])
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login=ACCOUNT, password="secret", connection_factory=lambda host, port: bare
        ),
    )
    result = send(to="bob@example.org", subject="s", body="b")
    assert result["sent"] is True
    assert result["saved_to_sent"] is False
    assert any("\\Sent" in note for note in result["notes"])


# -- the handler contract ---------------------------------------------------


def test_the_handler_never_raises_and_always_returns_json(env, imap, monkeypatch):
    def explode() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(tool, "build_smtp_client", explode)
    result = send(to="bob@example.org", subject="s", body="b")
    assert "Unexpected error sending message" in result["error"]


def test_missing_credentials_are_reported_not_raised(monkeypatch, imap, smtp):
    monkeypatch.setattr(config, "get_provider_env", lambda name: "")
    result = send(to="bob@example.org", subject="s", body="b")
    assert "error" in result


def test_the_result_is_serialisable_whatever_the_server_said(env, imap, monkeypatch):
    """smtplib hands back bytes; a payload carrying them would fail to encode."""
    wire_smtp(
        monkeypatch,
        FakeSMTP(
            rcpt_codes={"typo@exmaple.org": (550, b"5.1.1 \xff\xfe multi\r\nline " + b"x" * 400)}
        ),
    )
    result = send(to="bob@example.org,typo@exmaple.org", subject="s", body="b")
    assert json.dumps(result)
    text = result["refused"]["typo@exmaple.org"]["server_text"]
    assert "\n" not in text and len(text) <= 200


def test_the_send_schema_advertises_exactly_what_the_handler_accepts(env):
    properties = set(tool.SEND_SCHEMA["parameters"]["properties"])
    assert properties == set(tool._SEND_KEYS)
    assert tool.SEND_SCHEMA["parameters"]["required"] == ["to", "body"]


def test_a_cyrillic_body_and_subject_survive_the_wire(env, imap, smtp):
    result = send(to="bob@example.org", subject="Отчёт за июль", body="Здравствуйте!")
    assert result["sent"] is True
    assert smtp.written.isascii(), "non-ASCII must be encoded, not sent raw"
    assert compose.serialise(  # the stored copy decodes back to what was asked for
        compose.build_message(
            sender=ACCOUNT,
            recipients=["bob@example.org"],
            subject="Отчёт за июль",
            body="Здравствуйте!",
            message_id="<x@y.ru>",
        )
    )


def test_a_reply_to_uid_that_is_not_a_number_is_refused(env, imap, smtp):
    result = send(
        to="bob@example.org",
        body="b",
        reply_to_uid="the one about invoices",
        reply_to_folder="INBOX",
        reply_to_message_id=ANCHOR_ID,
    )
    assert "not a message UID" in result["error"]
    assert smtp.calls == []


def test_a_reply_to_a_message_that_is_gone_is_refused(env, monkeypatch, smtp):
    """Between reading a message and answering it, it can be moved or expunged."""
    empty = FakeIMAP(existing_uids=set())
    empty.responses["FETCH"] = ("OK", [])
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login=ACCOUNT, password="secret", connection_factory=lambda host, port: empty
        ),
    )
    result = send(to="bob@example.org", body="b", **REPLY_ARGS)
    assert "no longer in" in result["error"]
    assert smtp.calls == []


def test_a_result_that_cannot_be_assembled_still_reports_the_delivery(env, imap, smtp, monkeypatch):
    """The last line of defence: the message is gone, so this cannot be an error."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("payload broke")

    monkeypatch.setattr(tool, "_sent_payload", explode)
    result = send(to="bob@example.org", subject="s", body="b")
    assert "error" not in result, result
    assert result["sent"] is True
    assert result["recipients"] == ["bob@example.org"]
    assert any("could not be built" in note for note in result["notes"])


def test_enabling_sending_without_credentials_says_which_ones_are_missing(env, imap, smtp):
    """Switching the action on before configuring the account is a real order of events."""
    env.pop(config.ENV_LOGIN)
    env.pop(config.ENV_PASSWORD)
    result = send(to="bob@example.org", subject="s", body="b")
    assert config.ENV_LOGIN in result["error"]
    assert config.ENV_PASSWORD in result["error"]
    assert smtp.calls == []


# -- a hostile message being replied to -------------------------------------

#: An original written entirely by an attacker: an RFC 2047 Subject that decodes
#: to a header-injection attempt, a display name that is itself an address, a
#: Reply-To pointing elsewhere, and a References chain with junk in it. Built as
#: header bytes the server hands back, not as a handcrafted Python string, so the
#: decode path is part of what is under test.
HOSTILE_HEADERS = (
    "Subject: =?utf-8?B?"
    + base64.b64encode("Счёт\r\nBcc: collect@evil.example".encode()).decode()
    + "?=\r\n"
    'From: "me@victim.org" <attacker@evil.example>\r\n'
    "Reply-To: audit@evil.example\r\n"
    "To: me@yandex.ru\r\n"
    "Cc: archive@evil.example\r\n"
    "Message-ID: <hostile@evil.example>\r\n"
    "References: <a@x.org> junk-not-an-id\r\n"
    "\r\n"
).encode()


@pytest.fixture
def hostile(monkeypatch) -> FakeIMAP:
    fake = FakeIMAP()
    fake.responses["FETCH"] = (
        "OK",
        [
            (
                b"1 (UID 8 RFC822.SIZE 100 BODY[HEADER.FIELDS (X)] {%d}" % len(HOSTILE_HEADERS),
                HOSTILE_HEADERS,
            ),
            b")",
        ],
    )
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login=ACCOUNT, password="secret", connection_factory=lambda host, port: fake
        ),
    )
    return fake


HOSTILE_REPLY = {
    "reply_to_uid": "8",
    "reply_to_folder": "INBOX",
    "reply_to_message_id": "<hostile@evil.example>",
}


def test_an_encoded_subject_that_decodes_to_a_header_is_refused(env, hostile, smtp):
    result = send(to="bob@example.org", body="b", **HOSTILE_REPLY)
    assert "line break" in result["error"]
    assert smtp.calls == [], "nothing may reach the wire"
    assert b"Bcc" not in smtp.written


def test_the_attackers_reply_to_never_becomes_a_recipient(env, hostile, smtp):
    """Even with a subject of its own, the reply goes only where it was told."""
    result = send(to="bob@example.org", subject="Re: invoice", body="b", **HOSTILE_REPLY)
    assert "error" not in result, result
    assert [value for name, value in smtp.calls if name == "rcpt"] == ["bob@example.org"]
    assert result["recipient_sources"] == {"bob@example.org": "new"}
    assert any("bob@example.org" in note for note in result["notes"])
    assert b"audit@evil.example" not in smtp.written
    assert b"archive@evil.example" not in smtp.written


def test_junk_in_the_originals_references_does_not_reach_the_reply(env, hostile, smtp):
    send(to="bob@example.org", subject="Re: invoice", body="b", **HOSTILE_REPLY)
    wire = smtp.written.decode()
    assert "junk-not-an-id" not in wire
    assert "References: <a@x.org> <hostile@evil.example>" in wire


def test_a_message_with_no_id_of_its_own_says_so_instead_of_looping(env, monkeypatch, smtp):
    """The read result reports message_id "", so demanding one had no way out."""
    headers = b"Subject: No id here\r\nFrom: a@b.org\r\nTo: me@yandex.ru\r\n\r\n"
    fake = FakeIMAP()
    fake.responses["FETCH"] = (
        "OK",
        [(b"1 (UID 8 RFC822.SIZE 10 BODY[HEADER.FIELDS (X)] {%d}" % len(headers), headers), b")"],
    )
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login=ACCOUNT, password="secret", connection_factory=lambda host, port: fake
        ),
    )
    read = json.loads(tool.handle_read({"uid": "8", "folder": "INBOX"}))
    assert read["message"]["message_id"] == ""

    result = send(to="a@b.org", body="b", reply_to_uid="8", reply_to_folder="INBOX")
    assert "carries no Message-ID" in result["error"]
    assert "without the reply_to_* arguments" in result["error"]
    assert smtp.calls == []


def test_threading_a_reply_needs_the_grant_that_reading_needs(env, imap, smtp):
    """The send tool must not become a way around a withheld read_message."""
    env[config.ENV_ACTIONS] = "send_message"
    result = send(to="bob@example.org", body="b", **REPLY_ARGS)
    assert "'read_message' is not allowed" in result["error"]
    assert smtp.calls == []
    assert not [call for call in imap.calls if call[0] == "uid"]


def test_an_over_long_inherited_subject_blames_the_original_not_the_caller(env, monkeypatch, smtp):
    headers = (
        ("Subject: " + "оченьдлинная " * 60 + "\r\n")
        + "From: a@b.org\r\nTo: me@yandex.ru\r\nMessage-ID: <long@b.org>\r\n\r\n"
    ).encode()
    fake = FakeIMAP()
    fake.responses["FETCH"] = (
        "OK",
        [(b"1 (UID 8 RFC822.SIZE 10 BODY[HEADER.FIELDS (X)] {%d}" % len(headers), headers), b")"],
    )
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login=ACCOUNT, password="secret", connection_factory=lambda host, port: fake
        ),
    )
    args = {"reply_to_uid": "8", "reply_to_folder": "INBOX", "reply_to_message_id": "<long@b.org>"}
    result = send(to="a@b.org", body="b", **args)
    assert "message being replied to" in result["error"]
    assert "'subject'" in result["error"]
    assert smtp.calls == []
    # Supplying one explicitly is the way through, and the error says so.
    assert "error" not in send(to="a@b.org", body="b", subject="Re: short", **args)


def test_every_refusal_from_this_path_says_nothing_was_sent(env, imap, smtp):
    """The sentence is what tells an agent a retry is safe, so it cannot be optional.

    The interesting cases are the ones whose text comes from somewhere else —
    the config layer, the IMAP client, the standard library — because those
    modules know nothing about sending and end their sentences their own way.
    """
    written_here = [
        send(subject="s", body="b"),
        send(to="bob@example.org", body="b"),
        send(to="not-an-address", subject="s", body="b"),
        send(to="bob@example.org", subject="s", body="b", bcc="x@y.org"),
        send(to="bob@example.org", subject="s", body="b", reply_to_uid="nope"),
    ]
    env[config.ENV_LOGIN] = ""
    env[config.ENV_PASSWORD] = ""
    from_elsewhere = send(to="bob@example.org", subject="s", body="b")
    assert "app-passwords" in from_elsewhere["error"], "expected the config layer's own wording"

    for result in [*written_here, from_elsewhere]:
        assert result["error"].endswith("Nothing was sent."), result
    assert smtp.calls == []
