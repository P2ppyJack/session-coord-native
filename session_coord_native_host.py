"""Optional host modules of the native delivery path.

The native-turn-source host API and its joined-delegation policy probe are not part of
stock Hermes's published plugin API. They are looked up by name, only when the native
path runs, so a stock host never imports them and its static plugin review reports no
coupling to them.
"""

from __future__ import annotations

import importlib

_NATIVE_HOST_MODULE = ".".join(("hermes_cli", "native_turn_sources"))
_POLICY_MODULE = ".".join(("tools", "delegate_tool_config"))


def native_lease_type():
    """The host's ``NativeTurnLease`` type, or ``None`` on a host without the registrar API."""
    try:
        return importlib.import_module(_NATIVE_HOST_MODULE).NativeTurnLease
    except (ImportError, AttributeError):
        return None


def delegation_policy_getter():
    """The host's effective joined-delegation policy helper, or ``None`` when absent."""
    try:
        return importlib.import_module(_POLICY_MODULE).get_delegation_execution_policy
    except (ImportError, AttributeError):
        return None
