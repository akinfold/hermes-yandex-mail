"""Environment -> client, plus the folder and action allow-lists."""

from __future__ import annotations

import pytest

from hermes_yandex_mail import config
from hermes_yandex_mail.imap import DEFAULT_HOST, DEFAULT_PORT


@pytest.fixture
def env(monkeypatch):
    """Current-profile values, with absent keys distinct from empty values."""
    values: dict[str, str] = {}
    monkeypatch.setattr(
        config, "env_value", lambda name: None if name not in values else values[name].strip()
    )
    return values


def test_the_environment_variable_names_are_exact():
    """The names are composed from a prefix (see config), so pin what they spell."""
    assert config.ENV_LOGIN == "YANDEX_MAIL_LOGIN"
    assert config.ENV_PASSWORD == "YANDEX_MAIL_APP_PASSWORD"
    assert config.ENV_HOST == "YANDEX_MAIL_IMAP_HOST"
    assert config.ENV_PORT == "YANDEX_MAIL_IMAP_PORT"
    assert config.ENV_FOLDERS == "YANDEX_MAIL_FOLDERS"
    assert config.ENV_ACTIONS == "YANDEX_MAIL_ACTIONS"


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
        ("", set(config.DEFAULT_ACTIONS)),
        ("   ", set(config.DEFAULT_ACTIONS)),
        ("all", set(config.DEFAULT_ACTIONS)),
        ("read", {"list_folders", "search_messages", "read_message"}),
        ("write", {"mark_message", "move_message"}),
        ("delete", {"delete_message"}),
        ("read,write", set(config.DEFAULT_ACTIONS) - {"delete_message"}),
        ("READ", {"list_folders", "search_messages", "read_message"}),
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
    """Every action is reachable by a group — except the ones that need naming.

    ``send_message`` and ``save_attachment`` have no group of their own on
    purpose: a shorthand would be a short word an earlier configuration might
    already contain.
    """
    covered = set().union(*(v for k, v in config.ACTION_GROUPS.items() if k != "all"))
    assert covered == set(config.ACTIONS) - config.OPT_IN_ACTIONS
    assert set(config.SENDING_ACTIONS) == {"send_message"}
    assert set(config.OPT_IN_ACTIONS) == {"send_message", "save_attachment"}


# -- sending is the one action an empty allow-list does not grant ------------


def test_an_unset_allow_list_grants_everything_except_sending(env):
    allowed = config.allowed_actions()
    assert allowed == config.DEFAULT_ACTIONS
    assert "send_message" not in allowed
    assert "delete_message" in allowed


def test_the_all_shorthand_does_not_include_sending(env):
    env[config.ENV_ACTIONS] = "all"
    assert "send_message" not in config.allowed_actions()


@pytest.mark.parametrize("value", ["all,send_message", "send_message", "yandex_mail_send_message"])
def test_sending_is_granted_only_by_naming_it(env, value):
    env[config.ENV_ACTIONS] = value
    assert "send_message" in config.allowed_actions()


def test_a_bare_send_token_stays_inert(env):
    """``send`` was an unrecognised token before 0.3.0 and is still one.

    Anyone who had optimistically written it would otherwise have found
    sending switched on by the upgrade alone — which is the single thing the
    opt-in exists to prevent.
    """
    env[config.ENV_ACTIONS] = "all,send"
    assert "send_message" not in config.allowed_actions()
    assert "send" not in config.ACTION_GROUPS


# -- the recipient fence ----------------------------------------------------


def test_no_fence_by_default(env):
    assert config.allowed_send_recipients() is None


def test_a_fence_keeps_addresses_and_domains(env):
    env[config.ENV_SEND_TO] = " owner@yandex.ru , @example.org "
    assert config.allowed_send_recipients() == ["owner@yandex.ru", "@example.org"]


def test_a_fence_that_parses_to_nothing_is_empty_rather_than_absent(env):
    """Empty refuses everything; ``None`` would allow everything."""
    env[config.ENV_SEND_TO] = "nonsense, @, also-nonsense"
    assert config.allowed_send_recipients() == []


def test_a_fence_set_to_nothing_at_all_still_refuses_everything(env):
    """``YANDEX_MAIL_SEND_TO=`` with nothing after it is a fence somebody wrote."""
    env[config.ENV_SEND_TO] = ""
    assert config.allowed_send_recipients() == []


# -- the SMTP client --------------------------------------------------------


def test_the_smtp_client_defaults_to_yandex(env):
    env.update({config.ENV_LOGIN: "me@yandex.ru", config.ENV_PASSWORD: "pw"})
    client = config.build_smtp_client()
    assert client._host == "smtp.yandex.ru"
    assert client._port == 465


def test_the_smtp_host_and_port_can_be_overridden(env):
    env.update(
        {
            config.ENV_LOGIN: "me@yandex.ru",
            config.ENV_PASSWORD: "pw",
            config.ENV_SMTP_HOST: "smtp.example.org",
            config.ENV_SMTP_PORT: "2465",
        }
    )
    client = config.build_smtp_client()
    assert (client._host, client._port) == ("smtp.example.org", 2465)


def test_a_nonsense_smtp_port_falls_back_rather_than_failing(env):
    env.update(
        {config.ENV_LOGIN: "me@yandex.ru", config.ENV_PASSWORD: "pw", config.ENV_SMTP_PORT: "SMTP"}
    )
    assert config.build_smtp_client()._port == 465


def test_sending_needs_credentials_like_everything_else(env):
    with pytest.raises(config.MissingCredentials):
        config.build_smtp_client()
    with pytest.raises(config.MissingCredentials):
        config.account_address()


def test_the_account_address_is_the_configured_login(env):
    env.update({config.ENV_LOGIN: "me@yandex.ru", config.ENV_PASSWORD: "pw"})
    assert config.account_address() == "me@yandex.ru"


def test_a_fence_set_to_whitespace_refuses_everything(env):
    """Set but unparseable must not be indistinguishable from unset."""
    env[config.ENV_SEND_TO] = "   "
    assert config.allowed_send_recipients() == []


# -- the fence, read the way production reads it ----------------------------


@pytest.fixture
def real_env(monkeypatch, tmp_path):
    """No stand-in: the fence resolved through ``_compat`` itself.

    The ``env`` fixture above models the resolver, and a model can agree with
    itself while production disagrees — which is precisely how this fence came
    to fail open. Outside a Hermes host the resolver is the bundled shim. The
    throwaway home keeps the developer's own ``~/.hermes/.env`` out of it.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(config.ENV_SEND_TO, raising=False)
    return tmp_path


def test_only_an_absent_variable_removes_the_fence(real_env):
    assert config.allowed_send_recipients() is None


@pytest.mark.parametrize("value", ["", " ", "  \t ", ",", " , ", "bogus", "@", "nonsense, @"])
def test_a_fence_that_is_set_but_unusable_refuses_every_recipient(real_env, monkeypatch, value):
    monkeypatch.setenv(config.ENV_SEND_TO, value)
    assert config.allowed_send_recipients() == []


def test_a_usable_fence_survives_the_real_resolver(real_env, monkeypatch):
    monkeypatch.setenv(config.ENV_SEND_TO, " owner@yandex.ru , @example.org ")
    assert config.allowed_send_recipients() == ["owner@yandex.ru", "@example.org"]


def test_standalone_configuration_does_not_read_the_default_profile_file(real_env):
    (real_env / ".hermes").mkdir()
    (real_env / ".hermes" / ".env").write_text(
        f"{config.ENV_SEND_TO}=owner@yandex.ru\n", encoding="utf-8"
    )
    assert config.allowed_send_recipients() is None


@pytest.mark.parametrize(
    "name,builder",
    [(config.ENV_HOST, config.build_client), (config.ENV_SMTP_HOST, config.build_smtp_client)],
)
@pytest.mark.parametrize(
    "host",
    [
        "tls://mail.example",
        "owner@mail.example",
        "mail.example/path",
        "mail.example:993",
        "mail example",
        "mail\x00example",
        "mail\nexample",
        "mail\texample",
        "[::1]",
        "-mail.example",
        "mail-.example",
        "mail..example",
        "máil.example",
        "a" * 64 + ".example",
    ],
)
def test_invalid_endpoint_syntax_is_rejected_before_client_creation(env, name, builder, host):
    env.update({config.ENV_LOGIN: "me@yandex.ru", config.ENV_PASSWORD: "pw", name: host})
    with pytest.raises(ValueError, match="ASCII DNS hostname or an IP address"):
        builder()


@pytest.mark.parametrize("host", ["mail.example", "localhost", "mail.example.", "127.0.0.1", "::1"])
def test_valid_endpoint_syntax_is_preserved(env, host):
    env.update({config.ENV_LOGIN: "me@yandex.ru", config.ENV_PASSWORD: "pw", config.ENV_HOST: host})
    assert config.build_client()._host == host


@pytest.mark.parametrize("operation", [config.allowed_send_recipients, config.attachment_max_bytes])
def test_nullable_policy_settings_use_a_single_atomic_read(monkeypatch, operation):
    reads = []

    def resolve(name):
        reads.append(name)
        return None if len(reads) == 1 else ""

    monkeypatch.setattr(config, "env_value", resolve)
    operation()
    assert len(reads) == 1
