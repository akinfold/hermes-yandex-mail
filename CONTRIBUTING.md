# Contributing

Thanks for your interest in improving **hermes-yandex-mail** — a
[Hermes Agent](https://hermes-agent.nousresearch.com) plugin that reads and
organises a Yandex mailbox over IMAP and, when it is switched on, sends mail
over SMTP. Contributions of all sizes are welcome:
bug reports, docs, tests, and features.

All repository content — code, comments, docs, commit messages, issues, and
PRs — is in **English**. Be respectful and constructive; assume good intent.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

## Project layout

```
hermes_yandex_mail/
  imap_utf7.py  # modified UTF-7 for mailbox names (RFC 3501), no Hermes imports
  message.py    # MIME parsing: headers, body selection, attachments, no Hermes imports
  imap.py       # the IMAP client for Yandex, no Hermes imports
  compose.py    # builds an outgoing message and refuses unsafe addressing
  smtp.py       # hand-driven SMTP submission, no Hermes imports
  config.py     # env -> client, folder allow-list, action allow-list
  _compat.py    # real-vs-shim host env helper: a variable's value, and whether it is set
  tool.py       # tool schemas + handlers (JSON in, JSON string out)
  __init__.py   # register(ctx) — the plugin entry point
tests/          # unit tests (no network, scripted FakeIMAP and FakeSMTP in conftest.py)
tests/e2e/      # live tests against a real mailbox, marked `e2e`
```

## Ground rules

The plugin follows the Hermes plugin contract; a few of these are load-bearing:

- **Layering.** Keep the domain modules (`imap.py`, `imap_utf7.py`, `message.py`,
  `compose.py`, `smtp.py`)
  free of any `agent.*` imports so they stay unit-testable. The host-facing glue
  lives in `tool.py`, `config.py`, and `__init__.py`.
- **Never raise across the boundary.** Tool handlers (`handle_*`) must always
  return a JSON string — every failure becomes `{"error": "..."}`. The IMAP
  client raises `MailError`, which the handlers translate.
- **Never destroy a message.** A copy must exist before an original is removed,
  expunging is always UID-scoped (`UID EXPUNGE`, so a bare `EXPUNGE` cannot take
  someone else's `\Deleted` messages with it), and deletion means "move to
  Trash" unless the caller explicitly asked for permanence.
- **Never send as anybody else.** `From` and the envelope sender are
  `YANDEX_MAIL_LOGIN`; no tool argument may influence either. Recipients come
  only from what the caller passed — never from the message being replied to,
  whose headers are written by whoever sent it.
- **Never let a send be retried by accident, and never hide a refusal.**
  Anything that fails before the payload reaches the socket says so plainly.
  After it, the reply to end-of-data decides: `250` is a delivery; an explicit
  `4xx` or `5xx` is the server declining the message, so nothing reached anyone
  and it is an error with no copy filed in Sent and no `\Answered` flag set;
  and no verdict at all is the single unknown case, which must be reported as
  sent-but-unconfirmed so nobody retries it. `smtp.py` drives the transaction
  by hand for exactly this reason; `smtplib.send_message` cannot tell the three
  apart.
- **Relative imports only** in `__init__.py` — the plugin loads as
  `hermes_plugins.yandex_mail`.
- **Address comparison** goes through `imap.normalize_email` — Yandex treats
  `@ya.ru` and `@yandex.ru` as the same mailbox, and a second private copy of
  that rule will eventually disagree with the first.
- **Secrets** are resolved via `_compat.get_provider_env`; never log their values.
  That value arrives stripped, so it cannot tell "unset" from "set to
  whitespace". When absence and emptiness must mean different things — as they
  do for `YANDEX_MAIL_SEND_TO`, where absence means "no fence" — ask
  `_compat.provider_env_is_set` as well.
- **Non-ASCII on the wire.** `imaplib` encodes `str` arguments as ASCII, so any
  argument that can carry Cyrillic must be passed as `bytes` — folder names as
  modified UTF-7 through `_quote_mailbox` (RFC 3501), search terms as UTF-8
  through `_quoted` together with `CHARSET UTF-8`.

## Checks

```bash
ruff check . && ruff format --check .
pytest --cov=hermes_yandex_mail --cov-fail-under=90
radon cc -s -n C hermes_yandex_mail   # must print nothing
```

CI fails on any function radon rates **C or worse** — split it instead of raising
the bar. `radon cc -a hermes_yandex_mail` shows the average.

Unit tests must not open a socket: `tests/conftest.py` provides `FakeIMAP`, a
scriptable stand-in for an `imaplib.IMAP4` connection that records every command,
so tests can assert on the wire traffic (including the order of `COPY`, `STORE`,
and `EXPUNGE`), and `FakeSMTP`, the same for `smtplib.SMTP_SSL`, which models how
far the bytes got so the nothing-sent / possibly-delivered distinction can be
tested. Live tests go under `tests/e2e/`, are marked `@pytest.mark.e2e`,
and skip when credentials are absent.

## Running the live tests locally

Put the credentials in files the e2e conftest picks up and run `pytest -m e2e`:

```bash
umask 077 && printf '%s' 'you@yandex.ru' > ~/.yandex-mail-login
umask 077 && printf '%s' '<app password>' > ~/.yandex-mail-app-password
pytest -m e2e -v
```

The suite uploads one throwaway message with a unique marker via IMAP `APPEND`,
exercises the tools against it, and erases it in a `finally`. Since 0.3.0 it also
exercises the send path for real over SMTP: it sets `YANDEX_MAIL_SEND_TO` to the
test account itself, so mail is genuinely sent and delivered, but only ever to that
mailbox — no third party is emailed. Use a dedicated test mailbox anyway, never a personal one.
`YANDEX_MAIL_APP_PASSWORD` must be an app password with the **Mail** scope, and
IMAP must be enabled for the mailbox. Live tests are manual and never required
for a PR.

If a live search assertion fails intermittently, note that Yandex indexes
`SEARCH SUBJECT` asynchronously: a brand-new word is findable a second or two
after the message lands. The e2e suite polls (`_await_search`) rather than
sleeping a fixed amount; keep new assertions on the same footing.

## Commit & PR conventions

- Focused commits with imperative subject lines (e.g. `imap: keep expunge UID-scoped`).
- Open a PR against `main`, fill in the template, and link any related issue.
- CI (lint + tests on Python 3.11–3.13, coverage ≥ 90%) must pass.

## Reporting security issues

Please do not open a public issue for anything security-sensitive. Contact the
maintainer directly through the repository owner's GitHub profile.

## Releasing

Bump the version in **three** files that must stay in sync (a unit test enforces
this):

- `pyproject.toml`
- `hermes_yandex_mail/__init__.py`
- `hermes_yandex_mail/plugin.yaml`

Then tag:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

The publish workflow builds artifacts, creates a GitHub Release, and (if the repo
variable `PUBLISH_TO_PYPI=true` and a PyPI Trusted Publisher is configured)
publishes to PyPI. The `pypi` environment's approval gate is a deliberate human
checkpoint — a PyPI version cannot be republished.
