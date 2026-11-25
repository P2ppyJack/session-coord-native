"""Wake delivery on a stock Hermes host through the public plugin API.

A host without the native-turn-source registrar still publishes
``PluginContext.inject_message(content, session_key=...)``. The gateway admits that text as
a turn of the exact live session whose route equals the key (queued behind a person's
input), a TUI/Desktop host queues it on the session that owns the key, and a key that is
not live is refused synchronously. This module leases one board wake at a time, records
the receiver's ``attempting`` receipt, pushes the exact prompt through that call, and
confirms admission from the ``pre_llm_call`` hook of the turn that carries the prompt.

Delivery is limited to sessions this process has observed through its own hooks. A
classic CLI REPL host ignores ``session_key`` and queues injected text into its own
conversation, so an unobserved session is never a delivery target here.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from .session_coord_native_board import CoordinationBoardClient
    from .session_coord_native_receipts import (
        AdmissionDecision,
        CoordinationReceiptError,
        begin_admission,
        board_receipt_from_native,
        confirm_admission,
        mark_canceled,
        mark_not_sent,
        mark_unknown,
        tombstone_native_waits,
        validate_event_for_session,
    )
else:
    from session_coord_native_board import CoordinationBoardClient
    from session_coord_native_receipts import (
        AdmissionDecision,
        CoordinationReceiptError,
        begin_admission,
        board_receipt_from_native,
        confirm_admission,
        mark_canceled,
        mark_not_sent,
        mark_unknown,
        tombstone_native_waits,
        validate_event_for_session,
    )

logger = logging.getLogger(__name__)

# Board target kind -> the receiver surface the receipt layer validates against.
_KIND_SURFACE = {
    "cli": "cli",
    "desktop": "tui",
    "bot": "tui",
    "gateway": "gateway",
    "subagent_parent": "tui",
}
_EXPECTED_STATUS = {
    "delivered": "admitted",
    "not_sent": "not_sent",
    "unknown": "unknown",
    "canceled": "canceled",
}


@dataclass(frozen=True)
class TargetView:
    """The receiver identity a wake names, in the shape the receipt layer validates."""

    profile_home: Path
    profile: str
    surface: str
    session_id: str
    session_key: str | None
    compression_lineage: tuple[str, ...]
    owner_token: str | None = None


@dataclass
class _Inflight:
    event: dict[str, Any]
    decision: AdmissionDecision
    prompt: str
    native_ids: frozenset[str]
    deadline: float
    finished: bool = False
    result: bool = False


def _target_view(event: dict[str, Any], profile_home: Path) -> TargetView | None:
    target = event.get("target")
    if not isinstance(target, dict):
        return None
    surface = _KIND_SURFACE.get(str(target.get("kind") or ""))
    native = str(event.get("native_session_id") or "")
    if surface is None or not native:
        return None
    lineage = target.get("compression_lineage") or [native]
    if not isinstance(lineage, list):
        return None
    key = target.get("session_key")
    return TargetView(
        profile_home=profile_home,
        profile=str(target.get("profile") or ""),
        surface=surface,
        session_id=native,
        session_key=str(key) if key is not None else None,
        compression_lineage=tuple(str(value) for value in lineage),
    )


def _read_session_key() -> str:
    """The calling turn's gateway route, through the published session-env accessor."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return ""
    try:
        return str(get_session_env("HERMES_SESSION_KEY", "") or "").strip()
    except Exception:
        return ""


class InjectDelivery:
    """Push leased coordination wakes into live sessions with ``ctx.inject_message``."""

    def __init__(
        self,
        *,
        inject: Callable[..., bool],
        profile_home: Path,
        board_script: Path | None = None,
        board: Any = None,
        surfaces: tuple[str, ...] = ("cli", "tui", "gateway"),
        fast_seconds: float = 3.0,
        slow_seconds: float = 30.0,
        reconcile_seconds: float = 60.0,
        observe_seconds: float = 2 * 3600.0,
        max_backoff_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        session_key_reader: Callable[[], str] = _read_session_key,
    ):
        self.inject = inject
        self.profile_home = Path(profile_home).expanduser().resolve()
        self.board = board or CoordinationBoardClient(Path(board_script or ""))
        self.surfaces = frozenset(str(value) for value in surfaces)
        self.fast_seconds = float(fast_seconds)
        self.slow_seconds = float(slow_seconds)
        self.reconcile_seconds = float(reconcile_seconds)
        self.observe_seconds = float(observe_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.clock = clock
        self.session_key_reader = session_key_reader
        self.worker = f"inject:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._lock = threading.RLock()
        self._hosted: dict[str, str] = {}
        self._inflight: dict[str, _Inflight] = {}
        self._backoff: dict[str, tuple[float, float]] = {}
        self._waiting: frozenset[str] = frozenset()
        self._next_reconcile = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ hooks

    def register_hooks(self, register_hook: Callable[[str, Callable], Any]) -> None:
        register_hook("pre_llm_call", self.on_pre_llm_call)
        register_hook("on_session_start", self.on_session_seen)
        register_hook("on_session_open", self.on_session_seen)
        register_hook("on_session_end", self.on_session_end)
        register_hook("on_session_reset", self.on_session_reset)
        register_hook("on_session_delete", self.on_session_delete)
        register_hook("on_session_finalize", self.on_session_finalize)
        register_hook("post_tool_call", self.on_post_tool_call)

    def on_pre_llm_call(self, session_id: str = "", user_message: Any = "", **_kwargs):
        sid = str(session_id or "")
        if not sid:
            return None
        self._observe(sid)
        text = user_message if isinstance(user_message, str) else str(user_message or "")
        match = self._match_inflight(text)
        if match is not None:
            self._confirm(match)
        else:
            self._boundary(sid, "user_turn")
        self._nudge()
        return None

    def on_session_seen(self, session_id: str = "", **_kwargs):
        sid = str(session_id or "")
        if sid:
            self._observe(sid)
            self._nudge()
        return None

    def on_session_end(self, session_id: str = "", interrupted: bool = False, **_kwargs):
        sid = str(session_id or "")
        if sid and interrupted is True:
            self._boundary(sid, "user_stop")
        self._nudge()
        return None

    def on_session_reset(self, session_id: str = "", **_kwargs):
        sid = str(session_id or "")
        if sid:
            self._boundary(sid, "new_session")
            self._forget(sid)
        return None

    def on_session_delete(self, session_id: str = "", deleted_ids: Any = None, **_kwargs):
        ids = [str(session_id or "")]
        if isinstance(deleted_ids, (list, tuple)):
            ids.extend(str(value or "") for value in deleted_ids)
        for sid in dict.fromkeys(value for value in ids if value):
            self._boundary(sid, "session_delete")
            self._forget(sid)
        return None

    def on_session_finalize(self, session_id: Any = None, **_kwargs):
        # Closing a viewer is not a user boundary: the wake waits for the next open.
        sid = str(session_id or "")
        if sid:
            self._forget(sid)
        return None

    def on_post_tool_call(self, tool_name: str = "", args: Any = None, **_kwargs):
        if "session_coord" in str(args or ""):
            self._nudge()
        return None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="session-coord-inject", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.tick()
            except Exception:
                logger.warning("session-coord inject delivery pass failed", exc_info=True)
                delay = self.slow_seconds
            self._wake.wait(timeout=max(0.05, delay))
            self._wake.clear()

    def _nudge(self) -> None:
        self.start()
        self._wake.set()

    # ---------------------------------------------------------------- state

    def _observe(self, session_id: str) -> None:
        key = self.session_key_reader()
        with self._lock:
            if key or session_id not in self._hosted:
                self._hosted[session_id] = key

    def _forget(self, session_id: str) -> None:
        with self._lock:
            self._hosted.pop(session_id, None)

    def hosted(self) -> dict[str, str]:
        with self._lock:
            return dict(self._hosted)

    def _hosted_ids(self, event_or_episode: dict[str, Any]) -> list[str]:
        target = event_or_episode.get("target")
        ids = [str(event_or_episode.get("native_session_id") or "")]
        if isinstance(target, dict):
            lineage = target.get("compression_lineage")
            if isinstance(lineage, list):
                ids.extend(str(value) for value in lineage)
        with self._lock:
            return [value for value in dict.fromkeys(ids) if value and value in self._hosted]

    def _match_inflight(self, text: str) -> _Inflight | None:
        if not text:
            return None
        with self._lock:
            for inflight in self._inflight.values():
                if not inflight.finished and inflight.prompt in text:
                    return inflight
        return None

    # ------------------------------------------------------------- receipts

    def _report(self, event: dict[str, Any], outcome: str, receipt: dict[str, Any]) -> bool:
        try:
            proof = board_receipt_from_native(
                event, receipt, expected_status=_EXPECTED_STATUS[outcome]
            )
            return bool(self.board.result(event, outcome=outcome, receipt=proof))
        except Exception:
            logger.warning("Unable to report session-coord %s receipt", outcome, exc_info=True)
            return False

    def _finish(self, inflight: _Inflight, outcome: str, reason: str) -> None:
        finisher = {"not_sent": mark_not_sent, "unknown": mark_unknown, "canceled": mark_canceled}
        try:
            receipt = finisher[outcome](inflight.decision, reason)
        except (CoordinationReceiptError, OSError):
            logger.warning("Unable to persist session-coord %s receipt", outcome, exc_info=True)
            return
        self._report(inflight.event, outcome, receipt)

    def _confirm(self, inflight: _Inflight) -> bool:
        with self._lock:
            if inflight.finished:
                return inflight.result
            inflight.finished = True
            self._inflight.pop(str(inflight.event["event_id"]), None)
        try:
            receipt = confirm_admission(inflight.decision)
        except (CoordinationReceiptError, OSError) as exc:
            self._finish(inflight, "unknown", f"admission receipt failed: {exc}")
            return False
        if receipt.get("status") == "canceled":
            self._report(inflight.event, "canceled", receipt)
            return False
        inflight.result = self._report(inflight.event, "delivered", receipt)
        return inflight.result

    def _boundary(self, session_id: str, reason: str) -> None:
        try:
            tombstone_native_waits(self.profile_home, session_id, reason)
        except (CoordinationReceiptError, OSError):
            logger.warning("Unable to persist session-coord boundary tombstone", exc_info=True)
        with self._lock:
            waiting = session_id in self._waiting
            pending = any(
                session_id in inflight.native_ids for inflight in self._inflight.values()
            )
        if waiting or pending:
            try:
                self.board.cancel(
                    profile_home=self.profile_home,
                    native_session_id=session_id,
                    reason=reason,
                )
            except Exception:
                logger.warning("Unable to cancel session-coord board waits", exc_info=True)

    # --------------------------------------------------------------- delivery

    def _expire_inflight(self, now: float) -> None:
        with self._lock:
            expired = [
                item for item in self._inflight.values()
                if not item.finished and now >= item.deadline
            ]
            for item in expired:
                item.finished = True
                self._inflight.pop(str(item.event["event_id"]), None)
        for item in expired:
            self._finish(item, "unknown", "injected prompt was not observed in a turn")

    def tick(self) -> float:
        """Run one delivery pass; return the delay before the next pass."""
        now = self.clock()
        self._expire_inflight(now)
        if not self.hosted():
            return self.slow_seconds
        snapshot = self.board.snapshot(self.profile_home)
        if snapshot is None:
            return self.slow_seconds
        episodes, events = snapshot
        waiting: set[str] = set()
        for episode in episodes:
            waiting.update(self._hosted_ids(episode))
        with self._lock:
            self._waiting = frozenset(waiting)
        if not waiting:
            return self.slow_seconds
        if now >= self._next_reconcile:
            self._next_reconcile = now + self.reconcile_seconds
            try:
                self.board.reconcile()
            except Exception:
                logger.debug("session-coord reconcile failed inertly", exc_info=True)
        for event in events:
            status = str(event.get("board_status") or "pending")
            if status in {"leased", "unknown"}:
                with self._lock:
                    ours = str(event.get("event_id") or "") in self._inflight
                if not ours:
                    try:
                        status = self.board.reconcile_receiver_receipt(
                            event, profile_home=self.profile_home
                        )
                    except Exception:
                        status = "unresolved"
            if status != "pending" or not self._hosted_ids(event):
                continue
            event_id = str(event.get("event_id") or "")
            with self._lock:
                if event_id in self._inflight:
                    continue
                retry_at = self._backoff.get(event_id, (0.0, 0.0))[0]
            if now < retry_at:
                continue
            self._deliver(event, now)
        return self.fast_seconds

    def _delivery_key(self, event: dict[str, Any]) -> str:
        target = event.get("target") if isinstance(event.get("target"), dict) else {}
        recorded = str(target.get("delivery_session_key") or "").strip()
        hosted = self.hosted()
        observed = next(
            (hosted[sid] for sid in self._hosted_ids(event) if hosted.get(sid)), ""
        )
        if recorded and observed and recorded != observed:
            logger.warning(
                "session-coord wake %s names a different session key than its live session; "
                "not delivering", event.get("event_id"),
            )
            return ""
        return recorded or observed

    def _deliver(self, candidate: dict[str, Any], now: float) -> None:
        view = _target_view(candidate, self.profile_home)
        if view is None or view.surface not in self.surfaces:
            return
        kind = str(candidate["target"].get("kind") or "")
        key = self._delivery_key(candidate)
        if not key:
            return
        try:
            validate_event_for_session(candidate, view)
        except (CoordinationReceiptError, OSError, ValueError):
            return
        event = self.board.lease(
            worker=self.worker,
            profile_home=self.profile_home,
            native_session_id=view.session_id,
            kind=kind,
        )
        if event is None:
            return
        view = _target_view(event, self.profile_home)
        try:
            if view is None:
                raise CoordinationReceiptError("leased event has no deliverable target")
            event = validate_event_for_session(event, view)
            decision = begin_admission(
                event, session=view, owner={"delivery": "inject", "worker": self.worker}
            )
        except (CoordinationReceiptError, OSError, ValueError):
            logger.debug("session-coord leased an undeliverable event", exc_info=True)
            return
        if decision.action != "start":
            outcome = {"delivered": "delivered", "canceled": "canceled", "unknown": "unknown"}
            replay = outcome.get(decision.action)
            if replay is not None:
                self._report(event, replay, decision.receipt)
            return
        event_id = str(event["event_id"])
        inflight = _Inflight(
            event=event,
            decision=decision,
            prompt=str(event["prompt"]),
            native_ids=frozenset((view.session_id, *view.compression_lineage)),
            deadline=now + self.observe_seconds,
        )
        with self._lock:
            self._inflight[event_id] = inflight
        try:
            accepted = bool(self.inject(inflight.prompt, session_key=key))
        except Exception:
            logger.warning("session-coord inject_message raised", exc_info=True)
            accepted = False
        if accepted:
            with self._lock:
                self._backoff.pop(event_id, None)
            return
        with self._lock:
            if inflight.finished:
                return
            inflight.finished = True
            self._inflight.pop(event_id, None)
            previous = self._backoff.get(event_id, (0.0, self.fast_seconds))[1]
            delay = min(self.max_backoff_seconds, max(self.fast_seconds, previous * 2))
            self._backoff[event_id] = (now + delay, delay)
        self._finish(inflight, "not_sent", "host did not accept the injected wake")
