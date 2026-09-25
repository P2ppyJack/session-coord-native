from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import types

import pytest
import yaml

from conftest import PLUGIN_ROOT


class LiveHandle:
    active = True


class FrozenHostContext:
    profile_name = "default"

    def __init__(self):
        self.native_calls = []
        self.cli_calls = []

    def get_config(self, key, default=None):
        return default

    def list_profile_homes(self):
        profile_home = getattr(self, "profile_home", Path.home() / ".hermes")
        return ((self.profile_name, profile_home),)

    def register_native_turn_source(self, source, *, surfaces):
        self.native_calls.append((source, surfaces))
        return LiveHandle()

    def register_cli_command(self, **kwargs):
        self.cli_calls.append(kwargs)
        return LiveHandle()

    def dispatch_tool(self, _name, _args):
        raise AssertionError("registration must not dispatch tools")


def test_register_uses_live_native_registrar_and_top_level_cli(plugin_loader):
    plugin = plugin_loader()
    ctx = FrozenHostContext()

    plugin.register(ctx)

    assert len(ctx.native_calls) == 1
    source, surfaces = ctx.native_calls[0]
    assert source.name == "session-coord-native"
    assert surfaces == ("cli", "tui", "gateway")
    assert len(ctx.cli_calls) == 1
    command = ctx.cli_calls[0]
    assert command["name"] == "session-coord"
    assert command["handler_fn"] is not None

    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    assert parser.parse_args(["native-check", "--json"]).session_coord_command == "native-check"
    assert parser.parse_args(["watchdog-setup", "--json", "--check"]).check is True


def test_named_profile_uses_machine_root_board_script(plugin_loader, tmp_path):
    plugin = plugin_loader()
    ctx = FrozenHostContext()
    ctx.profile_name = "worker"
    ctx.profile_home = tmp_path / "profiles" / "worker"

    plugin.register(ctx)

    source, _surfaces = ctx.native_calls[0]
    assert source.board_script == (tmp_path / "scripts" / "session_coord.py").resolve()


def test_native_check_separates_native_support_from_optional_wait_policy(
    plugin_loader, capsys, monkeypatch
):
    # A missing cache entry can still import a real compatible host module.
    monkeypatch.setitem(__import__("sys").modules, "tools.delegate_tool_config", None)
    plugin = plugin_loader()
    ctx = FrozenHostContext()
    plugin.register(ctx)
    command = ctx.cli_calls[0]
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)

    result = command["handler_fn"](parser.parse_args(["native-check", "--json"]))

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "supported": True,
        "surfaces": ["cli", "tui", "gateway"],
        "wait_for_all_supported": False,
        "wait_for_all": None,
        "activation": "fresh_process_only",
        "reason": (
            "Native turn source is registered in this fresh process; the host does not "
            "expose tools.delegate_tool_config.get_delegation_execution_policy, so "
            "delegation wait-for-all cannot be verified."
        ),
    }


@pytest.mark.parametrize(
    ("wait_for_all", "expected_reason"),
    [
        (
            True,
            "Native turn source and joined delegation policy are available in this fresh "
            "process; restart resident Hermes processes to activate installed changes.",
        ),
        (
            False,
            "Native turn source is registered, but delegation wait-for-all is disabled "
            "for profile 'default'.",
        ),
    ],
)
def test_native_check_reads_public_effective_delegation_policy(
    plugin_loader, capsys, monkeypatch, wait_for_all, expected_reason
):
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = []
    policy_module = types.ModuleType("tools.delegate_tool_config")
    policy_module.get_delegation_execution_policy = lambda: {
        "wait_for_all": wait_for_all,
        "model_tasks": "joined" if wait_for_all else "asynchronous",
    }
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_config", policy_module)

    plugin = plugin_loader()
    ctx = FrozenHostContext()
    plugin.register(ctx)
    command = ctx.cli_calls[0]
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)

    command["handler_fn"](parser.parse_args(["native-check", "--json"]))

    payload = json.loads(capsys.readouterr().out)
    assert payload["wait_for_all_supported"] is True
    assert payload["wait_for_all"] is wait_for_all
    assert payload["reason"] == expected_reason


def test_manifest_names_standalone_plugin_and_declares_public_settings():
    manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "session-coord-native"
    assert manifest["version"] == "0.2.0"
    assert manifest["manifest_version"] == 2
    assert manifest["api_version"] == 1
    assert manifest["kind"] == "standalone"
    assert manifest["license"] == "MIT"
    assert manifest["homepage"] == "https://github.com/P2ppyJack/session-coord-native"
    assert manifest["platforms"] == ["linux", "macos"]
    assert manifest["python_dependencies"] == []
    assert manifest["config_schema"] == {
        "board_script": {
            "type": "str",
            "default": "",
            "description": "Optional absolute path to session_coord.py; empty uses the machine root scripts directory.",
        },
        "supported_surfaces": {
            "type": "list",
            "default": ["cli", "tui", "gateway"],
            "description": "Native Hermes surfaces on which coordination turns may be admitted.",
        },
    }


def test_native_check_fails_when_registration_handle_is_no_longer_live(
    plugin_loader, capsys
):
    plugin = plugin_loader()
    ctx = FrozenHostContext()
    plugin.register(ctx)
    command = ctx.cli_calls[0]
    handler = command["handler_fn"]
    handler.native_handle.active = False
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)

    result = handler(parser.parse_args(["native-check", "--json"]))

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["supported"] is False
    assert payload["surfaces"] == []
    assert payload["activation"] == "fresh_process_only"
    assert "no longer active" in payload["reason"]


def test_register_reads_only_plugin_settings_for_board_and_surfaces(
    plugin_loader, tmp_path
):
    class ConfiguredContext(FrozenHostContext):
        def __init__(self):
            super().__init__()
            self.lookups = []

        def get_config(self, key, default=None):
            self.lookups.append((key, default))
            return {
                "board_script": str(tmp_path / "board.py"),
                "supported_surfaces": ["cli", "gateway"],
            }.get(key, default)

    plugin = plugin_loader()
    ctx = ConfiguredContext()

    plugin.register(ctx)

    source, surfaces = ctx.native_calls[0]
    assert surfaces == ("cli", "gateway")
    assert source.surfaces == ("cli", "gateway")
    assert source.board_script == (tmp_path / "board.py").resolve()
    assert [key for key, _ in ctx.lookups] == ["supported_surfaces", "board_script"]


def test_register_degrades_gracefully_without_native_registrar(
    plugin_loader, caplog
):
    class OldHostContext:
        profile_name = "default"

        def get_config(self, _key, default=None):
            return default

        def register_cli_command(self, **_kwargs):
            raise AssertionError("unsupported registration must not expose a false-positive CLI")

    plugin = plugin_loader()

    with caplog.at_level(logging.WARNING, logger=plugin.__name__):
        assert plugin.register(OldHostContext()) is None

    assert caplog.messages == [plugin._HOST_API_UNAVAILABLE]


def test_register_degrades_gracefully_without_native_host_module(
    plugin_loader, monkeypatch, caplog
):
    plugin = plugin_loader()
    ctx = FrozenHostContext()
    monkeypatch.setitem(sys.modules, "hermes_cli.native_turn_sources", None)

    with caplog.at_level(logging.WARNING, logger=plugin.__name__):
        assert plugin.register(ctx) is None

    assert caplog.messages == [plugin._HOST_API_UNAVAILABLE]
    assert ctx.native_calls == []
    assert ctx.cli_calls == []


def test_native_check_reports_invalid_public_policy_response(
    plugin_loader, capsys, monkeypatch
):
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = []
    policy_module = types.ModuleType("tools.delegate_tool_config")
    policy_module.get_delegation_execution_policy = lambda: {"wait_for_all": "true"}
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_config", policy_module)
    plugin = plugin_loader()
    ctx = FrozenHostContext()
    plugin.register(ctx)
    command = ctx.cli_calls[0]
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)

    result = command["handler_fn"](parser.parse_args(["native-check", "--json"]))

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["supported"] is True
    assert payload["wait_for_all_supported"] is False
    assert payload["wait_for_all"] is None
    assert "did not return a Boolean" in payload["reason"]
