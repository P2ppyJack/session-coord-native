from __future__ import annotations

import json
import sys

from conftest import FrozenNativeSessionView
from test_native_source import _event


def _modules(plugin):
    return (
        sys.modules[f"{plugin.__name__}.session_coord_native_board"],
        sys.modules[f"{plugin.__name__}.session_coord_native_receipts"],
    )


def test_board_result_requires_exact_durable_native_receipt(plugin_loader, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    event = _event(home)
    calls = tmp_path / "calls.jsonl"
    fixture = tmp_path / "board_fixture.py"
    fixture.write_text(
        "import json, pathlib, sys\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        "with calls.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'ok': True}))\n",
        encoding="utf-8",
    )
    plugin = plugin_loader()
    board_module, receipt_module = _modules(plugin)
    board = board_module.CoordinationBoardClient(fixture)
    session = FrozenNativeSessionView(
        profile_home=home,
        profile="default",
        surface="cli",
        session_id="native-1",
        session_key="route-1",
        compression_lineage=(),
        owner_token="owner-1",
    )
    decision = receipt_module.begin_admission(event, session=session)
    receipt = receipt_module.confirm_admission(decision)

    assert board.result(
        event,
        outcome="delivered",
        receipt={**receipt, "payload_sha256": "fabricated"},
    ) is False
    assert not calls.exists()

    assert board.result(event, outcome="delivered", receipt=receipt) is True
    argv = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])
    proof = json.loads(argv[argv.index("--receipt-json") + 1])
    assert proof["admitted"] is True
    assert proof["durable"] is True
    assert proof["payload_hash"] == receipt["payload_sha256"]


def test_board_client_uses_exact_target_argv_without_profile_board_ambient_state(
    plugin_loader, tmp_path, monkeypatch
):
    home = tmp_path / "profile"
    home.mkdir()
    event = _event(home)
    calls = tmp_path / "calls.jsonl"
    fixture = tmp_path / "board_fixture.py"
    fixture.write_text(
        "import json, os, pathlib, sys\n"
        f"event = json.loads({json.dumps(json.dumps(event))})\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        "with calls.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'argv': sys.argv[1:], 'hermes_home': os.environ.get('HERMES_HOME')}) + '\\n')\n"
        "verb = sys.argv[1]\n"
        "if verb == 'wake-pending':\n"
        "    print(json.dumps({'events': [{'status': 'pending', 'event': event}]}))\n"
        "elif verb == 'wake-lease':\n"
        "    print(json.dumps({'event': event}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-profile"))
    plugin = plugin_loader()
    board_module, _ = _modules(plugin)
    board = board_module.CoordinationBoardClient(fixture)

    assert board.pending(home) == [{**event, "board_status": "pending"}]
    assert board.lease(
        worker="session-coord-native:owner-1",
        profile_home=home,
        native_session_id="native-1",
        kind="cli",
    ) == event

    invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert all(item["hermes_home"] is None for item in invocations)
    assert invocations[0]["argv"] == [
        "wake-pending",
        "--profile-home",
        str(home.resolve()),
        "--json",
    ]
    assert invocations[1]["argv"] == [
        "wake-lease",
        "--worker",
        "session-coord-native:owner-1",
        "--transport",
        "hermes",
        "--profile-home",
        str(home.resolve()),
        "--native-session-id",
        "native-1",
        "--kind",
        "cli",
        "--json",
    ]


def test_receiver_reconciliation_requeues_only_exact_durable_not_sent(
    plugin_loader, tmp_path
):
    home = tmp_path / "profile"
    home.mkdir()
    event = _event(home, board_status="unknown")
    calls = tmp_path / "calls.jsonl"
    fixture = tmp_path / "board_fixture.py"
    fixture.write_text(
        "import json, pathlib, sys\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        "with calls.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'ok': True}))\n",
        encoding="utf-8",
    )
    plugin = plugin_loader()
    board_module, receipt_module = _modules(plugin)
    board = board_module.CoordinationBoardClient(fixture)
    session = FrozenNativeSessionView(
        profile_home=home,
        profile="default",
        surface="cli",
        session_id="native-1",
        session_key="route-1",
        compression_lineage=(),
        owner_token="owner-1",
    )

    assert board.reconcile_receiver_receipt(event, profile_home=home) == "unresolved"
    decision = receipt_module.begin_admission(event, session=session)
    assert board.reconcile_receiver_receipt(event, profile_home=home) == "unresolved"
    receipt_module.mark_not_sent(decision, "host slot was not reserved")

    assert board.reconcile_receiver_receipt(event, profile_home=home) == "pending"
    invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert len(invocations) == 1
    assert invocations[0][0] == "wake-result"
    proof = json.loads(invocations[0][invocations[0].index("--receipt-json") + 1])
    assert proof["status"] == "not_sent"
    assert proof["not_sent"] is True
    assert proof["submitted"] is False
