"""Host boundary for profile-scoped configuration and attachment cache paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from agent.secret_scope import get_secret as _get_secret
except ModuleNotFoundError as exc:
    if exc.name != "agent":
        raise
    HERMES_AVAILABLE = False
    _get_secret = os.environ.get
else:
    HERMES_AVAILABLE = True


def env_value(name: str) -> str | None:
    """Read one current-profile value, preserving absence separately from emptiness.

    Hermes owns secret resolution and profile isolation. Standalone use reads
    only the process environment. Scope errors propagate to the tool boundary;
    registration must fail closed if its action permissions cannot be read.
    """
    value = _get_secret(name)
    return None if value is None else value.strip()


def _default_hermes_home() -> Path:
    """``HERMES_HOME``, else the platform default, as Hermes resolves them."""
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(os.path.expanduser(os.path.expandvars(configured)))
    if sys.platform == "win32":  # pragma: no cover - Windows only
        local = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"
    return Path.home() / ".hermes"


def document_cache_dir() -> Path:
    """Hermes' document cache, where a saved attachment goes.

    The same directory Hermes itself files incoming documents in: the gateway
    will deliver from it even in strict media mode, it is mounted into Docker,
    Modal and SSH sandboxes so the agent's own file tools can open what is
    there, and it sits outside the read denylist. Hermes resolves it with the
    active profile in mind; outside Hermes it is ``<HERMES_HOME>/cache/documents``.
    """
    try:
        from hermes_constants import get_hermes_dir
    except ImportError:
        if HERMES_AVAILABLE:
            raise
        return _default_hermes_home() / "cache" / "documents"
    return get_hermes_dir("cache/documents", "document_cache")


def agent_visible_path(path: Path) -> str:
    """Where the agent's own tools see ``path``, which lies in a Hermes cache.

    In a Docker or Modal sandbox the cache is mounted at ``/root/.hermes``, and
    over SSH it is synced under the remote ``~/.hermes``; Hermes' own tools
    translate their cache paths the same way. With the local backend, and
    outside Hermes, the path is unchanged.
    """
    try:
        from tools.credential_files import to_agent_visible_cache_path
    except ImportError:
        if HERMES_AVAILABLE:
            raise
        return str(path)
    return to_agent_visible_cache_path(str(path))


__all__ = [
    "HERMES_AVAILABLE",
    "agent_visible_path",
    "document_cache_dir",
    "env_value",
]
