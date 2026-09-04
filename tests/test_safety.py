"""Regression checks for tool permission and input/output boundaries."""

import json

import pytest

from hermes_yandex_mail import config, register, tool
from hermes_yandex_mail.imap import MailError, SearchQuery, YandexIMAPClient

from .conftest import FakeIMAP, fetch_body_response


@pytest.mark.parametrize("field", ["from_addr", "to", "subject", "text"])
@pytest.mark.parametrize("control", ["\r", "\n", "\x00"])
def test_search_rejects_control_characters_at_the_wire_sink(field, control):
    fake = FakeIMAP()
    client = YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake)
    with pytest.raises(MailError, match="control"):
        client.search("INBOX", SearchQuery(**{field: f"left{control}right"}))
    assert "SEARCH" not in fake.command_names()


def _registered_handlers(monkeypatch, actions: dict[str, str]):
    monkeypatch.setattr(
        config,
        "get_provider_env",
        lambda key: actions["value"] if key == config.ENV_ACTIONS else "",
    )
    handlers = {}

    class Context:
        def register_tool(self, **kwargs):
            handlers[kwargs["name"]] = kwargs["handler"]

    register(Context())
    return handlers


def test_registered_reader_cannot_mark_seen_without_permission(monkeypatch):
    actions = {"value": "read"}
    handlers = _registered_handlers(monkeypatch, actions)
    fake = FakeIMAP()
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake),
    )
    response = json.loads(
        handlers["yandex_mail_read_message"]({"uid": "8", "folder": "INBOX", "mark_read": True})
    )
    assert "mark_message" in response["error"]
    assert fake.calls == []


@pytest.mark.parametrize(
    "name,args",
    [
        ("yandex_mail_list_folders", {}),
        ("yandex_mail_search_messages", {}),
        ("yandex_mail_read_message", {"uid": "8", "folder": "INBOX"}),
        ("yandex_mail_mark_message", {"uid": "8", "folder": "INBOX", "read": True}),
        (
            "yandex_mail_move_message",
            {"uid": "8", "folder": "INBOX", "destination": "Trash"},
        ),
        ("yandex_mail_delete_message", {"uid": "8", "folder": "INBOX"}),
    ],
)
def test_registered_handlers_recheck_permission_before_building_client(monkeypatch, name, args):
    actions = {"value": "all"}
    handlers = _registered_handlers(monkeypatch, actions)
    actions["value"] = "disabled"
    built = []
    monkeypatch.setattr(tool, "build_client", lambda: built.append(True))
    response = json.loads(handlers[name](args))
    assert "not allowed" in response["error"]
    assert not built


def test_delete_in_trash_reports_no_change():
    fake = FakeIMAP()
    with YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake) as client:
        result = client.delete("Trash", ["8"])
    assert result == {
        "deleted": False,
        "reason": "already_in_trash",
        "folder": "Trash",
        "note": (
            "The message is already in Trash and was left untouched. "
            "Erasing it permanently is a separate, irreversible action."
        ),
    }
    assert fake.command_names() == []


def test_read_fetch_is_bounded():
    fake = FakeIMAP()
    fake.responses["FETCH"] = ("OK", fetch_body_response())
    with YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake) as client:
        client.fetch_message("INBOX", "8")
    fetch = next(c for c in fake.calls if c[:2] == ("uid", "FETCH"))
    assert fetch[3] == "(UID FLAGS BODY.PEEK[]<0.10485761>)"


@pytest.mark.parametrize("extra", [0, 1])
def test_body_limit_is_checked_after_fetch(extra):
    fake = FakeIMAP()
    raw = b"x" * (10485760 + extra)
    fake.responses["FETCH"] = ("OK", fetch_body_response(raw=raw))
    with YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake) as client:
        if extra:
            with pytest.raises(MailError, match="raw read limit"):
                client.fetch_message("INBOX", "8")
        else:
            assert client.fetch_message("INBOX", "8")[0] == raw


def test_read_output_has_a_hard_character_limit(monkeypatch):
    fake = FakeIMAP()
    raw = b"Content-Type: text/plain\r\n\r\n" + b"x" * 100001
    fake.responses["FETCH"] = ("OK", fetch_body_response(raw=raw))
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake),
    )
    message = json.loads(tool.handle_read({"uid": "8", "folder": "INBOX", "max_chars": 10**9}))[
        "message"
    ]
    assert len(message["body"]) == 100000
    assert message["truncated"] is True
