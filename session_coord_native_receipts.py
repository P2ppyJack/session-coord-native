"""Durable receiver receipts and user-boundary tombstones.

The board owns wake state; this module owns only the exact native receiver proof.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_RECEIPT_DIR = "coordination_wakes"
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_REQUIRED_EVENT_KEYS = frozenset(
    {
        "event_id",
        "attempt",
        "episode_id",
        "board_session_id",
        "native_session_id",
        "profile_home",
        "transport",
        "target",
        "checkpoint_path",
        "reason",
        "verify_required",
        "prompt",
        "created_at",
    }
)
_PAYLOAD_KEYS = (
    "event_id",
    "episode_id",
    "board_session_id",
    "native_session_id",
    "profile_home",
    "transport",
    "target",
    "checkpoint_path",
    "reason",
    "verify_required",
    "prompt",
    "created_at",
)
_ALLOWED_KINDS = {
    "cli": frozenset({"cli", "subagent_parent"}),
    "tui": frozenset({"desktop", "bot", "subagent_parent"}),
    "gateway": frozenset({"gateway", "subagent_parent"}),
}


class CoordinationReceiptError(ValueError):
    """A wake event or receiver receipt violates the exact-target contract."""


@dataclass(frozen=True)
class AdmissionDecision:
    action: str
    event: dict[str, Any]
    receipt: dict[str, Any]
    path: Path
    token: str = ""


def _root(profile_home: Path | str) -> Path:
    return Path(profile_home).expanduser().resolve() / "runtime" / _RECEIPT_DIR


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _receipt_path(root: Path, event_id: str) -> Path:
    return root / "receipts" / f"{_safe_key(event_id)}.json"


def _tombstone_path(root: Path, native_session_id: str) -> Path:
    return root / "tombstones" / f"{_safe_key(native_session_id)}.json"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _locked(profile_home: Path | str):
    root = _root(profile_home)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    for child in (root / "receipts", root / "tombstones"):
        child.mkdir(mode=0o700, exist_ok=True)
        child.chmod(0o700)
    lock_path = root / ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise CoordinationReceiptError(f"invalid coordination receipt: {path}")
    return value


def _write(path: Path, record: Mapping[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".coord-wake-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dict(record), stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_home(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


def _validate_event(event: Mapping[str, Any], profile_home: Path) -> dict[str, Any]:
    missing = sorted(_REQUIRED_EVENT_KEYS - event.keys())
    if missing:
        raise CoordinationReceiptError(f"coordination event missing keys: {', '.join(missing)}")
    clean = dict(event)
    event_id = clean.get("event_id")
    if not isinstance(event_id, str) or _EVENT_ID_RE.fullmatch(event_id) is None:
        raise CoordinationReceiptError("invalid coordination event id")
    if clean.get("transport") != "hermes":
        raise CoordinationReceiptError("coordination event has wrong transport")
    home_value = clean.get("profile_home")
    if not isinstance(home_value, str) or not home_value or not Path(home_value).is_absolute():
        raise CoordinationReceiptError("coordination event requires an absolute profile home")
    if _canonical_home(home_value) != profile_home:
        raise CoordinationReceiptError("coordination event belongs to a different profile home")
    target = clean.get("target")
    if not isinstance(target, dict):
        raise CoordinationReceiptError("coordination event target must be an object")
    # Legacy targets identify a profile by its exact home, without a label.
    # Validate supplied labels, but never invent one from a directory name.
    if "profile" in target and (not isinstance(target["profile"], str) or not target["profile"]):
        raise CoordinationReceiptError("coordination target profile must be a non-empty string")
    target_home = target.get("profile_home")
    if not isinstance(target_home, str) or not target_home or not Path(target_home).is_absolute():
        raise CoordinationReceiptError("coordination target requires an absolute profile home")
    if _canonical_home(target_home) != profile_home:
        raise CoordinationReceiptError("coordination target belongs to a different profile home")
    if str(target.get("session_id") or "") != str(clean.get("native_session_id") or ""):
        raise CoordinationReceiptError("coordination target session does not match native session")
    target_lineage = target.get("compression_lineage", [target.get("session_id")])
    if (
        not isinstance(target_lineage, list)
        or not target_lineage
        or any(not isinstance(item, str) or not item for item in target_lineage)
        or str(target.get("session_id")) not in target_lineage
    ):
        raise CoordinationReceiptError(
            "coordination target compression_lineage must contain its session_id"
        )
    if not isinstance(clean.get("prompt"), str) or not clean["prompt"]:
        raise CoordinationReceiptError("coordination event prompt is empty")
    return clean


def validate_event_for_session(event: Mapping[str, Any], session: Any) -> dict[str, Any]:
    home = _canonical_home(session.profile_home)
    clean = _validate_event(event, home)
    surface = str(session.surface)
    kind = str(clean["target"].get("kind") or "")
    if kind not in _ALLOWED_KINDS.get(surface, frozenset()):
        raise CoordinationReceiptError(
            f"coordination target kind {kind!r} is not supported on surface {surface!r}"
        )
    if kind == "subagent_parent":
        parent_session_id = str(clean["target"].get("parent_session_id") or "")
        if not parent_session_id:
            raise CoordinationReceiptError("subagent parent target is missing parent_session_id")
        if parent_session_id != str(clean["target"].get("session_id") or ""):
            raise CoordinationReceiptError(
                "subagent parent target session does not match parent_session_id"
            )
    lineage = {str(session.session_id), *(str(value) for value in session.compression_lineage)}
    if str(clean["native_session_id"]) not in lineage:
        raise CoordinationReceiptError("coordination event targets a different session lineage")
    target_profile = clean["target"].get("profile")
    if target_profile is not None and target_profile != str(session.profile):
        raise CoordinationReceiptError("coordination event targets a different profile")
    target_lineage = clean["target"].get("compression_lineage", [clean["target"]["session_id"]])
    if any(value not in lineage for value in target_lineage):
        raise CoordinationReceiptError("coordination event carries a stale session lineage")
    target_key = clean["target"].get("session_key")
    if target_key is not None and str(target_key) != str(session.session_key or ""):
        raise CoordinationReceiptError("coordination event targets a different session key")
    return clean


def _payload_hash(event: Mapping[str, Any]) -> str:
    payload = {key: event.get(key) for key in _PAYLOAD_KEYS}
    if "episode_created_at" in event:
        payload["episode_created_at"] = event.get("episode_created_at")
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _created_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 100_000_000_000_000_000:
            return number / 1_000_000_000
        if number > 100_000_000_000_000:
            return number / 1_000_000
        if number > 100_000_000_000:
            return number / 1_000
        return number
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        return None


def _canceled_by_tombstone(root: Path, event: Mapping[str, Any]) -> dict[str, Any] | None:
    tombstone = _read(_tombstone_path(root, str(event.get("native_session_id") or "")))
    if tombstone is None:
        return None
    created = _created_timestamp(event.get("episode_created_at"))
    if created is None or created <= float(tombstone.get("canceled_at") or 0):
        return tombstone
    return None


def begin_admission(
    event: Mapping[str, Any], *, session: Any, owner: Mapping[str, Any] | None = None
) -> AdmissionDecision:
    home = _canonical_home(session.profile_home)
    clean = validate_event_for_session(event, session)
    payload_hash = _payload_hash(clean)
    with _locked(home) as root:
        path = _receipt_path(root, clean["event_id"])
        existing = _read(path)
        if existing is not None:
            if existing.get("payload_sha256") != payload_hash:
                raise CoordinationReceiptError(
                    "coordination event id already belongs to a different payload"
                )
            status = str(existing.get("status") or "")
            action = {
                "admitted": "delivered",
                "canceled": "canceled",
                "unknown": "unknown",
                "attempting": "unknown",
            }.get(status)
            if action is not None:
                return AdmissionDecision(action, clean, existing, path)
            if status != "not_sent":
                raise CoordinationReceiptError(f"invalid coordination receipt status: {status}")
        tombstone = _canceled_by_tombstone(root, clean)
        if tombstone is not None:
            record = {
                "event_id": clean["event_id"],
                "episode_id": clean["episode_id"],
                "native_session_id": clean["native_session_id"],
                "current_session_id": str(session.session_id),
                "profile_home": str(home),
                "payload_sha256": payload_hash,
                "prompt_sha256": hashlib.sha256(clean["prompt"].encode("utf-8")).hexdigest(),
                "status": "canceled",
                "reason": tombstone.get("reason") or "user boundary",
                "canceled_at": time.time(),
            }
            _write(path, record)
            return AdmissionDecision("canceled", clean, record, path)
        token = uuid.uuid4().hex
        record = {
            "event_id": clean["event_id"],
            "episode_id": clean["episode_id"],
            "native_session_id": clean["native_session_id"],
            "current_session_id": str(session.session_id),
            "profile_home": str(home),
            "payload_sha256": payload_hash,
            "prompt_sha256": hashlib.sha256(clean["prompt"].encode("utf-8")).hexdigest(),
            "status": "attempting",
            "attempt": int(clean.get("attempt") or 0),
            "admission_token": token,
            "owner": dict(owner or {}),
            "attempting_at": time.time(),
        }
        _write(path, record)
        return AdmissionDecision("start", clean, record, path, token)


def _finish_admission(
    decision: AdmissionDecision,
    status: str,
    *,
    reject_if_tombstoned: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    if decision.action != "start" or not decision.token:
        raise CoordinationReceiptError("admission decision is not startable")
    home = Path(decision.receipt["profile_home"])
    with _locked(home) as root:
        path = _receipt_path(root, decision.event["event_id"])
        record = _read(path)
        if record is None:
            raise CoordinationReceiptError("coordination admission receipt disappeared")
        if record.get("payload_sha256") != decision.receipt.get("payload_sha256"):
            raise CoordinationReceiptError("coordination receipt payload changed")
        if record.get("status") == status:
            return record
        if record.get("status") != "attempting" or record.get("admission_token") != decision.token:
            raise CoordinationReceiptError("coordination admission is owned by another attempt")
        if reject_if_tombstoned:
            tombstone = _canceled_by_tombstone(root, decision.event)
            if tombstone is not None:
                record.update(
                    status="canceled",
                    canceled_at=time.time(),
                    reason=tombstone.get("reason") or "user boundary",
                )
                record.pop("admission_token", None)
                _write(path, record)
                return record
        record.update(status=status, **fields)
        record.pop("admission_token", None)
        _write(path, record)
        return record


def confirm_admission(decision: AdmissionDecision) -> dict[str, Any]:
    return _finish_admission(
        decision,
        "admitted",
        admitted_at=time.time(),
        reject_if_tombstoned=True,
    )


def mark_not_sent(decision: AdmissionDecision, reason: str) -> dict[str, Any]:
    return _finish_admission(decision, "not_sent", not_sent_at=time.time(), reason=str(reason))


def mark_unknown(decision: AdmissionDecision, reason: str) -> dict[str, Any]:
    return _finish_admission(decision, "unknown", unknown_at=time.time(), reason=str(reason))


def mark_canceled(decision: AdmissionDecision, reason: str) -> dict[str, Any]:
    return _finish_admission(decision, "canceled", canceled_at=time.time(), reason=str(reason))


def event_canceled_by_tombstone(event: Mapping[str, Any], profile_home: Path | str) -> bool:
    home = _canonical_home(profile_home)
    clean = _validate_event(event, home)
    with _locked(home) as root:
        return _canceled_by_tombstone(root, clean) is not None


def tombstone_native_waits(
    profile_home: Path | str, native_session_id: str, reason: str
) -> dict[str, Any]:
    if not native_session_id:
        raise CoordinationReceiptError("native session id is required")
    home = _canonical_home(profile_home)
    record = {
        "native_session_id": str(native_session_id),
        "profile_home": str(home),
        "reason": str(reason or "user boundary"),
        "canceled_at": time.time(),
    }
    with _locked(home) as root:
        _write(_tombstone_path(root, str(native_session_id)), record)
    return record


def read_admission_receipt(profile_home: Path | str, event_id: str) -> dict[str, Any] | None:
    if not isinstance(event_id, str) or _EVENT_ID_RE.fullmatch(event_id) is None:
        raise CoordinationReceiptError("invalid coordination event id")
    return _read(_receipt_path(_root(profile_home), event_id))


def board_receipt_from_native(
    event: Mapping[str, Any], receipt: Mapping[str, Any], *, expected_status: str
) -> dict[str, Any]:
    home = _canonical_home(str(event.get("profile_home") or ""))
    clean = _validate_event(event, home)
    stored = read_admission_receipt(home, clean["event_id"])
    if stored is None or _canonical_json(stored) != _canonical_json(dict(receipt)):
        raise CoordinationReceiptError("coordination receipt has no exact durable readback")
    expected_payload = _payload_hash(clean)
    expected_prompt = hashlib.sha256(clean["prompt"].encode("utf-8")).hexdigest()
    if (
        stored.get("status") != expected_status
        or stored.get("event_id") != clean["event_id"]
        or stored.get("native_session_id") != clean["native_session_id"]
        or _canonical_home(str(stored.get("profile_home") or "")) != home
        or stored.get("payload_sha256") != expected_payload
        or stored.get("prompt_sha256") != expected_prompt
    ):
        raise CoordinationReceiptError("coordination receipt does not match the immutable event")
    proof = dict(stored)
    proof["payload_hash"] = expected_payload
    if expected_status == "admitted":
        proof.update(admitted=True, durable=True)
    elif expected_status == "not_sent":
        proof.update(not_sent=True, submitted=False)
    return proof
