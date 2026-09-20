"""Compat layer: use Hermes' real ``get_provider_env`` when present, else a shim.

This plugin is a *standalone tool* (no WebSearchProvider), so the only host
symbol it needs is ``get_provider_env`` for reading secrets. Guard the import
with ``ImportError`` ONLY — real import errors (version mismatch, circular
imports) must surface rather than silently fall back to the shim.

This module is also the plugin's single boundary for host environment access,
so the other question about a variable — whether it is set at all, which no
resolved value can answer — is answered here too, by
:func:`provider_env_is_set`. The domain modules stay free of ``agent.*``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path


def _hermes_env_entries() -> Iterator[tuple[str, str]]:
    """``(key, value)`` for every assignment in ``~/.hermes/.env``.

    Nothing at all when the file is missing or unreadable — a plugin that
    cannot read it is in the same position as one running where it does not
    exist.
    """
    env_file = Path.home() / ".hermes" / ".env"
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        yield key.strip(), value.strip().strip('"').strip("'")


def provider_env_is_set(name: str) -> bool:
    """Whether ``name`` is *set*, whatever — if anything — it holds.

    :func:`get_provider_env` cannot answer this: it strips what it resolves, so
    a variable set to whitespace and a variable nobody ever set both arrive as
    the empty string. A setting whose *absence* means "no restriction" has to
    tell those apart, or a mistyped value silently removes the restriction.

    Set means: present as a key in ``os.environ``, or as a key in
    ``~/.hermes/.env``. Those are the two places this plugin can look. A Hermes
    host may also supply a value through its own configuration layer, which
    cannot be inspected from here without importing Hermes internals; a name
    that reaches the plugin only that way reads as unset.
    """
    if name in os.environ:
        return True
    return any(key == name for key, _ in _hermes_env_entries())


try:
    from agent.web_search_provider import get_provider_env

    HERMES_AVAILABLE = True
except ImportError:  # pragma: no cover - only outside Hermes (tests / standalone)
    HERMES_AVAILABLE = False

    def get_provider_env(name: str) -> str:  # type: ignore[misc]
        """Resolve a secret from ``os.environ`` first, then ``~/.hermes/.env``.

        Mirrors Hermes' own resolution order so keys set in ``~/.hermes/.env``
        work in gateway / subprocess runs even when the plugin is used
        standalone in tests. The value comes back stripped, exactly as the
        host's does — which is why "set to whitespace" can only be recognised
        through :func:`provider_env_is_set`.
        """
        value = os.environ.get(name)
        if value:
            return value.strip()
        for key, file_value in _hermes_env_entries():
            if key == name:
                return file_value
        return ""


__all__ = ["HERMES_AVAILABLE", "get_provider_env", "provider_env_is_set"]
