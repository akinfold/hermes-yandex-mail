"""Tests for register(): which tools reach Hermes, given YANDEX_MAIL_ACTIONS."""

from __future__ import annotations

import pytest

import hermes_yandex_mail as plugin
from hermes_yandex_mail import config


class FakeCtx:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


def _register_with(monkeypatch, actions: str) -> list[str]:
    monkeypatch.setattr(
        config,
        "get_provider_env",
        lambda name: actions if name == config.ENV_ACTIONS else "",
    )
    ctx = FakeCtx()
    plugin.register(ctx)
    return [t["name"] for t in ctx.tools]


def test_registers_every_tool_except_the_opt_in_ones_by_default(monkeypatch):
    names = _register_with(monkeypatch, "")
    assert names == [
        f"yandex_mail_{action}" for action in config.ACTIONS if action not in config.OPT_IN_ACTIONS
    ]
    assert "yandex_mail_send_message" not in names
    assert "yandex_mail_save_attachment" not in names


@pytest.mark.parametrize("actions", ["", "   ", "all", "read", "read,write,delete"])
def test_saving_attachments_is_absent_unless_named(monkeypatch, actions):
    assert "yandex_mail_save_attachment" not in _register_with(monkeypatch, actions)


@pytest.mark.parametrize(
    "actions",
    [
        "save_attachment",
        "read,save_attachment",
        "all,SAVE_ATTACHMENT",
        "yandex_mail_save_attachment",
    ],
)
def test_saving_attachments_appears_only_when_it_is_named(monkeypatch, actions):
    assert "yandex_mail_save_attachment" in _register_with(monkeypatch, actions)


@pytest.mark.parametrize("token", ["attachments", "attachment", "save", "files"])
def test_no_short_word_grants_saving_attachments(monkeypatch, token):
    """Upgrading must not turn a previously-ignored word into a live grant."""
    assert "yandex_mail_save_attachment" not in _register_with(monkeypatch, f"read,{token}")


@pytest.mark.parametrize("actions", ["all,send_message", "send_message", "read,send_message"])
def test_sending_appears_only_when_it_is_named(monkeypatch, actions):
    assert "yandex_mail_send_message" in _register_with(monkeypatch, actions)


def test_a_bare_send_token_registers_nothing(monkeypatch):
    """Upgrading must not turn a previously-ignored word into a live grant."""
    assert _register_with(monkeypatch, "send") == []
    assert "yandex_mail_send_message" not in _register_with(monkeypatch, "all,send")


def test_read_only_deployment(monkeypatch):
    assert _register_with(monkeypatch, "read") == [
        "yandex_mail_list_folders",
        "yandex_mail_search_messages",
        "yandex_mail_read_message",
    ]


def test_everything_but_delete(monkeypatch):
    names = _register_with(monkeypatch, "read,write")
    assert "yandex_mail_delete_message" not in names
    assert len(names) == 5


def test_single_action(monkeypatch):
    assert _register_with(monkeypatch, "read_message") == ["yandex_mail_read_message"]


def test_unknown_action_registers_nothing(monkeypatch):
    assert _register_with(monkeypatch, "nonsense") == []


@pytest.mark.parametrize("field", ["schema", "handler", "check_fn", "requires_env"])
def test_registration_payload(monkeypatch, field):
    monkeypatch.setattr(config, "get_provider_env", lambda name: "")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert all(t[field] for t in ctx.tools)
    for t in ctx.tools:
        assert t["toolset"] == "yandex_mail"
        assert t["schema"]["name"] == t["name"]
        assert t["description"] and t["emoji"]


def test_version_is_a_semver_string():
    assert plugin.__version__.count(".") == 2
