# Contributing

Thanks for your interest in improving **hermes-yandex-mail** — a
[Hermes Agent](https://hermes-agent.nousresearch.com) plugin that reads and
organises a Yandex mailbox over IMAP. Contributions of all sizes are welcome:
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
  config.py     # env -> client, folder allow-list, action allow-list
  _compat.py    # real-vs-shim host env helper
  tool.py       # tool schemas + handlers (JSON in, JSON string out)
  __init__.py   # register(ctx) — the plugin entry point
tests/          # unit tests (no network, a scripted FakeIMAP in conftest.py)
tests/e2e/      # live tests against a real mailbox, marked `e2e`
```

## Ground rules

The plugin follows the Hermes plugin contract; a few of these are load-bearing:

- **Layering.** Keep the domain modules (`imap.py`, `imap_utf7.py`, `message.py`)
  free of any `agent.*` imports so they stay unit-testable. The host-facing glue
  lives in `tool.py`, `config.py`, and `__init__.py`.
- **Never raise across the boundary.** Tool handlers (`handle_*`) must always
  return a JSON string — every failure becomes `{"error": "..."}`. The IMAP
  client raises `MailError`, which the handlers translate.
- **Never destroy a message.** A copy must exist before an original is removed,
  expunging is always UID-scoped (`UID EXPUNGE`, so a bare `EXPUNGE` cannot take
  someone else's `\Deleted` messages with it), and deletion means "move to
  Trash" unless the caller explicitly asked for permanence.
- **Relative imports only** in `__init__.py` — the plugin loads as
  `hermes_plugins.yandex_mail`.
- **Address comparison** goes through `imap.normalize_email` — Yandex treats
  `@ya.ru` and `@yandex.ru` as the same mailbox, and a second private copy of
  that rule will eventually disagree with the first.
- **Secrets** are resolved via `_compat.get_provider_env`; never log their values.
- **Non-ASCII on the wire.** `imaplib` encodes `str` arguments as ASCII, so any
  argument that can carry Cyrillic (folder names, search terms) must be passed as
  UTF-8 `bytes`.

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
and `EXPUNGE`). Live tests go under `tests/e2e/`, are marked `@pytest.mark.e2e`,
and skip when credentials are absent.

## Running the live tests locally

Put the credentials in files the e2e conftest picks up and run `pytest -m e2e`:

```bash
umask 077 && printf '%s' 'you@yandex.ru' > ~/.yandex-mail-login
umask 077 && printf '%s' '<app password>' > ~/.yandex-mail-app-password
pytest -m e2e -v
```

The suite uploads one throwaway message with a unique marker via IMAP `APPEND`,
exercises the tools against it, and erases it in a `finally` — nothing is sent,
so no one is emailed. Use a dedicated test mailbox anyway, never a personal one.
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
