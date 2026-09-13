"""Native turn source backed by the framework-neutral session-coord CLI."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

if __package__:
    from .session_coord_native_board import CoordinationBoardClient
    from .session_coord_native_receipts import (
        CoordinationReceiptError,
        begin_admission,
        board_receipt_from_native,
        confirm_admission,
        event_canceled_by_tombstone,
        mark_canceled,
        mark_not_sent,
        mark_unknown,
        tombstone_native_waits,
        validate_event_for_session,
    )
else:
    from session_coord_native_board import CoordinationBoardClient
    from session_coord_native_receipts import (
        CoordinationReceiptError,
        begin_admission,
        board_receipt_from_native,
        confirm_admission,
        event_canceled_by_tombstone,
        mark_canceled,
        mark_not_sent,
        mark_unknown,
        tombstone_native_waits,
        validate_event_for_session,
    )

logger = logging.getLogger(__name__)


def _owner_of(session: Any) -> dict[str, str]:
    values = {
        "owner_token": session.owner_token,
        "profile": session.profile,
        "session_key": session.session_key,
        "surface": session.surface,
    }
    return {key: str(value) for key, value in values.items() if value is not None}


def _worker_of(session: Any) -> str:
    identity = session.owner_token or session.session_key or session.session_id
    return f"session-coord-native:{identity}"


class _LeaseCallbacks:
    def __init__(self, *, board, decision, session):
        self.board = board
        self.decision = decision
        self.session = session
        self._lock = threading.Lock()
        self._finished = False
        self._commit_result = False

    def _report(self, outcome: str, receipt: dict[str, Any], expected_status: str) -> bool:
        try:
            proof = board_receipt_from_native(
                self.decision.event, receipt, expected_status=expected_status
            )
            return bool(self.board.result(self.decision.event, outcome=outcome, receipt=proof))
        except Exception:
            logger.warning("Unable to report exact session-coord receiver receipt", exc_info=True)
            return False

    def commit(self, session: Any) -> bool:
        with self._lock:
            if self._finished:
                return self._commit_result
            try:
                validate_event_for_session(self.decision.event, session)
                if _owner_of(session) != self.decision.receipt.get("owner", {}):
                    raise CoordinationReceiptError(
                        "native lease owner changed before prompt admission"
                    )
            except CoordinationReceiptError as exc:
                receipt = mark_not_sent(self.decision, str(exc))
                self._report("not_sent", receipt, "not_sent")
                self._finished = True
                return False
            if event_canceled_by_tombstone(self.decision.event, session.profile_home):
                receipt = mark_canceled(
                    self.decision, "explicit user boundary before native admission"
                )
                self._report("canceled", receipt, "canceled")
                self._finished = True
                return False
            try:
                receipt = confirm_admission(self.decision)
            except (CoordinationReceiptError, OSError) as exc:
                try:
                    receipt = mark_unknown(self.decision, str(exc))
                    self._report("unknown", receipt, "unknown")
                except (CoordinationReceiptError, OSError):
                    logger.warning(
                        "Unable to persist unknown session-coord admission", exc_info=True
                    )
                self._finished = True
                return False
            if receipt.get("status") == "canceled":
                self._report("canceled", receipt, "canceled")
                self._finished = True
                return False
            self._report("delivered", receipt, "admitted")
            self._commit_result = True
            self._finished = True
            return True

    def abort(self, outcome: str, reason: str) -> None:
        finishers = {
            "not_sent": (mark_not_sent, "not_sent", "not_sent"),
            "canceled": (mark_canceled, "canceled", "canceled"),
            "unknown": (mark_unknown, "unknown", "unknown"),
        }
        if outcome not in finishers:
            raise ValueError(f"invalid native lease abort outcome: {outcome}")
        with self._lock:
            if self._finished:
                return
            finisher, board_outcome, expected_status = finishers[outcome]
            receipt = finisher(self.decision, str(reason))
            self._report(board_outcome, receipt, expected_status)
            self._finished = True


class SessionCoordNativeSource:
    name = "session-coord-native"

    def __init__(
        self,
        *,
        board_script: Path,
        lease_type,
        surfaces: tuple[str, ...],
        board=None,
        poll_interval_seconds: float = 1.0,
    ):
        self.board_script = Path(board_script)
        self.lease_type = lease_type
        self.surfaces = surfaces
        self.board = board or CoordinationBoardClient(self.board_script)
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._poll_lock = threading.Lock()
        self._next_poll: dict[tuple[str, str, str], float] = {}

    def poll(self, session: Any):
        if str(session.surface) not in self.surfaces:
            return None
        home = Path(session.profile_home).expanduser().resolve()
        poll_key = (str(home), str(session.surface), str(session.session_key))
        now = time.monotonic()
        with self._poll_lock:
            if now < self._next_poll.get(poll_key, 0.0):
                return None
            self._next_poll[poll_key] = now + self.poll_interval_seconds
        candidate = None
        try:
            pending_events = self.board.pending(home)
        except Exception:
            logger.debug("session-coord poll failed inertly", exc_info=True)
            return None
        for event in pending_events:
            status = str(event.get("board_status") or "pending")
            if status in {"leased", "unknown"}:
                reconcile = getattr(self.board, "reconcile_receiver_receipt", None)
                try:
                    status = (
                        reconcile(event, profile_home=home) if callable(reconcile) else "unresolved"
                    )
                except Exception:
                    logger.debug(
                        "session-coord receipt reconciliation failed inertly",
                        exc_info=True,
                    )
                    status = "unresolved"
            if status != "pending":
                continue
            try:
                candidate = validate_event_for_session(event, session)
            except (CoordinationReceiptError, OSError, ValueError):
                continue
            break
        if candidate is None:
            return None
        kind = str(candidate["target"].get("kind") or "")
        try:
            event = self.board.lease(
                worker=_worker_of(session),
                profile_home=home,
                native_session_id=str(candidate["native_session_id"]),
                kind=kind,
            )
        except Exception:
            logger.debug("session-coord lease failed inertly", exc_info=True)
            return None
        if event is None:
            return None
        try:
            event = validate_event_for_session(event, session)
            decision = begin_admission(event, session=session, owner=_owner_of(session))
        except (CoordinationReceiptError, OSError, ValueError):
            logger.debug(
                "session-coord returned an invalid leased event; leaving it unresolved",
                exc_info=True,
            )
            return None
        if decision.action != "start":
            outcome = {
                "delivered": "delivered",
                "canceled": "canceled",
                "unknown": "unknown",
            }.get(decision.action)
            if outcome is not None:
                expected = {
                    "delivered": "admitted",
                    "canceled": "canceled",
                    "unknown": "unknown",
                }[outcome]
                try:
                    proof = board_receipt_from_native(
                        event, decision.receipt, expected_status=expected
                    )
                except (CoordinationReceiptError, OSError, ValueError):
                    proof = decision.receipt
                try:
                    self.board.result(event, outcome=outcome, receipt=proof)
                except Exception:
                    logger.debug(
                        "session-coord receipt replay acknowledgement failed inertly",
                        exc_info=True,
                    )
            return None
        callbacks = _LeaseCallbacks(board=self.board, decision=decision, session=session)
        return self.lease_type(
            lease_id=f"{event['event_id']}:{int(event.get('attempt') or 0)}",
            prompt=event["prompt"],
            commit=callbacks.commit,
            abort=callbacks.abort,
            display_kind="coordination_resume",
        )

    def on_user_boundary(self, session: Any, reason: str) -> None:
        home = Path(session.profile_home).expanduser().resolve()
        target_ids = []
        for value in (session.session_id, *session.compression_lineage):
            value = str(value or "")
            if value and value not in target_ids:
                target_ids.append(value)
        for native_session_id in target_ids:
            try:
                tombstone_native_waits(home, native_session_id, str(reason))
            except (CoordinationReceiptError, OSError):
                logger.warning("Unable to persist session-coord boundary tombstone", exc_info=True)
            try:
                self.board.cancel(
                    profile_home=home,
                    native_session_id=native_session_id,
                    reason=str(reason),
                )
            except Exception:
                logger.warning("Unable to cancel session-coord board waits", exc_info=True)
