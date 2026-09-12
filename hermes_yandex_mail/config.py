"""Build an IMAP client from the environment, and decide what it may do.

Secrets are resolved via :func:`get_provider_env` (env vars, then
``~/.hermes/.env``). Nothing here logs secret values.
"""

from __future__ import annotations

from ._compat import get_provider_env
from .imap import DEFAULT_HOST, DEFAULT_PORT, YandexIMAPClient

ENV_LOGIN = "YANDEX_MAIL_LOGIN"
ENV_PASSWORD = "YANDEX_MAIL_APP_PASSWORD"
ENV_HOST = "YANDEX_MAIL_IMAP_HOST"
ENV_PORT = "YANDEX_MAIL_IMAP_PORT"
ENV_FOLDERS = "YANDEX_MAIL_FOLDERS"
ENV_ACTIONS = "YANDEX_MAIL_ACTIONS"

#: Every action the plugin can expose, in the order the tools are registered.
ACTIONS: tuple[str, ...] = (
    "list_folders",
    "search_messages",
    "read_message",
    "mark_message",
    "move_message",
    "delete_message",
)

#: Shorthands accepted by ``YANDEX_MAIL_ACTIONS`` alongside single actions.
ACTION_GROUPS: dict[str, frozenset[str]] = {
    "all": frozenset(ACTIONS),
    "read": frozenset({"list_folders", "search_messages", "read_message"}),
    "write": frozenset({"mark_message", "move_message"}),
    "delete": frozenset({"delete_message"}),
}

_TOOL_PREFIX = "yandex_mail_"

__all__ = [
    "ACTIONS",
    "ACTION_GROUPS",
    "ENV_ACTIONS",
    "ENV_FOLDERS",
    "ENV_HOST",
    "ENV_LOGIN",
    "ENV_PASSWORD",
    "ENV_PORT",
    "MissingCredentials",
    "PermissionDenied",
    "allowed_actions",
    "allowed_folders",
    "build_client",
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
    case-insensitive. Unset or blank means every action, so existing installs
    are unaffected. Any other value is an explicit allow-list: names that match
    nothing are dropped rather than raising, so a typo can only ever withhold a
    tool, never grant one (a value naming nothing valid therefore allows
    nothing). Tools are filtered at registration time and permissions are
    checked again by each handler. Restart Hermes after editing configuration
    so its environment and registered toolset both reflect the change.
    """
    raw = get_provider_env(ENV_ACTIONS)
    if not raw.strip():
        return frozenset(ACTIONS)
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


def _port() -> int:
    raw = get_provider_env(ENV_PORT)
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def build_client() -> YandexIMAPClient:
    """Construct a :class:`YandexIMAPClient` from environment credentials."""
    login = get_provider_env(ENV_LOGIN)
    password = get_provider_env(ENV_PASSWORD)
    if not login or not password:
        raise MissingCredentials(
            f"{ENV_LOGIN} and {ENV_PASSWORD} must be set (create an app password with the "
            "Mail (IMAP) scope at https://id.yandex.ru/security/app-passwords)."
        )
    return YandexIMAPClient(
        login=login,
        password=password,
        host=get_provider_env(ENV_HOST) or DEFAULT_HOST,
        port=_port(),
        allowed_folders=allowed_folders(),
    )
