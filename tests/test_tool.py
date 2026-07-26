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
    message = result["messages"][0]
    assert message["uid"] == "8"
    assert message["subject"] == "Привет, мир"
    assert message["from"] == ["Яндекс <noreply@id.yandex.ru>"]
    assert message["unread"] is False


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
    message = call(tool.handle_read, uid="8")["message"]
    assert message["body"].startswith("Hello there.")
    assert message["body_from_html"] is False
    assert message["truncated"] is False
    assert message["attachments"] == []
    assert message["flags"] == ["\\Seen"]


def test_read_message_truncates(imap):
    imap.responses["FETCH"] = ("OK", fetch_body_response())
    message = call(tool.handle_read, uid="8", max_chars=5)["message"]
    assert message["body"] == "Hello"
    assert message["truncated"] is True


def test_read_message_falls_back_when_the_summary_is_gone(imap, monkeypatch):
    imap.responses["FETCH"] = ("OK", fetch_body_response())
    monkeypatch.setattr(YandexIMAPClient, "summary", lambda *a, **k: None)
    message = call(tool.handle_read, uid="8")["message"]
    assert message["uid"] == "8"
    assert message["folder"] == "INBOX"


def test_read_requires_a_uid(imap):
    assert "'uid' is required" in call(tool.handle_read)["error"]


def test_read_rejects_a_non_numeric_uid(imap):
    assert "Not a message UID" in call(tool.handle_read, uid="latest")["error"]


# -- mark -------------------------------------------------------------------


def test_mark_read_and_flagged(imap):
    result = call(tool.handle_mark, uid="8, 9", read=True, flagged=False)
    assert result == {
        "marked": True,
        "folder": "INBOX",
        "uids": ["8", "9"],
        "added": ["\\Seen"],
        "removed": ["\\Flagged"],
    }


def test_mark_unread(imap):
    assert call(tool.handle_mark, uid="8", read=False)["removed"] == ["\\Seen"]


def test_mark_needs_something_to_change(imap):
    assert "Nothing to change" in call(tool.handle_mark, uid="8")["error"]


# -- move -------------------------------------------------------------------


def test_move(imap):
    result = call(tool.handle_move, uid="8", destination="Trash")
    assert result == {
        "moved": True,
        "uids": ["8"],
        "from": "INBOX",
        "to": "Trash",
        "method": "move",
    }


def test_move_requires_a_destination(imap):
    assert "'destination' is required" in call(tool.handle_move, uid="8")["error"]


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
    assert "not in the allowed list" in call(tool.handle_move, uid="8", destination="Spam")["error"]


# -- delete -----------------------------------------------------------------


def test_delete_goes_to_trash(imap):
    result = call(tool.handle_delete, uid="8")
    assert result["method"] == "trash"
    assert result["uids"] == ["8"]


def test_delete_permanently(imap):
    assert call(tool.handle_delete, uid="8", permanent=True)["method"] == "expunge"


# -- the never-raise contract ----------------------------------------------

HANDLERS = [
    (tool.handle_list_folders, {}),
    (tool.handle_search, {}),
    (tool.handle_read, {"uid": "8"}),
    (tool.handle_mark, {"uid": "8", "read": True}),
    (tool.handle_move, {"uid": "8", "destination": "Trash"}),
    (tool.handle_delete, {"uid": "8"}),
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
