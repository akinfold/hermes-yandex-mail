"""Composing an outgoing message: what is accepted, and what is refused outright.

Most of these are about addressing. The caller is a language model whose
context contains text an attacker wrote, so the interesting question is never
"does a valid address work" but "can anything the attacker writes end up on the
envelope".
"""

from __future__ import annotations

import pytest

from hermes_yandex_mail import compose
from hermes_yandex_mail.imap import ReplyAnchor

ACCOUNT = "me@yandex.ru"


def anchor(**overrides) -> ReplyAnchor:
    base = {
        "uid": "8",
        "folder": "INBOX",
        "message_id": "<original@bank.example>",
        "references": "",
        "subject": "Счёт на оплату",
        "from_": ("Bank <noreply@bank.example>",),
        "to": (ACCOUNT,),
        "reply_to": (),
    }
    base.update(overrides)
    return ReplyAnchor(**base)


# -- addresses --------------------------------------------------------------


def test_plain_addresses_survive_exactly_as_written():
    assert compose.parse_recipients(" Bob@Example.ORG , x@y.co.uk ") == [
        "Bob@Example.ORG",
        "x@y.co.uk",
    ]


def test_one_token_can_never_become_two_recipients():
    """The display-name smuggle: ``getaddresses`` reads this as TWO addresses.

    This is the exact string ``message.addresses()`` renders for a ``From:`` of
    ``"a@victim.org" <b@evil.org>`` — i.e. a value a model is invited to copy.
    """
    with pytest.raises(compose.ComposeError, match="plain address"):
        compose.parse_recipients("a@victim.org <b@evil.org>")


@pytest.mark.parametrize(
    "value",
    [
        "Bob <bob@example.org>",
        "evil@attacker.example(<allowed@corp.example>)",
        "a@b",
        "@example.org",
        "bob@",
        "a b@example.org",
        "bob@@example.org",
        "юзер@example.org",
        "bob@example.org;carol@evil.example",
    ],
)
def test_anything_that_is_not_one_bare_address_is_refused(value):
    with pytest.raises(compose.ComposeError):
        compose.parse_recipients(value)


def test_empty_tokens_between_commas_are_ignored():
    assert compose.parse_recipients("bob@example.org, ,") == ["bob@example.org"]


def test_a_line_break_in_a_recipient_cannot_add_a_header():
    with pytest.raises(compose.ComposeError, match="line break"):
        compose.parse_recipients("bob@example.org\r\nBcc: collect@evil.example")


def test_duplicate_recipients_collapse_but_keep_their_first_spelling():
    assert compose.parse_recipients("Bob@ya.ru, bob@yandex.ru") == ["Bob@ya.ru"]


def test_more_than_the_cap_is_refused_before_anything_is_built():
    many = ", ".join(f"user{n}@example.org" for n in range(compose.MAX_RECIPIENTS + 1))
    with pytest.raises(compose.ComposeError, match="at most"):
        compose.parse_recipients(many)


# -- the operator's fence ---------------------------------------------------


def test_no_fence_allows_anything():
    assert compose.recipient_allowed("anyone@example.org", None) is True


def test_a_fence_that_parsed_to_nothing_refuses_everything():
    """Set but unusable must fail closed: a typo cannot read as "send anywhere"."""
    assert compose.recipient_allowed("anyone@example.org", []) is False


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("owner@yandex.ru", True),
        ("OWNER@ya.ru", True),  # same mailbox, folded domain
        ("owner@yandex.ru.evil.example", False),
        ("owner@notyandex.ru", False),
        ("other@yandex.ru", False),
    ],
)
def test_a_full_address_entry_matches_one_mailbox(address, allowed):
    assert compose.recipient_allowed(address, ["owner@yandex.ru"]) is allowed


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("a@example.org", True),
        ("a@EXAMPLE.ORG", True),
        ("a@sub.example.org", False),
        ("a@notexample.org", False),
        ("a@example.org.evil.example", False),
    ],
)
def test_a_domain_entry_matches_that_domain_and_no_other(address, allowed):
    assert compose.recipient_allowed(address, ["@example.org"]) is allowed


# -- threading --------------------------------------------------------------


def test_the_reply_subject_does_not_stack():
    assert compose.reply_subject("Счёт") == "Re: Счёт"
    assert compose.reply_subject("Re: Счёт") == "Re: Счёт"
    assert compose.reply_subject("") == "Re:"


def test_threading_chains_references_and_ends_with_the_original():
    in_reply_to, references = compose.thread_headers(anchor(references="<one@x.org> <two@x.org>"))
    assert in_reply_to == "<original@bank.example>"
    assert references == "<one@x.org> <two@x.org> <original@bank.example>"


def test_a_message_without_a_usable_id_cannot_be_threaded_onto():
    for broken in ("", "not-an-id", "<no-at-sign>", "<a b@c.org>"):
        with pytest.raises(compose.ComposeError, match="Message-ID"):
            compose.thread_headers(anchor(message_id=broken))


def test_junk_in_references_is_dropped_and_the_chain_is_bounded():
    hostile = " ".join(f"<id{n}@x.org>" for n in range(50)) + " garbage <bad id@x.org>"
    _, references = compose.thread_headers(anchor(references=hostile))
    ids = references.split()
    assert len(ids) == compose.MAX_REFERENCES
    assert ids[-1] == "<original@bank.example>"
    assert all(token.startswith("<") for token in ids)


def test_a_line_break_in_the_originals_headers_never_reaches_the_reply():
    with pytest.raises(compose.ComposeError, match="line break"):
        compose.thread_headers(anchor(message_id="<a@b.org>\r\nBcc: x@evil.example"))


# -- provenance -------------------------------------------------------------


def test_only_the_sender_and_the_to_line_count_as_participants():
    """Cc and Reply-To are free for a sender to write, so they confer nothing.

    An attacker who puts their own address in ``Cc:`` would otherwise have it
    reported as an ordinary participant of the thread.
    """
    sources = compose.recipient_sources(
        ["noreply@bank.example", "archive@evil.example", "stranger@evil.example", ACCOUNT],
        anchor(reply_to=("archive@evil.example",)),
        ACCOUNT,
    )
    assert sources == {
        "noreply@bank.example": "from",
        "archive@evil.example": "reply_to_only",
        "stranger@evil.example": "new",
        ACCOUNT: "self",
    }


def test_copying_yourself_does_not_make_a_stranger_a_participant():
    sources = compose.recipient_sources(["stranger@evil.example"], anchor(), ACCOUNT)
    assert sources == {"stranger@evil.example": "new"}


def test_every_recipient_is_reported_even_when_nothing_is_unusual():
    sources = compose.recipient_sources(["noreply@bank.example"], anchor(), ACCOUNT)
    assert sources == {"noreply@bank.example": "from"}


# -- building ---------------------------------------------------------------


def build(**overrides):
    args = {
        "sender": ACCOUNT,
        "recipients": ["bob@example.org"],
        "subject": "Hello",
        "body": "Text.",
        "message_id": "<new@yandex.ru>",
    }
    args.update(overrides)
    return compose.build_message(**args)


def test_the_sender_is_the_account_and_the_headers_are_set():
    message = build()
    assert message["From"] == ACCOUNT
    assert message["To"] == "bob@example.org"
    assert message["Subject"] == "Hello"
    assert message["Message-ID"] == "<new@yandex.ru>"
    assert message["Date"]


def test_a_line_break_in_the_subject_is_refused_rather_than_sanitised():
    with pytest.raises(compose.ComposeError, match="line break"):
        build(subject="Invoice\r\nBcc: collect@evil.example")


def test_an_oversized_body_or_subject_is_refused():
    with pytest.raises(compose.ComposeError, match="body"):
        build(body="x" * (compose.MAX_BODY_CHARS + 1))
    with pytest.raises(compose.ComposeError, match="subject"):
        build(subject="x" * (compose.MAX_SUBJECT_CHARS + 1))


def test_cyrillic_is_carried_as_base64_rather_than_tripled_by_quoted_printable():
    message = build(body="Здравствуйте, вот отчёт.\n")
    assert message["Content-Transfer-Encoding"] == "base64"
    raw = compose.serialise(message)
    assert "Здравствуйте".encode() not in raw
    assert raw.isascii()


def test_a_long_ascii_line_is_encoded_so_it_survives_the_wire():
    message = build(body="x" * 1200)
    assert message["Content-Transfer-Encoding"] == "quoted-printable"


def test_the_serialised_bytes_use_crlf_as_both_smtp_and_imap_require():
    raw = compose.serialise(build(body="one\ntwo\n"))
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_a_reply_carries_both_threading_headers():
    message = build(in_reply_to="<a@b.org>", references="<a@b.org>")
    assert message["In-Reply-To"] == "<a@b.org>"
    assert message["References"] == "<a@b.org>"


def test_an_address_in_angle_brackets_is_accepted_without_its_wrapper():
    """The one bracketed form that is still exactly one address."""
    assert compose.parse_recipients("<bob@example.org>") == ["bob@example.org"]


def test_an_empty_address_is_refused_rather_than_passed_on():
    with pytest.raises(compose.ComposeError, match="empty"):
        compose.validate_address("   ", "the sending address")


def test_a_recipient_the_original_was_addressed_to_is_a_participant():
    """The ``to`` branch: a reply-all to someone the original also went to."""
    sources = compose.recipient_sources(
        ["noreply@bank.example", "colleague@example.org"],
        anchor(to=(ACCOUNT, "colleague@example.org")),
        ACCOUNT,
    )
    assert sources == {"noreply@bank.example": "from", "colleague@example.org": "to"}


def test_an_encoded_word_in_an_address_is_refused():
    """``=?`` is ordinary address text, and EmailMessage decodes it on the way out."""
    token = "=?utf-8?B?eEBldmlsLm9yZywgeQ==?=@example.org"
    with pytest.raises(compose.ComposeError, match="encoded word"):
        compose.parse_recipients(token)


def test_a_threading_header_is_never_re_encoded_however_long_the_id():
    """An ordinary Outlook Message-ID is 81 characters and used to come out as
    an encoded word no client threads on."""
    long_id = "<AM0PR05MB48941B2C3D4E5F60718293A4B5C6D7@AM0PR05MB4894.eurprd05.prod.outlook.com>"
    message = build(in_reply_to=long_id, references=f"<a@x.org> {long_id}")
    raw = compose.serialise(message).decode()
    assert f"In-Reply-To: {long_id}" in raw
    assert "=?utf-8?q?" not in raw
    assert str(message["In-Reply-To"]) == long_id, "the tool must report what it sent"
    assert all(len(line.encode()) <= 998 for line in raw.splitlines())


def test_a_long_references_chain_folds_between_ids_and_never_inside_one():
    ids = [f"<id{n}{'a' * 40}@long.example.com>" for n in range(6)]
    message = build(in_reply_to=ids[-1], references=" ".join(ids))
    raw = compose.serialise(message).decode()
    assert "=?utf-8?q?" not in raw
    for identifier in ids:
        assert identifier in raw, "an id was split across a fold"


def test_a_non_ascii_message_id_is_refused_rather_than_transformed():
    with pytest.raises(compose.ComposeError, match="Message-ID"):
        compose.thread_headers(anchor(message_id="<привет@evil.example>"))


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x0b", "\x0c", "\x1e"])
def test_every_line_separator_python_knows_is_refused(separator):
    """policy.default splits headers on str.splitlines(), which is wider than CR/LF."""
    with pytest.raises(compose.ComposeError, match="line break"):
        compose.header_safe(f"Invoice{separator}Bcc: collect@evil.example", "'subject'")


def test_the_rendered_to_header_must_name_exactly_the_envelope_recipients():
    """The last line of defence: a value that passes inspection and then changes."""
    message = build(recipients=["a@example.org", "b@example.org"])
    assert message["To"] == "a@example.org, b@example.org"
    with pytest.raises(compose.ComposeError, match="without changing them"):
        compose._require_header_round_trip(message, ["a@example.org"])


def test_the_round_trip_guard_actually_runs_on_every_message(monkeypatch):
    """It exists for the quirk nobody has found yet, so no input demonstrates it.

    What can be pinned is that it is still called: without this, deleting the
    call from build_message leaves the whole suite green, since every rendering
    difference known today is refused earlier.
    """
    called: list[tuple] = []

    def record(message, recipients):
        called.append((message, recipients))

    monkeypatch.setattr(compose, "_require_header_round_trip", record)
    build(recipients=["a@example.org"])
    assert called, "build_message no longer reads the rendered header back"
    assert called[0][1] == ["a@example.org"]
