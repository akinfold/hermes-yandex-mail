"""Offline checks for live-test retries and cleanup; no credential loader is imported."""

from __future__ import annotations

import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from hermes_yandex_mail.imap import MailError

_spec = importlib.util.spec_from_file_location(
    "live_harness", Path(__file__).parent / "e2e" / "test_live.py"
)
assert _spec and _spec.loader
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", Mock())


@pytest.mark.parametrize("name", ["read", "search", "list_folders"])
def test_read_only_transport_retry_is_bounded(monkeypatch, name):
    handler = Mock(
        return_value=json.dumps({"error": "Cannot reach host: [Errno 101] Network is unreachable"})
    )
    monkeypatch.setattr(live.tool, f"handle_{name}", handler)
    result = live._read_only_handler(name, {})
    assert "error" in result
    assert handler.call_count == 3
    assert live.time.sleep.call_count == 2


@pytest.mark.parametrize(
    "error",
    [
        "Cannot reach imap.yandex.ru:993: command: CAPABILITY => problems with connection",
        "Login failed: problems with connection",
        "Cannot reach imap.yandex.ru:993: _ssl.c:989: The handshake operation timed out",
    ],
)
def test_observed_transport_failures_are_retryable(error):
    assert live._transport_failure(MailError(error))


def test_read_only_retry_returns_recovered_payload(monkeypatch):
    handler = Mock(
        side_effect=[json.dumps({"error": "socket error: EOF"}), '{"message": {"body": "ok"}}']
    )
    monkeypatch.setattr(live.tool, "handle_read", handler)
    assert live._read_only_handler("read", {"uid": "8"}) == {"message": {"body": "ok"}}
    assert handler.call_args_list[0] == handler.call_args_list[1]


@pytest.mark.parametrize(
    "error",
    [
        "Yandex Mail login failed: invalid credentials",
        "Cannot reach host: CERTIFICATE_VERIFY_FAILED",
        "SELECT failed: Permission denied",
        "FETCH failed: malformed response",
        "message not found",
    ],
)
def test_nontransport_handler_errors_are_not_retried(monkeypatch, error):
    handler = Mock(return_value=json.dumps({"error": error}))
    monkeypatch.setattr(live.tool, "handle_read", handler)
    assert live._read_only_handler("read", {}) == {"error": error}
    handler.assert_called_once()


@pytest.mark.parametrize("name", ["send", "mark", "move", "delete", "save_attachment"])
def test_write_handlers_cannot_enter_retry_helper(name):
    with pytest.raises(KeyError):
        live._read_only_handler(name, {})


def _clients(monkeypatch, outcomes):
    outcomes = iter(outcomes)
    built = Mock()

    @contextmanager
    def build():
        built()
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome

    monkeypatch.setattr(live.config, "build_client", build)
    return built


def _client(hits=(), *, search_error=None, delete_error=None):
    client = Mock()
    client.list_folders.return_value = [SimpleNamespace(name="INBOX")]
    client.search.side_effect = search_error
    client.search.return_value = [SimpleNamespace(uid=uid) for uid in hits]
    client.delete.side_effect = delete_error
    return client


def test_cleanup_reconnects_after_constructor_transport_failure(monkeypatch):
    empty = _client()
    built = _clients(monkeypatch, [MailError("CAPABILITY failed: socket error: EOF"), empty, empty])
    live._purge_everywhere("marker")
    assert built.call_count == 3
    assert empty.search.call_count == 4


def test_incomplete_scan_resets_quiet_counter(monkeypatch):
    empty, failed = _client(), _client(search_error=MailError("connection reset by peer"))
    built = _clients(monkeypatch, [empty, failed, empty, empty])
    live._purge_everywhere("marker")
    assert built.call_count == 4


def test_cleanup_never_treats_unreadable_folder_as_empty():
    client = _client(search_error=MailError("SELECT failed: Permission denied"))
    with pytest.raises(MailError, match="Permission denied"):
        live._find_everywhere(client, "marker")


def test_uncertain_delete_is_rescanned_instead_of_replayed(monkeypatch):
    first = _client(["8"], delete_error=MailError("connection reset by peer"))
    empty = _client()
    built = _clients(monkeypatch, [first, empty, empty])
    live._purge_everywhere("marker")
    assert built.call_count == 3
    first.delete.assert_called_once_with("INBOX", ["8"], permanent=True)
    empty.delete.assert_not_called()


def test_persistent_transport_failure_reports_cleanup_failure(monkeypatch):
    error = MailError("network is unreachable")
    built = _clients(monkeypatch, [error, error, error])
    with pytest.raises(RuntimeError, match="could not verify removal of marker"):
        live._purge_everywhere("marker")
    assert built.call_count == 3


def test_nontransport_cleanup_failure_is_not_retried(monkeypatch):
    built = _clients(
        monkeypatch, [_client(delete_error=MailError("UID EXPUNGE unsupported"), hits=["8"])]
    )
    with pytest.raises(RuntimeError, match="UID EXPUNGE unsupported"):
        live._purge_everywhere("marker")
    assert built.call_count == 1


def test_cleanup_timeout_does_not_claim_remaining_mail_is_removed(monkeypatch):
    monkeypatch.setattr(live.time, "monotonic", Mock(side_effect=[0, live._PURGE_TIMEOUT]))
    client = _client(["8"])
    _clients(monkeypatch, [client])
    with pytest.raises(RuntimeError, match="timed out for marker"):
        live._purge_everywhere("marker")


def test_planted_fixture_cleans_up_after_uncertain_append(monkeypatch):
    purge = Mock()
    monkeypatch.setattr(live, "_purge_everywhere", purge)
    client = Mock()
    client.append.side_effect = MailError("connection reset by peer")
    fixture = live.planted.__wrapped__(client, "test@example.invalid")
    with pytest.raises(MailError):
        next(fixture)
    client.append.assert_called_once()
    purge.assert_called_once()


def test_planted_fixture_cleans_up_when_append_returns_no_uid(monkeypatch):
    purge = Mock()
    monkeypatch.setattr(live, "_purge_everywhere", purge)
    client = Mock()
    client.append.return_value = ""
    fixture = live.planted.__wrapped__(client, "test@example.invalid")
    with pytest.raises(AssertionError, match="APPENDUID"):
        next(fixture)
    client.append.assert_called_once()
    purge.assert_called_once()


def test_live_workflow_serializes_all_refs_without_canceling_cleanup():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/e2e.yml").read_text())
    assert workflow["concurrency"] == {
        "group": "yandex-mail-e2e-account",
        "cancel-in-progress": False,
    }
