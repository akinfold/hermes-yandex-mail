"""The IMAP client against a scripted connection: wire format and safety rules."""

from __future__ import annotations

import imaplib

import pytest

from hermes_yandex_mail.imap import (
    _SPECIAL_NAMES,
    Folder,
    MailError,
    MessageSummary,
    SearchQuery,
    YandexIMAPClient,
    normalize_email,
    same_folder,
)

from .conftest import FakeIMAP, fetch_body_response, fetch_summary_response


def make_client(fake: FakeIMAP, **kwargs) -> YandexIMAPClient:
    return YandexIMAPClient(
        login="me@yandex.ru",
        password="secret",
        connection_factory=lambda host, port: fake,
        **kwargs,
    )


# -- helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Me@Ya.RU", "me@yandex.ru"),
        ("me@yandex.com", "me@yandex.ru"),
        ("me+tag@ya.ru", "me+tag@yandex.ru"),
        ("Name <ME@narod.ru>", "me@yandex.ru"),
        ("someone@example.org", "someone@example.org"),
        ("INBOX", "inbox"),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("INBOX", "inbox", True),
        ("Trash", "trash", True),
        ("Trash", "  trash  ", True),
        ("Trash", "Sent", False),
        ("удалённые", "УДАЛЁННЫЕ", True),
    ],
)
def test_same_folder(a, b, expected):
    assert same_folder(a, b) is expected


def test_message_summary_flag_properties():
    summary = MessageSummary(uid="1", folder="INBOX", flags=("\\Seen", "\\Flagged"))
    assert summary.seen and summary.flagged and not summary.answered


# -- connection -------------------------------------------------------------


def test_connect_is_cached_and_logs_out_once(fake_imap):
    with make_client(fake_imap) as client:
        assert client.connect() is client.connect()
    assert fake_imap.logged_out is True
    assert [c[0] for c in fake_imap.calls] == ["login", "logout"]


def test_authentication_failure_explains_the_two_usual_causes():
    fake = FakeIMAP(login_error="[AUTHENTICATIONFAILED] invalid credentials or IMAP is disabled")
    with pytest.raises(MailError) as excinfo, make_client(fake) as client:
        client.connect()
    message = str(excinfo.value)
    assert "app password" in message
    assert "mail.yandex.ru/#setup/client" in message


def test_unreachable_server_becomes_a_mail_error():
    def explode(host, port):
        raise OSError("nodename nor servname provided")

    client = YandexIMAPClient("me@yandex.ru", "x", connection_factory=explode)
    with pytest.raises(MailError, match="Cannot reach"):
        client.connect()


def test_close_swallows_a_failing_logout(fake_imap):
    fake_imap.logout_error = imaplib.IMAP4.error("connection reset")
    client = make_client(fake_imap)
    client.connect()
    client.close()  # must not raise
    assert client._conn is None


def test_close_without_a_connection_is_a_no_op(fake_imap):
    make_client(fake_imap).close()
    assert fake_imap.calls == []


def test_a_no_reply_becomes_a_mail_error(fake_imap):
    # INBOX resolves without touching the network, so this reaches the SELECT
    # this test actually means to exercise.
    fake_imap.responses["SELECT"] = ("NO", [b"Mailbox does not exist"])
    with make_client(fake_imap) as client, pytest.raises(MailError, match="Mailbox does not exist"):
        client.search("INBOX", SearchQuery())


def test_socket_error_mid_command_becomes_a_mail_error(fake_imap):
    fake_imap.responses["LIST"] = OSError("broken pipe")
    with make_client(fake_imap) as client, pytest.raises(MailError, match="broken pipe"):
        client.list_folders()


# -- folders ----------------------------------------------------------------


def test_list_folders_decodes_names_and_roles(fake_imap):
    with make_client(fake_imap) as client:
        folders = client.list_folders()
    by_name = {f.name: f for f in folders}
    assert by_name["INBOX"].special_use == "inbox"
    assert by_name["Sent"].special_use == "sent"
    assert by_name["Spam"].special_use == "junk"
    assert by_name["Trash"].special_use == "trash"
    assert by_name["Drafts"].special_use == "drafts"
    assert by_name["Outbox"].special_use == ""
    # The modified UTF-7 name is decoded for the agent.
    assert "Отправленные" in by_name


def test_list_folders_skips_unparseable_lines():
    fake = FakeIMAP(list_data=[b"garbage", None, b'(\\HasNoChildren) "|" INBOX'])
    with make_client(fake) as client:
        assert [f.name for f in client.list_folders()] == ["INBOX"]


def test_list_folders_with_counts(fake_imap):
    with make_client(fake_imap) as client:
        folders = client.list_folders(with_counts=True)
    assert folders[0].messages == 3
    assert folders[0].unseen == 2


def test_counts_are_optional_when_status_fails(fake_imap):
    fake_imap.responses["STATUS"] = ("NO", [b"unavailable"])
    with make_client(fake_imap) as client:
        folders = client.list_folders(with_counts=True)
    assert folders[0].messages is None


def test_allow_list_hides_other_folders(fake_imap):
    with make_client(fake_imap, allowed_folders=["INBOX", "Sent"]) as client:
        assert [f.name for f in client.list_folders()] == ["INBOX", "Sent"]
        assert client.default_folder() == "INBOX"
        assert client.check_folder("sent") == "sent"
        # check_folder is a cheap, network-free pre-check: it lets "Trash" through
        # because that word *could* resolve to an allowed folder once the server's
        # real list is known. _select (via search here) has the final say.
        with pytest.raises(MailError, match="not in the allowed list"):
            client.search("Trash", SearchQuery())


def test_check_folder_defaults_to_inbox(fake_imap):
    with make_client(fake_imap) as client:
        assert client.check_folder(None) == "INBOX"
        assert client.check_folder("  ") == "INBOX"
        assert client.check_folder("Spam") == "Spam"


def test_find_special_folder(fake_imap):
    with make_client(fake_imap) as client:
        assert client.find_special_folder("trash") == "Trash"
        assert client.find_special_folder("nothing-like-this") is None


# -- folder resolution --------------------------------------------------------
# IMAP mailbox names are case-sensitive except INBOX: Yandex rejects
# SELECT "spam" with [CLIENTBUG] No such folder while SELECT "Spam" works.


def test_resolve_folder_matches_case_insensitively(fake_imap):
    with make_client(fake_imap) as client:
        client.search("spam", SearchQuery())
    select = next(c for c in fake_imap.calls if c[0] == "select")
    assert select[1] == b'"Spam"'


def test_resolve_folder_matches_a_role_word(fake_imap):
    with make_client(fake_imap) as client:
        client.search("junk", SearchQuery())
    select = next(c for c in fake_imap.calls if c[0] == "select")
    assert select[1] == b'"Spam"'


def test_resolve_folder_matches_a_localized_synonym(fake_imap):
    # "удалённые" is not itself a folder name here, but _SPECIAL_NAMES maps it
    # to the "trash" role, and the account's Trash-flagged folder is "Trash".
    with make_client(fake_imap) as client:
        client.search("удалённые", SearchQuery())
    select = next(c for c in fake_imap.calls if c[0] == "select")
    assert select[1] == b'"Trash"'


def test_resolve_folder_reports_available_folders_when_nothing_matches(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError) as excinfo:
        client.search("does-not-exist", SearchQuery())
    message = str(excinfo.value)
    assert "No such folder" in message
    assert "Trash" in message
    assert "Spam" in message


def test_inbox_resolves_without_touching_the_network(fake_imap):
    with make_client(fake_imap) as client:
        client.search("inbox", SearchQuery())
    assert [c for c in fake_imap.calls if c[0] == "list"] == []


def test_folder_resolution_is_cached_per_connection(fake_imap):
    with make_client(fake_imap) as client:
        client.search("spam", SearchQuery())
        client.summary("Trash", "8")
    assert len([c for c in fake_imap.calls if c[0] == "list"]) == 1


def test_folder_cache_is_dropped_on_close(fake_imap):
    client = make_client(fake_imap)
    client.search("spam", SearchQuery())
    client.close()
    client.search("spam", SearchQuery())
    assert len([c for c in fake_imap.calls if c[0] == "list"]) == 2


def test_check_folder_lets_a_possibly_valid_synonym_through(fake_imap):
    with make_client(fake_imap, allowed_folders=["Trash"]) as client:
        # Cheap and network-free: "удалённые" is a recognized synonym that could
        # resolve to the allowed "Trash" folder, so it is not rejected here.
        assert client.check_folder("удалённые") == "удалённые"


def test_check_folder_rejects_an_unrecognizable_name_without_touching_the_network(fake_imap):
    with (
        make_client(fake_imap, allowed_folders=["INBOX"]) as client,
        pytest.raises(MailError, match="not in the allowed list"),
    ):
        client.check_folder("qwerty")
    assert fake_imap.calls == []


def test_allow_list_is_enforced_against_the_resolved_name(fake_imap):
    with (
        make_client(fake_imap, allowed_folders=["Sent"]) as client,
        pytest.raises(MailError, match="not in the allowed list"),
    ):
        client.search("trash", SearchQuery())


def test_resolve_folder_role_word_with_no_matching_folder_is_reported(fake_imap):
    # "архив" recognizes as the "archive" role, but no folder in LIST_LINES
    # plays that role — this must fall through to the same "no such folder"
    # error as any other unresolvable name, never a wrong guess.
    with make_client(fake_imap) as client, pytest.raises(MailError, match="No such folder"):
        client.search("архив", SearchQuery())


def test_no_such_folder_message_lists_only_allowed_folders(fake_imap):
    with (
        make_client(fake_imap, allowed_folders=["Sent"]) as client,
        pytest.raises(MailError) as excinfo,
    ):
        client.search("does-not-exist", SearchQuery())
    message = str(excinfo.value)
    assert "Sent" in message
    assert "Trash" not in message


def test_cached_folder_list_skips_non_bytes_entries(fake_imap):
    fake_imap.responses["LIST"] = ("OK", [b'(\\HasNoChildren \\Marked \\Trash) "|" Trash', None])
    with make_client(fake_imap) as client:
        client.search("trash", SearchQuery())
    select = next(c for c in fake_imap.calls if c[0] == "select")
    assert select[1] == b'"Trash"'


# One folder per role, so every alias in _SPECIAL_NAMES has something to
# resolve to — the shared LIST_LINES fixture has no Archive-flagged folder.
_ROLE_LIST_LINES = [
    b'(\\HasNoChildren \\Marked \\NoInferiors) "|" INBOX',
    b'(\\HasNoChildren \\Unmarked \\Sent) "|" Sent',
    b'(\\HasNoChildren \\Marked \\Trash) "|" Trash',
    b'(\\HasNoChildren \\Unmarked \\Junk) "|" Spam',
    b'(\\HasNoChildren \\Unmarked \\Drafts) "|" Drafts',
    b'(\\HasNoChildren \\Unmarked \\Archive) "|" Archive',
]

_SELECT_BYTES_BY_ROLE = {
    "inbox": b'"INBOX"',
    "sent": b'"Sent"',
    "trash": b'"Trash"',
    "junk": b'"Spam"',
    "drafts": b'"Drafts"',
    "archive": b'"Archive"',
}


@pytest.mark.parametrize("alias", sorted(_SPECIAL_NAMES))
def test_every_special_name_alias_resolves_to_its_role_folder(alias):
    # A model will say "spam", "Корзина", "входящие", "архив" etc. constantly;
    # every alias this package recognizes must actually resolve on the server.
    fake = FakeIMAP(list_data=list(_ROLE_LIST_LINES))
    role = _SPECIAL_NAMES[alias]
    with make_client(fake) as client:
        client.search(alias, SearchQuery())
    select = next(c for c in fake.calls if c[0] == "select")
    assert select[1] == _SELECT_BYTES_BY_ROLE[role]


# -- search and fetch -------------------------------------------------------


def test_search_sends_utf8_criteria_and_returns_newest_first(fake_imap):
    fake_imap.responses["FETCH"] = (
        "OK",
        fetch_summary_response(uid=5) + fetch_summary_response(uid=8),
    )
    query = SearchQuery(from_addr="иван", subject='say "hi"', since="2026-07-01", unread_only=True)
    with make_client(fake_imap) as client:
        found = client.search("INBOX", query, limit=10)

    search_call = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "SEARCH")
    args = list(search_call[2:])
    assert args[:2] == ["CHARSET", "UTF-8"]
    assert b"FROM" in args
    assert b'"\xd0\xb8\xd0\xb2\xd0\xb0\xd0\xbd"' in args  # utf-8 bytes, quoted
    assert b'"say \\"hi\\""' in args  # embedded quotes escaped
    assert b"1-Jul-2026" in args
    assert b"UNSEEN" in args
    assert [m.uid for m in found] == ["8", "5"]


def test_search_without_criteria_asks_for_all(fake_imap):
    with make_client(fake_imap) as client:
        client.search("INBOX", SearchQuery())
    search_call = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "SEARCH")
    assert b"ALL" in search_call


def test_search_selects_readonly_so_nothing_is_marked_read(fake_imap):
    with make_client(fake_imap) as client:
        client.search("INBOX", SearchQuery())
    select = next(c for c in fake_imap.calls if c[0] == "select")
    assert select[1] == b'"INBOX"'
    assert select[2] is True


def test_search_limit_keeps_the_newest_uids(fake_imap):
    fake_imap.responses["SEARCH"] = ("OK", [b"1 2 3 4 5"])
    with make_client(fake_imap) as client:
        client.search("INBOX", SearchQuery(), limit=2)
    fetch = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "FETCH")
    assert fetch[2] == "4,5"


def test_search_with_no_matches_skips_the_fetch(fake_imap):
    fake_imap.responses["SEARCH"] = ("OK", [b""])
    with make_client(fake_imap) as client:
        assert client.search("INBOX", SearchQuery()) == []
    assert "FETCH" not in fake_imap.command_names()


def test_summary_decodes_headers(fake_imap):
    with make_client(fake_imap) as client:
        summary = client.summary("INBOX", "8")
    assert summary is not None
    assert summary.subject == "Привет, мир"
    assert summary.from_ == ["Яндекс <noreply@id.yandex.ru>"]
    assert summary.cc == ["second@example.org"]
    assert summary.date.startswith("2026-07-26T01:40:20")
    assert summary.size == 30761
    assert summary.seen is True
    assert summary.message_id == "<abc123@yandex.ru>"


def test_summary_of_a_missing_message_is_none(fake_imap):
    fake_imap.responses["FETCH"] = ("OK", [None])
    with make_client(fake_imap) as client:
        assert client.summary("INBOX", "999") is None


def test_fetch_message_peeks_by_default(fake_imap):
    fake_imap.responses["FETCH"] = ("OK", fetch_body_response())
    with make_client(fake_imap) as client:
        raw, flags = client.fetch_message("INBOX", "8")
    assert b"Hello there." in raw
    assert flags == ("\\Seen",)
    fetch = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "FETCH")
    assert "BODY.PEEK[]" in fetch[3]
    assert next(c for c in fake_imap.calls if c[0] == "select")[2] is True


def test_fetch_message_can_mark_seen(fake_imap):
    fake_imap.responses["FETCH"] = ("OK", fetch_body_response())
    with make_client(fake_imap) as client:
        client.fetch_message("INBOX", "8", mark_seen=True)
    fetch = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "FETCH")
    assert "BODY[]" in fetch[3] and "PEEK" not in fetch[3]
    assert next(c for c in fake_imap.calls if c[0] == "select")[2] is False


def test_fetch_message_missing(fake_imap):
    fake_imap.responses["FETCH"] = ("OK", [b"1 (FLAGS (\\Seen))"])
    with make_client(fake_imap) as client, pytest.raises(MailError, match="not found"):
        client.fetch_message("INBOX", "404")


def test_select_is_not_repeated_for_the_same_folder(fake_imap):
    with make_client(fake_imap) as client:
        client.search("INBOX", SearchQuery())
        client.summary("INBOX", "8")
    assert len([c for c in fake_imap.calls if c[0] == "select"]) == 1


def test_store_flags_adds_and_removes(fake_imap):
    with make_client(fake_imap) as client:
        client.store_flags("INBOX", ["8", "9"], add=["\\Seen"], remove=["\\Flagged"])
    stores = [c for c in fake_imap.calls if c[0] == "uid" and c[1] == "STORE"]
    assert stores[0][2:] == ("8,9", "+FLAGS", "(\\Seen)")
    assert stores[1][2:] == ("8,9", "-FLAGS", "(\\Flagged)")


def test_store_flags_needs_uids(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError, match="No message UID"):
        client.store_flags("INBOX", [], add=["\\Seen"])


def test_move_uses_the_server_move_when_available(fake_imap):
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Trash")
    assert result.method == "move"
    # FETCH reads the Message-ID before the move; the second FETCH ranges over
    # the destination's UIDNEXT: to verify the new UID afterwards (Yandex does
    # not support SEARCH HEADER MESSAGE-ID).
    assert fake_imap.command_names() == ["FETCH", "MOVE", "FETCH"]


def test_move_without_move_capability_copies_before_deleting(fake_imap):
    fake_imap.capabilities = ("IMAP4REV1", "UIDPLUS")
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Drafts")
    assert result.method == "copy+expunge"
    # Order matters: the copy must exist before the original is marked deleted.
    assert fake_imap.command_names() == ["FETCH", "COPY", "STORE", "EXPUNGE", "FETCH"]


def test_move_without_uidplus_leaves_the_original_flagged_not_expunged(fake_imap):
    fake_imap.capabilities = ("IMAP4REV1",)
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Drafts")
    assert result.method == "copy+flagged"
    assert "EXPUNGE" not in fake_imap.command_names()


def test_move_refuses_the_same_folder(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError, match="same"):
        client.move("INBOX", ["8"], "inbox")
    # Resolving both names to "INBOX" needs no network, so this fails before
    # any command — not even a connection is opened.
    assert fake_imap.calls == []


def test_move_needs_uids(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError, match="No message UID"):
        client.move("INBOX", [], "Trash")


def test_a_failed_copy_never_deletes_the_original(fake_imap):
    fake_imap.capabilities = ("IMAP4REV1", "UIDPLUS")
    fake_imap.responses["COPY"] = ("NO", [b"Over quota"])
    with make_client(fake_imap) as client, pytest.raises(MailError, match="Over quota"):
        client.move("INBOX", ["8"], "Drafts")
    # The Message-ID read is harmless and happens regardless; the important
    # invariant — no STORE/EXPUNGE before a verified copy — still holds.
    assert fake_imap.command_names() == ["FETCH", "COPY"]


# Yandex does not support SEARCH HEADER MESSAGE-ID (verified live: it answers
# "[UNAVAILABLE] UID SEARCH Backend error"), so the destination UID lookup
# ranges over UIDNEXT:* instead and matches Message-ID headers exactly. The
# default fixture scripts a static FETCH reply, so the tests below that need
# the pre- and post-move FETCH calls to answer differently use a counter.

_OTHER_MESSAGE_HEADERS = (
    b"Subject: Unrelated\r\n"
    b"From: someone@example.org\r\n"
    b"To: hermesplugins@yandex.ru\r\n"
    b"Date: Sun, 26 Jul 2026 01:40:20 +0300\r\n"
    b"Message-ID: <different@yandex.ru>\r\n"
    b"\r\n"
)


def test_move_returns_the_verified_destination_uid(fake_imap):
    fake_imap.responses["UIDNEXT"] = ("OK", [b"13"])
    state = {"fetches": 0}

    def fetch_reply():
        state["fetches"] += 1
        # 1st FETCH: source uid 8's Message-ID, before the move.
        # 2nd FETCH: the UIDNEXT: range in the destination, after the move —
        # the same message landed there as uid 13.
        return "OK", fetch_summary_response(uid=8 if state["fetches"] == 1 else 13)

    fake_imap.responses["FETCH"] = fetch_reply
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Trash")
    assert result.method == "move"
    assert result.destination_uids == {"8": "13"}
    fetches = [c for c in fake_imap.calls if c[0] == "uid" and c[1] == "FETCH"]
    assert fetches[1][2] == "13:*"


def test_move_excludes_a_destination_message_with_a_different_message_id(fake_imap):
    # A message that merely happens to land in the UIDNEXT: range (e.g. new
    # mail arriving between the move and the lookup) must never be mistaken
    # for the one that was moved — only an exact Message-ID match counts.
    fake_imap.responses["UIDNEXT"] = ("OK", [b"13"])
    state = {"fetches": 0}

    def fetch_reply():
        state["fetches"] += 1
        if state["fetches"] == 1:
            return "OK", fetch_summary_response(uid=8)
        return "OK", fetch_summary_response(uid=13, headers=_OTHER_MESSAGE_HEADERS)

    fake_imap.responses["FETCH"] = fetch_reply
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Trash")
    assert result.destination_uids == {}


def test_move_skips_the_destination_lookup_without_uidnext(fake_imap):
    # No UIDNEXT captured (e.g. the server sent none) means the range is
    # unknown, so the lookup is skipped entirely rather than scanning blind.
    fake_imap.responses["UIDNEXT"] = ("OK", [])
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Trash")
    assert result.destination_uids == {}
    assert fake_imap.command_names().count("FETCH") == 1


def test_move_destination_uidnext_capture_failure_does_not_fail_the_move(fake_imap):
    # The 1st SELECT is the source (message-id read); the 2nd is the
    # destination EXAMINE used only to capture UIDNEXT — that one fails here,
    # so the lookup is skipped, but the move itself must still go through.
    state = {"selects": 0}

    def flaky_select():
        state["selects"] += 1
        if state["selects"] == 2:
            raise imaplib.IMAP4.error("cannot examine destination")
        return "OK", [b"1"]

    fake_imap.responses["SELECT"] = flaky_select
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Trash")
    assert result.method == "move"
    assert result.destination_uids == {}


def test_move_destination_fetch_failure_does_not_fail_the_move(fake_imap):
    state = {"fetches": 0}

    def fetch_reply():
        state["fetches"] += 1
        if state["fetches"] == 1:
            return "OK", fetch_summary_response(uid=8)
        raise OSError("broken pipe")

    fake_imap.responses["FETCH"] = fetch_reply
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Trash")
    assert result.method == "move"
    assert result.destination_uids == {}


def test_move_destination_examine_failure_does_not_fail_the_move(fake_imap):
    # The 1st SELECT is the source (message-id read), the 2nd is the
    # destination EXAMINE for UIDNEXT (must succeed so the lookup is even
    # attempted), the 3rd re-selects the source before the move itself, and
    # the 4th is the destination EXAMINE inside the post-move lookup — that
    # one fails here.
    state = {"selects": 0}

    def flaky_select():
        state["selects"] += 1
        if state["selects"] == 4:
            raise imaplib.IMAP4.error("cannot select destination")
        return "OK", [b"1"]

    fake_imap.responses["SELECT"] = flaky_select
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Trash")
    assert result.method == "move"
    assert result.destination_uids == {}


def test_move_skips_the_destination_lookup_without_a_message_id(fake_imap):
    fake_imap.responses["FETCH"] = ("OK", [(b"1 (UID 8 BODY[HEADER] {2}", b"\r\n"), b")"])
    with make_client(fake_imap) as client:
        result = client.move("INBOX", ["8"], "Trash")
    assert result.destination_uids == {}
    # No Message-ID was captured, so the destination is never even examined.
    assert fake_imap.command_names().count("FETCH") == 1


def test_delete_moves_to_trash_by_default(fake_imap):
    fake_imap.responses["UIDNEXT"] = ("OK", [b"13"])
    state = {"fetches": 0}

    def fetch_reply():
        # The destination really does renumber: the message read as uid 8 in
        # INBOX comes back as uid 13 in Trash. Scripting both reads apart is
        # what makes this test able to fail — a static reply would let the
        # source UID be echoed back as if it had been verified.
        state["fetches"] += 1
        return "OK", fetch_summary_response(uid=8 if state["fetches"] == 1 else 13)

    fake_imap.responses["FETCH"] = fetch_reply
    with make_client(fake_imap) as client:
        result = client.delete("INBOX", ["8"])
    assert result == {
        "deleted": True,
        # The real method, not a hard-coded "trash": had the server lacked
        # MOVE and left the original behind, the caller would see it here.
        "method": "move",
        "trash_folder": "Trash",
        "destination_uids": {"8": "13"},
    }
    assert "MOVE" in fake_imap.command_names()
    assert "EXPUNGE" not in fake_imap.command_names()


def test_delete_to_trash_still_reports_destination_uids_under_an_allow_list(fake_imap):
    """The soft-delete safety net must not lose the destination lookup.

    Trash is deliberately exempt from the folder allow-list so restricting
    YANDEX_MAIL_FOLDERS cannot turn every delete into an irreversible
    expunge. The lookups that run inside that move (the destination's
    UIDNEXT, and the post-move read) must therefore not re-run the
    allow-list check either — doing so silently stripped the mapping from
    every soft delete on a fenced deployment.
    """
    fake_imap.responses["UIDNEXT"] = ("OK", [b"13"])
    state = {"fetches": 0}

    def fetch_reply():
        state["fetches"] += 1
        return "OK", fetch_summary_response(uid=8 if state["fetches"] == 1 else 13)

    fake_imap.responses["FETCH"] = fetch_reply
    with make_client(fake_imap, allowed_folders=["INBOX"]) as client:
        result = client.delete("INBOX", ["8"])
    assert result["trash_folder"] == "Trash"
    assert result["destination_uids"] == {"8": "13"}


def test_delete_from_trash_refuses_to_silently_no_op(fake_imap):
    # This was the bug: deleting from Trash without permanent=True used to
    # return {"deleted": True, ...} while sending no command at all.
    with make_client(fake_imap) as client, pytest.raises(MailError, match="already in the Trash"):
        client.delete("Trash", ["8"])
    assert fake_imap.command_names() == []


def test_delete_from_trash_is_detected_regardless_of_case(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError, match="already in the Trash"):
        client.delete("trash", ["8"])
    assert fake_imap.command_names() == []


def test_delete_permanently_expunges_only_those_uids(fake_imap):
    with make_client(fake_imap) as client:
        result = client.delete("INBOX", ["8", "9"], permanent=True)
    assert result == {"deleted": True, "method": "expunge", "folder": "INBOX"}
    # The FETCH is the existence probe: a UID that no longer exists must be
    # refused before anything is flagged, not silently expunged into nothing.
    assert fake_imap.command_names() == ["FETCH", "STORE", "EXPUNGE"]
    expunge = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "EXPUNGE")
    assert expunge[2] == "8,9"


def test_permanent_delete_refuses_without_uidplus(fake_imap):
    fake_imap.capabilities = ("IMAP4REV1",)
    with make_client(fake_imap) as client, pytest.raises(MailError, match="UIDPLUS"):
        client.delete("INBOX", ["8"], permanent=True)
    assert "EXPUNGE" not in fake_imap.command_names()


def test_delete_without_a_trash_folder_explains_itself():
    fake = FakeIMAP(list_data=[b'(\\HasNoChildren) "|" INBOX'])
    with make_client(fake) as client, pytest.raises(MailError, match=r"no folder flagged"):
        client.delete("INBOX", ["8"])


def test_delete_needs_uids(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError, match="No message UID"):
        client.delete("INBOX", [])


# -- append (used by the live e2e suite) ------------------------------------


def test_append_returns_the_new_uid(fake_imap):
    with make_client(fake_imap) as client:
        assert client.append("INBOX", b"raw", flags=["\\Seen"]) == "42"
    call = next(c for c in fake_imap.calls if c[0] == "append")
    assert call[1] == b'"INBOX"'
    assert call[2] == "(\\Seen)"


def test_append_without_appenduid(fake_imap):
    fake_imap.responses["APPEND"] = ("OK", [b"APPEND completed"])
    with make_client(fake_imap) as client:
        assert client.append("Черновики", b"raw") is None
    call = next(c for c in fake_imap.calls if c[0] == "append")
    assert call[1] == b'"&BCcENQRABD0EPgQyBDgEOgQ4-"'
    assert call[2] is None


def test_folder_name_with_a_quote_is_escaped():
    # _quote_mailbox is exercised end to end elsewhere (e.g. test_append_*);
    # tested directly here since a folder name has to exist on the server to
    # reach SELECT at all now that folder resolution runs first.
    from hermes_yandex_mail.imap import _quote_mailbox

    assert _quote_mailbox('Odd"name') == b'"Odd\\"name"'


def test_bad_date_is_reported_clearly(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError, match="Invalid date"):
        client.search("INBOX", SearchQuery(since="yesterday"))


def test_iso_datetime_is_accepted_as_a_search_date(fake_imap):
    with make_client(fake_imap) as client:
        client.search("INBOX", SearchQuery(before="2026-12-31T23:59:59+03:00"))
    search = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "SEARCH")
    assert b"31-Dec-2026" in search


def test_folder_dataclass_defaults():
    assert Folder(name="INBOX").flags == ()


# -- defensive parsing ------------------------------------------------------


def test_a_folder_line_without_a_name_is_skipped():
    fake = FakeIMAP(list_data=[b'(\\HasNoChildren) "|" ""', b'(\\HasNoChildren) "|" INBOX'])
    with make_client(fake) as client:
        assert [f.name for f in client.list_folders()] == ["INBOX"]


def test_a_fetch_item_without_a_uid_is_skipped(fake_imap):
    fake_imap.responses["FETCH"] = ("OK", [(b"1 (RFC822.SIZE 10 BODY[HEADER] {2}", b"\r\n"), b")"])
    with make_client(fake_imap) as client:
        assert client.summary("INBOX", "8") is None


def test_a_summary_without_flags_reads_as_unread(fake_imap):
    fake_imap.responses["FETCH"] = (
        "OK",
        [(b"1 (UID 8 BODY[HEADER.FIELDS (SUBJECT)] {11}", b"Subject: x\n"), b")"],
    )
    with make_client(fake_imap) as client:
        summary = client.summary("INBOX", "8")
    assert summary is not None and summary.flags == () and summary.seen is False


def test_a_protocol_error_mid_command_becomes_a_mail_error(fake_imap):
    fake_imap.responses["SEARCH"] = imaplib.IMAP4.error("BAD invalid criteria")
    with make_client(fake_imap) as client, pytest.raises(MailError, match="invalid criteria"):
        client.search("INBOX", SearchQuery())


def test_a_date_only_prefix_is_accepted(fake_imap):
    with make_client(fake_imap) as client:
        client.search("INBOX", SearchQuery(since="2026-07-26 10:00 MSK"))
    search = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "SEARCH")
    assert b"26-Jul-2026" in search


def test_a_socket_failure_during_login_is_reported():
    class Rude(FakeIMAP):
        def login(self, user, password):
            raise OSError("connection reset by peer")

    with pytest.raises(MailError, match="Login failed"):
        make_client(Rude()).connect()
