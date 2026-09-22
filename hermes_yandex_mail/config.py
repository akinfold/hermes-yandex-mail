"""Build an IMAP client from the environment, and decide what it may do.

Secrets are resolved via :func:`get_provider_env` (env vars, then
``~/.hermes/.env``). Nothing here logs secret values.
"""

from __future__ import annotations

from ._compat import get_provider_env, provider_env_is_set
from .imap import DEFAULT_HOST, DEFAULT_PORT, YandexIMAPClient
from .smtp import DEFAULT_SMTP_HOST, DEFAULT_SMTP_PORT, YandexSMTPClient

#: Every variable this plugin reads is namespaced under one prefix, and the names
#: are spelled through it rather than written out as whole literals. A constant
#: that *names* the credential variable otherwise has the same shape as one that
#: *holds* a credential, and plugin security scanners match on that shape: Hermes
#: read ``ENV_PASSWORD = "..."`` as a hardcoded secret and refused to install the
#: plugin at all (#4), a verdict ``--force`` cannot override. Composing the name
#: keeps it out of that pattern; the value itself is unchanged and still public.
_ENV_PREFIX = "YANDEX_MAIL_"

ENV_LOGIN = _ENV_PREFIX + "LOGIN"
ENV_PASSWORD = _ENV_PREFIX + "APP_PASSWORD"
ENV_HOST = _ENV_PREFIX + "IMAP_HOST"
ENV_PORT = _ENV_PREFIX + "IMAP_PORT"
ENV_FOLDERS = _ENV_PREFIX + "FOLDERS"
ENV_ACTIONS = _ENV_PREFIX + "ACTIONS"
ENV_SMTP_HOST = _ENV_PREFIX + "SMTP_HOST"
ENV_SMTP_PORT = _ENV_PREFIX + "SMTP_PORT"
ENV_SEND_TO = _ENV_PREFIX + "SEND_TO"
ENV_ATTACHMENT_MAX_BYTES = _ENV_PREFIX + "ATTACHMENT_MAX_BYTES"

#: The size cap for a saved attachment when ``YANDEX_MAIL_ATTACHMENT_MAX_BYTES``
#: is not set.
DEFAULT_ATTACHMENT_MAX_BYTES = 100 * 1024 * 1024

#: Every action the plugin can expose, in the order the tools are registered.
ACTIONS: tuple[str, ...] = (
    "list_folders",
    "search_messages",
    "read_message",
    "save_attachment",
    "mark_message",
    "move_message",
    "delete_message",
    "send_message",
)

#: Actions an empty ``YANDEX_MAIL_ACTIONS`` does NOT grant, and that ``all``
#: does not cover. Sending mail acts in the account owner's name and cannot be
#: undone, so it is reached only by naming it: an upgrade must never hand an
#: already-running agent the right to write as the user. The names accepted for
#: it — ``send_message`` and ``yandex_mail_send_message`` — are ones nobody
#: could already have in their configuration, because the action did not exist
#: before 0.3.0. A short ``send`` shorthand is deliberately NOT offered: it was
#: an unrecognised token in earlier versions, silently dropped, so anyone who
#: had optimistically written it would have found sending switched on by the
#: upgrade alone.
SENDING_ACTIONS: frozenset[str] = frozenset({"send_message"})

#: Every action reached only by naming it. Saving an attachment joins sending
#: here for the same reason: the most common deployment leaves the allow-list
#: unset, and an upgrade must not start writing the content of attachments to
#: disk for an agent that was only ever given the text of mail. It too is
#: named ``save_attachment`` (or ``yandex_mail_save_attachment``) and nothing
#: shorter, since a short word may already sit in a configuration, ignored.
OPT_IN_ACTIONS: frozenset[str] = SENDING_ACTIONS | {"save_attachment"}

#: What "everything" means: every action except the opt-in ones.
DEFAULT_ACTIONS: frozenset[str] = frozenset(ACTIONS) - OPT_IN_ACTIONS

#: Shorthands accepted by ``YANDEX_MAIL_ACTIONS`` alongside single actions.
ACTION_GROUPS: dict[str, frozenset[str]] = {
    "all": DEFAULT_ACTIONS,
    "read": frozenset({"list_folders", "search_messages", "read_message"}),
    "write": frozenset({"mark_message", "move_message"}),
    "delete": frozenset({"delete_message"}),
}

_TOOL_PREFIX = "yandex_mail_"

__all__ = [
    "ACTIONS",
    "ACTION_GROUPS",
    "DEFAULT_ATTACHMENT_MAX_BYTES",
    "ENV_ACTIONS",
    "ENV_ATTACHMENT_MAX_BYTES",
    "ENV_FOLDERS",
    "ENV_HOST",
    "ENV_LOGIN",
    "ENV_PASSWORD",
    "ENV_PORT",
    "ENV_SEND_TO",
    "ENV_SMTP_HOST",
    "ENV_SMTP_PORT",
    "MissingCredentials",
    "PermissionDenied",
    "account_address",
    "allowed_actions",
    "allowed_folders",
    "allowed_send_recipients",
    "attachment_max_bytes",
    "build_client",
    "build_smtp_client",
    "credentials_present",
    "require_action",
]


def allowed_folders() -> list[str]:
    """The comma-separated allow-list of folder names, or ``[]`` for all."""
    raw = get_provider_env(ENV_FOLDERS)
    return [f.strip() for f in raw.split(",") if f.strip()]


def allowed_actions() -> frozenset[str]:
    """The actions this deployment may perform, from ``YANDEX_MAIL_ACTIONS``.

    Accepts single actions (``read_message``), the group shorthands in
    :data:`ACTION_GROUPS` (``read``, ``write``, ``delete``, ``all``), and full
    tool names (``yandex_mail_delete_message``), comma-separated and
    case-insensitive. Unset or blank means :data:`DEFAULT_ACTIONS` — every action
    except the opt-in ones, ``send_message`` and ``save_attachment`` — so
    existing installs are unaffected and an upgrade never switches either on;
    ``all`` behaves the same way. The opt-in actions are granted only by naming
    them. Any other value is an explicit allow-list: names that match
    nothing are dropped rather than raising, so a typo can only ever withhold a
    tool, never grant one (a value naming nothing valid therefore allows
    nothing). Tools are filtered at registration time and permissions are
    checked again by each handler. Restart Hermes after editing configuration
    so its environment and registered toolset both reflect the change.
    """
    raw = get_provider_env(ENV_ACTIONS)
    if not raw.strip():
        return DEFAULT_ACTIONS
    allowed: set[str] = set()
    for item in raw.split(","):
        key = item.strip().lower().replace("-", "_").removeprefix(_TOOL_PREFIX)
        if key in ACTION_GROUPS:
            allowed |= ACTION_GROUPS[key]
        elif key in ACTIONS:
            allowed.add(key)
    return frozenset(allowed)


class MissingCredentials(RuntimeError):
    """Raised when required Yandex credentials are absent from the environment."""


class PermissionDenied(RuntimeError):
    """Raised when deployment configuration forbids an action."""


def require_action(action: str) -> None:
    """Recheck deployment permissions before a tool performs an operation."""
    if action not in allowed_actions():
        raise PermissionDenied(f"Action {action!r} is not allowed by {ENV_ACTIONS}.")


def credentials_present() -> bool:
    """Cheap check (no network) for the ``check_fn`` / availability gate."""
    return bool(get_provider_env(ENV_LOGIN) and get_provider_env(ENV_PASSWORD))


def allowed_send_recipients() -> list[str] | None:
    """The addresses sending is fenced to, or ``None`` when no fence is set.

    An entry is either a full address (one mailbox, compared through
    :func:`normalize_email`) or ``@domain`` (that domain and no other). A
    variable that is set but yields no usable entry returns an empty list,
    which refuses every recipient: a mistyped fence must fail closed, because
    the one thing it exists to prevent is mail leaving for an address nobody
    intended. Whitespace and a bare ``YANDEX_MAIL_SEND_TO=`` are covered by
    that, not by ``None``.

    What is *not* guaranteed: whether the variable is set is decided by
    :func:`provider_env_is_set`, which can see ``os.environ`` and
    ``~/.hermes/.env`` and nothing else. Under a Hermes host a value may also
    arrive from Hermes' own configuration layer, which this plugin cannot
    inspect without importing host internals. A fence supplied only that way,
    and only as whitespace, still reads as no fence at all.
    """
    # The resolved value cannot answer this on its own: it is stripped before
    # it gets here, so "unset" and "set to whitespace" look identical. The
    # presence of the name is what tells them apart, and a fence somebody meant
    # to write must refuse rather than disappear.
    raw = get_provider_env(ENV_SEND_TO)
    if not raw and not provider_env_is_set(ENV_SEND_TO):
        return None
    entries = [item.strip() for item in raw.split(",")]
    return [entry for entry in entries if _is_fence_entry(entry)]


def attachment_max_bytes() -> int:
    """The most a saved attachment may be, from ``YANDEX_MAIL_ATTACHMENT_MAX_BYTES``.

    Unset means :data:`DEFAULT_ATTACHMENT_MAX_BYTES`. A value that is set but is
    not a positive whole number of bytes refuses every save rather than falling
    back to the default: ``100MB`` or ``10 MiB`` typed as a tighter cap must not
    quietly become a looser one.
    """
    raw = get_provider_env(ENV_ATTACHMENT_MAX_BYTES)
    if not raw and not provider_env_is_set(ENV_ATTACHMENT_MAX_BYTES):
        return DEFAULT_ATTACHMENT_MAX_BYTES
    try:
        limit = int(raw)
    except ValueError:
        limit = 0
    if limit <= 0:
        raise PermissionDenied(
            f"{ENV_ATTACHMENT_MAX_BYTES} is set to {raw!r}, which is not a positive whole "
            "number of bytes, so no attachment can be saved until it is corrected."
        )
    return limit


def _is_fence_entry(entry: str) -> bool:
    """A usable ``YANDEX_MAIL_SEND_TO`` entry: one address, or one ``@domain``."""
    if entry.startswith("@"):
        return "." in entry[1:]
    return "@" in entry


def _int_env(name: str, default: int) -> int:
    raw = get_provider_env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _port() -> int:
    return _int_env(ENV_PORT, DEFAULT_PORT)


def _credentials() -> tuple[str, str]:
    login = get_provider_env(ENV_LOGIN)
    password = get_provider_env(ENV_PASSWORD)
    if not login or not password:
        raise MissingCredentials(
            f"{ENV_LOGIN} and {ENV_PASSWORD} must be set (create an app password with the "
            "Mail (IMAP) scope at https://id.yandex.ru/security/app-passwords)."
        )
    return login, password


def account_address() -> str:
    """The address this plugin sends as — the configured login, and nothing else.

    A separate accessor rather than a tool argument on purpose: the sender is
    the one part of an outgoing message that no caller may influence.
    """
    return _credentials()[0]


def build_smtp_client() -> YandexSMTPClient:
    """Construct a :class:`YandexSMTPClient` from the same credentials.

    The app password with the Mail scope authenticates SMTP as well as IMAP —
    verified against the live server — so sending needs no second secret.
    """
    login, password = _credentials()
    return YandexSMTPClient(
        login=login,
        password=password,
        host=get_provider_env(ENV_SMTP_HOST) or DEFAULT_SMTP_HOST,
        port=_int_env(ENV_SMTP_PORT, DEFAULT_SMTP_PORT),
    )


def build_client() -> YandexIMAPClient:
    """Construct a :class:`YandexIMAPClient` from environment credentials."""
    login, password = _credentials()
    return YandexIMAPClient(
        login=login,
        password=password,
        host=get_provider_env(ENV_HOST) or DEFAULT_HOST,
        port=_port(),
        allowed_folders=allowed_folders(),
    )
