from __future__ import annotations

import importlib.util
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class FrozenNativeTurnLease:
    lease_id: str
    prompt: str
    commit: object
    abort: object
    display_kind: str | None = None


@dataclass(frozen=True)
class FrozenNativeSessionView:
    profile_home: Path
    profile: str
    surface: str
    session_id: str
    session_key: str | None
    compression_lineage: tuple[str, ...]
    owner_token: str | None


@pytest.fixture
def plugin_loader(monkeypatch):
    """Load the directory plugin as Hermes does, with a frozen host ABI."""

    host_module = type(sys)("hermes_cli.native_turn_sources")
    host_module.NativeTurnLease = FrozenNativeTurnLease
    host_module.NativeSessionView = FrozenNativeSessionView
    monkeypatch.setitem(sys.modules, "hermes_cli.native_turn_sources", host_module)

    loaded: list[str] = []

    def load():
        name = f"session_coord_native_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(
            name,
            PLUGIN_ROOT / "__init__.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loaded.append(name)
        spec.loader.exec_module(module)
        return module

    yield load

    for package_name in loaded:
        for module_name in list(sys.modules):
            if module_name == package_name or module_name.startswith(package_name + "."):
                sys.modules.pop(module_name, None)
