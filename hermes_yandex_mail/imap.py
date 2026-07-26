"""IMAP client for Yandex Mail — no Hermes imports, so it stays unit-testable.

Built on the standard library's :mod:`imaplib`. Everything addresses messages by
**UID** (stable within a folder) rather than sequence number, and every failure
becomes a :class:`MailError` so the tool layer has a single thing to catch.

Data-safety rules encoded here, not left to the caller:

* a move uses the server's ``UID MOVE`` when available, otherwise
  ``COPY`` → verify → mark deleted, so the copy exists before the original goes;
* expunging is always ``UID EXPUNGE`` (UIDPLUS), which touches only the UIDs we
  name — a bare ``EXPUNGE`` would also erase messages someone else flagged
  ``\\Deleted`` in that folder;
* deletion means "move to Trash" unless the caller explicitly asks otherwise.
"""

from __future__ import annotations

import contextlib
import imaplib
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from types import TracebackType

from . import imap_utf7
from .message import addresses, decode_header_value, header_date_iso, parse_message_bytes

__all__ = [
    "DEFAULT_FOLDER",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "Folder",
    "MailError",
    "MessageSummary",
    "SearchQuery",
    "YandexIMAPClient",
    "normalize_email",
]

DEFAULT_HOST = "imap.yandex.ru"
DEFAULT_PORT = 993
DEFAULT_FOLDER = "INBOX"

#: Yandex hands out several interchangeable domains for the same mailbox.
_YANDEX_DOMAINS = frozenset(
    {"ya.ru", "yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "narod.ru"}
)
_CANONICAL_DOMAIN = "yandex.ru"


class MailError(RuntimeError):
    """Any IMAP-level failure: connection, authentication, or a NO/BAD reply."""


def normalize_email(value: str) -> str:
    """Canonical form of an address, for "is this the same mailbox?" checks.

    Lower-cases and folds Yandex' interchangeable domains onto ``@yandex.ru``.
    The local part is left alone, so a ``+tag`` sub-address stays distinct.
    Every comparison in this package folds through here — two private copies of
    this rule would eventually disagree.
    """
    text = value.strip().lower()
    if "<" in text and ">" in text:
        text = text[text.rfind("<") + 1 : text.rfind(">")].strip()
    local, sep, domain = text.partition("@")
    if not sep:
        return local
    return f"{local}@{_CANONICAL_DOMAIN}" if domain in _YANDEX_DOMAINS else f"{local}@{domain}"


# -- data -------------------------------------------------------------------


@dataclass(frozen=True)
class Folder:
    """One mailbox folder as the agent sees it."""

    name: str
    flags: tuple[str, ...] = ()
    special_use: str = ""
    messages: int | None = None
    unseen: int | None = None


@dataclass
class MessageSummary:
    """Envelope-level view of a message: enough to decide whether to open it."""

    uid: str
    folder: str
    subject: str = ""
    from_: list[str] = field(default_factory=list)
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    date: str = ""
    size: int = 0
    flags: tuple[str, ...] = ()
    message_id: str = ""

    @property
    def seen(self) -> bool:
        return "\\Seen" in self.flags

    @property
    def flagged(self) -> bool:
        return "\\Flagged" in self.flags

    @property
    def answered(self) -> bool:
        return "\\Answered" in self.flags


@dataclass
class SearchQuery:
    """The subset of IMAP SEARCH this plugin exposes."""

    from_addr: str = ""
    to: str = ""
    subject: str = ""
    text: str = ""
    since: str = ""
    before: str = ""
    unread_only: bool = False
    flagged_only: bool = False


# -- response parsing -------------------------------------------------------

_LIST_RE = re.compile(rb"^\((?P<flags>[^)]*)\)\s+(?P<delim>\"[^\"]*\"|NIL)\s+(?P<name>.+)$")
_UID_RE = re.compile(rb"UID\s+(\d+)")
_FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)")
_APPENDUID_RE = re.compile(rb"APPENDUID\s+\d+\s+(\d+)")

#: LIST flag -> the role a folder plays, when the server bothers to say.
_SPECIAL_FLAGS = {
    "\\sent": "sent",
    "\\trash": "trash",
    "\\junk": "junk",
    "\\spam": "junk",
    "\\drafts": "drafts",
    "\\archive": "archive",
    "\\all": "archive",
}

#: Fallback by name — Yandex labels folders in Russian for Russian accounts.
_SPECIAL_NAMES = {
    "inbox": "inbox",
    "входящие": "inbox",
    "sent": "sent",
    "отправленные": "sent",
    "trash": "trash",
    "удалённые": "trash",
    "удаленные": "trash",
    "spam": "junk",
    "junk": "junk",
    "спам": "junk",
    "drafts": "drafts",
    "черновики": "drafts",
    "archive": "archive",
    "архив": "archive",
}


def _quote_mailbox(name: str) -> bytes:
    """Encode a folder name to modified UTF-7 and quote it for the wire."""
    encoded = imap_utf7.encode(name)
    escaped = encoded.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    return b'"' + escaped + b'"'


def _unquote_mailbox(raw: bytes) -> str:
    text = raw.strip()
    if text.startswith(b'"') and text.endswith(b'"'):
        text = text[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return imap_utf7.decode(text)


def _special_use(name: str, flags: Sequence[str]) -> str:
    for flag in flags:
        role = _SPECIAL_FLAGS.get(flag.lower())
        if role:
            return role
    return _SPECIAL_NAMES.get(name.strip().lower(), "")


def _parse_list_line(line: bytes) -> Folder | None:
    match = _LIST_RE.match(line.strip())
    if not match:
        return None
    flags = tuple(match.group("flags").decode("ascii", "replace").split())
    name = _unquote_mailbox(match.group("name"))
    if not name:
        return None
    return Folder(name=name, flags=flags, special_use=_special_use(name, flags))


def _iter_fetch_items(data: Iterable[object]) -> Iterator[tuple[bytes, bytes]]:
    """Yield ``(info, literal)`` pairs from an imaplib FETCH response.

    imaplib returns a flat list mixing ``(info, literal)`` tuples with the
    stray bytes that close them; anything without a literal is skipped.
    """
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            info, literal = item[0], item[1]
            if isinstance(info, bytes) and isinstance(literal, bytes):
                yield info, literal


def _int_group(pattern: re.Pattern[bytes], info: bytes, default: int = 0) -> int:
    match = pattern.search(info)
    return int(match.group(1)) if match else default


def _flags_of(info: bytes) -> tuple[str, ...]:
    match = _FLAGS_RE.search(info)
    if not match:
        return ()
    return tuple(match.group(1).decode("ascii", "replace").split())


def _summary_from_fetch(folder: str, info: bytes, headers: bytes) -> MessageSummary | None:
    uid_match = _UID_RE.search(info)
    if not uid_match:
        return None
    parsed = parse_message_bytes(headers)
    return MessageSummary(
        uid=uid_match.group(1).decode("ascii"),
        folder=folder,
        subject=decode_header_value(parsed.get("Subject")),
        from_=addresses(parsed.get("From")),
        to=addresses(parsed.get("To")),
        cc=addresses(parsed.get("Cc")),
        date=header_date_iso(parsed.get("Date")),
        size=_int_group(_SIZE_RE, info),
        flags=_flags_of(info),
        message_id=(parsed.get("Message-ID") or "").strip(),
    )


_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _imap_date(value: str) -> str:
    """Convert an ISO date (or an already-IMAP one) to ``d-Mon-yyyy``."""
    text = value.strip()
    if not text:
        return ""
    try:
        parsed: date = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            parsed = date.fromisoformat(text[:10])
        except ValueError as exc:
            raise MailError(f"Invalid date {value!r}: expected ISO 8601, e.g. 2026-07-25") from exc
    return f"{parsed.day}-{_IMAP_MONTHS[parsed.month - 1]}-{parsed.year}"


def _quoted(value: str) -> bytes:
    """A SEARCH string argument: UTF-8 bytes, quoted and escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return b'"' + escaped.encode("utf-8") + b'"'


def _search_criteria(query: SearchQuery) -> list[bytes]:
    """Build the SEARCH argument list; empty means ``ALL``."""
    criteria: list[bytes] = []
    for key, value in (
        (b"FROM", query.from_addr),
        (b"TO", query.to),
        (b"SUBJECT", query.subject),
        (b"TEXT", query.text),
    ):
        if value.strip():
            criteria += [key, _quoted(value.strip())]
    for key, value in ((b"SINCE", query.since), (b"BEFORE", query.before)):
        if value.strip():
            criteria += [key, _imap_date(value).encode("ascii")]
    if query.unread_only:
        criteria.append(b"UNSEEN")
    if query.flagged_only:
        criteria.append(b"FLAGGED")
    return criteria or [b"ALL"]


def _first_text(data: object) -> str:
    if isinstance(data, list | tuple):
        parts = [_first_text(item) for item in data if item]
        return " ".join(p for p in parts if p)
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return "" if data is None else str(data)


# -- client -----------------------------------------------------------------

_SUMMARY_FIELDS = (
    "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])"
)


class YandexIMAPClient:
    """A thin, UID-based IMAP client for one Yandex mailbox.

    Use it as a context manager; the connection is opened lazily on first use
    and always logged out afterwards.
    """

    def __init__(
        self,
        login: str,
        password: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        allowed_folders: Sequence[str] = (),
        connection_factory: Callable[[str, int], imaplib.IMAP4] | None = None,
    ) -> None:
        self._login = login
        self._password = password
        self._host = host
        self._port = port
        self._allowed = [f.strip() for f in allowed_folders if f.strip()]
        self._factory = connection_factory or (lambda h, p: imaplib.IMAP4_SSL(h, p))
        self._conn: imaplib.IMAP4 | None = None
        self._selected: tuple[str, bool] | None = None

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> YandexIMAPClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> imaplib.IMAP4:
        """Open and authenticate the connection, or return the open one."""
        if self._conn is not None:
            return self._conn
        try:
            conn = self._factory(self._host, self._port)
        except OSError as exc:
            raise MailError(f"Cannot reach {self._host}:{self._port}: {exc}") from exc
        try:
            conn.login(self._login, self._password)
        except imaplib.IMAP4.error as exc:
            raise MailError(
                "Authentication failed. Use an app password with the Mail (IMAP) scope from "
                "https://id.yandex.ru/security/app-passwords, and make sure IMAP is enabled "
                f"for the account at https://mail.yandex.ru/#setup/client ({_first_text(exc.args)})"
            ) from exc
        except OSError as exc:
            raise MailError(f"Login failed: {exc}") from exc
        self._conn = conn
        return conn

    def close(self) -> None:
        """Log out, swallowing errors — a failed logout must not mask a result."""
        conn, self._conn, self._selected = self._conn, None, None
        if conn is None:
            return
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            conn.logout()

    # -- plumbing -----------------------------------------------------------

    def _run(self, what: str, func: Callable[..., tuple[str, list]], *args: object) -> list:
        """Run one imaplib call, turning every failure mode into MailError."""
        try:
            typ, data = func(*args)
        except imaplib.IMAP4.error as exc:
            raise MailError(f"{what} failed: {_first_text(exc.args)}") from exc
        except OSError as exc:
            raise MailError(f"{what} failed: {exc}") from exc
        if typ != "OK":
            raise MailError(f"{what} failed: {_first_text(data) or typ}")
        return data

    def _capabilities(self) -> frozenset[str]:
        caps = getattr(self.connect(), "capabilities", ())
        return frozenset(str(c).upper() for c in caps)

    def default_folder(self) -> str:
        """The folder to use when the caller does not name one."""
        return self._allowed[0] if self._allowed else DEFAULT_FOLDER

    def check_folder(self, name: str | None) -> str:
        """Resolve and authorise a folder name against the allow-list."""
        folder = (name or "").strip() or self.default_folder()
        if self._allowed and not any(folder.lower() == a.lower() for a in self._allowed):
            raise MailError(
                f"Folder {folder!r} is not in the allowed list ({', '.join(self._allowed)})."
            )
        return folder

    def _select(self, folder: str, readonly: bool = True) -> None:
        if self._selected == (folder, readonly):
            return
        conn = self.connect()
        self._run(f"Selecting {folder}", conn.select, _quote_mailbox(folder), readonly)
        self._selected = (folder, readonly)

    def _uid(self, what: str, command: str, *args: object) -> list:
        conn = self.connect()
        return self._run(what, conn.uid, command, *args)

    # -- folders ------------------------------------------------------------

    def list_folders(self, with_counts: bool = False) -> list[Folder]:
        """All folders the plugin may use, optionally with message counts."""
        data = self._run("LIST", self.connect().list)
        folders: list[Folder] = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            folder = _parse_list_line(line)
            if folder is None:
                continue
            if self._allowed and not any(folder.name.lower() == a.lower() for a in self._allowed):
                continue
            folders.append(self._with_counts(folder) if with_counts else folder)
        return folders

    def _with_counts(self, folder: Folder) -> Folder:
        """Add MESSAGES/UNSEEN counts; a folder that refuses STATUS still lists."""
        try:
            data = self._run(
                f"STATUS {folder.name}",
                self.connect().status,
                _quote_mailbox(folder.name),
                "(MESSAGES UNSEEN)",
            )
        except MailError:
            return folder
        text = _first_text(data)
        messages = re.search(r"MESSAGES\s+(\d+)", text)
        unseen = re.search(r"UNSEEN\s+(\d+)", text)
        return Folder(
            name=folder.name,
            flags=folder.flags,
            special_use=folder.special_use,
            messages=int(messages.group(1)) if messages else None,
            unseen=int(unseen.group(1)) if unseen else None,
        )

    def find_special_folder(self, role: str) -> str | None:
        """The name of the folder playing ``role`` (``trash``, ``sent``, …)."""
        for folder in self.list_folders():
            if folder.special_use == role:
                return folder.name
        return None

    # -- reading ------------------------------------------------------------

    def search(self, folder: str, query: SearchQuery, limit: int = 25) -> list[MessageSummary]:
        """Search one folder and return the newest ``limit`` matches."""
        self._select(folder, readonly=True)
        data = self._uid("SEARCH", "SEARCH", "CHARSET", "UTF-8", *_search_criteria(query))
        uids = _first_text(data).split()
        if not uids:
            return []
        selected = uids[-limit:] if limit > 0 else uids
        summaries = self._fetch_summaries(folder, selected)
        order = {uid: index for index, uid in enumerate(selected)}
        summaries.sort(key=lambda s: order.get(s.uid, 0), reverse=True)
        return summaries

    def _fetch_summaries(self, folder: str, uids: Sequence[str]) -> list[MessageSummary]:
        data = self._uid("FETCH", "FETCH", ",".join(uids), _SUMMARY_FIELDS)
        out: list[MessageSummary] = []
        for info, headers in _iter_fetch_items(data):
            summary = _summary_from_fetch(folder, info, headers)
            if summary is not None:
                out.append(summary)
        return out

    def fetch_message(
        self, folder: str, uid: str, mark_seen: bool = False
    ) -> tuple[bytes, tuple[str, ...]]:
        """Return the raw bytes and flags of one message.

        ``mark_seen`` decides between ``BODY[]`` (which sets ``\\Seen``) and
        ``BODY.PEEK[]`` (which does not) — reading a message must not silently
        change its state.
        """
        self._select(folder, readonly=not mark_seen)
        part = "BODY[]" if mark_seen else "BODY.PEEK[]"
        data = self._uid("FETCH", "FETCH", uid, f"(UID FLAGS {part})")
        for info, raw in _iter_fetch_items(data):
            if _UID_RE.search(info):
                return raw, _flags_of(info)
        raise MailError(f"Message {uid} not found in {folder}.")

    def summary(self, folder: str, uid: str) -> MessageSummary | None:
        """The envelope of a single message, or ``None`` if it is gone."""
        self._select(folder, readonly=True)
        found = self._fetch_summaries(folder, [uid])
        return found[0] if found else None

    # -- writing ------------------------------------------------------------

    def store_flags(
        self,
        folder: str,
        uids: Sequence[str],
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> None:
        """Add and/or remove flags on the given UIDs."""
        if not uids:
            raise MailError("No message UID given.")
        self._select(folder, readonly=False)
        uid_set = ",".join(uids)
        for command, flags in (("+FLAGS", add), ("-FLAGS", remove)):
            if flags:
                self._uid("STORE", "STORE", uid_set, command, f"({' '.join(flags)})")

    def move(self, folder: str, uids: Sequence[str], destination: str) -> str:
        """Move messages to another folder. Returns how it was done.

        Prefers the server's atomic ``UID MOVE``. Without it, the copy is made
        and verified first and only then is the original flagged ``\\Deleted``
        and expunged — a failure can leave a duplicate, never a hole.
        """
        if not uids:
            raise MailError("No message UID given.")
        if normalize_email(folder) == normalize_email(destination):
            raise MailError("Source and destination folders are the same.")
        self._select(folder, readonly=False)
        uid_set = ",".join(uids)
        if "MOVE" in self._capabilities():
            self._uid("MOVE", "MOVE", uid_set, _quote_mailbox(destination))
            return "move"
        self._uid("COPY", "COPY", uid_set, _quote_mailbox(destination))
        self._uid("STORE", "STORE", uid_set, "+FLAGS", "(\\Deleted)")
        return "copy+expunge" if self._expunge(uid_set) else "copy+flagged"

    def _expunge(self, uid_set: str) -> bool:
        """Expunge exactly these UIDs; skipped (returning False) without UIDPLUS.

        A plain ``EXPUNGE`` would also remove anything else in the folder that
        happens to carry ``\\Deleted``, which is not ours to decide.
        """
        if "UIDPLUS" not in self._capabilities():
            return False
        self._uid("EXPUNGE", "EXPUNGE", uid_set)
        return True

    def delete(
        self, folder: str, uids: Sequence[str], permanent: bool = False
    ) -> dict[str, object]:
        """Delete messages: to Trash by default, irreversibly on request."""
        if not uids:
            raise MailError("No message UID given.")
        if not permanent:
            trash = self.find_special_folder("trash")
            if trash is None:
                raise MailError(
                    "No Trash folder found. Pass permanent=true to expunge instead, or move "
                    "the message to a folder of your choice."
                )
            if trash.lower() == folder.lower():
                return {"deleted": True, "method": "expunge", "folder": folder}
            self.move(folder, uids, trash)
            return {"deleted": True, "method": "trash", "trash_folder": trash}
        self._select(folder, readonly=False)
        uid_set = ",".join(uids)
        self._uid("STORE", "STORE", uid_set, "+FLAGS", "(\\Deleted)")
        if not self._expunge(uid_set):
            raise MailError(
                "The server does not support UIDPLUS, so this message can only be flagged "
                "as deleted, not expunged safely. It is now marked \\Deleted."
            )
        return {"deleted": True, "method": "expunge", "folder": folder}

    def append(
        self,
        folder: str,
        raw: bytes,
        flags: Sequence[str] = (),
        when: datetime | None = None,
    ) -> str | None:
        """Upload a message into a folder; returns its new UID when the server says.

        Not exposed as a tool — the live e2e suite uses it to plant the
        throwaway message it then reads, moves, and removes.
        """
        conn = self.connect()
        self._selected = None
        flag_text = f"({' '.join(flags)})" if flags else None
        data = self._run(
            "APPEND",
            conn.append,
            _quote_mailbox(folder),
            flag_text,
            when,
            raw,
        )
        match = _APPENDUID_RE.search(_first_text(data).encode("utf-8", "replace"))
        return match.group(1).decode("ascii") if match else None
