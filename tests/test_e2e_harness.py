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
        return_value=json.dumps({"error": "SEARCH failed: [Errno 101] Network is unreachable"})
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
        "Selecting Outbox failed: command: EXAMINE => problems with connection",
        "SEARCH failed: command: UID => problems with connection",
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
        "SEARCH failed: command: UID => BAD invalid search criteria",
        "Selecting Outbox failed: command: EXAMINE => Permission denied",
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


def test_cleanup_does_not_repeat_exhausted_connection_attempts(monkeypatch):
    built = _clients(monkeypatch, [MailError("Cannot reach host: socket error: EOF")])
    with pytest.raises(RuntimeError, match="could not verify removal"):
        live._purge_everywhere("marker")
    assert built.call_count == 1


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


def test_harness_preconnects_once_before_the_caller_body(monkeypatch):
    client = _client()
    built = _clients(monkeypatch, [client])
    with live._connected_client() as connected:
        connected.connect.assert_called_once()
        connected.append("INBOX", b"sample")
    assert built.call_count == 1
    client.append.assert_called_once()


def test_harness_closes_after_exhausted_production_connection_recovery(monkeypatch):
    client = _client()
    client.connect.side_effect = MailError("Cannot reach host: socket error: EOF")
    closed = Mock()

    @contextmanager
    def build():
        try:
            yield client
        finally:
            closed()

    monkeypatch.setattr(live.config, "build_client", build)
    with pytest.raises(MailError), live._connected_client():
        pytest.fail("an unconnected client reached the caller body")
    client.connect.assert_called_once()
    closed.assert_called_once()


@pytest.mark.parametrize("prefix", ["Cannot reach host:", "Login failed:"])
def test_handlers_do_not_multiply_exhausted_connection_attempts(monkeypatch, prefix):
    handler = Mock(return_value=json.dumps({"error": f"{prefix} socket error: EOF"}))
    monkeypatch.setattr(live.tool, "handle_read", handler)
    assert "error" in live._read_only_handler("read", {})
    handler.assert_called_once()


@pytest.mark.parametrize("operation", ["append", "send", "store_flags", "move", "delete"])
def test_connection_recovery_never_replays_caller_write(monkeypatch, operation):
    client = _client()
    getattr(client, operation).side_effect = MailError("command: UID => problems with connection")
    built = _clients(monkeypatch, [client])
    with pytest.raises(MailError), live._connected_client() as connected:
        getattr(connected, operation)("write")
    assert built.call_count == 1
    getattr(client, operation).assert_called_once_with("write")


def test_samples_never_replay_an_append(monkeypatch):
    client = _client()
    client.append.side_effect = ["8", MailError("command: APPEND => problems with connection")]
    built = _clients(monkeypatch, [client])
    monkeypatch.setattr(
        live, "_sample_messages", lambda *_: {"first": b"first", "second": b"second"}
    )
    purge = Mock()
    monkeypatch.setattr(live, "_purge_everywhere", purge)
    fixture = live.samples.__wrapped__("test@example.invalid")
    with pytest.raises(MailError):
        next(fixture)
    assert built.call_count == 1
    client.connect.assert_called_once()
    assert client.append.call_count == 2
    purge.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        "Selecting Outbox failed: command: EXAMINE => problems with connection",
        "SEARCH failed: command: UID => problems with connection",
    ],
)
def test_cleanup_reconnects_after_observed_command_transport_failures(monkeypatch, error):
    incomplete, empty = _client(search_error=MailError(error)), _client()
    built = _clients(monkeypatch, [incomplete, empty, empty])
    live._purge_everywhere("marker")
    assert built.call_count == 3
    incomplete.delete.assert_not_called()
