# hermes-yandex-mail

[![PyPI version](https://img.shields.io/pypi/v/hermes-yandex-mail.svg)](https://pypi.org/project/hermes-yandex-mail/)
[![CI](https://github.com/akinfold/hermes-yandex-mail/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/akinfold/hermes-yandex-mail/actions/workflows/ci.yml)
[![E2E (live)](https://github.com/akinfold/hermes-yandex-mail/actions/workflows/e2e.yml/badge.svg)](https://github.com/akinfold/hermes-yandex-mail/actions/workflows/e2e.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/akinfold/hermes-yandex-mail/badges/coverage.json&v=1)](https://github.com/akinfold/hermes-yandex-mail/actions/workflows/ci.yml)
[![CodeFactor](https://www.codefactor.io/repository/github/akinfold/hermes-yandex-mail/badge)](https://www.codefactor.io/repository/github/akinfold/hermes-yandex-mail)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Let your [Hermes Agent](https://hermes-agent.nousresearch.com) work through your
Yandex inbox.** *"What came in overnight?"* — *"Read me the one from the bank."* —
*"File everything from GitHub into Archive and mark it read."* The agent works on
your real mailbox, over IMAP, with no third-party service in the middle.

- 📬 **Seven tools, one toolset** — list folders with unread counts, search, read
  (body plus attachment inventory), flag, move, delete, and — only if you switch
  it on — send.
- ✉️ **Sending is off until you name it** — `send_message` is not in `all` and not
  granted by leaving the allow-list empty, so upgrading never hands a running
  agent the ability to write as you.
- 🔒 **You choose what it may touch** — restrict it to specific folders, and to
  specific actions (`read`, `read,write`, …). A disallowed action is not in the
  toolset at all, so the model cannot be talked into calling it.
- 🛟 **Nothing is lost, and nothing is over-claimed** — a copy exists before an
  original is removed, deletion means Trash unless you insist otherwise, an
  expunge only ever names the UIDs it was given, and a UID that no longer
  exists is refused instead of reported as done.
- 👓 **Reading does not mark as read** — the agent peeks; `\Seen` changes only
  when you ask.
- 🪶 **No runtime dependencies** — IMAP and MIME parsing come from Python's own
  standard library.
- 🔑 **App password, not your account password** — scoped to mail, revocable in
  one click.

Tested against Hermes **0.19.x–0.21.x**, Python **3.11–3.13**.

## Quick start

```bash
# 1. Install into Hermes (alternatively: pip install hermes-yandex-mail)
hermes plugins install akinfold/hermes-yandex-mail --enable

# 2. Add your credentials — the app password comes from
#    https://id.yandex.ru/security/app-passwords (scope: "Почта" / Mail)
printf 'YANDEX_MAIL_LOGIN=%s\nYANDEX_MAIL_APP_PASSWORD=%s\n' \
  'you@yandex.ru' 'your-app-password' >> ~/.hermes/.env
```

Then enable it in `~/.hermes/config.yaml` (third-party plugins are off by default):

```yaml
plugins:
  enabled: [yandex_mail]
```

**Before the first run, switch IMAP on for the mailbox** — see
[Enabling IMAP](#enabling-imap-in-yandex-mail). Yandex refuses to log in
otherwise, and the error looks exactly like a wrong password.

That's it. Ask the agent *"anything unread in my inbox?"* and it will tell you.

> Not ready to hand over write access? Add `YANDEX_MAIL_ACTIONS=read` and it can
> only look — see [Restricting what the agent can do](#restricting-what-the-agent-can-do).

## The tools

Up to seven standalone tools, in the `yandex_mail` toolset:

| Tool | Purpose |
|---|---|
| `yandex_mail_list_folders` | List folders with their role (inbox, sent, trash, junk, drafts, archive) and total/unread counts. |
| `yandex_mail_search_messages` | Search a folder by sender, recipient, subject, full text, date range, unread or flagged state; returns subject, addresses, date, size, flags, and the `uid`. Pages with `offset`, and reports `total` so you know whether more exist. |
| `yandex_mail_read_message` | Read one message: headers, text body (HTML-only mail is converted to text), and the attachment list. Peeks by default. |
| `yandex_mail_mark_message` | Mark messages read/unread and flagged/unflagged. |
| `yandex_mail_move_message` | Move messages to another folder, reporting which UID each message was verified to have on arrival. |
| `yandex_mail_delete_message` | Delete messages — to Trash by default. A message already there is left untouched; permanent deletion is a separate, irreversible request. |
| `yandex_mail_send_message` | **Off by default.** Send a plain-text message, optionally threaded as a reply. Files a copy in Sent and reports exactly which recipients the server accepted. |

Yandex Mail has no public REST API, so this plugin speaks **IMAP**
(`imap.yandex.ru:993`) directly — the same protocol Yandex documents for mail
clients. Nothing is proxied through anyone else's servers.

Messages are addressed by `folder` + `uid`, and **both are required** for read,
mark, move, and delete: UID numbering is independent per folder, so a UID paired
with the wrong folder would silently name a different message. Every result
reports the folder in the server's own spelling — pass that value straight back.
Several UIDs can be given at once, comma-separated: `"101,102"`; they are acted
on all-or-nothing, so a batch containing a UID that no longer exists is refused
rather than half-applied.

Folder names are matched generously on the way in — `spam`, `Spam`, `junk` and
`Корзина` all resolve to the right mailbox — because IMAP itself is
case-sensitive and would simply answer *"No such folder"*.

Sending goes over **SMTP** (`smtp.yandex.ru:465`) with the same app password, and
is off unless you switch it on — see [Sending mail](#sending-mail).

## Configuration

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `YANDEX_MAIL_LOGIN` | yes | — | Your full Yandex address, e.g. `you@yandex.ru`. Sending needs the complete address: it becomes `From` and the envelope sender, and `yandex_mail_send_message` refuses a bare login. |
| `YANDEX_MAIL_APP_PASSWORD` | yes | — | App password with the Mail scope — an account password will not work. |
| `YANDEX_MAIL_IMAP_HOST` | no | `imap.yandex.ru` | Override for a Yandex 360 domain or for testing. |
| `YANDEX_MAIL_IMAP_PORT` | no | `993` | IMAP over TLS. |
| `YANDEX_MAIL_FOLDERS` | no | *(all)* | Comma-separated allow-list of folders, spelled as `yandex_mail_list_folders` reports them; case is ignored, but role words and synonyms such as `sent` or `spam` are **not** expanded here, so on an account whose Sent folder carries a localised name, that localised name is the one to list. The first entry is the default folder. |
| `YANDEX_MAIL_ACTIONS` | no | *(all but sending)* | Comma-separated allow-list of actions the agent may perform — see below. |
| `YANDEX_MAIL_SMTP_HOST` | no | `smtp.yandex.ru` | Override for a Yandex 360 domain or for testing. |
| `YANDEX_MAIL_SMTP_PORT` | no | `465` | SMTP over implicit TLS. |
| `YANDEX_MAIL_SEND_TO` | no | *(any address)* | Comma-separated fence on who may be written to: full addresses, or `@domain` for a whole domain. |

Credentials are read from the environment first, then from `~/.hermes/.env`, so
they work in gateway and subprocess runs. Secret values are never logged.

Dates are ISO 8601 (`2026-07-25`). Folder names may be in any language — the
Cyrillic names Yandex gives Russian accounts (`Отправленные`, `Спам`) are encoded
and decoded for you.

### Restricting what the agent can do

`YANDEX_MAIL_ACTIONS` decides which of the seven tools are registered at all. A
disallowed action is not merely refused at call time: the tool never appears in
the agent's toolset, so it cannot be invoked, and the model is not tempted to try.

Accepted values, comma-separated and case-insensitive — individual actions
(`list_folders`, `search_messages`, `read_message`, `mark_message`,
`move_message`, `delete_message`, `send_message`), full tool names
(`yandex_mail_delete_message`), or the shorthands:

| Shorthand | Expands to |
|---|---|
| `read` | `list_folders`, `search_messages`, `read_message` |
| `write` | `mark_message`, `move_message` |
| `delete` | `delete_message` |
| `all` | every action **except** `send_message` (the default) |

`read_message` with `mark_read=true` changes the `\Seen` flag, so it also needs
`mark_message`. Under `YANDEX_MAIL_ACTIONS=read` such a call is refused and
nothing is read.

```dotenv
# Read the mail, change nothing:
YANDEX_MAIL_ACTIONS=read

# Full triage, but the agent can never delete anything:
YANDEX_MAIL_ACTIONS=read,write

# Just enough to report what is unread:
YANDEX_MAIL_ACTIONS=list_folders,search_messages
```

Leave it unset for the six reading and organising tools. **Sending is the one
exception to "unset means everything"**: `send_message` has to be named, either
on its own or alongside a shorthand —

```dotenv
YANDEX_MAIL_ACTIONS=all,send_message
```

There is deliberately no short `send` spelling. A short word could already be
sitting in somebody's configuration from before the action existed, where it was
silently ignored; giving it meaning now would switch sending on by upgrading
alone, which is precisely what this gate exists to prevent.

A name that matches nothing is ignored, so a
typo can only ever withhold a tool, never grant one — and a value that names
nothing recognisable therefore registers nothing at all. Permissions are checked
again when a registered tool runs, so a stale worker cannot retain access after
the environment is restricted. Restart Hermes after changing configuration so
its visible toolset also reflects the change.

Pair it with `YANDEX_MAIL_FOLDERS` to fence off the rest of the mailbox: with
`YANDEX_MAIL_FOLDERS=INBOX`, every other folder is invisible to the agent and
cannot be named — as a source *or* as a move destination.

Two folders stay reachable by the plugin itself, never by name. A delete still
moves the message into the folder the server flags as Trash, and a sent message
is still filed into the one it flags as Sent, whether or not the allow-list
mentions them. Naming either folder in a tool call is still refused; only these
two built-in steps may reach it.

## Sending mail

Sending is the one thing here that cannot be undone: the message leaves your
mailbox and reaches the people named. Everything about the design follows from
that.

Switch it on, and decide who the agent may write to:

```dotenv
YANDEX_MAIL_ACTIONS=all,send_message

# Optional, and worth setting: who may be written to at all.
YANDEX_MAIL_SEND_TO=you@yandex.ru,@yourcompany.example
```

The fence is checked before a socket is opened. A full entry matches one
mailbox (`@ya.ru` and `@yandex.ru` are understood to be the same account); an
`@domain` entry matches that domain exactly — not its subdomains, and not a
domain that merely ends with it. Set it to something unparseable — including
whitespace — and nothing is allowed through: a mistyped fence fails closed.
Leave it unset for no fence at all.

**The rules the tool enforces, whatever it is asked to do:**

- **You are always the sender.** `From` and the envelope sender are
  `YANDEX_MAIL_LOGIN`. No argument can change them.
- **Recipients are only ever what the caller states.** Replying threads a
  message onto another one — it never takes an address from it. This matters:
  `From`, `Reply-To`, `To` and `Cc` are all written by whoever sent you the
  message, so deriving a reply's recipients from them would let a sender choose
  where your reply goes.
- **An address is an address, not a display name.** `Bob <bob@example.org>` is
  refused; pass `bob@example.org`. A display name containing an `@` parses as a
  second address, which is how a reply quietly acquires an extra recipient.
- **A reply must name the message it answers.** `reply_to_uid`,
  `reply_to_folder` and `reply_to_message_id` are required together. A UID
  identifies a slot, not a message; comparing the `message_id` against what the
  server reports now is what makes "reply to the message I read" mean that. A
  message carrying no `Message-ID` of its own cannot be replied to — the tool
  says so rather than threading onto nothing.
- **Threading needs the grant that reading needs.** Answering a message means
  reading its headers, so a reply also requires `read_message`. Enabling
  `send_message` alone gives a tool that can write, not one that can also look.
- **Nothing gets to become a header.** A line break in a subject, a recipient or
  a copied `Message-ID` is refused, not stripped.
- **A copy is filed in Sent**, and the message being answered is flagged
  `\Answered` if flagging is allowed. Neither can fail the send: once the
  message has gone, the result says so and a bookkeeping problem is a note.

**What the result tells you.** `sent`, the recipients the server actually
accepted, anything it refused and why, and — on a reply — where each recipient
stands in that thread:

```json
{
  "sent": true,
  "delivery": "confirmed",
  "recipients": ["counterparty@example.org"],
  "from": "you@yandex.ru",
  "subject": "Re: Contract",
  "message_id": "<178985821691.1252.15561456921@yandex.ru>",
  "recipient_sources": {"counterparty@example.org": "from"},
  "in_reply_to": "<original@example.org>",
  "replied_to": {
    "uid": "8",
    "folder": "INBOX",
    "subject": "Contract",
    "from": ["Counterparty <counterparty@example.org>"],
    "message_id": "<original@example.org>"
  },
  "saved_to_sent": true,
  "sent_folder": "Sent",
  "marked_answered": true,
  "notes": []
}
```

`recipient_sources` is always present on a reply, and says whether each address
is your own account (`self`), sent the original (`from`), was among its `To`
recipients (`to`), appeared only in its `Reply-To` (`reply_to_only`), or is none
of those (`new`). The original's `Cc` is deliberately not consulted, so someone
who was only Cc'd on it also comes back as `new`. `reply_to_only` and `new` are
the two worth reading: a message asking for replies at an address it was not
sent from is the standard shape of a phishing redirect, and the tool says so in
`notes` rather than deciding for you.

`delivery` is `unconfirmed` when the message went out but the server never
acknowledged it. That is not a failure and must not be retried — sending again
would deliver a second copy. Anything that goes wrong *before* the message is
written says "Nothing was sent" and is safe to try again.

Plain text only: no HTML, no attachments, and no Cc or Bcc — every recipient goes
in `to`, and an argument the tool does not know (`cc`, `bcc`, `from`,
`attachments`, …) refuses the whole call instead of being silently dropped. At
most 10 recipients, a 500-character subject, and a 100 000-character body per
message.

## Enabling IMAP in Yandex Mail

A mailbox that has never been used with a mail client does not accept IMAP
connections until you switch the protocol on. Until you do, every login fails
with `[AUTHENTICATIONFAILED] ... invalid credentials or IMAP is disabled`, which
reads exactly like a wrong password.

1. Open <https://mail.yandex.ru/#setup/client> (⚙ **Настройки → Почтовые
   программы**).
2. Tick **«С сервера imap.yandex.ru по протоколу IMAP»** — *"allow access to the
   mailbox over IMAP"*.
3. Save. It takes effect within seconds.

In a Yandex 360 organisation an administrator may have to allow mail clients for
the whole domain first.

## Getting the app password

IMAP does not accept your normal account password.

1. Open <https://id.yandex.ru/security/app-passwords>.
2. Add a password with the **Почта (IMAP, SMTP)** / **Mail** scope. A password
   created for another service — a CalDAV one, for instance — will not work here.
3. Copy it into `YANDEX_MAIL_APP_PASSWORD`. It is shown only once, and you can
   revoke it at any time without touching your account password.

If a tool answers *"Authentication failed"*, it is one of these two things: IMAP
is off, or the app password lacks the Mail scope.

## Good to know

- **A just-arrived message may not match a subject search for a second or two.**
  Yandex indexes `SEARCH SUBJECT` asynchronously; searching by `text`, or simply
  asking again a moment later, finds it.
- **UIDs change when a message moves,** so `yandex_mail_move_message` returns a
  `destination_uids` map from each source UID to the one the message was
  verified to have on arrival. Use it rather than searching — Yandex cannot
  search by `Message-ID`. A message that could not be verified is simply absent
  from the map, never guessed.
- **The TLS certificate and hostname are verified**, and every connection
  carries a 30-second timeout. A private or self-signed CA is supplied the
  standard way, via `SSL_CERT_FILE` / `SSL_CERT_DIR`; there is no setting for
  turning verification off. *(Releases 0.1.0 and 0.2.0 did not verify — see
  [Security](#security).)*
- **Attachments are listed, not downloaded** — name, MIME type, and size. The
  body is capped (20 000 characters by default, raised with `max_chars` up to
  100 000) and says when it was truncated. A message whose raw size exceeds
  10 MiB, typically one carrying large attachments, is refused outright rather
  than read.
- **Yandex' SMTP does not advertise `SMTPUTF8`,** so an address with non-ASCII
  characters in it cannot be sent to at all. It is refused with a sentence
  saying why, rather than failing somewhere inside the standard library.
  Subjects and message bodies in any language are fine; display names are not,
  because every recipient must be given as a bare address.

## Installing the plugin into Hermes

### Option A — from Git (recommended)

```bash
hermes plugins install akinfold/hermes-yandex-mail --enable
```

### Option B — pip

```bash
pip install hermes-yandex-mail
```

Hermes discovers it through the `hermes_agent.plugins` entry point; add
`yandex_mail` to `plugins.enabled`.

### Option C — drop-in directory

Download `hermes-yandex-mail-plugin-<version>.zip` from the release — not the
wheel, the `.tar.gz`, or GitHub's "Source code" archives — and unzip it into
`~/.hermes/plugins/` so you end up with
`~/.hermes/plugins/yandex_mail/plugin.yaml`, then enable it the same way.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest                       # unit tests, no network
```

## Running the live E2E tests

The `e2e`-marked tests hit a real Yandex mailbox and are deselected by default.
They upload one throwaway message with a unique marker via IMAP `APPEND`, then
search, read, flag, move, and erase it, and the cleanup sweeps until the server
agrees nothing is left — so a successful run leaves the mailbox as it found it.

Since 0.3.0 they also **really send**, which is the only way to test sending at
all. Every message goes to the test account itself and nowhere else, held there
by two independent guards: the suite sets `YANDEX_MAIL_SEND_TO` to that account,
so the plugin's own fence refuses anything else before a socket opens, and the
test process asserts the same thing again before each call. Use a dedicated test
mailbox regardless.

### Locally

```bash
YANDEX_MAIL_LOGIN=you@yandex.ru \
YANDEX_MAIL_APP_PASSWORD=xxxx \
pytest -m e2e
```

Or keep both out of the command line, in `~/.yandex-mail-login` and
`~/.yandex-mail-app-password`, and just run `pytest -m e2e` — see
`tests/e2e/conftest.py`.

### On GitHub Actions

The **E2E (live)** workflow is manual (`workflow_dispatch`). It reads
`YANDEX_MAIL_LOGIN` and `YANDEX_MAIL_APP_PASSWORD` from a GitHub Environment
named `yandex-mail-e2e`.

## Related Hermes plugins

Part of a family of Yandex plugins for Hermes Agent:

- [hermes-yandex-disk](https://github.com/akinfold/hermes-yandex-disk) — browse, read, write, and share files on Yandex Disk (REST API).
- [hermes-yandex-calendar](https://github.com/akinfold/hermes-yandex-calendar) — list, create, update, respond to, move, and delete Yandex Calendar events (CalDAV).
- [hermes-yandex-search-api](https://github.com/akinfold/hermes-yandex-search-api) — Yandex web search backend and generative, cited answers for Hermes (Yandex Search API).

## Security

**0.1.0 and 0.2.0 connected without verifying the server's TLS certificate.**
`imaplib.IMAP4_SSL` with no explicit `ssl_context` falls back to
`ssl._create_stdlib_context()`, which sets `verify_mode=CERT_NONE` and
`check_hostname=False`: the connection was encrypted but unauthenticated, so
anyone positioned to intercept it — a hostile Wi-Fi network, a spoofed DNS
answer, an intercepting proxy — could present their own certificate and read
the app password and every message. **0.2.1 fixes this; upgrade.**

```bash
pip install --upgrade hermes-yandex-mail
```

If you ran an earlier version over a network you do not control, revoke the app
password at <https://id.yandex.ru/security/app-passwords> and issue a new one.

Found with thanks by [@ukko](https://github.com/ukko) in
[#2](https://github.com/akinfold/hermes-yandex-mail/pull/2).

Please report security issues privately rather than as a public issue — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
layout, the plugin contract rules worth knowing, and the release process.

## License

MIT — see [LICENSE](LICENSE).
