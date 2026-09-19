"""Build an outgoing message, and refuse anything that could address it wrongly.

Pure: no Hermes, no sockets, no IMAP session — fixture bytes in, an
:class:`email.message.EmailMessage` out, so every rule here is unit-testable.

The rules exist because the caller is a language model whose context contains
attacker-authored text. Three of them carry the weight:

* **An address is a bare addr-spec, never a display name.** ``getaddresses`` is
  not used on caller input at all: ``"bob@example.org <alice@evil.org>"`` parses
  as *two* addresses, so a display name copied out of a hostile ``From:`` would
  silently add an envelope recipient. One comma-separated token here yields
  exactly one address or an error — the count can never grow.
* **Every value that reaches a header passes :func:`header_safe`.** That
  includes the ones lifted off the wire (an original's ``Subject``,
  ``Message-ID`` and ``References``), not only the ones the caller typed: a
  header is attacker-controlled data exactly like a body is.
* **Refuse, never sanitise.** A value that cannot be represented safely stops
  the send. Quietly repairing it would send *something* to *someone*, and this
  is the one tool in the plugin whose mistakes cannot be taken back.
"""

from __future__ import annotations

import re
from email import policy
from email.headerregistry import HeaderRegistry, UnstructuredHeader
from email.message import EmailMessage
from email.utils import formatdate

from .imap import ReplyAnchor, normalize_email

__all__ = [
    "MAX_BODY_CHARS",
    "MAX_RECIPIENTS",
    "MAX_SUBJECT_CHARS",
    "ComposeError",
    "ReplyAnchor",
    "build_message",
    "header_safe",
    "parse_recipients",
    "recipient_allowed",
    "recipient_sources",
    "reply_subject",
    "thread_headers",
    "validate_address",
]

#: One call addresses a handful of people. A reply-all storm, a mailing list
#: pasted into an argument, or a loop that keeps appending recipients all hit
#: this before the socket opens.
MAX_RECIPIENTS = 10
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 100_000
#: RFC 5322 allows an unbounded References chain; a hostile one is unbounded in
#: practice. Threading survives trimming — the trailing ids are the ones that
#: matter — so keep the last few and drop the rest.
MAX_REFERENCES = 20

#: Where a header ends, as Python itself decides it. ``policy.default`` splits
#: on ``str.splitlines()``, which is CR and LF plus VT, FF, the file/group/record
#: separators, NEL, U+2028 and U+2029 — so testing for CR and LF alone would
#: leave the stdlib, not this module, deciding what a header break is.
_NUL = "\x00"
#: Characters that only ever appear in an address as structure, never inside a
#: bare addr-spec: brackets and quotes delimit display names, parentheses start
#: comments, and comma/colon/semicolon separate addresses and groups. Any of
#: them left in a token means the token is not one plain address.
_ADDRESS_STRUCTURE = set('<>,"();:\\[]')
_ENCODED_WORD = "=?"
#: Printable ASCII only, and no angle brackets inside. Anything else would be
#: re-encoded on the way out (see :class:`_MessageIdHeader`) into something no
#: client threads on, so it is refused rather than silently transformed.
_ID_CHARS = r"[\x21-\x3b\x3d\x3f-\x7e]"
_MSG_ID_RE = re.compile(rf"^<{_ID_CHARS}{{1,250}}@{_ID_CHARS}{{1,250}}>$")


#: Where a folded header line is wrapped. Well under RFC 5322's 998-octet hard
#: limit, and a single over-long id still fits on a line of its own.
_FOLD_WIDTH = 78


class _MessageIdHeader(UnstructuredHeader):
    """``In-Reply-To`` / ``References``: ids emitted verbatim, folded between them.

    ``policy.default`` classifies both as *unstructured* text, so its folder
    RFC 2047-encodes any token it cannot fit on a line — and an ordinary Outlook
    ``Message-ID`` is 81 characters. The reply then goes out carrying
    ``In-Reply-To: =?utf-8?q?=3CAM0PR…?=``, which no mail client threads on,
    while the tool reads the header back *decoded* and reports a correctly
    threaded reply that was never sent. Folding only at the spaces between ids
    is both legal and lossless: a References chain is a list, and the only place
    it may be broken is between its elements.
    """

    def fold(self, *, policy: object) -> str:
        lines = [f"{self.name}:"]
        for token in str(self).split():
            if len(lines[-1]) + 1 + len(token) > _FOLD_WIDTH and lines[-1] != f"{self.name}:":
                lines.append("")
            lines[-1] = f"{lines[-1]} {token}"
        separator = getattr(policy, "linesep", "\n")
        return separator.join(lines) + separator


_HEADERS = HeaderRegistry()
_HEADERS.map_to_type("in-reply-to", _MessageIdHeader)
_HEADERS.map_to_type("references", _MessageIdHeader)
_POLICY = policy.default.clone(header_factory=_HEADERS)


class ComposeError(RuntimeError):
    """A message that must not be sent as asked."""


def header_safe(value: str, field: str) -> str:
    """Return *value* unchanged, or refuse it for containing header structure.

    CR, LF and NUL are how one header becomes two: a ``Subject`` carrying
    ``"\\r\\nBcc: collect@evil.example"`` would otherwise add a recipient that no
    part of this module ever saw. Deliberately the twin of ``imap._quoted()``'s
    guard on the IMAP side, and deliberately a named function: one test
    enumerates every field that has to pass through it.
    """
    text = str(value)
    if len(text.splitlines()) > 1 or _NUL in text:
        raise ComposeError(
            f"{field} contains a line break or a NUL character, which cannot appear in a "
            "mail header. Nothing was sent."
        )
    return text


def _bare_token(value: str, field: str) -> str:
    """Strip the one optional ``<...>`` wrapper a caller may legitimately write."""
    text = header_safe(value, field).strip()
    if not text:
        raise ComposeError(f"{field} is empty. Nothing was sent.")
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    return text


def _require_plain_address(text: str, field: str) -> None:
    """Refuse anything that is more than one bare addr-spec.

    ASCII only: Yandex' SMTP server does not advertise ``SMTPUTF8`` (verified
    against the live server), so an internationalised address cannot be
    transmitted at all. Saying so here gives the agent a sentence it can relay,
    instead of an exception from inside ``smtplib`` or a mangled address.
    """
    if not text.isascii():
        raise ComposeError(
            f"{field} {text!r} contains non-ASCII characters. This server cannot send to "
            "internationalised addresses. Nothing was sent."
        )
    if any(char.isspace() for char in text) or _ADDRESS_STRUCTURE & set(text):
        raise ComposeError(
            f"{field} {text!r} is not a plain address. Pass the address on its own — "
            "'bob@example.org', not 'Bob <bob@example.org>'. Nothing was sent."
        )
    if _ENCODED_WORD in text:
        # ``=?`` and ``?=`` are ordinary address characters, so an encoded word
        # passes every check above — and then ``policy.default`` decodes it when
        # rendering the To: header, so one token the envelope treats as a single
        # recipient reaches the reader as two. The envelope is unaffected, but
        # the message everyone reads, and the copy filed in Sent, would name
        # someone the caller never did.
        raise ComposeError(
            f"{field} {text!r} contains an encoded word, which is not part of an address. "
            "Nothing was sent."
        )


def _require_mailbox_shape(text: str, field: str) -> None:
    """One ``@``, a non-empty local part, and a domain with real labels."""
    local, sep, domain = text.rpartition("@")
    if not sep or not local or not domain or "@" in local:
        raise ComposeError(
            f"{field} {text!r} is not an e-mail address: it needs exactly one '@'. "
            "Nothing was sent."
        )
    labels = domain.split(".")
    if len(labels) < 2 or not all(labels):
        raise ComposeError(f"{field} {text!r} has no valid domain. Nothing was sent.")


def validate_address(value: str, field: str) -> str:
    """Check one bare address, returning it exactly as written."""
    text = _bare_token(value, field)
    _require_plain_address(text, field)
    _require_mailbox_shape(text, field)
    return text


def parse_recipients(raw: str, field: str = "'to'") -> list[str]:
    """Split a comma-separated list into validated bare addresses.

    One token in, one address out — never more. That invariant is the whole
    defence against a display name smuggling a second envelope recipient past
    the caller's, and the reader's, notice.
    """
    tokens = [part.strip() for part in str(raw or "").split(",")]
    tokens = [part for part in tokens if part]
    if not tokens:
        raise ComposeError(f"{field} is required: name at least one recipient. Nothing was sent.")
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        address = validate_address(token, field)
        key = normalize_email(address)
        if key in seen:
            continue
        seen.add(key)
        out.append(address)
    if len(out) > MAX_RECIPIENTS:
        raise ComposeError(
            f"{field} names {len(out)} recipients; at most {MAX_RECIPIENTS} are allowed in one "
            "message. Nothing was sent."
        )
    return out


def recipient_allowed(address: str, entries: list[str] | None) -> bool:
    """Is *address* inside the operator's recipient fence?

    ``None`` means no fence was configured. An empty list means one was
    configured and nothing in it parsed — that fails closed, because a
    mistyped fence must not read as "send anywhere".

    A plain entry matches one mailbox, folded through :func:`normalize_email`
    so ``@ya.ru`` and ``@yandex.ru`` are the same account. An ``@domain`` entry
    matches that domain and no other: equality on the whole domain, never a
    suffix test, so ``@example.org`` refuses ``evil-example.org``,
    ``sub.example.org`` and ``example.org.evil.com`` alike.
    """
    if entries is None:
        return True
    domain = address.rpartition("@")[2].casefold()
    folded = normalize_email(address)
    for entry in entries:
        if entry.startswith("@"):
            if domain == entry[1:].casefold():
                return True
        elif folded == normalize_email(entry):
            return True
    return False


def reply_subject(original: str) -> str:
    """``Re:`` the original subject, without stacking a second ``Re:``."""
    subject = header_safe(original, "the original subject").strip()
    if not subject:
        return "Re:"
    if subject[:3].casefold() == "re:":
        return subject
    return f"Re: {subject}"


def thread_headers(anchor: ReplyAnchor) -> tuple[str, str]:
    """``(In-Reply-To, References)`` for a reply to *anchor*.

    Both are built only from ids that look like ids. A ``Message-ID`` the
    server handed back malformed is refused rather than propagated, because a
    reply that cannot be threaded onto the message it claims to answer is a
    reply to nothing — and the caller asked to answer something specific.
    """
    message_id = header_safe(anchor.message_id, "the original Message-ID").strip()
    if not _MSG_ID_RE.match(message_id):
        raise ComposeError(
            "The message being replied to has no usable Message-ID, so this reply cannot be "
            "threaded onto it. Nothing was sent."
        )
    previous = [
        token
        for token in header_safe(anchor.references, "the original References").split()
        if _MSG_ID_RE.match(token)
    ]
    chain = [*previous, message_id]
    if len(chain) > MAX_REFERENCES:
        chain = chain[-MAX_REFERENCES:]
    return message_id, " ".join(chain)


def recipient_sources(recipients: list[str], anchor: ReplyAnchor, account: str) -> dict[str, str]:
    """Where each recipient of a reply stands in relation to the original.

    Always returned in full, never abbreviated to "nothing unusual": an absent
    key would be read as an absent problem, and the interesting case — an
    address that was on the original's ``Cc`` or ``Reply-To`` but is neither
    its sender nor one of its ``To`` recipients — is exactly the one an
    attacker arranges. ``Cc`` and ``Reply-To`` are free for a sender to write
    and prove nothing about who was delivered to, so they do not confer
    ``from`` or ``to`` standing here; the account's own address is excluded so
    that copying yourself cannot make a stranger look like a participant.
    """
    own = normalize_email(account)
    senders = {normalize_email(a) for a in anchor.from_} - {own}
    addressed = {normalize_email(a) for a in anchor.to} - {own}
    mentioned = {normalize_email(a) for a in anchor.reply_to} - {own}
    sources: dict[str, str] = {}
    for address in recipients:
        folded = normalize_email(address)
        if folded == own:
            sources[address] = "self"
        elif folded in senders:
            sources[address] = "from"
        elif folded in addressed:
            sources[address] = "to"
        elif folded in mentioned:
            sources[address] = "reply_to_only"
        else:
            sources[address] = "new"
    return sources


def _content_encoding(body: str) -> str:
    """Pick a transfer encoding that survives the wire without bloating.

    Quoted-printable triples the size of Cyrillic text (every byte becomes
    ``=D0``), so non-ASCII goes base64. ASCII stays as it is unless a line is
    long enough to risk the 998-octet limit.
    """
    if not body.isascii():
        return "base64"
    if any(len(line) > 900 for line in body.splitlines()):
        return "quoted-printable"
    return "7bit"


def build_message(
    *,
    sender: str,
    recipients: list[str],
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str = "",
    references: str = "",
) -> EmailMessage:
    """Assemble the message. Every header value has already been checked."""
    if len(body) > MAX_BODY_CHARS:
        raise ComposeError(
            f"'body' is {len(body)} characters; at most {MAX_BODY_CHARS} are allowed. "
            "Nothing was sent."
        )
    if len(subject) > MAX_SUBJECT_CHARS:
        raise ComposeError(
            f"'subject' is {len(subject)} characters; at most {MAX_SUBJECT_CHARS} are allowed. "
            "Nothing was sent."
        )
    message = EmailMessage(policy=_POLICY)
    message["From"] = validate_address(sender, "the sending address")
    message["To"] = ", ".join(recipients)
    message["Subject"] = header_safe(subject, "'subject'")
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = header_safe(message_id, "the outgoing Message-ID")
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = references
    message.set_content(body, subtype="plain", charset="utf-8", cte=_content_encoding(body))
    _require_header_round_trip(message, recipients)
    return message


def _require_header_round_trip(message: EmailMessage, recipients: list[str]) -> None:
    """The To: a reader will see must name exactly who the envelope names.

    Every value here has been checked, but the check happens before
    :class:`~email.message.EmailMessage` renders it — and rendering is not the
    identity. Reading the header back is the only thing that catches a value
    that passes inspection and then becomes something else, so the guarantee
    holds against the next such quirk as well as the one already known.
    """
    rendered = [address.addr_spec for address in message["To"].addresses]
    if rendered != recipients:
        raise ComposeError(
            "The recipients could not be written to the message without changing them "
            f"({recipients} became {rendered}). Nothing was sent."
        )


def serialise(message: EmailMessage) -> bytes:
    """The exact bytes to transmit — CRLF line endings, as both SMTP and IMAP want.

    ``policy.default`` emits bare LF, which is wrong on the wire and would make
    the copy filed in Sent differ from the message that was actually delivered.
    """
    return message.as_bytes(policy=message.policy.clone(linesep="\r\n"))
