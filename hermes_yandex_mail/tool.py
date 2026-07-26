"""Standalone Hermes tools for Yandex Mail.

Each handler has the signature ``handle(args: dict, **kwargs) -> str``, returns
a JSON string, and NEVER raises — every failure path becomes
``{"error": "..."}`` so the agent gets a usable message instead of a crash.
"""

from __future__ import annotations

import json
from typing import Any

from .config import MissingCredentials, build_client
from .imap import Folder, MailError, MessageSummary, SearchQuery, YandexIMAPClient
from .message import extract_body, parse_message_bytes

TOOLSET = "yandex_mail"

# -- schemas ----------------------------------------------------------------

_FOLDER_HINT = (
    "Folder name as returned by yandex_mail_list_folders (e.g. 'INBOX', 'Sent', 'Spam'). "
    "Omit for the default folder (INBOX)."
)
# A single string, not an array: strict function-calling validators reject union
# item types, and one comma-separated field keeps batch operations expressible.
_UID_HINT = (
    "Message UID from yandex_mail_search_messages. Several may be given "
    "comma-separated ('101,102'). A UID is only meaningful together with its folder."
)
_DATE_HINT = "ISO 8601 date, e.g. '2026-07-25'."

LIST_FOLDERS_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_list_folders",
    "description": (
        "List the mail folders this plugin can use, with their role (inbox, sent, trash, "
        "junk, drafts, archive) and, by default, how many messages each holds and how many "
        "are unread. Use a returned name as the 'folder' argument of the other tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_counts": {
                "type": "boolean",
                "description": "Include total/unread message counts (true by default).",
            },
        },
        "required": [],
    },
}

SEARCH_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_search_messages",
    "description": (
        "Search a mail folder and return the newest matching messages: subject, sender, "
        "recipients, date, size, flags, and the UID needed to read, flag, move, or delete "
        "them. All criteria are combined with AND; omit them all to list the latest messages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "folder": {"type": "string", "description": _FOLDER_HINT},
            "from": {"type": "string", "description": "Match the sender (substring of From)."},
            "to": {"type": "string", "description": "Match a recipient (substring of To)."},
            "subject": {"type": "string", "description": "Match the subject (substring)."},
            "text": {
                "type": "string",
                "description": "Match anywhere in the message, headers and body included.",
            },
            "since": {
                "type": "string",
                "description": f"Only messages on or after this date. {_DATE_HINT}",
            },
            "before": {
                "type": "string",
                "description": f"Only messages before this date. {_DATE_HINT}",
            },
            "unread_only": {"type": "boolean", "description": "Only unread messages."},
            "flagged_only": {"type": "boolean", "description": "Only flagged (starred) messages."},
            "limit": {
                "type": "integer",
                "description": "How many of the newest matches to return (default 25, max 100).",
            },
        },
        "required": [],
    },
}

READ_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_read_message",
    "description": (
        "Read one message in full: headers, the text body (an HTML-only message is converted "
        "to text), and the list of attachments (name, type, size). Does not mark the message "
        "as read unless you ask it to."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": "The UID of the message to read."},
            "folder": {"type": "string", "description": _FOLDER_HINT},
            "mark_read": {
                "type": "boolean",
                "description": "Mark the message as read while opening it (false by default).",
            },
            "max_chars": {
                "type": "integer",
                "description": "Truncate the body to this many characters (default 20000).",
            },
        },
        "required": ["uid"],
    },
}

MARK_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_mark_message",
    "description": (
        "Change the state of one or more messages: mark them read or unread, flagged "
        "(starred) or unflagged. Only the properties you provide are changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": _UID_HINT},
            "folder": {"type": "string", "description": _FOLDER_HINT},
            "read": {"type": "boolean", "description": "true marks as read, false as unread."},
            "flagged": {
                "type": "boolean",
                "description": "true flags (stars) the message, false removes the flag.",
            },
        },
        "required": ["uid"],
    },
}

MOVE_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_move_message",
    "description": (
        "Move one or more messages to another folder. The copy is created before the "
        "original is removed, so a message is never lost in transit. Note that UIDs change "
        "on arrival — search the destination folder if you need the new ones."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": _UID_HINT},
            "destination": {
                "type": "string",
                "description": "Destination folder name, from yandex_mail_list_folders.",
            },
            "folder": {"type": "string", "description": f"Source folder. {_FOLDER_HINT}"},
        },
        "required": ["uid", "destination"],
    },
}

DELETE_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_delete_message",
    "description": (
        "Delete one or more messages. By default they are moved to the Trash folder and can "
        "still be recovered; pass permanent=true to erase them irreversibly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": _UID_HINT},
            "folder": {"type": "string", "description": _FOLDER_HINT},
            "permanent": {
                "type": "boolean",
                "description": (
                    "Erase the message instead of moving it to Trash. This cannot be undone."
                ),
            },
        },
        "required": ["uid"],
    },
}


# -- helpers ----------------------------------------------------------------

_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100
_DEFAULT_MAX_CHARS = 20000


def _error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _uids(args: dict[str, Any]) -> list[str]:
    """Parse the ``uid`` argument into a list of numeric UIDs."""
    raw = str(args.get("uid") or "").replace(";", ",").replace(" ", ",")
    uids = [part.strip() for part in raw.split(",") if part.strip()]
    if not uids:
        raise ValueError("'uid' is required.")
    invalid = [u for u in uids if not u.isdigit()]
    if invalid:
        raise ValueError(f"Not a message UID: {', '.join(invalid)}. UIDs are numbers.")
    return uids


def _int_arg(args: dict[str, Any], name: str, default: int, maximum: int | None = None) -> int:
    value = args.get(name)
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{name}' must be a number.") from exc
    if number <= 0:
        return default
    return min(number, maximum) if maximum else number


def _folder_to_dict(folder: Folder) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": folder.name, "role": folder.special_use or None}
    if folder.messages is not None:
        payload["messages"] = folder.messages
    if folder.unseen is not None:
        payload["unread"] = folder.unseen
    return payload


def _summary_to_dict(summary: MessageSummary) -> dict[str, Any]:
    return {
        "uid": summary.uid,
        "folder": summary.folder,
        "subject": summary.subject,
        "from": summary.from_,
        "to": summary.to,
        "cc": summary.cc,
        "date": summary.date,
        "size": summary.size,
        "unread": not summary.seen,
        "flagged": summary.flagged,
        "answered": summary.answered,
        "flags": list(summary.flags),
    }


def _query_from_args(args: dict[str, Any]) -> SearchQuery:
    return SearchQuery(
        from_addr=str(args.get("from") or ""),
        to=str(args.get("to") or ""),
        subject=str(args.get("subject") or ""),
        text=str(args.get("text") or ""),
        since=str(args.get("since") or ""),
        before=str(args.get("before") or ""),
        unread_only=bool(args.get("unread_only")),
        flagged_only=bool(args.get("flagged_only")),
    )


# -- handlers ---------------------------------------------------------------


def handle_list_folders(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        counts = args.get("include_counts")
        with build_client() as client:
            folders = client.list_folders(with_counts=counts is None or bool(counts))
        return _dump({"count": len(folders), "folders": [_folder_to_dict(f) for f in folders]})
    except MissingCredentials as exc:
        return _error(str(exc))
    except MailError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error listing folders: {exc}")


def handle_search(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        limit = _int_arg(args, "limit", _DEFAULT_LIMIT, _MAX_LIMIT)
        with build_client() as client:
            folder = client.check_folder(args.get("folder"))
            messages = client.search(folder, _query_from_args(args), limit=limit)
        return _dump(
            {
                "folder": folder,
                "count": len(messages),
                "messages": [_summary_to_dict(m) for m in messages],
            }
        )
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error searching messages: {exc}")


def _read_payload(client: YandexIMAPClient, folder: str, args: dict[str, Any]) -> dict[str, Any]:
    uid = _uids(args)[0]
    max_chars = _int_arg(args, "max_chars", _DEFAULT_MAX_CHARS)
    raw, flags = client.fetch_message(folder, uid, mark_seen=bool(args.get("mark_read")))
    parsed = parse_message_bytes(raw)
    body = extract_body(parsed, max_chars=max_chars)
    summary = client.summary(folder, uid)
    payload: dict[str, Any] = (
        _summary_to_dict(summary) if summary else {"uid": uid, "folder": folder}
    )
    payload["flags"] = list(flags) or payload.get("flags", [])
    payload["body"] = body.text
    payload["body_from_html"] = body.is_html
    payload["truncated"] = body.truncated
    payload["attachments"] = [
        {"filename": a.filename, "content_type": a.content_type, "size": a.size}
        for a in body.attachments
    ]
    return payload


def handle_read(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        with build_client() as client:
            folder = client.check_folder(args.get("folder"))
            return _dump({"message": _read_payload(client, folder, args)})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error reading message: {exc}")


def _flag_changes(args: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Translate the requested state into flags to add and flags to remove."""
    add: list[str] = []
    remove: list[str] = []
    if "read" in args and args["read"] is not None:
        (add if bool(args["read"]) else remove).append("\\Seen")
    if "flagged" in args and args["flagged"] is not None:
        (add if bool(args["flagged"]) else remove).append("\\Flagged")
    return add, remove


def handle_mark(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        uids = _uids(args)
        add, remove = _flag_changes(args)
        if not add and not remove:
            return _error("Nothing to change: provide 'read' and/or 'flagged'.")
        with build_client() as client:
            folder = client.check_folder(args.get("folder"))
            client.store_flags(folder, uids, add=add, remove=remove)
        return _dump(
            {"marked": True, "folder": folder, "uids": uids, "added": add, "removed": remove}
        )
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error marking message: {exc}")


def handle_move(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        uids = _uids(args)
        destination = str(args.get("destination") or "").strip()
        if not destination:
            return _error("'destination' is required.")
        with build_client() as client:
            folder = client.check_folder(args.get("folder"))
            target = client.check_folder(destination)
            method = client.move(folder, uids, target)
        return _dump({"moved": True, "uids": uids, "from": folder, "to": target, "method": method})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error moving message: {exc}")


def handle_delete(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        uids = _uids(args)
        with build_client() as client:
            folder = client.check_folder(args.get("folder"))
            result = client.delete(folder, uids, permanent=bool(args.get("permanent")))
        return _dump({**result, "uids": uids, "folder": folder})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error deleting message: {exc}")
