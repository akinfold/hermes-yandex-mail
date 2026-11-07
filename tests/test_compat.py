"""Current-profile configuration stays atomic and fails closed."""

from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from hermes_yandex_mail import _compat, config

NAME = "YANDEX_MAIL_SEND_TO"


@pytest.mark.parametrize("value,expected", [(None, None), ("", ""), (" \t", ""), (" x ", "x")])
def test_env_value_preserves_absence_separately_from_emptiness(monkeypatch, value, expected):
    reads = []

    def get_secret(name):
        reads.append(name)
        return value

    monkeypatch.setattr(_compat, "_get_secret", get_secret)
    assert _compat.env_value(NAME) == expected
    assert reads == [NAME]


def test_standalone_reads_only_the_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(NAME, raising=False)
    directory = tmp_path / ".hermes"
    directory.mkdir()
    (directory / ".env").write_text(f"{NAME}=other-profile@example.org\n")
    monkeypatch.setattr(_compat, "_get_secret", __import__("os").environ.get)
    assert _compat.env_value(NAME) is None
    monkeypatch.setenv(NAME, "")
    assert _compat.env_value(NAME) == ""
    monkeypatch.setenv(NAME, " current@example.org ")
    assert _compat.env_value(NAME) == "current@example.org"


def test_profile_values_override_process_values_for_every_setting(monkeypatch):
    values = {
        config.ENV_LOGIN: "work@example.org",
        config.ENV_PASSWORD: "profile-password",
        config.ENV_ACTIONS: "send_message",
        config.ENV_FOLDERS: "Archive",
        config.ENV_HOST: "imap.work.example",
        config.ENV_PORT: "1993",
        config.ENV_SMTP_HOST: "smtp.work.example",
        config.ENV_SMTP_PORT: "2465",
        config.ENV_SEND_TO: "",
        config.ENV_ATTACHMENT_MAX_BYTES: "",
    }
    for key in values:
        monkeypatch.setenv(key, "other-profile-value")
    reads = []

    def get_secret(name):
        reads.append(name)
        return values.get(name)

    monkeypatch.setattr(_compat, "_get_secret", get_secret)
    assert config.allowed_actions() == {"send_message"}
    assert config.allowed_send_recipients() == []
    with pytest.raises(config.PermissionDenied):
        config.attachment_max_bytes()
    client = config.build_client()
    smtp = config.build_smtp_client()
    assert (client._login, client._password) == ("work@example.org", "profile-password")
    assert (client._host, client._port, client._allowed) == ("imap.work.example", 1993, ["Archive"])
    assert (smtp._login, smtp._password) == ("work@example.org", "profile-password")
    assert (smtp._host, smtp._port) == ("smtp.work.example", 2465)
    assert set(reads) == set(values)
    values[config.ENV_SEND_TO] = "work@example.org"
    assert config.allowed_send_recipients() == ["work@example.org"]


def _load_compat():
    spec = importlib.util.spec_from_file_location("compat_probe", _compat.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hermes_imports_the_public_secret_api(monkeypatch):
    agent = types.ModuleType("agent")
    secret_scope = types.ModuleType("agent.secret_scope")
    calls = []
    secret_scope.get_secret = lambda name: calls.append(name) or " scoped "
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    compat = _load_compat()
    assert compat.HERMES_AVAILABLE
    assert compat.env_value(NAME) == "scoped"
    assert calls == [NAME]


def test_a_missing_secret_api_in_hermes_is_not_a_standalone_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
    with pytest.raises(ModuleNotFoundError):
        _load_compat()


@pytest.mark.parametrize("operation", ["document_cache_dir", "agent_visible_path"])
def test_a_missing_host_path_api_is_not_a_local_path_fallback(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(_compat, "HERMES_AVAILABLE", True)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setitem(sys.modules, "tools.credential_files", None)
    args = [tmp_path / "attachment"] if operation == "agent_visible_path" else []
    with pytest.raises(ImportError):
        getattr(_compat, operation)(*args)
