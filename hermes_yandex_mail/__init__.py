"""Yandex Mail plugin for Hermes Agent.

Registers standalone tools that talk to Yandex Mail over IMAP: list folders,
search, read, flag, move, and delete messages. Which of them appear is governed
by ``YANDEX_MAIL_ACTIONS`` (see :func:`config.allowed_actions`). Uses RELATIVE
imports so it loads both as a dropped-in directory plugin
(``hermes_plugins.yandex_mail``) and as a pip package.
"""

from __future__ import annotations

from typing import Any

from . import tool
from .config import ENV_LOGIN, ENV_PASSWORD, allowed_actions, credentials_present

__version__ = "0.2.0"

__all__ = ["__version__", "register"]

_REQUIRES_ENV = [ENV_LOGIN, ENV_PASSWORD]

# (action, schema, handler, description, emoji) — the action names are the ones
# YANDEX_MAIL_ACTIONS accepts.
_TOOLS: tuple[tuple[str, dict, Any, str, str], ...] = (
    (
        "list_folders",
        tool.LIST_FOLDERS_SCHEMA,
        tool.handle_list_folders,
        "List Yandex Mail folders with unread and total message counts.",
        "📂",
    ),
    (
        "search_messages",
        tool.SEARCH_SCHEMA,
        tool.handle_search,
        "Search a Yandex Mail folder by sender, subject, text, date, or unread state.",
        "🔎",
    ),
    (
        "read_message",
        tool.READ_SCHEMA,
        tool.handle_read,
        "Read a Yandex Mail message: headers, text body, and attachment list.",
        "📧",
    ),
    (
        "mark_message",
        tool.MARK_SCHEMA,
        tool.handle_mark,
        "Mark Yandex Mail messages read/unread or flagged/unflagged.",
        "🔖",
    ),
    (
        "move_message",
        tool.MOVE_SCHEMA,
        tool.handle_move,
        "Move Yandex Mail messages to another folder.",
        "📤",
    ),
    (
        "delete_message",
        tool.DELETE_SCHEMA,
        tool.handle_delete,
        "Delete Yandex Mail messages (to Trash by default).",
        "🗑️",
    ),
)


def register(ctx: Any) -> None:
    """Called by Hermes at load time with a PluginContext.

    Only the tools whose action is allowed are registered, so a disallowed
    action is not merely refused — the agent never sees the tool at all.
    """
    allowed = allowed_actions()
    for action, schema, handler, description, emoji in _TOOLS:
        if action not in allowed:
            continue
        ctx.register_tool(
            name=schema["name"],
            toolset=tool.TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=credentials_present,
            requires_env=_REQUIRES_ENV,
            description=description,
            emoji=emoji,
        )
