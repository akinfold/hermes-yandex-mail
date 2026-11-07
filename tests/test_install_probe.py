"""The install probe follows Hermes' credential bootstrap before discovery."""

from __future__ import annotations

import builtins
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

import hermes_yandex_mail as plugin
from hermes_yandex_mail import _compat, config

from .install import probe
from .test_register import FakeCtx


@pytest.mark.parametrize("has_credentials", [True, False])
def test_probe_bootstraps_before_loading_the_tool_catalog(monkeypatch, tmp_path, has_credentials):
    for name in (config.ENV_LOGIN, config.ENV_PASSWORD, config.ENV_ACTIONS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_compat, "_get_secret", os.environ.get)
    monkeypatch.setattr(probe, "_mail_boundary", lambda: None)
    ctx = FakeCtx()
    bootstrapped = False

    def load_hermes_dotenv(*, project_env):
        nonlocal bootstrapped
        assert project_env == tmp_path / ".env"
        if has_credentials:
            monkeypatch.setenv(config.ENV_LOGIN, "install-check@example.invalid")
            monkeypatch.setenv(config.ENV_PASSWORD, "install-check-not-a-password")
        bootstrapped = True

    def discover_plugins():
        assert bootstrapped
        plugin.register(ctx)

    def get_tool_definitions(**kwargs):
        assert kwargs == {
            "enabled_toolsets": ["yandex_mail"],
            "quiet_mode": True,
            "skip_tool_search_assembly": True,
        }
        return [{"function": row["schema"]} for row in ctx.tools if row["check_fn"]()]

    modules = {
        "hermes_cli.config": {
            "get_project_root": lambda: tmp_path,
            "load_config": lambda: {},
        },
        "hermes_cli.env_loader": {"load_hermes_dotenv": load_hermes_dotenv},
        "hermes_cli.plugins": {
            "discover_plugins": discover_plugins,
            "get_plugin_manager": lambda: SimpleNamespace(list_plugins=lambda: []),
        },
        "hermes_cli.tools_config": {"_get_platform_tools": lambda *args: {"yandex_mail"}},
        "tools.registry": {
            "registry": SimpleNamespace(
                get_entry=lambda name: SimpleNamespace(toolset="yandex_mail")
            )
        },
        "model_tools": {"get_tool_definitions": get_tool_definitions},
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)

    original_import = builtins.__import__

    def import_after_bootstrap(name, *args, **kwargs):
        if name == "model_tools":
            assert bootstrapped, "Tool imports must follow Hermes' environment bootstrap"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_after_bootstrap)
    report = probe._report()
    expected = {row["name"]: "yandex_mail" for row in ctx.tools} if has_credentials else {}
    assert report["tools"] == expected
    assert len(ctx.tools) == 6
