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


def test_registers_every_tool_by_default(monkeypatch):
    names = _register_with(monkeypatch, "")
    assert names == [f"yandex_mail_{action}" for action in config.ACTIONS]


def test_read_only_deployment(monkeypatch):
    assert _register_with(monkeypatch, "read") == [
        "yandex_mail_list_folders",
        "yandex_mail_search_messages",
        "yandex_mail_read_message",
        "yandex_mail_read_attachment",
    ]


def test_everything_but_delete(monkeypatch):
    names = _register_with(monkeypatch, "read,write")
    assert "yandex_mail_delete_message" not in names
    assert len(names) == 6


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
