"""IMAP client for Yandex Mail — no Hermes imports, so it stays unit-testable.

Built on the standard library's :mod:`imaplib`. Everything addresses messages by
**UID** (stable within a folder) rather than sequence number, and every failure
becomes a :class:`MailError` so the tool layer has a single thing to catch.

Data-safety rules encoded here, not left to the caller:

* every UID-scoped write (flag, move, delete) first confirms the UIDs actually
  exist in the folder — RFC 3501 §6.4.8 lets a UID command that names a
  nonexistent UID succeed silently, so without this a stale UID from an
  earlier turn would be reported as acted on while nothing happened;
* a move uses the server's ``UID MOVE`` when available, otherwise ``COPY``
  first and only then ``\\Deleted`` + expunge — RFC 3501 makes a COPY that
  answers OK atomic, so a failure can leave a duplicate, never a hole;
* expunging is always ``UID EXPUNGE`` (UIDPLUS), which touches only the UIDs we
  name — a bare ``EXPUNGE`` would also erase messages someone else flagged
  ``\\Deleted`` in that folder;
* deletion means "move to Trash" unless the caller explicitly asks otherwise.
"""

from __future__ import annotations

import contextlib
import imaplib
import re
import ssl
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from types import TracebackType

from . import imap_utf7
from .message import addresses, decode_header_value, header_date_iso, parse_message_bytes
from .mime import MimePart, flatten_parts, response_fields

__all__ = [
    "DEFAULT_FOLDER",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "Folder",
    "MailError",
    "MessageSummary",
    "MoveResult",
    "SearchQuery",
    "SearchResult",
    "YandexIMAPClient",
    "normalize_email",
    "same_folder",
]

DEFAULT_HOST = "imap.yandex.ru"
DEFAULT_PORT = 993
DEFAULT_FOLDER = "INBOX"
_MAX_MESSAGE_BYTES = 10 * 1024 * 1024

#: Seconds to wait on the connection and on every subsequent socket operation.
#: Without it a stalled server hangs the tool call — and therefore the agent —
#: forever, since imaplib blocks indefinitely by default.
DEFAULT_TIMEOUT = 30

#: Yandex hands out several interchangeable domains for the same mailbox.
_YANDEX_DOMAINS = frozenset(
    {"ya.ru", "yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "narod.ru"}
)
_CANONICAL_DOMAIN = "yandex.ru"


def _default_connection(host: str, port: int) -> imaplib.IMAP4:
    """Open a TLS connection that actually verifies who it is talking to.

    ``imaplib.IMAP4_SSL`` with no ``ssl_context`` falls back to
    ``ssl._create_stdlib_context()``, which sets ``verify_mode=CERT_NONE`` and
    ``check_hostname=False`` — the connection is encrypted but unauthenticated,
    so anyone able to intercept it can present their own certificate and read
    the app password and every message. ``ssl.create_default_context()``
    verifies the chain and the hostname.

    A private or self-signed CA is supplied the standard way, through the
    ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` environment variables that
    ``create_default_context`` already honours; there is deliberately no
    setting here for turning verification off.
    """
    return imaplib.IMAP4_SSL(
        host, port, ssl_context=ssl.create_default_context(), timeout=DEFAULT_TIMEOUT
    )


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


def same_folder(a: str, b: str) -> bool:
    """Are these two names the same mailbox?

    A dedicated comparison instead of routing folder names through
    :func:`normalize_email`: that function canonicalises e-mail *addresses*
    (folding Yandex' interchangeable domains onto one), and it "worked" for
    folder names only by accident, because it also lower-cases. ``INBOX`` is
    case-insensitive per RFC 3501; every other name is compared the same way
    here too, since that is how folder resolution matches them below.
    """
    return a.strip().casefold() == b.strip().casefold()


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


class SearchResult(list):
    """The messages ``search`` selected for one page, plus the total matched.

    A ``list`` subclass rather than a new return shape: every existing
    caller that treats a search result as ``list[MessageSummary]`` keeps
    working unchanged (iteration, ``==``, truthiness all still work), while
    a caller that needs to know whether more messages exist beyond the
    page it asked for can read ``.total``.
    """

    def __init__(self, messages: Iterable[MessageSummary], total: int) -> None:
        super().__init__(messages)
        self.total = total


@dataclass(frozen=True)
class MoveResult:
    """What happened when messages were moved to another folder.

    ``destination_uids`` maps each source UID to the UID it was verified to
    receive in the destination folder — never a bare positional tuple. A
    tuple silently mismatched in two ways: two moved messages sharing one
    Message-ID (routine for newsletters) collapsed onto the same value
    instead of their own, and a message with no Message-ID left a hole that
    shifted every UID after it into the wrong slot. A source UID absent from
    the mapping means it could not be verified — ``imaplib`` does not surface
    ``COPYUID`` for ``UID MOVE`` the way it does for ``COPY``, so an
    unverified message is reported as missing, never guessed.
    """

    method: str
    destination_uids: dict[str, str] = field(default_factory=dict)

    @property
    def original_removed(self) -> bool:
        """Did the source message actually go?

        ``copy+flagged`` means the copy landed but the server could not
        expunge the original, which is still in the source folder carrying
        ``\\Deleted``. Callers must be able to say "moved" only when that is
        true, rather than reading it out of the method name.
        """
        return self.method != "copy+flagged"


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
    "корзина": "trash",
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
    if any(control in value for control in ("\r", "\n", "\x00")):
        raise MailError("Search values cannot contain CR, LF, or NUL control characters.")
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


def _page(uids: list[str], limit: int, offset: int) -> list[str]:
    """The ascending-order slice of ``uids`` holding one page of results.

    ``uids`` is SEARCH's ascending-UID order, so the newest matches sit at
    the end. Paging skips ``offset`` of those newest first (trimming them
    off the end), then keeps the last ``limit`` of what remains — still in
    ascending order, which is what ``search`` already expects to reverse
    when it sorts the fetched summaries newest-first.

    An ``offset`` at or beyond the total is clamped to an empty page rather
    than sliced with ``uids[: len(uids) - offset]``: once ``offset`` exceeds
    ``len(uids)`` that index goes negative, and Python reinterprets a
    negative stop as "count from the end" — silently turning "page past the
    end" into "the oldest few matches again", which a pager walking forward
    with a growing offset would read as already-seen mail resurfacing as new.
    """
    if offset >= len(uids):
        return []
    trimmed = uids[: len(uids) - offset] if offset > 0 else uids
    return trimmed[-limit:] if limit > 0 else trimmed


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
_MESSAGE_ID_FIELDS = "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"


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
        self._factory = connection_factory or _default_connection
        self._conn: imaplib.IMAP4 | None = None
        self._caps: frozenset[str] = frozenset()
        self._selected: tuple[str, bool] | None = None
        #: All server folders, resolved once per connection; see _cached_folder_list.
        self._folder_cache: list[Folder] | None = None

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
        # Read before anything selects a mailbox: imaplib fills `capabilities`
        # from the pre-authentication greeting and never refreshes it, while a
        # server that advertises MOVE or UIDPLUS only after LOGIN puts them in
        # the untagged CAPABILITY that `imaplib.select()` then flushes. Missing
        # them would silently downgrade every move to copy-and-flag and make
        # permanent delete impossible.
        self._caps = self._read_capabilities(conn)
        return conn

    @staticmethod
    def _read_capabilities(conn: imaplib.IMAP4) -> frozenset[str]:
        caps = {str(c).upper() for c in getattr(conn, "capabilities", ())}
        untagged = getattr(conn, "untagged_responses", {}).get("CAPABILITY") or []
        for line in untagged:
            text = line.decode("ascii", "replace") if isinstance(line, bytes) else str(line)
            caps |= {token.upper() for token in text.split()}
        return frozenset(caps)

    def close(self) -> None:
        """Log out, swallowing errors — a failed logout must not mask a result."""
        conn, self._conn, self._selected = self._conn, None, None
        self._caps = frozenset()
        self._folder_cache = None
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
        """What the server said it can do, captured once at login."""
        self.connect()
        return self._caps

    def default_folder(self) -> str:
        """The folder to use when the caller does not name one."""
        return self._allowed[0] if self._allowed else DEFAULT_FOLDER

    def check_folder(self, name: str | None) -> str:
        """Default and sanity-check a folder name, without touching the network.

        This is the fast pre-check ``tool.py`` runs before a connection even
        exists, so it can only reject names that could *never* resolve to an
        allowed folder. A role word or localized synonym (``spam``,
        ``удалённые``) might still resolve to an allowed folder once the
        server's real folder list is known — those are let through here, and
        ``_select`` has the final, authoritative say once it can check the
        name it actually resolved to.
        """
        folder = (name or "").strip() or self.default_folder()
        if self._allowed and not self._maybe_allowed(folder):
            raise MailError(
                f"Folder {folder!r} is not in the allowed list ({', '.join(self._allowed)})."
            )
        return folder

    def _maybe_allowed(self, folder: str) -> bool:
        if any(same_folder(folder, a) for a in self._allowed):
            return True
        return folder.strip().lower() in _SPECIAL_NAMES

    def _select(self, folder: str, readonly: bool = True) -> str:
        """Resolve, authorize, and select or EXAMINE ``folder``.

        Returns the resolved folder name, so a caller that only had the
        caller-supplied spelling (e.g. a role word) can report back or
        complain about the server's exact one.
        """
        resolved = self._resolve_and_authorize(folder)
        self._select_resolved(resolved, readonly)
        return resolved

    def _select_resolved(self, resolved: str, readonly: bool) -> None:
        """SELECT/EXAMINE a folder name that is already resolved — and, when
        the allow-list should not gate it here, already deliberately
        authorized by the caller instead of by the generic check. Trash is
        the one such case: exempted from the allow-list on purpose by
        ``_resolve_trash_for_delete``, re-running ``_resolve_and_authorize``
        here on the way to selecting it would defeat that exemption.

        Skips the round trip if already selected. The cache is cleared
        *before* attempting the SELECT, not after a failure — per RFC 3501 a
        failed SELECT deselects whatever was selected before it and leaves no
        mailbox selected at all, so leaving the old cache entry in place
        would have it claim a folder is still selected when the server
        disagrees. A subsequent call would then skip a SELECT it actually
        needs and send a UID command with nothing selected. Clearing first
        means the cache can only ever under-claim — worst case, one
        redundant SELECT — never over-claim.
        """
        if self._selected == (resolved, readonly):
            return
        self._selected = None
        conn = self.connect()
        self._run(f"Selecting {resolved}", conn.select, _quote_mailbox(resolved), readonly)
        self._selected = (resolved, readonly)

    def _capture_response_int(self, name: str) -> int | None:
        """Read one untagged numeric response after a SELECT/EXAMINE, e.g.
        ``UIDNEXT``. Never raises: an absent or unparsable value must not
        break the select that already succeeded — it just means the caller
        cannot use it.
        """
        try:
            _typ, data = self.connect().response(name)
            raw = data[0] if data else None
            return int(_first_text(raw)) if raw else None
        except Exception:
            return None

    # -- folder resolution ---------------------------------------------------

    def resolve_folder(self, name: str) -> str:
        """The server's exact spelling of ``name``, allow-list enforced.

        A thin public wrapper around the resolution ``_select`` already does
        internally, for callers that need to report back the same name the
        server will actually use — the point where a multi-turn conversation
        converges on a folder name that is guaranteed to resolve again next
        time, instead of echoing back whatever spelling or case the caller
        happened to supply. Needs a connection (LIST may run to resolve a
        role word or synonym), unlike the network-free ``check_folder``.
        """
        return self._resolve_and_authorize(name)

    def _resolve_and_authorize(self, name: str) -> str:
        """The server's real spelling of ``name``, checked against the allow-list.

        This is the point where the allow-list is authoritative: ``name`` may
        be a role word or synonym that ``check_folder`` could not verify.
        """
        resolved = self._resolve_folder(name)
        if self._allowed and not self._is_allowed(resolved):
            raise MailError(
                f"Folder {resolved!r} is not in the allowed list ({', '.join(self._allowed)})."
            )
        return resolved

    def _is_allowed(self, resolved: str) -> bool:
        """Is ``resolved`` — an exact, server-spelled folder name — allow-listed?

        Checked exactly first. Falling straight to a case-insensitive compare
        (as this used to) is unsafe: with both ``Archive`` and ``archive`` on
        the server and only ``Archive`` allow-listed, a caller asking for
        ``archive`` resolves — correctly, ``_match_by_name`` prefers an exact
        match — to the *other*, non-allow-listed mailbox, and a case-folded
        comparison would then wave it through as if it were the allowed one.
        The case-insensitive fallback below is taken only when it cannot be
        confused with a sibling: if more than one server folder folds to the
        same name, none of them get a case-insensitive pass, since there is
        no way to tell which one the allow-list entry actually meant.
        """
        if any(resolved == a for a in self._allowed):
            return True
        if not any(same_folder(resolved, a) for a in self._allowed):
            return False
        siblings = sum(1 for f in self._cached_folder_list() if same_folder(f.name, resolved))
        return siblings <= 1

    def _resolve_folder(self, name: str) -> str:
        """Map a caller-given folder name onto the server's exact spelling.

        IMAP mailbox names are case-sensitive except ``INBOX`` — Yandex
        rejects ``SELECT "spam"`` with ``[CLIENTBUG] No such folder`` while
        ``SELECT "Spam"`` works. Tried in order: an exact match, a
        case-insensitive match, and a role word or localized synonym
        (``spam``, ``удалённые``) resolved through the same table
        ``special_use`` detection uses.
        """
        if name.strip().upper() == "INBOX":
            return "INBOX"
        folders = self._cached_folder_list()
        resolved = self._match_by_name(folders, name) or self._match_by_role(folders, name)
        if resolved is None:
            raise MailError(self._no_such_folder_message(name, folders))
        return resolved

    def _match_by_name(self, folders: list[Folder], name: str) -> str | None:
        for folder in folders:
            if folder.name == name:
                return folder.name
        for folder in folders:
            if same_folder(folder.name, name):
                return folder.name
        return None

    def _match_by_role(self, folders: list[Folder], name: str) -> str | None:
        role = _SPECIAL_NAMES.get(name.strip().lower(), "")
        if not role:
            return None
        for folder in folders:
            if folder.special_use == role:
                return folder.name
        return None

    def _no_such_folder_message(self, name: str, folders: list[Folder]) -> str:
        names = {f.name for f in folders}
        if self._allowed:
            names = {n for n in names if any(same_folder(n, a) for a in self._allowed)}
        available = ", ".join(sorted(names)) or "none"
        return f"No such folder {name!r}. Available folders: {available}."

    def _cached_folder_list(self) -> list[Folder]:
        """All server folders, fetched once per connection and reused.

        Resolving several folder names across a multi-step conversation then
        costs one LIST, not one per call. This is separate from the allow-list
        -filtered view ``list_folders`` returns, which keeps its own call.
        """
        if self._folder_cache is None:
            data = self._run("LIST", self.connect().list)
            folders: list[Folder] = []
            for line in data:
                if not isinstance(line, bytes):
                    continue
                folder = _parse_list_line(line)
                if folder is not None:
                    folders.append(folder)
            self._folder_cache = folders
        return self._folder_cache

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
            # Same exact-then-case-insensitive rule the allow-list is enforced
            # with, so this cannot advertise a folder a later call refuses.
            if self._allowed and not self._is_allowed(folder.name):
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
        """The name of the folder playing ``role`` (``trash``, ``sent``, …),
        resolved from the full, unfiltered, cached server folder list —
        deliberately not the allow-list-``list_folders()``.

        Two reasons: performance — reusing ``_cached_folder_list()`` costs no
        extra LIST when a folder has already been resolved earlier in the
        same call, where ``list_folders()`` always issues a fresh one — and
        correctness for ``_delete_to_trash``, which needs to find Trash even
        when ``YANDEX_MAIL_FOLDERS`` excludes it: Trash is this tool's own
        safety net for "delete", not a folder the agent chose, so a
        deployment fencing off everything but INBOX (the README's
        recommended safest setup) must not lose it and fall back to
        pointing at an irreversible expunge instead.
        """
        for folder in self._cached_folder_list():
            if folder.special_use == role:
                return folder.name
        return None

    def find_flagged_folder(self, role: str) -> str | None:
        """Like :meth:`find_special_folder`, but only the server's own
        special-use FLAG counts — never the folder's name.

        :meth:`_special_use` falls back to matching well-known names when a
        server sends no flag, which is right for answering "what is this
        folder for?" but wrong for anything that bypasses the allow-list: an
        ordinary user folder called ``Корзина`` or ``trash`` would otherwise
        capture the role and become a delete destination the deployment
        explicitly fenced off.
        """
        for folder in self._cached_folder_list():
            if any(_SPECIAL_FLAGS.get(flag.lower()) == role for flag in folder.flags):
                return folder.name
        return None

    # -- reading ------------------------------------------------------------

    def message_parts(self, folder: str, uid: str) -> list[MimePart]:
        """Read MIME metadata without fetching any text or attachment payload."""
        self._select(self.check_folder(folder), readonly=True)
        data = self._uid("FETCH structure", "FETCH", uid, "(UID BODYSTRUCTURE)")
        try:
            structure = response_fields(data, uid).get("BODYSTRUCTURE")
            if not isinstance(structure, list):
                raise ValueError("Server did not return BODYSTRUCTURE.")
            return flatten_parts(structure)
        except (ValueError, TypeError, IndexError) as exc:
            raise MailError(f"Cannot parse MIME structure: {exc}") from exc

    def iter_part(self, folder: str, uid: str, part_id: str) -> Iterator[bytes]:
        """Stream one encoded MIME section in bounded, non-mutating requests."""
        if not re.fullmatch(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", part_id):
            raise MailError("Invalid MIME part_id.")
        self._select(self.check_folder(folder), readonly=True)
        offset, count = 0, 65536
        while True:
            data = self._uid(
                "FETCH part", "FETCH", uid, f"(UID BODY.PEEK[{part_id}]<{offset}.{count}>)"
            )
            try:
                fields = response_fields(data, uid, literal_bytes=True)
            except (ValueError, TypeError, IndexError) as exc:
                raise MailError(f"Cannot parse MIME part response: {exc}") from exc
            raw = fields.get(f"BODY[{part_id}]<{offset}>")
            if not isinstance(raw, bytes):
                raise MailError("Server did not return the requested MIME part range.")
            if len(raw) > count:
                raise MailError("Server exceeded the MIME chunk size limit.")
            yield raw
            if len(raw) < count:
                return
            offset += len(raw)

    def search(
        self, folder: str, query: SearchQuery, limit: int = 25, offset: int = 0
    ) -> SearchResult:
        """Search one folder for the newest ``limit`` matches, ``offset`` many
        of the newest skipped first, plus the total number matched.

        The page is sliced out of the UID list SEARCH returns, before any
        FETCH — a page deep into old mail still costs exactly one SEARCH and
        ``limit`` FETCHes, never a FETCH for a UID that is skipped or lies
        beyond the page.
        """
        self._select(folder, readonly=True)
        data = self._uid("SEARCH", "SEARCH", "CHARSET", "UTF-8", *_search_criteria(query))
        uids = _first_text(data).split()
        page = _page(uids, limit, offset)
        if not page:
            return SearchResult([], len(uids))
        summaries = self._fetch_summaries(folder, page)
        order = {uid: index for index, uid in enumerate(page)}
        summaries.sort(key=lambda s: order.get(s.uid, 0), reverse=True)
        return SearchResult(summaries, len(uids))

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
        data = self._uid("FETCH", "FETCH", uid, f"(UID FLAGS {part}<0.{_MAX_MESSAGE_BYTES + 1}>)")
        for info, raw in _iter_fetch_items(data):
            if _UID_RE.search(info):
                if len(raw) > _MAX_MESSAGE_BYTES:
                    raise MailError(
                        f"Message {uid} exceeds the {_MAX_MESSAGE_BYTES // (1024 * 1024)} MiB "
                        "raw read limit."
                    )
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
        self._verify_uids_exist(folder, uids, readonly=False)
        uid_set = ",".join(uids)
        for command, flags in (("+FLAGS", add), ("-FLAGS", remove)):
            if flags:
                self._uid("STORE", "STORE", uid_set, command, f"({' '.join(flags)})")

    def _verify_uids_exist(self, folder: str, uids: Sequence[str], readonly: bool) -> None:
        """Raise MailError naming any UID in ``uids`` that is not actually in
        ``folder``, before anything else touches them.

        RFC 3501 §6.4.8: a UID command that names a UID which does not exist
        is not an error — the server silently ignores that UID and still
        answers OK. Without this check, marking or deleting a stale UID (left
        over from an earlier conversation turn, or a message another client
        already moved or removed) reports success while doing nothing.
        All-or-nothing rather than best-effort: if only some of the given
        UIDs exist, the whole batch is refused instead of silently acting on
        the subset, so the caller is never told a partial result was
        complete — the error names exactly which UIDs to drop and retry with.
        """
        resolved = self._select(folder, readonly=readonly)
        data = self._uid("FETCH", "FETCH", ",".join(uids), "(UID)")
        found = self._existing_uids(data)
        missing = [uid for uid in uids if uid not in found]
        if missing:
            raise self._missing_uids_error(resolved, missing)

    @staticmethod
    def _existing_uids(data: Iterable[object]) -> set[str]:
        """The UIDs present in a FETCH response.

        Tolerant of both shapes imaplib hands back: a bare ``b'n (UID x)'``
        line — what a FETCH with no literal-bearing field returns, which is
        what the plain ``(UID)`` existence check sends — and an
        ``(info, literal)`` tuple for fields that do carry one, so this can
        also read existence off a fuller fetch without a second round trip.
        """
        found: set[str] = set()
        for item in data:
            info = item[0] if isinstance(item, tuple) and item else item
            if isinstance(info, bytes):
                match = _UID_RE.search(info)
                if match:
                    found.add(match.group(1).decode("ascii"))
        return found

    @staticmethod
    def _missing_uids_error(folder: str, missing: Sequence[str]) -> MailError:
        return MailError(
            f"UID(s) {', '.join(missing)} not found in {folder!r} — the message may already "
            "have been deleted or moved by another client, or the UID is from an earlier, "
            "now-stale result. No changes were made; retry with only the UIDs that still exist."
        )

    def move(self, folder: str, uids: Sequence[str], destination: str) -> MoveResult:
        """Move messages to another folder.

        Prefers the server's atomic ``UID MOVE``. Without it the copy is made
        first, and only once the server has answered OK to it — which RFC 3501
        makes an atomic guarantee that the copy exists — is the original
        flagged ``\\Deleted`` and expunged. A failure can leave a duplicate,
        never a hole.

        The destination assigns new UIDs, so ``Message-ID`` headers and the
        destination's ``UIDNEXT`` are captured before the move, and every UID
        assigned from there on is re-fetched and matched afterwards — see
        :meth:`_find_destination_uids` for why (Yandex does not support
        ``SEARCH HEADER MESSAGE-ID``).
        """
        if not uids:
            raise MailError("No message UID given.")
        resolved_folder = self._resolve_and_authorize(folder)
        resolved_destination = self._resolve_and_authorize(destination)
        return self._move_resolved(resolved_folder, uids, resolved_destination)

    def _move_resolved(
        self, resolved_folder: str, uids: Sequence[str], resolved_destination: str
    ) -> MoveResult:
        """The move itself, once both folder names are resolved and authorized.

        Split out from :meth:`move` so ``_delete_to_trash`` can move into
        Trash even when the allow-list excludes it: Trash is this tool's own
        safety net, authorized once by ``_delete_to_trash`` itself rather
        than a folder the agent freely chooses, while every other caller
        (a plain ``move``) still goes through the full allow-list check
        there. Compares the two resolved names with plain equality, not
        :func:`same_folder`: both are already the server's exact spelling at
        this point, and case-folding them could treat two distinct,
        case-differing mailboxes as the same one.
        """
        if resolved_folder == resolved_destination:
            raise MailError("Source and destination folders are the same.")
        message_ids = self._message_ids_for(resolved_folder, uids, readonly=False)
        uidnext = self._destination_uidnext(resolved_destination)
        # Capturing the destination's UIDNEXT selected the destination, so the
        # source must be selected again before the move command names UIDs in
        # it — without this the move would act on the wrong mailbox.
        self._select_resolved(resolved_folder, readonly=False)
        uid_set = ",".join(uids)
        method = self._move_or_copy(uid_set, resolved_destination)
        destination_uids = self._find_destination_uids(
            resolved_destination, uids, message_ids, uidnext
        )
        return MoveResult(method=method, destination_uids=destination_uids)

    def _move_or_copy(self, uid_set: str, destination: str) -> str:
        if "MOVE" in self._capabilities():
            self._uid("MOVE", "MOVE", uid_set, _quote_mailbox(destination))
            return "move"
        self._uid("COPY", "COPY", uid_set, _quote_mailbox(destination))
        self._uid("STORE", "STORE", uid_set, "+FLAGS", "(\\Deleted)")
        return "copy+expunge" if self._expunge(uid_set) else "copy+flagged"

    def _message_ids_for(
        self, resolved: str, uids: Sequence[str], readonly: bool = True
    ) -> dict[str, str]:
        """Message-ID header for each of ``uids`` that has one, keyed by its
        (source) UID. Selects the folder itself — this must not depend on the
        caller having already selected it — and is read before a move, the
        only reliable way to re-find the messages by their new UID afterwards.

        Takes a name already resolved AND authorized by the caller, and
        selects it without re-running the allow-list check: re-authorizing
        here would undo ``_delete_to_trash``'s deliberate bypass and silently
        strip the destination lookup from every soft delete on a deployment
        that fences YANDEX_MAIL_FOLDERS.

        Doubles as the move's UID-existence check (see
        :meth:`_verify_uids_exist`): every requested UID that does not come
        back in this fetch is reported to the caller rather than the move
        silently proceeding on whatever subset does exist, and reusing this
        read costs no extra round trip.
        """
        self._select_resolved(resolved, readonly)
        summaries = self._fetch_summaries(resolved, uids)
        found = {s.uid for s in summaries}
        missing = [uid for uid in uids if uid not in found]
        if missing:
            raise self._missing_uids_error(resolved, missing)
        return {s.uid: s.message_id for s in summaries if s.message_id}

    def _destination_uidnext(self, destination: str) -> int | None:
        """EXAMINE the destination and read its UIDNEXT before the move, so the
        post-move lookup can be scoped to exactly the messages that just
        arrived. ``None`` if the folder cannot be examined or sends none."""
        try:
            self._select_resolved(destination, readonly=True)
        except MailError:
            return None
        return self._capture_response_int("UIDNEXT")

    def _find_destination_uids(
        self,
        destination: str,
        uids: Sequence[str],
        message_ids: dict[str, str],
        uidnext: int | None,
    ) -> dict[str, str]:
        """Best-effort source-UID -> destination-UID mapping for the just-moved
        messages.

        Yandex does not support ``SEARCH HEADER MESSAGE-ID`` (it answers
        ``[UNAVAILABLE] UID SEARCH Backend error``), so this ranges over every
        UID assigned since the move started — from the UIDNEXT captured
        beforehand — and matches ``Message-ID`` headers exactly instead. That
        is deterministic and unaffected by other mail landing in the
        destination in between. A lookup failure must not fail the move,
        which has already succeeded, and a source UID that cannot be
        verified is simply absent from the mapping rather than guessed.

        Matched in ascending numeric UID order on both sides, not the order
        ``uids`` was given in: two moved messages can share one Message-ID
        (routine for newsletters and list digests), and only pairing the
        Nth-smallest source UID with the Nth-smallest arrival sharing that
        Message-ID — the order a UID set is processed in on both ends — keeps
        the two from collapsing onto the same reported destination UID.
        """
        if not message_ids or uidnext is None:
            return {}
        try:
            self._select_resolved(destination, readonly=True)
            data = self._uid("FETCH", "FETCH", f"{uidnext}:*", _MESSAGE_ID_FIELDS)
        except MailError:
            return {}
        arrived = self._message_ids_by_uid(data)
        result: dict[str, str] = {}
        for uid in sorted(uids, key=int):
            message_id = message_ids.get(uid)
            candidates = arrived.get(message_id) if message_id else None
            if candidates:
                result[uid] = candidates.pop(0)
        return result

    def _message_ids_by_uid(self, data: Iterable[object]) -> dict[str, list[str]]:
        """Map Message-ID -> the destination UIDs carrying it, oldest first.

        A list per Message-ID rather than a single UID: last-write-wins would
        collapse two newly-arrived messages that share one Message-ID onto
        whichever was seen last, mis-mapping the other back to the wrong
        source UID (or dropping it as unverified). Consumed in the same
        ascending order by :meth:`_find_destination_uids`.
        """
        result: dict[str, list[str]] = {}
        for info, headers in _iter_fetch_items(data):
            summary = _summary_from_fetch("", info, headers)
            if summary is not None and summary.message_id:
                result.setdefault(summary.message_id, []).append(summary.uid)
        return result

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
        if permanent:
            return self._delete_permanently(folder, uids)
        return self._delete_to_trash(folder, uids)

    def _delete_to_trash(self, folder: str, uids: Sequence[str]) -> dict[str, object]:
        resolved_folder = self._resolve_and_authorize(folder)
        trash = self._resolve_trash_for_delete()
        if trash == resolved_folder:
            # Compared exactly, not with same_folder: a server can have both
            # 'Trash' (flagged \Trash) and a distinct, literally-named
            # 'trash' folder, and case-folding them here would treat two
            # different mailboxes as one, wrongly refusing to move mail
            # between them.
            #
            # Claiming success here without acting was the original bug: the
            # message never moved, and the caller was told it was deleted.
            return {
                "deleted": False,
                "reason": "already_in_trash",
                "folder": trash,
                "note": (
                    "The message is already in Trash and was left untouched. "
                    "Erasing it permanently is a separate, irreversible action."
                ),
            }
        result = self._move_resolved(resolved_folder, uids, trash)
        return {
            # False when the original survived the move (copy+flagged): the
            # message now exists in both folders and the caller must not be
            # told the delete completed.
            "deleted": result.original_removed,
            # The real method, never a hard-coded "trash": "copy+flagged"
            # means the copy landed in Trash but the original is still in
            # its source folder, merely flagged \Deleted — the caller needs
            # to know that, not a claim that the source message is gone.
            "method": result.method,
            "trash_folder": trash,
            "destination_uids": result.destination_uids,
        }

    def _resolve_trash_for_delete(self) -> str:
        """Trash's exact server name, bypassing the allow-list.

        Trash is this tool's own safety net for "delete", not a folder the
        agent chooses — restricting YANDEX_MAIL_FOLDERS to, say, INBOX (the
        README's recommended safest setup) must not silently turn every
        delete into an unrecoverable expunge just because Trash itself is
        not on the allow-list. A plain ``move`` call that names Trash
        explicitly is unaffected and still allow-list gated (see
        :meth:`move`).
        """
        trash = self.find_flagged_folder("trash")
        if trash is None:
            raise MailError(
                "This account has no folder flagged \\Trash, so messages cannot be "
                "soft-deleted. Move the message to a folder of your choice with "
                "yandex_mail_move_message, or pass permanent=true if an irreversible "
                "delete is really what is wanted."
            )
        return trash

    def _delete_permanently(self, folder: str, uids: Sequence[str]) -> dict[str, object]:
        # Existence and capability are both checked before anything is sent:
        # a refusal here must leave the message exactly as it was, never
        # flagged \Deleted first and only then found unable to expunge it.
        self._verify_uids_exist(folder, uids, readonly=False)
        if "UIDPLUS" not in self._capabilities():
            raise MailError(
                "The server does not support UIDPLUS, so this message cannot be expunged "
                "safely — a plain EXPUNGE could also remove other mail already flagged "
                "\\Deleted in this folder. No changes were made."
            )
        uid_set = ",".join(uids)
        self._uid("STORE", "STORE", uid_set, "+FLAGS", "(\\Deleted)")
        try:
            self._expunge(uid_set)
        except MailError:
            # The flag was ours and the erase did not happen, so take it back:
            # leaving \Deleted set would hide the message in most clients and
            # let the next client-issued EXPUNGE purge it — a delete the user
            # was told had failed. Best-effort; the original error still wins.
            with contextlib.suppress(MailError):
                self._uid("STORE", "STORE", uid_set, "-FLAGS", "(\\Deleted)")
            raise
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
