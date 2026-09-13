from __future__ import annotations

import importlib
import json
from pathlib import Path

from conftest import FrozenNativeSessionView, FrozenNativeTurnLease


def _event(home: Path, **updates):
    event = {
        "event_id": "event-1",
        "attempt": 1,
        "episode_id": "episode-1",
        "board_session_id": "board-1",
        "native_session_id": "native-1",
        "profile_home": str(home.resolve()),
        "transport": "hermes",
        "target": {
            "profile": "default",
            "profile_home": str(home.resolve()),
            "kind": "cli",
            "session_id": "native-1",
            "compression_lineage": ["native-1"],
        },
        "checkpoint_path": str((home / "checkpoint.json").resolve()),
        "reason": "released",
        "verify_required": False,
        "prompt": "Resume the durable coordination episode.",
        "episode_created_at": 1_700_000_000.0,
        "created_at": 1_700_000_001.0,
    }
    event.update(updates)
    return event


def _session(home: Path, **updates):
    values = {
        "profile_home": home,
        "profile": "default",
        "surface": "cli",
        "session_id": "native-1",
        "session_key": "route-1",
        "compression_lineage": (),
        "owner_token": "owner-1",
    }
    values.update(updates)
    return FrozenNativeSessionView(**values)


class Board:
    def __init__(self, event):
        self.event = event
        self.results = []
        self.leases = []

    def pending(self, _home):
        return [dict(self.event, board_status="pending")]

    def lease(self, **kwargs):
        self.leases.append(kwargs)
        return dict(self.event)

    def result(self, event, *, outcome, receipt):
        self.results.append((event, outcome, receipt))
        return True


def test_poll_returns_opaque_lease_and_commit_durably_acknowledges_exact_event(
    plugin_loader, tmp_path
):
    home = tmp_path / "profile"
    home.mkdir()
    event = _event(home)
    board = Board(event)
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )
    session = _session(home)

    lease = source.poll(session)

    assert isinstance(lease, FrozenNativeTurnLease)
    assert lease.lease_id == "event-1:1"
    assert lease.prompt == event["prompt"]
    assert lease.display_kind == "coordination_resume"
    assert board.leases == [
        {
            "worker": "session-coord-native:owner-1",
            "profile_home": home.resolve(),
            "native_session_id": "native-1",
            "kind": "cli",
        }
    ]

    assert lease.commit(session) is True
    assert len(board.results) == 1
    _, outcome, board_receipt = board.results[0]
    assert outcome == "delivered"
    assert board_receipt["event_id"] == "event-1"
    assert board_receipt["native_session_id"] == "native-1"
    assert board_receipt["admitted"] is True
    assert board_receipt["durable"] is True
    assert len(board_receipt["payload_hash"]) == 64

    receipt_files = list(
        (home / "runtime" / "coordination_wakes" / "receipts").glob("*.json")
    )
    assert len(receipt_files) == 1
    stored = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert stored == {key: board_receipt[key] for key in stored}
    assert stored["status"] == "admitted"
    assert stored["owner"] == {
        "owner_token": "owner-1",
        "profile": "default",
        "session_key": "route-1",
        "surface": "cli",
    }


def test_commit_refuses_when_reserved_native_owner_changes(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    event = _event(home)
    board = Board(event)
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )
    session = _session(home)
    lease = source.poll(session)
    assert lease is not None

    committed = lease.commit(_session(home, owner_token="replacement-owner"))

    assert committed is False
    assert len(board.results) == 1
    _, outcome, receipt = board.results[0]
    assert outcome == "not_sent"
    assert receipt["status"] == "not_sent"
    assert receipt["not_sent"] is True
    assert receipt["submitted"] is False
    assert "owner changed" in receipt["reason"]


def test_poll_is_inert_when_board_client_raises(plugin_loader, tmp_path):
    class BrokenBoard:
        def pending(self, _home):
            raise OSError("board unavailable")

    home = tmp_path / "profile"
    home.mkdir()
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=BrokenBoard(),
    )

    assert source.poll(_session(home)) is None


def test_poll_is_inert_when_board_lease_raises(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()

    class BrokenLeaseBoard(Board):
        def lease(self, **kwargs):
            raise OSError("lease unavailable")

    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=BrokenLeaseBoard(_event(home)),
    )

    assert source.poll(_session(home)) is None


def test_wrong_exact_target_never_reaches_board_lease(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    board = Board(
        _event(
            home,
            native_session_id="other-native",
            target={
                "profile_home": str(home.resolve()),
                "kind": "cli",
                "session_id": "other-native",
            },
        )
    )
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )

    assert source.poll(_session(home)) is None
    assert board.leases == []
    assert board.results == []


def test_poll_is_inert_when_receiver_reconciliation_raises(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()

    class BrokenReconcileBoard(Board):
        def pending(self, _home):
            return [dict(self.event, board_status="unknown")]

        def reconcile_receiver_receipt(self, _event, *, profile_home):
            raise OSError(f"receipt store unavailable: {profile_home}")

    board = BrokenReconcileBoard(_event(home))
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )

    assert source.poll(_session(home)) is None
    assert board.leases == []


def test_durable_commit_survives_board_ack_exception_for_later_reconciliation(
    plugin_loader, tmp_path
):
    home = tmp_path / "profile"
    home.mkdir()

    class AckFailureBoard(Board):
        def result(self, event, *, outcome, receipt):
            raise RuntimeError(f"ack unavailable for {event['event_id']}:{outcome}")

    board = AckFailureBoard(_event(home))
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )
    session = _session(home)
    lease = source.poll(session)
    assert lease is not None

    assert lease.commit(session) is True
    receipt_files = list(
        (home / "runtime" / "coordination_wakes" / "receipts").glob("*.json")
    )
    assert len(receipt_files) == 1
    assert json.loads(receipt_files[0].read_text(encoding="utf-8"))["status"] == "admitted"


def test_invalid_leased_event_and_result_failure_stay_inert(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()

    class InvalidLeaseBoard(Board):
        def lease(self, **kwargs):
            self.leases.append(kwargs)
            return _event(
                home,
                native_session_id="wrong-native",
                target={
                    "profile_home": str(home.resolve()),
                    "kind": "cli",
                    "session_id": "wrong-native",
                },
            )

        def result(self, event, *, outcome, receipt):
            raise RuntimeError(f"cannot reject {event['event_id']}:{outcome}:{receipt}")

    board = InvalidLeaseBoard(_event(home))
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )

    assert source.poll(_session(home)) is None


def test_replayed_durable_receipt_ack_failure_stays_inert(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()

    class ReplayBoard(Board):
        fail_results = False

        def result(self, event, *, outcome, receipt):
            if self.fail_results:
                raise RuntimeError(f"replay ack unavailable: {event['event_id']}:{outcome}")
            return super().result(event, outcome=outcome, receipt=receipt)

    board = ReplayBoard(_event(home))
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )
    session = _session(home)
    first = source.poll(session)
    assert first is not None
    assert first.commit(session) is True
    board.fail_results = True

    assert source.poll(session) is None


def test_subagent_parent_target_requires_exact_parent_session_id(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    board = Board(
        _event(
            home,
            target={
                "profile": "default",
                "profile_home": str(home.resolve()),
                "kind": "subagent_parent",
                "session_id": "native-1",
                "compression_lineage": ["native-1"],
            },
        )
    )
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )

    assert source.poll(_session(home)) is None
    assert board.leases == []


def test_valid_subagent_parent_target_routes_to_exact_parent(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    event = _event(
        home,
        target={
            "profile": "default",
            "profile_home": str(home.resolve()),
            "kind": "subagent_parent",
            "session_id": "native-1",
            "compression_lineage": ["native-1"],
            "parent_session_id": "native-1",
        },
        prompt="Redispatch the completed child from its durable checkpoint.",
    )
    board = Board(event)
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )

    lease = source.poll(_session(home))

    assert lease is not None
    assert lease.prompt == event["prompt"]
    assert board.leases[0]["kind"] == "subagent_parent"


def test_user_boundary_tombstones_lease_before_commit_and_cancels_lineage(
    plugin_loader, tmp_path
):
    home = tmp_path / "profile"
    home.mkdir()

    class CancelBoard(Board):
        def __init__(self, event):
            super().__init__(event)
            self.cancels = []

        def cancel(self, **kwargs):
            self.cancels.append(kwargs)
            return True

    board = CancelBoard(_event(home))
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )
    session = _session(home, compression_lineage=("ancestor-1", "native-1"))
    lease = source.poll(session)
    assert lease is not None

    source.on_user_boundary(session, "explicit Stop")

    assert lease.commit(session) is False
    assert [entry["native_session_id"] for entry in board.cancels] == [
        "native-1",
        "ancestor-1",
    ]
    assert all(entry["reason"] == "explicit Stop" for entry in board.cancels)
    assert board.results[-1][1] == "canceled"
    assert board.results[-1][2]["status"] == "canceled"


def test_abort_unknown_is_durable_idempotent_and_never_commits(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    board = Board(_event(home))
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )
    session = _session(home)
    lease = source.poll(session)
    assert lease is not None

    lease.abort("unknown", "host reservation outcome is uncertain")
    lease.abort("unknown", "duplicate host callback")

    assert lease.commit(session) is False
    assert len(board.results) == 1
    assert board.results[0][1] == "unknown"
    assert board.results[0][2]["status"] == "unknown"
    assert board.results[0][2]["reason"] == "host reservation outcome is uncertain"


def test_ambiguous_attempting_receipt_is_never_released_for_retry(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()

    class UnknownBoard(Board):
        def pending(self, _home):
            return [dict(self.event, board_status="unknown")]

        def reconcile_receiver_receipt(self, event, *, profile_home):
            assert event["event_id"] == "event-1"
            assert profile_home == home.resolve()
            return "unresolved"

    board = UnknownBoard(_event(home))
    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli", "tui", "gateway"),
        board=board,
    )

    assert source.poll(_session(home)) is None
    assert board.leases == []
    assert board.results == []


def test_poll_is_throttled_per_exact_session(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()

    class EmptyBoard(Board):
        def __init__(self):
            super().__init__(_event(home))
            self.pending_calls = 0

        def pending(self, _home):
            self.pending_calls += 1
            return []

    plugin = plugin_loader()
    board = EmptyBoard()
    source = plugin.SessionCoordNativeSource(
        board_script=tmp_path / "unused.py",
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli",),
        board=board,
        poll_interval_seconds=60,
    )

    assert source.poll(_session(home)) is None
    assert source.poll(_session(home)) is None
    assert board.pending_calls == 1


def test_tombstone_and_admission_commit_are_one_atomic_decision(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    plugin = plugin_loader()
    receipts = importlib.import_module(
        f"{plugin.__name__}.session_coord_native_receipts"
    )
    session = _session(home)
    event = _event(home)
    decision = receipts.begin_admission(event, session=session, owner={})

    receipts.tombstone_native_waits(home, session.session_id, "user stop")
    receipt = receipts.confirm_admission(decision)

    assert receipt["status"] == "canceled"
    stored = receipts.read_admission_receipt(home, event["event_id"])
    assert stored is not None
    assert stored["status"] == "canceled"
