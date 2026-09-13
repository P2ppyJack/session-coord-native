"""Fail-inert JSON subprocess client for the framework-neutral board CLI."""

from __future__ import annotations

import json
import logging
import os
import subprocess  # nosec B404
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __package__:
    from .session_coord_native_receipts import (
        CoordinationReceiptError,
        board_receipt_from_native,
        read_admission_receipt,
    )
else:
    from session_coord_native_receipts import (
        CoordinationReceiptError,
        board_receipt_from_native,
        read_admission_receipt,
    )

logger = logging.getLogger(__name__)
_OUTCOMES = frozenset({"delivered", "not_sent", "unknown", "canceled"})
_UNACKNOWLEDGED = frozenset({"leased", "unknown"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CoordinationBoardClient:
    """Invoke only public board verbs and parse their final JSON line."""

    def __init__(self, script_path: Path | str, *, timeout: float = 8.0):
        self.script_path = Path(script_path).expanduser().resolve()
        self.timeout = float(timeout)

    @property
    def available(self) -> bool:
        return self.script_path.is_file()

    def _run(self, args: list[str]) -> tuple[int, Any] | None:
        if not self.available:
            return None
        env = os.environ.copy()
        env.pop("HERMES_HOME", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            # The configured board script is trusted local code; argv is never shell-parsed.
            completed = subprocess.run(  # nosec B603
                [sys.executable, str(self.script_path), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("session-coord command unavailable", exc_info=True)
            return None
        if completed.returncode not in {0, 75}:
            logger.debug(
                "session-coord %s failed rc=%s: %s",
                args[0] if args else "",
                completed.returncode,
                completed.stderr.strip()[:300],
            )
            return None
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            return completed.returncode, None
        try:
            return completed.returncode, json.loads(lines[-1])
        except json.JSONDecodeError:
            logger.warning("session-coord emitted non-JSON output for %s", args[0])
            return None

    def pending(self, profile_home: Path | str) -> list[dict[str, Any]]:
        response = self._run(
            [
                "wake-pending",
                "--profile-home",
                str(Path(profile_home).expanduser().resolve()),
                "--json",
            ]
        )
        payload = response[1] if response is not None else None
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("events", payload.get("pending", payload.get("wakes", [])))
        else:
            rows = []
        events: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            nested = row.get("event")
            if isinstance(nested, dict):
                event = dict(nested)
                event["board_status"] = str(row.get("status") or "")
                events.append(event)
            else:
                events.append(dict(row))
        return events

    def lease(
        self,
        *,
        worker: str,
        profile_home: Path | str,
        native_session_id: str,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        args = [
            "wake-lease",
            "--worker",
            str(worker),
            "--transport",
            "hermes",
            "--profile-home",
            str(Path(profile_home).expanduser().resolve()),
            "--native-session-id",
            str(native_session_id),
        ]
        if kind:
            args.extend(["--kind", str(kind)])
        args.append("--json")
        response = self._run(args)
        payload = response[1] if response is not None else None
        event = payload.get("event") if isinstance(payload, dict) else None
        return dict(event) if isinstance(event, dict) else None

    def result(self, event: Mapping[str, Any], *, outcome: str, receipt: Mapping[str, Any]) -> bool:
        if outcome not in _OUTCOMES:
            raise ValueError(f"invalid coordination wake outcome: {outcome}")
        expected_status = {
            "delivered": "admitted",
            "not_sent": "not_sent",
            "unknown": "unknown",
            "canceled": "canceled",
        }[outcome]
        supplied = dict(receipt)
        native_receipt = dict(supplied)
        for field in ("payload_hash", "admitted", "durable", "not_sent", "submitted"):
            native_receipt.pop(field, None)
        try:
            proof = board_receipt_from_native(
                event, native_receipt, expected_status=expected_status
            )
        except (CoordinationReceiptError, OSError, ValueError):
            logger.debug(
                "session-coord result lacks an exact durable native receipt",
                exc_info=True,
            )
            return False
        if supplied != native_receipt and _canonical_json(supplied) != _canonical_json(proof):
            logger.debug("session-coord result proof fields do not match durable readback")
            return False
        response = self._run(
            [
                "wake-result",
                "--event",
                str(event.get("event_id") or ""),
                "--attempt",
                str(int(event.get("attempt") or 0)),
                "--outcome",
                outcome,
                "--receipt-json",
                _canonical_json(proof),
                "--json",
            ]
        )
        return response is not None and response[0] == 0

    def reconcile_receiver_receipt(
        self, event: Mapping[str, Any], *, profile_home: Path | str
    ) -> str:
        status = str(event.get("board_status") or "pending")
        if status == "pending":
            return "pending"
        if status in {"delivered", "canceled"}:
            return "settled"
        if status not in _UNACKNOWLEDGED:
            return "unresolved"
        home = Path(profile_home).expanduser().resolve()
        try:
            if Path(str(event.get("profile_home") or "")).expanduser().resolve() != home:
                return "unresolved"
            receipt = read_admission_receipt(home, str(event.get("event_id") or ""))
            native_status = str((receipt or {}).get("status") or "")
            outcome = {
                "admitted": "delivered",
                "not_sent": "not_sent",
                "canceled": "canceled",
            }.get(native_status)
            if outcome is None or receipt is None:
                return "unresolved"
            expected = {
                "delivered": "admitted",
                "not_sent": "not_sent",
                "canceled": "canceled",
            }[outcome]
            proof = board_receipt_from_native(event, receipt, expected_status=expected)
            if not self.result(event, outcome=outcome, receipt=proof):
                return "unresolved"
            return "pending" if outcome == "not_sent" else "settled"
        except (CoordinationReceiptError, OSError, ValueError):
            logger.debug("receiver receipt reconciliation failed", exc_info=True)
            return "unresolved"

    def cancel(self, *, profile_home: Path | str, native_session_id: str, reason: str) -> bool:
        response = self._run(
            [
                "cancel-wait",
                "--native-session-id",
                str(native_session_id),
                "--profile-home",
                str(Path(profile_home).expanduser().resolve()),
                "--reason",
                str(reason),
                "--json",
            ]
        )
        return response is not None and response[0] == 0

    def reconcile(self) -> dict[str, Any] | None:
        response = self._run(["wake-reconcile", "--json"])
        payload = response[1] if response is not None else None
        return dict(payload) if isinstance(payload, dict) else None
