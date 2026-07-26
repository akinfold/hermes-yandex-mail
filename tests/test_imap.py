"""The IMAP client against a scripted connection: wire format and safety rules."""

from __future__ import annotations

import imaplib

import pytest

from hermes_yandex_mail.imap import (
    Folder,
    MailError,
    MessageSummary,
    SearchQuery,
    YandexIMAPClient,
    normalize_email,
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
    fake_imap.responses["SELECT"] = ("NO", [b"Mailbox does not exist"])
    with make_client(fake_imap) as client, pytest.raises(MailError, match="Mailbox does not exist"):
        client.search("Nope", SearchQuery())


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
        with pytest.raises(MailError, match="not in the allowed list"):
            client.check_folder("Trash")


def test_check_folder_defaults_to_inbox(fake_imap):
    with make_client(fake_imap) as client:
        assert client.check_folder(None) == "INBOX"
        assert client.check_folder("  ") == "INBOX"
        assert client.check_folder("Spam") == "Spam"


def test_find_special_folder(fake_imap):
    with make_client(fake_imap) as client:
        assert client.find_special_folder("trash") == "Trash"
        assert client.find_special_folder("nothing-like-this") is None


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


# -- flags, move, delete ----------------------------------------------------


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
        assert client.move("INBOX", ["8"], "Trash") == "move"
    assert fake_imap.command_names() == ["MOVE"]


def test_move_without_move_capability_copies_before_deleting(fake_imap):
    fake_imap.capabilities = ("IMAP4REV1", "UIDPLUS")
    with make_client(fake_imap) as client:
        assert client.move("INBOX", ["8"], "Archive") == "copy+expunge"
    # Order matters: the copy must exist before the original is marked deleted.
    assert fake_imap.command_names() == ["COPY", "STORE", "EXPUNGE"]


def test_move_without_uidplus_leaves_the_original_flagged_not_expunged(fake_imap):
    fake_imap.capabilities = ("IMAP4REV1",)
    with make_client(fake_imap) as client:
        assert client.move("INBOX", ["8"], "Archive") == "copy+flagged"
    assert "EXPUNGE" not in fake_imap.command_names()


def test_move_refuses_the_same_folder(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError, match="same"):
        client.move("INBOX", ["8"], "inbox")


def test_move_needs_uids(fake_imap):
    with make_client(fake_imap) as client, pytest.raises(MailError, match="No message UID"):
        client.move("INBOX", [], "Trash")


def test_a_failed_copy_never_deletes_the_original(fake_imap):
    fake_imap.capabilities = ("IMAP4REV1", "UIDPLUS")
    fake_imap.responses["COPY"] = ("NO", [b"Over quota"])
    with make_client(fake_imap) as client, pytest.raises(MailError, match="Over quota"):
        client.move("INBOX", ["8"], "Archive")
    assert fake_imap.command_names() == ["COPY"]


def test_delete_moves_to_trash_by_default(fake_imap):
    with make_client(fake_imap) as client:
        result = client.delete("INBOX", ["8"])
    assert result == {"deleted": True, "method": "trash", "trash_folder": "Trash"}
    assert "MOVE" in fake_imap.command_names()
    assert "EXPUNGE" not in fake_imap.command_names()


def test_delete_from_trash_expunges(fake_imap):
    with make_client(fake_imap) as client:
        result = client.delete("Trash", ["8"])
    assert result["method"] == "expunge"


def test_delete_permanently_expunges_only_those_uids(fake_imap):
    with make_client(fake_imap) as client:
        result = client.delete("INBOX", ["8", "9"], permanent=True)
    assert result == {"deleted": True, "method": "expunge", "folder": "INBOX"}
    assert fake_imap.command_names() == ["STORE", "EXPUNGE"]
    expunge = next(c for c in fake_imap.calls if c[0] == "uid" and c[1] == "EXPUNGE")
    assert expunge[2] == "8,9"


def test_permanent_delete_refuses_without_uidplus(fake_imap):
    fake_imap.capabilities = ("IMAP4REV1",)
    with make_client(fake_imap) as client, pytest.raises(MailError, match="UIDPLUS"):
        client.delete("INBOX", ["8"], permanent=True)
    assert "EXPUNGE" not in fake_imap.command_names()


def test_delete_without_a_trash_folder_explains_itself():
    fake = FakeIMAP(list_data=[b'(\\HasNoChildren) "|" INBOX'])
    with make_client(fake) as client, pytest.raises(MailError, match="No Trash folder"):
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


def test_folder_name_with_a_quote_is_escaped(fake_imap):
    with make_client(fake_imap) as client:
        client.search('Odd"name', SearchQuery())
    select = next(c for c in fake_imap.calls if c[0] == "select")
    assert select[1] == b'"Odd\\"name"'


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
