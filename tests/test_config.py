"""Environment -> client, plus the folder and action allow-lists."""

from __future__ import annotations

import pytest

from hermes_yandex_mail import config
from hermes_yandex_mail.imap import DEFAULT_HOST, DEFAULT_PORT


@pytest.fixture
def env(monkeypatch):
    """A dict standing in for the environment ``get_provider_env`` reads."""
    values: dict[str, str] = {}
    monkeypatch.setattr(config, "get_provider_env", lambda name: values.get(name, ""))
    return values


def test_credentials_present(env):
    assert config.credentials_present() is False
    env[config.ENV_LOGIN] = "me@yandex.ru"
    assert config.credentials_present() is False
    env[config.ENV_PASSWORD] = "app-password"
    assert config.credentials_present() is True


def test_build_client_defaults(env):
    env.update({config.ENV_LOGIN: "me@yandex.ru", config.ENV_PASSWORD: "pw"})
    client = config.build_client()
    assert client._host == DEFAULT_HOST
    assert client._port == DEFAULT_PORT
    assert client.default_folder() == "INBOX"


def test_build_client_overrides(env):
    env.update(
        {
            config.ENV_LOGIN: "me@yandex.ru",
            config.ENV_PASSWORD: "pw",
            config.ENV_HOST: "imap.example.org",
            config.ENV_PORT: "1993",
            config.ENV_FOLDERS: " INBOX , Sent ",
        }
    )
    client = config.build_client()
    assert (client._host, client._port) == ("imap.example.org", 1993)
    assert client._allowed == ["INBOX", "Sent"]


def test_a_nonsense_port_falls_back_to_the_default(env):
    env.update(
        {config.ENV_LOGIN: "me@yandex.ru", config.ENV_PASSWORD: "pw", config.ENV_PORT: "IMAP"}
    )
    assert config.build_client()._port == DEFAULT_PORT


def test_build_client_without_credentials_explains_where_to_get_them(env):
    with pytest.raises(config.MissingCredentials, match="app-passwords"):
        config.build_client()


def test_allowed_folders(env):
    assert config.allowed_folders() == []
    env[config.ENV_FOLDERS] = "INBOX,Спам, "
    assert config.allowed_folders() == ["INBOX", "Спам"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", set(config.ACTIONS)),
        ("   ", set(config.ACTIONS)),
        ("all", set(config.ACTIONS)),
        ("read", {"list_folders", "search_messages", "read_message", "read_attachment"}),
        ("write", {"mark_message", "move_message"}),
        ("delete", {"delete_message"}),
        ("read,write", set(config.ACTIONS) - {"delete_message"}),
        ("READ", {"list_folders", "search_messages", "read_message", "read_attachment"}),
        ("read-message", {"read_message"}),
        ("yandex_mail_delete_message", {"delete_message"}),
        ("read_message, nonsense", {"read_message"}),
        ("nonsense", set()),
    ],
)
def test_allowed_actions(env, raw, expected):
    env[config.ENV_ACTIONS] = raw
    assert config.allowed_actions() == frozenset(expected)


def test_action_groups_cover_every_action():
    covered = set().union(*(v for k, v in config.ACTION_GROUPS.items() if k != "all"))
    assert covered == set(config.ACTIONS)
