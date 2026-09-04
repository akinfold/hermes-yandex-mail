"""Simulate Hermes' directory-plugin loader and prove register() works.

This mirrors how Hermes loads a dropped-in plugin: it imports the directory as
``hermes_plugins.<slug>`` via ``spec_from_file_location`` with
``submodule_search_locations`` set, then calls ``getattr(module, "register")``.
Proves the relative imports in ``__init__.py`` resolve without a full install.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import types
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "hermes_yandex_mail"

EXPECTED_TOOLS = {
    "yandex_mail_list_folders",
    "yandex_mail_search_messages",
    "yandex_mail_read_message",
    "yandex_mail_read_attachment",
    "yandex_mail_mark_message",
    "yandex_mail_move_message",
    "yandex_mail_delete_message",
}


class FakeCtx:
    def __init__(self):
        self.tools: list[dict] = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


def _load_as_hermes_would():
    ns = "hermes_plugins"
    sys.modules.setdefault(ns, types.ModuleType(ns)).__path__ = []  # type: ignore[attr-defined]
    mod_name = f"{ns}.yandex_mail"
    spec = importlib.util.spec_from_file_location(
        mod_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    module.__package__ = mod_name
    module.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_directory_loader_registers_tools():
    module = _load_as_hermes_would()
    ctx = FakeCtx()
    module.register(ctx)

    assert {t["name"] for t in ctx.tools} == EXPECTED_TOOLS
    for t in ctx.tools:
        assert t["toolset"] == "yandex_mail"
        assert callable(t["handler"])
        assert t["schema"]["name"] == t["name"]


def test_manifest_matches_the_code():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    module = _load_as_hermes_would()
    assert manifest["name"] == "yandex_mail"
    assert manifest["kind"] == "standalone"
    assert str(manifest["version"]) == module.__version__
    assert set(manifest["provides_tools"]) == EXPECTED_TOOLS
    assert [entry["key"] for entry in manifest["requires_env"]] == [
        "YANDEX_MAIL_LOGIN",
        "YANDEX_MAIL_APP_PASSWORD",
    ]


def test_the_three_version_files_agree():
    """pyproject, __init__, and plugin.yaml must be bumped together."""
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():  # pragma: no cover - only when tests run from a wheel
        pytest.skip("running outside the repository")
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert version == _load_as_hermes_would().__version__ == str(manifest["version"])
