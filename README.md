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

- 📬 **Six tools, one toolset** — list folders with unread counts, search, read
  (body plus attachment inventory), flag, move, delete.
- 🔒 **You choose what it may touch** — restrict it to specific folders, and to
  specific actions (`read`, `read,write`, …). A disallowed action is not in the
  toolset at all, so the model cannot be talked into calling it.
- 🛟 **Nothing is lost** — a copy exists before an original is removed, deletion
  means Trash unless you insist otherwise, and an expunge only ever names the
  UIDs it was given.
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

Up to six standalone tools, in the `yandex_mail` toolset:

| Tool | Purpose |
|---|---|
| `yandex_mail_list_folders` | List folders with their role (inbox, sent, trash, junk, drafts, archive) and total/unread counts. |
| `yandex_mail_search_messages` | Search a folder by sender, recipient, subject, full text, date range, unread or flagged state; returns subject, addresses, date, size, flags, and the `uid`. |
| `yandex_mail_read_message` | Read one message: headers, text body (HTML-only mail is converted to text), and the attachment list. Peeks by default. |
| `yandex_mail_mark_message` | Mark messages read/unread and flagged/unflagged. |
| `yandex_mail_move_message` | Move messages to another folder. |
| `yandex_mail_delete_message` | Delete messages — to Trash by default, permanently on request. |

Yandex Mail has no public REST API, so this plugin speaks **IMAP**
(`imap.yandex.ru:993`) directly — the same protocol Yandex documents for mail
clients. Nothing is proxied through anyone else's servers.

Messages are addressed by `folder` + `uid`. UIDs are unique within a folder and
change when a message moves, so pass back the folder each result reports. Several
UIDs can be given at once, comma-separated: `"101,102"`.

**This plugin does not send mail.** IMAP reads and organises an existing mailbox;
sending is SMTP, which is deliberately out of scope — the agent can triage your
inbox but cannot mail anyone on your behalf.

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

`YANDEX_MAIL_ACTIONS` decides which of the six tools are registered at all. A
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

Leave it unset for all six tools. A name that matches nothing is ignored, so a
typo can only ever withhold a tool, never grant one — and a value that names
nothing recognisable therefore registers nothing at all. The list is applied when
the plugin loads: restart Hermes after changing it.

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
- **UIDs change when a message moves.** After a move, search the destination
  folder if you need the new identifier.
- **Attachments are listed, not downloaded** — name, MIME type, and size. The
  body is capped (20 000 characters by default) and says when it was truncated.

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

## Contributing

Issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
layout, the plugin contract rules worth knowing, and the release process.

## License

MIT — see [LICENSE](LICENSE).
