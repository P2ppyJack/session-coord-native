"""Stock-host delivery against the real board CLI: lease, inject, hook-confirmed admission.

The injection call is a recorder and the clock is controlled; every board transition and
receipt is the real one. Set SESSION_COORD_TEST_BOARD_SCRIPT to enable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

KEY = "agent:main:local:dm:receiver"


@pytest.fixture
def board(tmp_path, monkeypatch):
    script = os.environ.get("SESSION_COORD_TEST_BOARD_SCRIPT")
    if not script:
        pytest.skip("requires the canonical board script")
    home = tmp_path / ".hermes"
    home.mkdir()
    # CoordinationBoardClient copies os.environ, so the scratch board must be set here.
    for name, value in {
        "HERMES_COORD_DB": str(tmp_path / "board.db"),
        "HERMES_COORD_DISABLED": "0",
        "HERMES_COORD_DISABLED_FILE": str(tmp_path / "disabled"),
        "HERMES_COORD_CRON_MANIFEST": str(tmp_path / "cron.json"),
        "HERMES_COORD_CRON_JOBS": str(tmp_path / "jobs.json"),
        "HERMES_COORD_PROFILES_DIR": str(tmp_path / "profiles"),
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_HOME"):
        monkeypatch.delenv(name, raising=False)
    checkpoint = home / "checkpoint.json"
    checkpoint.write_text('{"step":"resume"}', encoding="utf-8")

    def sc(*args, expected=0, env=None):
        result = subprocess.run(
            [sys.executable, str(Path(script).resolve()), *args, "--json"],
            capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
            env={**os.environ, **(env or {})},
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return json.loads(result.stdout.splitlines()[-1])

    def park(waiter="waiter", native="receiver", key=KEY, explicit_target=None):
        if explicit_target is not None:
            sc("register", "--id", waiter, "--task", "resume", "--surface", "desktop",
               "--native-session-id", native, "--wake-transport", "hermes",
               "--wake-target-json", json.dumps(explicit_target))
        else:
            env = {"HERMES_SESSION_ID": native, "HERMES_HOME": str(home)}
            if key:
                env["HERMES_SESSION_KEY"] = key
            sc("register", "--id", waiter, "--task", "resume", "--surface", "desktop", env=env)
        sc("claim", "--id", waiter, "--res", "res:first", "--res", "res:second",
           "--yield", "--checkpoint", str(checkpoint), expected=75)

    sc("register", "--id", "holder", "--task", "hold")
    sc("claim", "--id", "holder", "--res", "res:first", "--res", "res:second")
    return type("Board", (), {"sc": staticmethod(sc), "park": staticmethod(park),
                              "home": home, "script": Path(script).resolve()})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_delivery(plugin, board, *, accept=True, key=KEY, **kwargs):
    calls = []

    def inject(content, *, session_key):
        calls.append((content, session_key))
        return accept() if callable(accept) else accept

    clock = Clock()
    delivery = plugin.InjectDelivery(
        inject=inject, profile_home=board.home, board_script=board.script,
        clock=clock, session_key_reader=lambda: key, **kwargs,
    )
    delivery.start = lambda: None  # passes run synchronously through tick()
    return delivery, calls, clock


def events(board):
    return board.sc("wake-pending", "--profile-home", str(board.home))["events"]


def test_hosted_wake_is_injected_by_exact_key_and_confirmed_by_its_turn(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    delivery, calls, _clock = make_delivery(plugin, board)
    delivery.on_session_seen(session_id="receiver")
    board.sc("done", "--id", "holder")

    delivery.tick()

    assert len(calls) == 1
    prompt, session_key = calls[0]
    assert session_key == KEY
    assert "continue --id waiter --event" in prompt
    assert [item["status"] for item in events(board)] == ["leased"]
    delivery.on_pre_llm_call(session_id="receiver", user_message=prompt)
    [event] = events(board)
    assert event["status"] == "delivered"
    continued = board.sc("continue", "--id", "waiter", "--event", event["event"]["event_id"])
    assert continued["acquired"] is True
    delivery.tick()
    assert len(calls) == 1  # delivered once, never again


def test_unobserved_session_is_never_a_delivery_target(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    delivery, calls, _clock = make_delivery(plugin, board)
    delivery.on_session_seen(session_id="someone-else")
    board.sc("done", "--id", "holder")

    delivery.tick()

    assert calls == []
    assert [item["status"] for item in events(board)] == ["pending"]


def test_refused_injection_records_not_sent_and_backs_off_before_retrying(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    answers = iter([False, True])
    delivery, calls, clock = make_delivery(plugin, board, accept=lambda: next(answers))
    delivery.on_session_seen(session_id="receiver")
    board.sc("done", "--id", "holder")

    delivery.tick()
    assert len(calls) == 1
    assert [item["status"] for item in events(board)] == ["pending"]
    delivery.tick()
    assert len(calls) == 1  # backoff holds the retry
    clock.now += 60
    delivery.tick()
    assert len(calls) == 2
    delivery.on_pre_llm_call(session_id="receiver", user_message=calls[1][0])
    assert [item["status"] for item in events(board)] == ["delivered"]


def test_user_turn_before_the_queued_wake_cancels_it(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    delivery, calls, _clock = make_delivery(plugin, board)
    delivery.on_session_seen(session_id="receiver")
    board.sc("done", "--id", "holder")
    delivery.tick()
    prompt = calls[0][0]
    [leased] = events(board)
    event_id = leased["event"]["event_id"]

    delivery.on_pre_llm_call(session_id="receiver", user_message="stop, do something else")
    delivery.on_pre_llm_call(session_id="receiver", user_message=prompt)

    pending = board.sc("wake-pending", "--profile-home", str(board.home))
    assert pending["episodes"] == []
    refused = board.sc("continue", "--id", "waiter", "--event", event_id, expected=75)
    assert refused["acquired"] is False
    receipts = sys.modules[plugin.__name__ + ".session_coord_native_receipts"]
    assert receipts.read_admission_receipt(board.home, event_id)["status"] == "canceled"


def test_recorded_and_observed_keys_must_agree(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    delivery, calls, _clock = make_delivery(plugin, board, key="agent:main:local:dm:other")
    delivery.on_session_seen(session_id="receiver")
    board.sc("done", "--id", "holder")

    delivery.tick()

    assert calls == []
    assert [item["status"] for item in events(board)] == ["pending"]


def test_observed_key_delivers_a_target_recorded_without_one(plugin_loader, board):
    plugin = plugin_loader()
    board.park(explicit_target={
        "profile_home": str(board.home), "kind": "desktop", "session_id": "receiver",
    })
    delivery, calls, _clock = make_delivery(plugin, board)
    delivery.on_session_seen(session_id="receiver")
    board.sc("done", "--id", "holder")

    delivery.tick()

    assert [key for _prompt, key in calls] == [KEY]


def test_no_key_anywhere_means_no_delivery(plugin_loader, board):
    plugin = plugin_loader()
    board.park(key=None)
    delivery, calls, _clock = make_delivery(plugin, board, key="")
    delivery.on_session_seen(session_id="receiver")
    board.sc("done", "--id", "holder")

    delivery.tick()

    assert calls == []


def test_unobserved_injection_expires_to_unknown_and_is_not_resent(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    delivery, calls, clock = make_delivery(plugin, board, observe_seconds=30)
    delivery.on_session_seen(session_id="receiver")
    board.sc("done", "--id", "holder")
    delivery.tick()

    clock.now += 31
    delivery.tick()

    assert [item["status"] for item in events(board)] == ["unknown"]
    clock.now += 3600
    delivery.tick()
    assert len(calls) == 1


def test_surface_filter_excludes_unconfigured_kinds(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    delivery, calls, _clock = make_delivery(plugin, board, surfaces=("cli", "gateway"))
    delivery.on_session_seen(session_id="receiver")
    board.sc("done", "--id", "holder")

    delivery.tick()

    assert calls == []


def test_interrupted_session_end_is_a_user_boundary(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    delivery, calls, _clock = make_delivery(plugin, board)
    delivery.on_session_seen(session_id="receiver")
    delivery.tick()  # learns the waiting episode before the boundary

    delivery.on_session_end(session_id="receiver", interrupted=True)
    board.sc("done", "--id", "holder")
    delivery.tick()

    assert calls == []
    assert board.sc("wake-pending", "--profile-home", str(board.home))["episodes"] == []


def test_closing_a_viewer_is_not_a_boundary_and_reopening_delivers(plugin_loader, board):
    plugin = plugin_loader()
    board.park()
    delivery, calls, _clock = make_delivery(plugin, board)
    delivery.on_session_seen(session_id="receiver")
    delivery.on_session_finalize(session_id="receiver")
    board.sc("done", "--id", "holder")

    delivery.tick()
    assert calls == []
    delivery.on_session_seen(session_id="receiver")
    delivery.tick()
    assert len(calls) == 1
