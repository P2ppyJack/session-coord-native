"""Standalone native-turn-source bridge for session-coord."""

from __future__ import annotations

import logging
from pathlib import Path

if __package__:
    from .session_coord_native_cli import SessionCoordCommand, configure_cli
    from .session_coord_native_source import SessionCoordNativeSource
else:  # pytest may import a hyphenated plugin root as bare ``__init__``
    from session_coord_native_cli import SessionCoordCommand, configure_cli
    from session_coord_native_source import SessionCoordNativeSource

PLUGIN_NAME = "session-coord-native"
DEFAULT_SURFACES = ("cli", "tui", "gateway")
_ALLOWED_SURFACES = frozenset(DEFAULT_SURFACES)
_HOST_API_UNAVAILABLE = "This Hermes host does not provide the public native-turn-source API"
logger = logging.getLogger(__name__)


def _configured_surfaces(ctx) -> tuple[str, ...]:
    raw = ctx.get_config("supported_surfaces", list(DEFAULT_SURFACES))
    if not isinstance(raw, (list, tuple)) or isinstance(raw, (str, bytes)):
        raise ValueError("supported_surfaces must be a list of cli, tui, and/or gateway")
    surfaces = tuple(str(value).strip().lower() for value in raw)
    if not surfaces or len(set(surfaces)) != len(surfaces):
        raise ValueError("supported_surfaces must be a non-empty list without duplicates")
    unknown = sorted(set(surfaces) - _ALLOWED_SURFACES)
    if unknown:
        raise ValueError(f"unsupported native surface(s): {', '.join(unknown)}")
    return surfaces


def _configured_board_script(ctx) -> Path:
    configured = ctx.get_config("board_script", None)
    if configured is None or not str(configured).strip():
        lister = getattr(ctx, "list_profile_homes", None)
        if not callable(lister):
            raise RuntimeError(
                "This Hermes host cannot resolve the active profile home through PluginContext"
            )
        homes = {str(name): Path(home).expanduser().resolve() for name, home in lister()}
        profile = str(getattr(ctx, "profile_name", "default") or "default")
        profile_home = homes.get(profile)
        if profile_home is None:
            raise RuntimeError(f"Hermes did not report a profile home for {profile!r}")
        machine_root = (
            profile_home.parent.parent if profile_home.parent.name == "profiles" else profile_home
        )
        return (machine_root / "scripts" / "session_coord.py").resolve()
    return Path(str(configured)).expanduser().resolve()


def register(ctx):
    """Register the generic native source and the public ``session-coord`` CLI."""
    try:
        from hermes_cli.native_turn_sources import NativeTurnLease
    except (ImportError, AttributeError):
        logger.warning(_HOST_API_UNAVAILABLE)
        return None

    registrar = getattr(ctx, "register_native_turn_source", None)
    if not callable(registrar):
        logger.warning(_HOST_API_UNAVAILABLE)
        return None

    surfaces = _configured_surfaces(ctx)
    source = SessionCoordNativeSource(
        board_script=_configured_board_script(ctx),
        lease_type=NativeTurnLease,
        surfaces=surfaces,
    )
    native_handle = registrar(source, surfaces=surfaces)
    if native_handle is None or getattr(native_handle, "active", False) is not True:
        raise RuntimeError("Hermes returned no live native-turn-source registration handle")

    command = SessionCoordCommand(
        ctx=ctx,
        native_handle=native_handle,
        surfaces=surfaces,
        board_script=source.board_script,
    )
    ctx.register_cli_command(
        name="session-coord",
        help="Inspect native continuation support and configure its watchdog",
        description="Session coordination native integration",
        setup_fn=configure_cli,
        handler_fn=command,
    )
