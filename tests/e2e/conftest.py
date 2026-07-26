"""Credential resolution for the live e2e suite.

In CI the credentials arrive as environment variables from the
``yandex-mail-e2e`` environment. For local runs, keeping them in files is
easier than pasting them onto every command line, so this also accepts:

* ``~/.yandex-mail-login``
* ``~/.yandex-mail-app-password``

Create them with restrictive permissions::

    umask 077 && printf '%s' 'you@yandex.ru' > ~/.yandex-mail-login
    umask 077 && printf '%s' '<app password>' > ~/.yandex-mail-app-password

Anything already present in the environment wins, and the tests skip when no
credentials turn up at all.
"""

from __future__ import annotations

import os
from pathlib import Path

_FILE_FOR_ENV = {
    "YANDEX_MAIL_LOGIN": ".yandex-mail-login",
    "YANDEX_MAIL_APP_PASSWORD": ".yandex-mail-app-password",
}


def pytest_configure(config) -> None:
    """Fill unset credentials from the local files, before tests are imported."""
    for env_var, filename in _FILE_FOR_ENV.items():
        if os.environ.get(env_var):
            continue
        try:
            value = (Path.home() / filename).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            os.environ[env_var] = value
