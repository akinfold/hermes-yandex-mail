"""Compat layer: use Hermes' real ``get_provider_env`` when present, else a shim.

This plugin is a *standalone tool* (no WebSearchProvider), so the only host
symbol it needs is ``get_provider_env`` for reading secrets. Guard the import
with ``ImportError`` ONLY — real import errors (version mismatch, circular
imports) must surface rather than silently fall back to the shim.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from agent.web_search_provider import get_provider_env

    HERMES_AVAILABLE = True
except ImportError:  # pragma: no cover - only outside Hermes (tests / standalone)
    HERMES_AVAILABLE = False

    def get_provider_env(name: str) -> str:  # type: ignore[misc]
        """Resolve a secret from ``os.environ`` first, then ``~/.hermes/.env``.

        Mirrors Hermes' own resolution order so keys set in ``~/.hermes/.env``
        work in gateway / subprocess runs even when the plugin is used
        standalone in tests.
        """
        value = os.environ.get(name)
        if value:
            return value.strip()
        env_file = Path.home() / ".hermes" / ".env"
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == name:
                    return val.strip().strip('"').strip("'")
        except OSError:
            pass
        return ""


__all__ = ["HERMES_AVAILABLE", "get_provider_env"]
