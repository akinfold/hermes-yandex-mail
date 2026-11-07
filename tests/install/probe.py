"""Report what Hermes loaded, from inside Hermes' own interpreter.

``test_install.py`` runs this with the Python of the Hermes virtualenv, after
an install. It discovers plugins the way the agent does when a session starts,
then prints one line, ``PROBE <json>``, holding:

* ``plugins`` — every plugin that did not ship with Hermes, as Hermes' plugin
  manager lists it: source, enabled, number of tools, load error;
* ``tools`` — every tool in the agent's catalog for the CLI platform, with its
  toolset. Hermes leaves out a tool whose availability check fails, so a tool
  that needs credentials is here only when Hermes can see them.

The catalog is the full one. Hermes may show the model only part of it and
defer plugin tools behind ``tool_search``; deferred tools stay callable, so
they count.

This uses Hermes internals, and a Hermes refactor can break it. It then
reports ``probe_error`` with the traceback rather than a verdict, and the test
fails saying so.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback


def _mail_boundary() -> dict | None:
    """Exercise the installed plugin's secret and cache-path host APIs."""
    candidates = [
        module
        for name, module in list(sys.modules.items())
        if name.endswith("._compat")
        and hasattr(module, "env_value")
        and getattr(sys.modules.get(name.removesuffix("._compat") + ".config"), "ENV_LOGIN", None)
        == "YANDEX_MAIL_LOGIN"
    ]
    if not candidates:
        # Upgrade checks also probe the previous release, before it has this API.
        return None
    assert len(candidates) == 1, "Expected one installed Yandex Mail module"
    compat = candidates[0]
    package = compat.__name__.removesuffix("._compat")
    config = sys.modules[package + ".config"]
    tool = sys.modules[package + ".tool"]
    from agent import secret_scope
    from tools import credential_files

    active = secret_scope.is_multiplex_active()
    scope = secret_scope.set_secret_scope(
        {
            config.ENV_SEND_TO: "",
            config.ENV_ATTACHMENT_MAX_BYTES: "",
            config.ENV_ACTIONS: "read",
            config.ENV_LOGIN: "probe@example.org",
            config.ENV_PASSWORD: "probe-password",
        }
    )
    try:
        secret_scope.set_multiplex_active(True)
        assert compat.env_value("YANDEX_MAIL_PROBE_ABSENT") is None
        assert config.allowed_send_recipients() == []
        try:
            config.attachment_max_bytes()
        except config.PermissionDenied:
            pass
        else:
            raise AssertionError("An empty profile attachment cap must refuse saves")
        assert config.allowed_actions() == {"list_folders", "search_messages", "read_message"}
        assert config.credentials_present()
    finally:
        secret_scope.reset_secret_scope(scope)
        secret_scope.set_multiplex_active(active)

    scope = secret_scope.set_secret_scope(None)
    try:
        secret_scope.set_multiplex_active(True)
        assert config.credentials_present() is False
        guarded = tool.with_action_guard("list_folders", tool.handle_list_folders)
        assert "error" in json.loads(guarded({}))
    finally:
        secret_scope.reset_secret_scope(scope)
        secret_scope.set_multiplex_active(active)

    directory = compat.document_cache_dir()
    path = directory / "yandex-mail-probe.txt"
    backend = credential_files._terminal_backend
    mapped = {}
    try:
        for name, prefix in [
            ("local", str(directory)),
            ("docker", "/root/.hermes/cache/documents"),
            ("ssh", "~/.hermes/cache/documents"),
        ]:
            credential_files._terminal_backend = lambda name=name: name
            mapped[name] = compat.agent_visible_path(path)
            assert mapped[name] == prefix + "/yandex-mail-probe.txt", mapped
    finally:
        credential_files._terminal_backend = backend
    return {"profile_scope": True, "scope_errors": True, "cache_paths": mapped}


def _report() -> dict:
    import model_tools
    from hermes_cli.config import load_config
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from hermes_cli.tools_config import _get_platform_tools
    from tools.registry import registry

    discover_plugins()
    plugins = [p for p in get_plugin_manager().list_plugins() if p["source"] != "bundled"]
    toolsets = sorted(_get_platform_tools(load_config(), "cli"))
    catalog = model_tools.get_tool_definitions(
        enabled_toolsets=toolsets, quiet_mode=True, skip_tool_search_assembly=True
    )
    tools = {}
    for definition in catalog:
        name = definition["function"]["name"]
        entry = registry.get_entry(name)
        tools[name] = entry.toolset if entry else None
    return {"plugins": plugins, "tools": tools, "mail_boundary": _mail_boundary()}


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        report = _report()
    except Exception:
        report = {"probe_error": traceback.format_exc()}
    print("PROBE " + json.dumps(report, default=str))


if __name__ == "__main__":
    main()
