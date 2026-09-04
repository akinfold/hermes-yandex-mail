"""Standalone Hermes tools for Yandex Mail.

Each handler has the signature ``handle(args: dict, **kwargs) -> str``, returns
a JSON string, and NEVER raises — every failure path becomes
``{"error": "..."}`` so the agent gets a usable message instead of a crash.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .config import MissingCredentials, PermissionDenied, build_client, require_action
from .imap import Folder, MailError, MessageSummary, SearchQuery, YandexIMAPClient
from .mime import MimePart
from .paging import decoded_chunks, take_page, text_chunks

TOOLSET = "yandex_mail"

# -- schemas ----------------------------------------------------------------

_FOLDER_HINT = (
    "Folder name as returned by yandex_mail_list_folders (e.g. 'INBOX', 'Sent', 'Spam'). "
    "Omit for the default folder (INBOX)."
)
# Required on every UID-scoped tool (read/mark/move/delete): a UID is only meaningful
# inside the folder it came from, and UID numbering is independent per folder — reusing
# a folder name from an earlier turn can silently operate on a different message.
_REQUIRED_FOLDER_HINT = (
    "The folder the message is in, exactly as a previous result reported it (e.g. the "
    "'folder' field from yandex_mail_search_messages or yandex_mail_read_message). Do not "
    "guess or reuse a folder name from an earlier turn."
)
# A single string, not an array: strict function-calling validators reject union
# item types, and one comma-separated field keeps batch operations expressible.
_UID_HINT = (
    "Message UID from a previous result, e.g. yandex_mail_search_messages. Several may be "
    "given comma-separated ('101,102'). A UID is only meaningful together with the 'folder' "
    "that same result reported for it — always pass both."
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
            "offset": {
                "type": "integer",
                "description": (
                    "Skip this many of the newest matches before taking 'limit' — page "
                    "through results, e.g. offset=25 for the page after the first 25. "
                    "Check the previous result's 'total' to know whether another page exists."
                ),
            },
        },
        "required": [],
    },
}

READ_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_read_message",
    "description": (
        "Read a page of message text plus headers and attachment metadata. Text offsets "
        "count decoded characters; pass next_offset to continue. Attachments are not "
        "downloaded. Does not mark the message as read unless you ask it to."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {
                "type": "string",
                "description": (
                    "The UID of the message to read, from a previous result. Must be paired "
                    "with the 'folder' that result reported the UID in."
                ),
            },
            "folder": {"type": "string", "description": _REQUIRED_FOLDER_HINT},
            "offset": {"type": "integer", "description": "Decoded character offset (default 0)."},
            "mark_read": {
                "type": "boolean",
                "description": (
                    "Mark the message as read while opening it (false by default). "
                    "Requires the mark_message action to be allowed."
                ),
            },
            "max_chars": {
                "type": "integer",
                "description": "Body character limit (default 20000, maximum 100000).",
            },
        },
        "required": ["uid", "folder"],
    },
}

ATTACHMENT_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_read_attachment",
    "description": (
        "Read a page of one attachment as base64. Use part_id from read_message's "
        "attachment list and next_offset to continue. Offsets and limits count decoded "
        "file bytes, not base64 characters. Does not save files or mark mail as read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": _UID_HINT},
            "folder": {"type": "string", "description": _REQUIRED_FOLDER_HINT},
            "part_id": {"type": "string", "description": "Attachment part_id from read_message."},
            "offset": {"type": "integer", "description": "Decoded byte offset (default 0)."},
            "limit": {
                "type": "integer",
                "description": "Page size in bytes (default 49152, maximum 262144).",
            },
        },
        "required": ["uid", "folder", "part_id"],
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
            "folder": {"type": "string", "description": _REQUIRED_FOLDER_HINT},
            "read": {"type": "boolean", "description": "true marks as read, false as unread."},
            "flagged": {
                "type": "boolean",
                "description": "true flags (stars) the message, false removes the flag.",
            },
        },
        "required": ["uid", "folder"],
    },
}

MOVE_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_move_message",
    "description": (
        "Move one or more messages to another folder. The copy is created before the "
        "original is removed, so a failure can leave a duplicate but never lose the "
        "message. UIDs change on arrival: the result maps each source UID to the UID "
        "the message was verified to have in the destination — use that, do not guess."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": _UID_HINT},
            "destination": {
                "type": "string",
                "description": "Destination folder name, from yandex_mail_list_folders.",
            },
            "folder": {
                "type": "string",
                "description": f"Source folder — {_REQUIRED_FOLDER_HINT}",
            },
        },
        "required": ["uid", "destination", "folder"],
    },
}

DELETE_SCHEMA: dict[str, Any] = {
    "name": "yandex_mail_delete_message",
    "description": (
        "Delete one or more messages. By default they are moved to the Trash folder and can "
        "still be recovered. A message already in Trash is left untouched. Erasing a message "
        "permanently is a separate, irreversible action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": _UID_HINT},
            "folder": {"type": "string", "description": _REQUIRED_FOLDER_HINT},
            "permanent": {
                "type": "boolean",
                "description": (
                    "Erase the message instead of moving it to Trash. This cannot be undone."
                ),
            },
        },
        "required": ["uid", "folder"],
    },
}


# -- helpers ----------------------------------------------------------------

_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100
_DEFAULT_MAX_CHARS = 20000
_MAX_CHARS = 100000


def _error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def with_action_guard(action: str, handler: Any) -> Any:
    """Recheck a registered tool's permission immediately before each call."""

    def guarded(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            require_action(action)
        except PermissionDenied as exc:
            return _error(str(exc))
        return handler(args, **kwargs)

    return guarded


def _uids(args: dict[str, Any]) -> list[str]:
    """Parse the ``uid`` argument into a list of numeric UIDs."""
    raw = str(args.get("uid") or "").replace(";", ",").replace(" ", ",")
    uids = [part.strip() for part in raw.split(",") if part.strip()]
    if not uids:
        raise ValueError("'uid' is required.")
    invalid = [u for u in uids if not u.isdigit()]
    if invalid:
        raise ValueError(f"Not a message UID: {', '.join(invalid)}. UIDs are numbers.")
    # Normalised before de-duplication: the server answers "UID 8", so a
    # model writing "008" would have its own, existing message reported
    # missing — and with all-or-nothing batching, take the rest down with it.
    # De-duplicated too, order preserved, so a repeated UID neither becomes
    # "UID STORE 8,8" nor is counted twice in the reported result.
    return list(dict.fromkeys(str(int(u)) for u in uids))


def _required_folder(args: dict[str, Any]) -> str:
    """The ``folder`` argument, required for anything UID-scoped.

    A UID is only meaningful together with the folder it came from — UID
    numbering is independent per folder on this server. The schema already
    marks ``folder`` required, but a caller can still omit it; this is the
    belt-and-suspenders check that turns an omission into a clear error
    instead of silently landing on the default folder.
    """
    folder = str(args.get("folder") or "").strip()
    if not folder:
        raise ValueError(
            "'folder' is required: pass back the exact folder name the previous result reported."
        )
    return folder


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
        offset = _int_arg(args, "offset", 0)
        with build_client() as client:
            folder = client.resolve_folder(client.check_folder(args.get("folder")))
            result = client.search(folder, _query_from_args(args), limit=limit, offset=offset)
        return _dump(
            {
                "folder": folder,
                "offset": offset,
                "count": len(result),
                "total": result.total,
                "messages": [_summary_to_dict(m) for m in result],
            }
        )
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error searching messages: {exc}")


def _read_payload(
    client: YandexIMAPClient, folder: str, uid: str, args: dict[str, Any]
) -> dict[str, Any]:
    offset = _offset(args)
    max_chars = _int_arg(args, "max_chars", _DEFAULT_MAX_CHARS, _MAX_CHARS)
    parts = client.message_parts(folder, uid)
    selected, is_html = _readable_parts(parts)
    body, eof = take_page(_body_chunks(client, folder, uid, selected), offset, max_chars, "")
    summary = client.summary(folder, uid)
    payload: dict[str, Any] = (
        _summary_to_dict(summary) if summary else {"uid": uid, "folder": folder}
    )
    if args.get("mark_read"):
        _mark_read(client, folder, uid, payload)
    payload.update(
        body=body,
        body_from_html=is_html,
        truncated=not eof,
        offset=offset,
        next_offset=None if eof else offset + len(body),
        eof=eof,
        attachments=[part.attachment_info() for part in parts if part.attachment],
    )
    return payload


def _readable_parts(parts: list[MimePart]) -> tuple[list[MimePart], bool]:
    readable = [part for part in parts if not part.attachment]
    plain = [part for part in readable if part.content_type == "text/plain"]
    if plain:
        return plain, False
    html = [part for part in readable if part.content_type == "text/html"]
    return html, bool(html)


def _mark_read(client: YandexIMAPClient, folder: str, uid: str, payload: dict[str, Any]) -> None:
    client.store_flags(folder, [uid], add=["\\Seen"])
    payload["flags"] = list(dict.fromkeys([*payload.get("flags", []), "\\Seen"]))
    payload["unread"] = False


def _offset(args: dict[str, Any]) -> int:
    value = args.get("offset", 0)
    if type(value) is not int or value < 0:
        raise ValueError("'offset' must be a non-negative integer.")
    return value


def _body_chunks(client: YandexIMAPClient, folder: str, uid: str, parts: list[MimePart]):
    for index, part in enumerate(parts):
        if index:
            yield "\n"
        raw = client.iter_part(folder, uid, part.part_id)
        decoded = decoded_chunks(raw, part.encoding)
        yield from text_chunks(decoded, part.charset, html=part.content_type == "text/html")


def handle_read_attachment(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        uid, offset = _uids(args)[0], _offset(args)
        folder_arg = _required_folder(args)
        limit = _int_arg(args, "limit", 49152, 262144)
        with build_client() as client:
            folder = client.resolve_folder(client.check_folder(folder_arg))
            parts = client.message_parts(folder, uid)
            part = next(
                (part for part in parts if part.part_id == args.get("part_id") and part.attachment),
                None,
            )
            if part is None:
                raise ValueError("Attachment part_id was not found in this message.")
            raw = client.iter_part(folder, uid, part.part_id)
            data, eof = take_page(decoded_chunks(raw, part.encoding), offset, limit, b"")
        return _dump(
            {
                "uid": uid,
                "folder": folder,
                **part.attachment_info(),
                "offset": offset,
                "next_offset": None if eof else offset + len(data),
                "eof": eof,
                "bytes_returned": len(data),
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
        )
    except (MissingCredentials, MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error reading attachment: {exc}")


def handle_read(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        uid = _uids(args)[0]
        folder_arg = _required_folder(args)
        if args.get("mark_read"):
            require_action("mark_message")
        with build_client() as client:
            folder = client.resolve_folder(client.check_folder(folder_arg))
            return _dump({"message": _read_payload(client, folder, uid, args)})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, PermissionDenied, ValueError) as exc:
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
        folder_arg = _required_folder(args)
        add, remove = _flag_changes(args)
        if not add and not remove:
            return _error("Nothing to change: provide 'read' and/or 'flagged'.")
        with build_client() as client:
            folder = client.resolve_folder(client.check_folder(folder_arg))
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
        folder_arg = _required_folder(args)
        with build_client() as client:
            folder = client.resolve_folder(client.check_folder(folder_arg))
            target = client.resolve_folder(client.check_folder(destination))
            result = client.move(folder, uids, target)
        return _dump(
            {
                # False when the copy landed but the original could not be
                # removed: the headline field is what a model reads first, and
                # it must not say the move completed when a duplicate is left
                # behind — "method" alone is one key too far away.
                "moved": result.original_removed,
                "original_removed": result.original_removed,
                "uids": uids,
                "from": folder,
                "to": target,
                "method": result.method,
                # The mapping itself, not list(...): that would emit the
                # dict's KEYS — the source UIDs — under a name promising
                # destination ones, which is exactly the wrong-identifier
                # bug this field exists to prevent.
                "destination_uids": dict(result.destination_uids),
            }
        )
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error moving message: {exc}")


def handle_delete(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        uids = _uids(args)
        folder_arg = _required_folder(args)
        with build_client() as client:
            folder = client.resolve_folder(client.check_folder(folder_arg))
            result = client.delete(folder, uids, permanent=bool(args.get("permanent")))
        return _dump({**result, "uids": uids, "folder": folder})
    except MissingCredentials as exc:
        return _error(str(exc))
    except (MailError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error deleting message: {exc}")
