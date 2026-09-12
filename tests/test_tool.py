"""Tool handlers: the JSON they return, and the promise never to raise."""

from __future__ import annotations

import json

import pytest

from hermes_yandex_mail import tool
from hermes_yandex_mail.config import MissingCredentials
from hermes_yandex_mail.imap import YandexIMAPClient

from .conftest import FakeIMAP, fetch_body_response, fetch_summary_response


@pytest.fixture
def imap(monkeypatch) -> FakeIMAP:
    """Point every handler at a scripted connection."""
    fake = FakeIMAP()
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login="me@yandex.ru",
            password="secret",
            connection_factory=lambda host, port: fake,
        ),
    )
    return fake


def call(handler, **args) -> dict:
    return json.loads(handler(args))


# -- schemas ----------------------------------------------------------------

ALL_SCHEMAS = [
    tool.LIST_FOLDERS_SCHEMA,
    tool.SEARCH_SCHEMA,
    tool.READ_SCHEMA,
    tool.MARK_SCHEMA,
    tool.MOVE_SCHEMA,
    tool.DELETE_SCHEMA,
]


@pytest.mark.parametrize("schema", ALL_SCHEMAS)
def test_schema_shape(schema):
    assert schema["name"].startswith("yandex_mail_")
    assert schema["description"]
    params = schema["parameters"]
    assert params["type"] == "object"
    for name, prop in params["properties"].items():
        assert prop.get("description"), name
        # Strict function-calling validators reject union types.
        assert isinstance(prop["type"], str), name
    assert set(params["required"]) <= set(params["properties"])


# -- list_folders -----------------------------------------------------------


def test_list_folders(imap):
    result = call(tool.handle_list_folders)
    assert result["count"] == 7
    inbox = next(f for f in result["folders"] if f["name"] == "INBOX")
    assert inbox == {"name": "INBOX", "role": "inbox", "messages": 3, "unread": 2}


def test_list_folders_without_counts(imap):
    result = call(tool.handle_list_folders, include_counts=False)
    assert "messages" not in result["folders"][0]
    assert not [c for c in imap.calls if c[0] == "status"]


# -- search -----------------------------------------------------------------


def test_search_returns_summaries(imap):
    result = call(tool.handle_search, limit=1)
    assert result["folder"] == "INBOX"
    assert result["offset"] == 0
    assert result["total"] == 3  # the default SEARCH mock matches uids 5, 6, 8
    message = result["messages"][0]
    assert message["uid"] == "8"
    assert message["subject"] == "Привет, мир"
    assert message["from"] == ["Яндекс <noreply@id.yandex.ru>"]
    assert message["unread"] is False


def test_search_reports_the_servers_folder_spelling(imap):
    result = call(tool.handle_search, folder="spam")
    assert result["folder"] == "Spam"


def test_search_offset_pages_past_the_newest_matches(imap):
    imap.responses["SEARCH"] = ("OK", [b"1 2 3 4 5"])
    imap.responses["FETCH"] = ("OK", fetch_summary_response(uid=3))
    result = call(tool.handle_search, limit=2, offset=2)
    assert result["total"] == 5
    assert result["offset"] == 2
    fetch = next(c for c in imap.calls if c[0] == "uid" and c[1] == "FETCH")
    # Newest 2 (4, 5) skipped; the next 2 (2, 3) are the page actually fetched.
    assert fetch[2] == "2,3"


@pytest.mark.parametrize("offset", [3, 4, 5, 6, 10])
def test_search_offset_beyond_the_last_match_returns_no_messages(imap, offset):
    """The pre-fix slice was ``uids[: len(uids) - offset]``, which goes negative
    and re-serves already-seen mail for ``len < offset < 2 * len`` — offsets 4
    and 5 here. An offset of 10 alone would have missed the bug entirely, since
    the broken slice also yields nothing once ``offset >= 2 * len``.
    """
    imap.responses["SEARCH"] = ("OK", [b"1 2 3"])
    result = call(tool.handle_search, offset=offset)
    assert result["total"] == 3
    assert result["messages"] == []
    assert "FETCH" not in imap.command_names()


@pytest.mark.parametrize("offset", ["", None, 0, -3])
def test_search_offset_falls_back_to_zero(imap, offset):
    imap.responses["SEARCH"] = ("OK", [b"1 2 3 4 5"])
    imap.responses["FETCH"] = ("OK", fetch_summary_response(uid=5))
    call(tool.handle_search, limit=1, offset=offset)
    fetch = next(c for c in imap.calls if c[0] == "uid" and c[1] == "FETCH")
    assert fetch[2] == "5"


def test_search_rejects_a_non_numeric_offset(imap):
    assert "must be a number" in call(tool.handle_search, offset="many")["error"]


def test_search_passes_every_criterion(imap):
    call(
        tool.handle_search,
        folder="Spam",
        **{"from": "boss@example.org"},
        to="me@yandex.ru",
        subject="invoice",
        text="urgent",
        since="2026-01-01",
        before="2026-12-31",
        unread_only=True,
        flagged_only=True,
    )
    search = next(c for c in imap.calls if c[0] == "uid" and c[1] == "SEARCH")
    for expected in (
        b"FROM",
        b"TO",
        b"SUBJECT",
        b"TEXT",
        b"SINCE",
        b"BEFORE",
        b"UNSEEN",
        b"FLAGGED",
    ):
        assert expected in search


def test_search_limit_is_capped(imap):
    imap.responses["SEARCH"] = ("OK", [" ".join(str(i) for i in range(1, 500)).encode()])
    call(tool.handle_search, limit=5000)
    fetch = next(c for c in imap.calls if c[0] == "uid" and c[1] == "FETCH")
    assert len(fetch[2].split(",")) == 100


@pytest.mark.parametrize("limit", ["", None, 0, -3])
def test_search_limit_falls_back_to_the_default(imap, limit):
    imap.responses["SEARCH"] = ("OK", [" ".join(str(i) for i in range(1, 100)).encode()])
    call(tool.handle_search, limit=limit)
    fetch = next(c for c in imap.calls if c[0] == "uid" and c[1] == "FETCH")
    assert len(fetch[2].split(",")) == 25


def test_search_rejects_a_non_numeric_limit(imap):
    assert "must be a number" in call(tool.handle_search, limit="many")["error"]


def test_search_reports_a_bad_date(imap):
    assert "Invalid date" in call(tool.handle_search, since="last week")["error"]


# -- read -------------------------------------------------------------------


def test_read_message(imap):
    imap.responses["FETCH"] = ("OK", fetch_body_response())
    message = call(tool.handle_read, uid="8", folder="INBOX")["message"]
    assert message["body"].startswith("Hello there.")
    assert message["body_from_html"] is False
    assert message["truncated"] is False
    assert message["attachments"] == []
    assert message["flags"] == ["\\Seen"]


def test_read_message_truncates(imap):
    imap.responses["FETCH"] = ("OK", fetch_body_response())
    message = call(tool.handle_read, uid="8", folder="INBOX", max_chars=5)["message"]
    assert message["body"] == "Hello"
    assert message["truncated"] is True


def test_read_message_reports_the_servers_folder_spelling(imap):
    imap.responses["FETCH"] = ("OK", fetch_body_response())
    message = call(tool.handle_read, uid="8", folder="spam")["message"]
    assert message["folder"] == "Spam"


def test_read_message_falls_back_when_the_summary_is_gone(imap, monkeypatch):
    imap.responses["FETCH"] = ("OK", fetch_body_response())
    monkeypatch.setattr(YandexIMAPClient, "summary", lambda *a, **k: None)
    message = call(tool.handle_read, uid="8", folder="INBOX")["message"]
    assert message["uid"] == "8"
    assert message["folder"] == "INBOX"


def test_read_unread_and_flags_never_contradict_each_other(imap):
    # The summary is fetched AFTER the body, so with mark_read=true it must be
    # the one trusted: the body-fetch flags (queued first) deliberately still
    # lack \Seen — the state before this call marked the message read — while
    # the summary (queued second) reflects \Seen having been set. Overwriting
    # 'flags' from the body fetch, as the old code did, would report
    # unread=False (from the summary) together with flags lacking \Seen —
    # this asserts the two can never disagree.
    imap.responses["FETCH"] = [
        ("OK", fetch_body_response(flags=rb"\Flagged")),
        ("OK", fetch_summary_response(flags=rb"\Seen \Flagged")),
    ]
    message = call(tool.handle_read, uid="8", folder="INBOX", mark_read=True)["message"]
    assert message["unread"] is False
    assert "\\Seen" in message["flags"]


def test_read_requires_a_uid(imap):
    assert "'uid' is required" in call(tool.handle_read, folder="INBOX")["error"]


def test_read_rejects_a_non_numeric_uid(imap):
    assert "Not a message UID" in call(tool.handle_read, uid="latest", folder="INBOX")["error"]


# -- mark -------------------------------------------------------------------


def test_mark_read_and_flagged(imap):
    result = call(tool.handle_mark, uid="8, 9", folder="INBOX", read=True, flagged=False)
    assert result == {
        "marked": True,
        "folder": "INBOX",
        "uids": ["8", "9"],
        "added": ["\\Seen"],
        "removed": ["\\Flagged"],
    }


def test_mark_unread(imap):
    assert call(tool.handle_mark, uid="8", folder="INBOX", read=False)["removed"] == ["\\Seen"]


def test_mark_de_duplicates_repeated_uids(imap):
    result = call(tool.handle_mark, uid="8,8, 9", folder="INBOX", read=True)
    assert result["uids"] == ["8", "9"]
    store = next(c for c in imap.calls if c[0] == "uid" and c[1] == "STORE")
    assert store[2] == "8,9"


def test_a_uid_written_with_a_leading_zero_still_finds_its_message(imap):
    """The server answers "UID 8", so comparing "008" as text would report the
    caller's own existing message missing — and refuse the whole batch with it.
    """
    result = call(tool.handle_mark, uid="008", folder="INBOX", read=True)
    assert result["uids"] == ["8"]
    store = next(c for c in imap.calls if c[0] == "uid" and c[1] == "STORE")
    assert store[2] == "8"


def test_mark_needs_something_to_change(imap):
    assert "Nothing to change" in call(tool.handle_mark, uid="8", folder="INBOX")["error"]


# -- move -------------------------------------------------------------------


def test_move(imap):
    # A fresh destination UID (108) proves the move reports what the server
    # actually assigned, not a coincidental echo of the source UID: FakeIMAP's
    # FETCH queue answers the pre-move (source) and post-move (destination)
    # reads differently, so a bug that reported the source UID unchanged
    # would fail this test.
    imap.responses["FETCH"] = [
        ("OK", fetch_summary_response(uid=8)),
        ("OK", fetch_summary_response(uid=108)),
    ]
    result = call(tool.handle_move, uid="8", folder="INBOX", destination="Trash")
    assert result == {
        "moved": True,
        "original_removed": True,
        "uids": ["8"],
        "from": "INBOX",
        "to": "Trash",
        "method": "move",
        "destination_uids": {"8": "108"},
    }


def test_move_reports_the_servers_folder_spelling(imap):
    imap.responses["FETCH"] = [
        ("OK", fetch_summary_response(uid=8)),
        ("OK", fetch_summary_response(uid=108)),
    ]
    result = call(tool.handle_move, uid="8", folder="spam", destination="trash")
    assert result["from"] == "Spam"
    assert result["to"] == "Trash"


def test_move_does_not_report_success_when_the_original_survived(imap):
    """Without MOVE and without UIDPLUS the copy lands but the original stays,
    flagged \\Deleted. The headline "moved" is what a model reads first, so it
    must not say the move completed while a duplicate is left behind.
    """
    imap.capabilities = ("IMAP4REV1",)
    result = call(tool.handle_move, uid="8", folder="INBOX", destination="Trash")
    assert result["method"] == "copy+flagged"
    assert result["moved"] is False
    assert result["original_removed"] is False


def test_delete_does_not_report_success_when_the_original_survived(imap):
    imap.capabilities = ("IMAP4REV1",)
    result = call(tool.handle_delete, uid="8", folder="INBOX")
    assert result["method"] == "copy+flagged"
    assert result["deleted"] is False


def test_move_requires_a_destination(imap):
    assert "'destination' is required" in call(tool.handle_move, uid="8", folder="INBOX")["error"]


def test_move_respects_the_folder_allow_list(monkeypatch):
    fake = FakeIMAP()
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            login="me@yandex.ru",
            password="x",
            allowed_folders=["INBOX"],
            connection_factory=lambda host, port: fake,
        ),
    )
    result = call(tool.handle_move, uid="8", folder="INBOX", destination="Spam")
    assert "not in the allowed list" in result["error"]


# -- delete -----------------------------------------------------------------


def test_delete_goes_to_trash(imap):
    imap.responses["FETCH"] = [
        ("OK", fetch_summary_response(uid=8)),
        ("OK", fetch_summary_response(uid=108)),
    ]
    result = call(tool.handle_delete, uid="8", folder="INBOX")
    # The real method the move used, not a hard-coded "trash": if the server
    # had left the original behind, the caller would see "copy+flagged" here.
    assert result["method"] == "move"
    assert result["trash_folder"] == "Trash"
    assert result["destination_uids"] == {"8": "108"}
    assert result["uids"] == ["8"]


def test_delete_permanently(imap):
    assert call(tool.handle_delete, uid="8", folder="INBOX", permanent=True)["method"] == "expunge"


# -- the never-raise contract ----------------------------------------------

HANDLERS = [
    (tool.handle_list_folders, {}),
    (tool.handle_search, {}),
    (tool.handle_read, {"uid": "8", "folder": "INBOX"}),
    (tool.handle_mark, {"uid": "8", "folder": "INBOX", "read": True}),
    (tool.handle_move, {"uid": "8", "folder": "INBOX", "destination": "Trash"}),
    (tool.handle_delete, {"uid": "8", "folder": "INBOX"}),
]


@pytest.mark.parametrize(("handler", "args"), HANDLERS)
def test_missing_credentials_becomes_an_error_object(monkeypatch, handler, args):
    def explode():
        raise MissingCredentials("YANDEX_MAIL_LOGIN and YANDEX_MAIL_APP_PASSWORD must be set")

    monkeypatch.setattr(tool, "build_client", explode)
    assert "must be set" in json.loads(handler(args))["error"]


@pytest.mark.parametrize(("handler", "args"), HANDLERS)
def test_an_imap_failure_becomes_an_error_object(monkeypatch, handler, args):
    fake = FakeIMAP(login_error="[AUTHENTICATIONFAILED] nope")
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient("me@yandex.ru", "x", connection_factory=lambda h, p: fake),
    )
    assert "app password" in json.loads(handler(args))["error"]


@pytest.mark.parametrize(("handler", "args"), HANDLERS)
def test_an_unexpected_error_still_returns_json(monkeypatch, handler, args):
    def explode():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tool, "build_client", explode)
    payload = json.loads(handler(args))
    assert "kaboom" in payload["error"]
    assert "Unexpected error" in payload["error"]


@pytest.mark.parametrize(("handler", "args"), HANDLERS)
def test_handlers_accept_hermes_kwargs(imap, handler, args):
    imap.responses["FETCH"] = ("OK", fetch_body_response())
    assert json.loads(handler(args, agent=object(), session_id="x"))


def test_error_and_dump_keep_unicode_readable():
    assert "Спам" in tool._error("Папка Спам не найдена")
    assert "Спам" in tool._dump({"folder": "Спам"})


def test_summary_serialisation_is_stable(imap):
    imap.responses["FETCH"] = ("OK", fetch_summary_response(flags=rb"\Seen \Flagged \Answered"))
    message = call(tool.handle_search)["messages"][0]
    assert message["unread"] is False
    assert message["flagged"] is True
    assert message["answered"] is True


# -- multi-turn conversation scenarios ---------------------------------------
#
# An LLM drives these tools across several turns: "what's in Spam?" -> "read
# the second one" -> "mark it" -> "move it". Each step below feeds the PREVIOUS
# step's JSON result into the next call, the way the model actually would, and
# asserts on the commands FakeIMAP recorded — not just the returned text.

REQUIRED_FOLDER_SCHEMAS = [tool.READ_SCHEMA, tool.MARK_SCHEMA, tool.MOVE_SCHEMA, tool.DELETE_SCHEMA]


@pytest.mark.parametrize("schema", REQUIRED_FOLDER_SCHEMAS)
def test_folder_is_a_required_schema_argument(schema):
    assert "folder" in schema["parameters"]["required"]


def test_list_search_read_mark_move_chain_converges_on_the_servers_names(imap):
    imap.responses["SEARCH"] = ("OK", [b"5 6 8"])
    imap.responses["FETCH"] = ("OK", fetch_summary_response(uid=8))

    folders = call(tool.handle_list_folders)
    spam = next(f for f in folders["folders"] if f["role"] == "junk")
    assert spam["name"] == "Spam"

    searched = call(tool.handle_search, folder=spam["name"], limit=1)
    assert searched["folder"] == "Spam"
    message = searched["messages"][0]
    assert message["folder"] == "Spam"

    imap.responses["FETCH"] = ("OK", fetch_body_response(uid=int(message["uid"])))
    read = call(tool.handle_read, uid=message["uid"], folder=message["folder"], mark_read=True)
    assert read["message"]["folder"] == "Spam"
    assert read["message"]["uid"] == message["uid"]

    marked = call(
        tool.handle_mark,
        uid=read["message"]["uid"],
        folder=read["message"]["folder"],
        flagged=True,
    )
    assert marked["folder"] == "Spam"

    imap.responses["FETCH"] = [
        ("OK", fetch_summary_response(uid=int(marked["uids"][0]))),
        ("OK", fetch_summary_response(uid=int(marked["uids"][0]) + 100)),
    ]
    moved = call(
        tool.handle_move, uid=marked["uids"][0], folder=marked["folder"], destination="Trash"
    )
    assert moved["from"] == "Spam"
    assert moved["to"] == "Trash"
    # The mapping keeps source and destination paired, so the next turn can
    # look up where a specific message went instead of guessing by position.
    assert moved["destination_uids"] == {marked["uids"][0]: str(int(marked["uids"][0]) + 100)}

    selected = [c[1] for c in imap.calls if c[0] == "select"]
    assert b'"Spam"' in selected
    assert b'"Trash"' in selected


def test_chain_survives_the_model_writing_the_folder_in_the_wrong_case(imap):
    imap.responses["SEARCH"] = ("OK", [b"5 6 8"])
    imap.responses["FETCH"] = ("OK", fetch_summary_response(uid=8))

    searched = call(tool.handle_search, folder="spam", limit=1)
    assert searched["folder"] == "Spam"

    imap.responses["FETCH"] = ("OK", fetch_body_response(uid=8))
    read = call(tool.handle_read, uid="8", folder="spam")
    assert read["message"]["folder"] == "Spam"

    marked = call(tool.handle_mark, uid="8", folder="spam", read=True)
    assert marked["folder"] == "Spam"

    imap.responses["FETCH"] = [
        ("OK", fetch_summary_response(uid=8)),
        ("OK", fetch_summary_response(uid=77)),
    ]
    moved = call(tool.handle_move, uid="8", folder="spam", destination="trash")
    assert moved["from"] == "Spam"
    assert moved["to"] == "Trash"


@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (tool.handle_read, {"uid": "8"}),
        (tool.handle_mark, {"uid": "8", "read": True}),
        (tool.handle_move, {"uid": "8", "destination": "Trash"}),
        (tool.handle_delete, {"uid": "8"}),
    ],
)
def test_a_model_that_omits_folder_gets_a_clear_error_never_a_silent_inbox(imap, handler, args):
    result = call(handler, **args)
    assert "folder" in result["error"]
    # No request reached the server at all — proof this is not a silent
    # fallback to INBOX that merely happened to fail some other way.
    assert imap.calls == []


def test_delete_on_a_message_already_in_trash_sends_nothing_destructive(imap):
    result = call(tool.handle_delete, uid="8", folder="Trash")
    assert result["deleted"] is False
    assert result["reason"] == "already_in_trash"
    assert "irreversible" in result["note"]
    assert imap.command_names() == []
