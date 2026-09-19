"""The shipped tree must survive Hermes' install-time security scan.

``hermes plugins install`` scans a plugin before it ever runs, and a *critical*
finding is a hard block: ``--force`` does not override a dangerous verdict, and
``hermes plugins update`` disables an already-installed plugin that starts
producing one. Runtime code is where findings keep that severity — docs and the
test tree are demoted — so the guard here reads the package, not the repository.

The pattern below is Hermes' own (``tools/threat_patterns.py``). It cannot tell a
constant that *names* a credential variable from one that *holds* a credential,
which is how a line of ours that only ever held ``"YANDEX_MAIL_APP_PASSWORD"``
blocked every install of 0.2.2 (#4). Pinning the shape keeps a later edit from
writing the block back in.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "hermes_yandex_mail"

#: Verbatim from Hermes' ``hardcoded_secret`` rule, matched case-insensitively.
HARDCODED_SECRET = re.compile(
    r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\'][A-Za-z0-9+/=_-]{20,}',
    re.IGNORECASE,
)


def test_no_runtime_line_looks_like_a_hardcoded_secret():
    offenders = [
        f"{source.relative_to(PACKAGE)}:{number}: {line.strip()}"
        for source in sorted(PACKAGE.rglob("*.py"))
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1)
        if HARDCODED_SECRET.search(line)
    ]
    assert not offenders, "Hermes blocks the install on these lines:\n" + "\n".join(offenders)


def test_the_guard_catches_the_shape_it_is_meant_to_catch():
    """Without this the test above passes just as well on an empty pattern."""
    assert HARDCODED_SECRET.search('ENV_PASSWORD = "YANDEX_MAIL_APP_PASSWORD"')
    assert not HARDCODED_SECRET.search('ENV_PASSWORD = _ENV_PREFIX + "APP_PASSWORD"')
