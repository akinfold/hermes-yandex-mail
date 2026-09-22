"""The host-environment boundary: what a variable holds, and whether it is set.

``get_provider_env`` answers only the first question, and cannot answer the
second: it strips what it resolves, so a variable set to whitespace reaches the
caller as the empty string, exactly like one nobody ever set. Anything that must
fail closed needs the other question answered too.
"""

from __future__ import annotations

import pytest

from hermes_yandex_mail import _compat

NAME = "YANDEX_MAIL_SEND_TO"

#: Under a real Hermes host, resolving a *value* is the host's job, not the
#: shim's. Presence is always answered here, so only these two are conditional.
requires_shim = pytest.mark.skipif(
    _compat.HERMES_AVAILABLE, reason="values come from the host when Hermes is installed"
)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A throwaway home directory, so the real ``~/.hermes/.env`` is never read."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(NAME, raising=False)
    return tmp_path


def write_hermes_env(home, text: str) -> None:
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    (home / ".hermes" / ".env").write_text(text, encoding="utf-8")


def test_a_variable_nobody_set_is_not_set(home):
    assert _compat.provider_env_is_set(NAME) is False
    assert _compat.get_provider_env(NAME) == ""


@pytest.mark.parametrize("value", ["", " ", "\t ", "owner@yandex.ru"])
def test_a_variable_in_the_environment_is_set_whatever_it_holds(home, monkeypatch, value):
    """The whole point: presence is about the key, never about the value."""
    monkeypatch.setenv(NAME, value)
    assert _compat.provider_env_is_set(NAME) is True


def test_a_name_in_the_hermes_env_file_is_set_even_with_nothing_after_the_equals(home):
    write_hermes_env(home, f"# a comment\n\n{NAME}=\n")
    assert _compat.provider_env_is_set(NAME) is True


def test_a_hermes_env_file_that_does_not_mention_it_leaves_it_unset(home):
    write_hermes_env(home, "OTHER_KEY=1\nnot-an-assignment\n\n")
    assert _compat.provider_env_is_set(NAME) is False


def test_an_unreadable_hermes_env_file_is_not_an_error(home):
    """A directory where the file should be: the same OSError a bad mode gives."""
    (home / ".hermes" / ".env").mkdir(parents=True)
    assert _compat.provider_env_is_set(NAME) is False
    assert _compat.get_provider_env(NAME) == ""


@requires_shim
def test_the_value_still_comes_from_the_hermes_env_file(home):
    write_hermes_env(home, f'{NAME}="owner@yandex.ru"\n')
    assert _compat.get_provider_env(NAME) == "owner@yandex.ru"
    assert _compat.provider_env_is_set(NAME) is True


@requires_shim
def test_the_environment_still_wins_over_the_file(home, monkeypatch):
    write_hermes_env(home, f"{NAME}=file@example.org\n")
    monkeypatch.setenv(NAME, "env@example.org")
    assert _compat.get_provider_env(NAME) == "env@example.org"
