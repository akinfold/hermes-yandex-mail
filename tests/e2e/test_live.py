"""Live end-to-end tests against a real Yandex mailbox over IMAP.

Marked ``e2e`` and deselected by default; run them with ``pytest -m e2e``.

They are self-contained: nothing is *sent*, so no third party is ever emailed.
The suite uploads (IMAP ``APPEND``) one throwaway message with a unique marker
in its subject, exercises the whole tool surface against it, and erases it in a
``finally`` — so a failed assertion still leaves the mailbox as it was found.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

from hermes_yandex_mail import config, tool
from hermes_yandex_mail.imap import MailError, SearchQuery, YandexIMAPClient, normalize_email

pytestmark = pytest.mark.e2e

MARKER_PREFIX = "hermes-yandex-mail e2e"

#: Yandex indexes SUBJECT asynchronously: a word that has never been seen before
#: (our random marker) is searchable a second or two after the message lands,
#: while UID FETCH and TEXT search see it at once. That is server behaviour, not
#: something the plugin can fix, so the live suite waits for the index instead of
#: pretending it is instant.
_INDEX_TIMEOUT = 30.0
_INDEX_POLL = 1.0


def _await_search(
    client: YandexIMAPClient,
    folder: str,
    marker: str,
    *,
    expect_found: bool,
) -> list:
    """Search until the index agrees with ``expect_found``, or time out."""
    deadline = time.monotonic() + _INDEX_TIMEOUT
    found: list = []
    while True:
        found = client.search(folder, SearchQuery(subject=marker), limit=10)
        if bool(found) == expect_found or time.monotonic() > deadline:
            return found
        time.sleep(_INDEX_POLL)


def _credentials() -> tuple[str, str]:
    login = config.get_provider_env(config.ENV_LOGIN)
    password = config.get_provider_env(config.ENV_PASSWORD)
    if not login or not password:
        pytest.skip(f"{config.ENV_LOGIN} / {config.ENV_PASSWORD} are not set")
    return login, password


@pytest.fixture(scope="module")
def account() -> str:
    return _credentials()[0]


@pytest.fixture
def client():
    _credentials()
    with config.build_client() as live:
        yield live


def _throwaway_message(account: str, marker: str) -> tuple[bytes, str]:
    """A multipart message with a plain part, an HTML part, and an attachment."""
    token = f"body-token-{marker}"
    message = EmailMessage()
    message["Subject"] = f"[{MARKER_PREFIX}] {marker} — проверка кириллицы"
    # Real mail always has one, and the post-move UID lookup matches on it:
    # without it the plugin rightly reports no destination UIDs, and this
    # suite would never exercise the mapping at all.
    message["Message-ID"] = make_msgid(domain="hermes-yandex-mail.test")
    message["From"] = account
    message["To"] = account
    message.set_content(f"Plain body.\n{token}\n")
    message.add_alternative(f"<html><body><p>HTML body {token}</p></body></html>", subtype="html")
    message.add_attachment(
        b"attachment payload",
        maintype="text",
        subtype="plain",
        filename="note.txt",
    )
    return message.as_bytes(), token


@pytest.fixture
def planted(client, account):
    """Upload a throwaway message and guarantee it is gone afterwards."""
    marker = uuid.uuid4().hex[:12]
    raw, token = _throwaway_message(account, marker)
    uid = client.append("INBOX", raw, flags=["\\Seen"])
    assert uid, "APPEND did not return an APPENDUID; the server may lack UIDPLUS"
    state = {"uid": uid, "folder": "INBOX", "marker": marker, "token": token}
    try:
        yield state
    finally:
        _purge_everywhere(marker)


def _purge_everywhere(marker: str) -> None:
    """Erase every trace of one throwaway message, wherever it ended up.

    Deliberately searches by marker instead of trusting the UID the test was
    last holding: a test that fails midway through a move leaves the message
    in a folder the tracked state does not name, and a cleanup that trusted
    that state would both miss the message and raise a second error on top
    of the real failure.
    """
    with config.build_client() as cleanup:
        for folder in cleanup.list_folders():
            try:
                found = cleanup.search(folder.name, SearchQuery(text=marker), limit=50)
                if found:
                    cleanup.delete(folder.name, [m.uid for m in found], permanent=True)
            except MailError:  # pragma: no cover - diagnostics only
                # One unreadable folder must not stop the others being cleaned.
                continue


def test_folders_report_roles_and_counts(client):
    folders = client.list_folders(with_counts=True)
    by_role = {f.special_use: f.name for f in folders if f.special_use}
    assert "inbox" in by_role, f"no INBOX among {[f.name for f in folders]}"
    assert "trash" in by_role, f"no Trash among {[f.name for f in folders]}"
    inbox = next(f for f in folders if f.special_use == "inbox")
    assert inbox.messages is not None and inbox.messages >= 0
    assert inbox.unseen is not None


def test_the_account_owns_its_own_address(account):
    assert normalize_email(account) == normalize_email(account.replace("@yandex.ru", "@ya.ru"))


def test_search_finds_the_planted_message(client, planted):
    found = _await_search(client, "INBOX", planted["marker"], expect_found=True)
    uids = [m.uid for m in found]
    assert planted["uid"] in uids, f"expected UID {planted['uid']} among {uids}"
    message = next(m for m in found if m.uid == planted["uid"])
    assert MARKER_PREFIX in message.subject
    assert "проверка кириллицы" in message.subject, f"subject came back as {message.subject!r}"
    assert message.seen is True


def test_read_prefers_the_plain_part_and_lists_the_attachment(planted):
    payload = json.loads(tool.handle_read({"uid": planted["uid"], "folder": "INBOX"}))
    assert "error" not in payload, payload
    message = payload["message"]
    assert planted["token"] in message["body"]
    assert message["body"].startswith("Plain body."), message["body"][:200]
    assert message["body_from_html"] is False
    assert [a["filename"] for a in message["attachments"]] == ["note.txt"]


def test_flags_round_trip(client, planted):
    uid, folder = planted["uid"], planted["folder"]
    client.store_flags(folder, [uid], add=["\\Flagged"], remove=["\\Seen"])
    after = client.summary(folder, uid)
    assert after is not None
    assert after.flagged is True, f"flags came back as {after.flags}"
    assert after.seen is False, f"flags came back as {after.flags}"

    client.store_flags(folder, [uid], add=["\\Seen"], remove=["\\Flagged"])
    restored = client.summary(folder, uid)
    assert restored is not None and restored.seen and not restored.flagged


def test_text_and_attachment_pages(planted):
    args = {"uid": planted["uid"], "folder": "INBOX"}
    full = json.loads(tool.handle_read(args))["message"]
    text, offset = [], 0
    for _ in range(100):
        page = json.loads(tool.handle_read({**args, "offset": offset, "max_chars": 10}))["message"]
        text.append(page["body"])
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert "".join(text) == full["body"]
    attachment = full["attachments"][0]
    data, offset = [], 0
    for _ in range(20):
        page = json.loads(
            tool.handle_read_attachment(
                {**args, "part_id": attachment["part_id"], "offset": offset, "limit": 4}
            )
        )
        assert "error" not in page, page
        data.append(base64.b64decode(page["data_base64"]))
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert b"".join(data) == b"attachment payload"


def test_move_to_trash_then_purge(client, planted):
    trash = client.find_special_folder("trash")
    assert trash, "the account has no Trash folder"

    result = client.delete("INBOX", [planted["uid"]], permanent=False)
    # The real method the move used — "trash" was a hard-coded label that hid
    # the copy+flagged case, where the original is left behind.
    assert result["method"] in {"move", "copy+expunge"}, result
    assert result["trash_folder"] == trash, result
    # The soft delete reports where each message landed, so the next turn does
    # not have to re-derive it (Yandex cannot search by Message-ID).
    assert result["destination_uids"].get(planted["uid"]), result

    # The message keeps its identity but not its UID: find it again by subject.
    moved = _await_search(client, trash, planted["marker"], expect_found=True)
    assert moved, f"the message did not arrive in {trash}"
    planted["folder"] = trash
    planted["uid"] = moved[0].uid

    gone = _await_search(client, "INBOX", planted["marker"], expect_found=False)
    assert not gone, f"the message is still in INBOX as {[m.uid for m in gone]}"


def test_the_tool_handlers_work_against_the_live_account(client, planted):
    folders = json.loads(tool.handle_list_folders({}))
    assert "error" not in folders, folders
    assert folders["count"] >= 1

    _await_search(client, "INBOX", planted["marker"], expect_found=True)
    search = json.loads(tool.handle_search({"subject": planted["marker"], "limit": 5}))
    assert "error" not in search, search
    assert search["count"] == 1, search

    marked = json.loads(
        tool.handle_mark({"uid": planted["uid"], "folder": "INBOX", "flagged": True})
    )
    assert marked.get("marked") is True, marked
