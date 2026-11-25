"""Standalone continuation bridge for session-coord.

Two delivery paths, never both in one process:

* ``native``: a host that publishes ``ctx.register_native_turn_source`` polls the
  registered source and admits leased wakes itself.
* ``inject``: a stock host without that registrar. Wakes are pushed through the public
  ``ctx.inject_message(..., session_key=...)`` call and confirmed from plugin hooks.

``delivery: auto`` (the default) selects ``native`` when the registrar exists and
``inject`` otherwise.
"""

from __future__ import annotations

import logging
from pathlib import Path

if __package__:
    from .session_coord_native_cli import SessionCoordCommand, configure_cli
    from .session_coord_native_host import native_lease_type
    from .session_coord_native_inject import InjectDelivery
    from .session_coord_native_source import SessionCoordNativeSource
else:  # pytest may import a hyphenated plugin root as bare ``__init__``
    from session_coord_native_cli import SessionCoordCommand, configure_cli
    from session_coord_native_host import native_lease_type
    from session_coord_native_inject import InjectDelivery
    from session_coord_native_source import SessionCoordNativeSource

PLUGIN_NAME = "session-coord-native"
DEFAULT_SURFACES = ("cli", "tui", "gateway")
_ALLOWED_SURFACES = frozenset(DEFAULT_SURFACES)
_DELIVERY_MODES = frozenset({"auto", "native", "inject"})
_HOST_API_UNAVAILABLE = "This Hermes host does not provide the public native-turn-source API"
_INJECT_UNAVAILABLE = "This Hermes host does not provide PluginContext.inject_message and hooks"
_NO_DELIVERY = (
    "This Hermes host provides neither the native-turn-source API nor "
    "PluginContext.inject_message and hooks"
)
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


def _configured_delivery(ctx) -> str:
    raw = ctx.get_config("delivery", "auto")
    mode = str(raw if isinstance(raw, str) else "auto").strip().lower() or "auto"
    if mode not in _DELIVERY_MODES:
        raise ValueError("delivery must be auto, native, or inject")
    return mode


def _profile_home(ctx) -> Path:
    """This plugin manager's own profile home.

    A host with complete profile discovery names it exactly; otherwise ``register`` runs
    inside the manager's profile scope, where the published ``get_hermes_home`` is exact.
    """
    lister = getattr(ctx, "list_profile_homes", None)
    if callable(lister):
        homes = {str(name): Path(home).expanduser().resolve() for name, home in lister()}
        profile = str(getattr(ctx, "profile_name", "default") or "default")
        profile_home = homes.get(profile)
        if profile_home is None:
            raise RuntimeError(f"Hermes did not report a profile home for {profile!r}")
        return profile_home
    try:
        from hermes_constants import get_hermes_home
    except ImportError as exc:
        raise RuntimeError(
            "This Hermes host cannot resolve the active profile home through PluginContext"
        ) from exc
    return Path(get_hermes_home()).expanduser().resolve()


def _configured_board_script(ctx, profile_home: Path | None = None) -> Path:
    configured = ctx.get_config("board_script", None)
    if configured is None or not str(configured).strip():
        home = profile_home or _profile_home(ctx)
        machine_root = home.parent.parent if home.parent.name == "profiles" else home
        return (machine_root / "scripts" / "session_coord.py").resolve()
    return Path(str(configured)).expanduser().resolve()


def _register_cli(ctx, command) -> None:
    ctx.register_cli_command(
        name="session-coord",
        help="Inspect native continuation support and configure its watchdog",
        description="Session coordination native integration",
        setup_fn=configure_cli,
        handler_fn=command,
    )


def _register_native(ctx, lease_type) -> None:
    surfaces = _configured_surfaces(ctx)
    source = SessionCoordNativeSource(
        board_script=_configured_board_script(ctx),
        lease_type=lease_type,
        surfaces=surfaces,
    )
    native_handle = ctx.register_native_turn_source(source, surfaces=surfaces)
    if native_handle is None or getattr(native_handle, "active", False) is not True:
        raise RuntimeError("Hermes returned no live native-turn-source registration handle")
    _register_cli(
        ctx,
        SessionCoordCommand(
            ctx=ctx,
            native_handle=native_handle,
            surfaces=surfaces,
            board_script=source.board_script,
        ),
    )


def _register_inject(ctx) -> InjectDelivery:
    surfaces = _configured_surfaces(ctx)
    profile_home = _profile_home(ctx)
    board_script = _configured_board_script(ctx, profile_home)
    delivery = InjectDelivery(
        inject=ctx.inject_message,
        profile_home=profile_home,
        board_script=board_script,
        surfaces=surfaces,
    )
    delivery.register_hooks(ctx.register_hook)
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(delivery.stop)
    _register_cli(
        ctx,
        SessionCoordCommand(
            ctx=ctx,
            native_handle=None,
            surfaces=surfaces,
            board_script=board_script,
            delivery=delivery,
        ),
    )
    return delivery


def register(ctx):
    """Register exactly one delivery path and the public ``session-coord`` CLI."""
    mode = _configured_delivery(ctx)
    lease_type = native_lease_type()
    native_available = lease_type is not None and callable(
        getattr(ctx, "register_native_turn_source", None)
    )
    inject_available = callable(getattr(ctx, "inject_message", None)) and callable(
        getattr(ctx, "register_hook", None)
    )
    if mode == "auto":
        if not (native_available or inject_available):
            logger.warning(_NO_DELIVERY)
            return None
        mode = "native" if native_available else "inject"
    if mode == "native":
        if not native_available:
            logger.warning(_HOST_API_UNAVAILABLE)
            return None
        _register_native(ctx, lease_type)
        return None
    if not inject_available:
        logger.warning(_INJECT_UNAVAILABLE)
        return None
    _register_inject(ctx)
    return None
