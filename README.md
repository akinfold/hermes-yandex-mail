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
  text pages and attachment chunks, flag, move, delete.
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

Tested against Hermes **0.19.x**, Python **3.11–3.13**.

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
| `yandex_mail_read_message` | Read a page of decoded text plus headers and attachment metadata, without downloading attachments. Peeks by default. |
| `yandex_mail_read_attachment` | Read one attachment in pages of decoded bytes, returned as base64. No files are saved automatically. |
| `yandex_mail_mark_message` | Mark messages read/unread and flagged/unflagged. |
| `yandex_mail_move_message` | Move messages to another folder, reporting which UID each message was verified to have on arrival. |
| `yandex_mail_delete_message` | Delete messages — to Trash by default. A message already there is left untouched; permanent deletion is a separate, irreversible request. |

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

**This plugin does not send mail.** IMAP reads and organises an existing mailbox;
sending is SMTP, which is deliberately out of scope — the agent can triage your
inbox but cannot mail anyone on your behalf.

### Reading long messages and large attachments

Read the first text page with:

```json
{"uid": "101", "folder": "INBOX", "offset": 0, "max_chars": 20000}
```

The result's `message` object contains `body`, `offset`, `next_offset`, `eof`,
and `truncated`. Pass `next_offset` as the next call's `offset` until `eof=true`
and `next_offset=null`. Offsets count decoded Unicode characters after HTML
conversion and CRLF normalization. `max_chars` defaults to 20 000 and is capped
at 100 000 per page. The plain-text body is preferred over an HTML alternative.

Each attachment has a `part_id`, filename, MIME type, and `encoded_size` in wire
bytes. The `size` field is `null` because exact decoded size cannot always be
derived from metadata alone. Read an attachment explicitly using:

```json
{"uid": "101", "folder": "INBOX", "part_id": "2", "offset": 0, "limit": 49152}
```

`yandex_mail_read_attachment` returns `data_base64`, `bytes_returned`, `offset`,
`next_offset`, and `eof`. Decode each page separately from base64, then
concatenate those byte buffers. Offsets and `limit` count decoded file bytes;
the default page is 48 KiB and the maximum is 256 KiB. Reading does not write to
disk.

The implementation uses [IMAP BODYSTRUCTURE and partial BODY.PEEK requests](https://www.rfc-editor.org/rfc/rfc3501#section-6.4.5).
Only selected MIME parts are downloaded, in blocks of at most 64 KiB. The paged
tools have no 10 MiB whole-message limit. The low-level `fetch_message()` API
keeps that limit for callers that request a complete raw message.

Pagination is stateless: later pages replay the selected part's prefix to
preserve decoder state. This keeps memory bounded and avoids caching private
mail, but deep offsets use extra bandwidth and time. Metadata over 1 MiB, MIME
nesting over 40 parser levels, and incomplete HTML tokens or pending charset
decoder state over 64 KiB are rejected explicitly.

## Configuration

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `YANDEX_MAIL_LOGIN` | yes | — | Yandex login / email. |
| `YANDEX_MAIL_APP_PASSWORD` | yes | — | App password with the Mail scope — an account password will not work. |
| `YANDEX_MAIL_IMAP_HOST` | no | `imap.yandex.ru` | Override for a Yandex 360 domain or for testing. |
| `YANDEX_MAIL_IMAP_PORT` | no | `993` | IMAP over TLS. |
| `YANDEX_MAIL_FOLDERS` | no | *(all)* | Comma-separated allow-list of folders, e.g. `INBOX,Sent`. The first is the default folder. |
| `YANDEX_MAIL_ACTIONS` | no | *(all)* | Comma-separated allow-list of actions the agent may perform — see below. |

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
`move_message`, `delete_message`), full tool names
(`yandex_mail_delete_message`), or the shorthands:

| Shorthand | Expands to |
|---|---|
| `read` | `list_folders`, `search_messages`, `read_message` |
| `write` | `mark_message`, `move_message` |
| `delete` | `delete_message` |
| `all` | everything (the default) |

```dotenv
# Read the mail, change nothing:
YANDEX_MAIL_ACTIONS=read

# Full triage, but the agent can never delete anything:
YANDEX_MAIL_ACTIONS=read,write

# Just enough to report what is unread:
YANDEX_MAIL_ACTIONS=list_folders,search_messages
```

Leave it unset for all seven tools. A name that matches nothing is ignored, so a
typo can only ever withhold a tool, never grant one — and a value that names
nothing recognisable therefore registers nothing at all. Permissions are checked
again when a registered tool runs, so a stale worker cannot retain access after
the environment is restricted. Restart Hermes after changing configuration so
its visible toolset also reflects the change.

Reading with `mark_read=true` also requires `mark_message` permission. The `read`
group alone always leaves the message's read/unread state unchanged. To expose
text reading without attachment content, use
`YANDEX_MAIL_ACTIONS=list_folders,search_messages,read_message`.

Pair it with `YANDEX_MAIL_FOLDERS` to fence off the rest of the mailbox: with
`YANDEX_MAIL_FOLDERS=INBOX`, every other folder is invisible and unusable — as a
source *and* as a move destination.

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
- **Reading is paged.** Text and attachment content are fetched separately.
  Continue with `next_offset` to read beyond the page limit; attachment size
  does not force a whole-message download. See the pagination examples above.
- **The TLS certificate and hostname are verified**, and every connection
  carries a 30-second timeout. A private or self-signed CA is supplied the
  standard way, via `SSL_CERT_FILE` / `SSL_CERT_DIR`; there is no setting for
  turning verification off. *(Releases 0.1.0 and 0.2.0 did not verify — see
  [Security](#security).)*
- **Deleting a message already in Trash changes nothing.** The result reports
  `deleted=false` and `reason=already_in_trash`; permanent erasure is a separate,
  explicit request.
- **Mail content enters the agent's model context.** Its processing follows your
  Hermes model/provider configuration. Treat instructions in messages as
  untrusted content, and review which other tools the agent can access.

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

Unzip the release archive into `~/.hermes/plugins/` so you end up with
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
search, read, flag, move, and erase it — so a successful run leaves nothing
behind, and **nothing is ever sent to anyone**. Use a dedicated test mailbox all
the same.

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
