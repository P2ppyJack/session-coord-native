"""Stock-host delivery through a real Hermes checkout's plugin manager.

Discovery, ``register``, hook dispatch and ``PluginContext.inject_message`` (including its
``allow_gateway_injection`` consent gate) are the host's own code. Only the final TUI
message host is a recorder, standing in for a live Desktop/TUI session. Run in a
subprocess so unit fixtures cannot substitute host modules.

Set SESSION_COORD_TEST_HERMES_ROOT and SESSION_COORD_TEST_BOARD_SCRIPT to enable.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

KEY = "agent:main:local:dm:receiver"


def _host_has_native_api(host: str) -> bool:
    return (Path(host) / "hermes_cli" / "native_turn_sources.py").is_file()


@pytest.mark.parametrize(
    ("mode", "allow", "host_slot"),
    [
        ("inject", True, "tui"),
        ("inject", True, "gateway"),
        ("inject", False, "tui"),
        ("auto", True, "tui"),
    ],
)
def test_stock_plugin_manager_delivers_through_inject_message(tmp_path, mode, allow, host_slot):
    host = os.environ.get("SESSION_COORD_TEST_HERMES_ROOT")
    board = os.environ.get("SESSION_COORD_TEST_BOARD_SCRIPT")
    if not host or not board:
        pytest.skip("requires a real Hermes checkout and the canonical board")
    if mode == "auto" and _host_has_native_api(host):
        pytest.skip("auto selects the native path on a host with the native registrar")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / ".hermes"),
        "PYTHONPATH": host,
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_COORD_DB": str(tmp_path / "board.db"),
        "HERMES_COORD_DISABLED": "0",
        "HERMES_COORD_DISABLED_FILE": str(tmp_path / "disabled"),
        "HERMES_COORD_CRON_MANIFEST": str(tmp_path / "cron.json"),
        "HERMES_COORD_CRON_JOBS": str(tmp_path / "jobs.json"),
        "HERMES_COORD_PROFILES_DIR": str(tmp_path / "profiles"),
    }
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), board, mode, "1" if allow else "0",
         host_slot],
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    outcome = json.loads(result.stdout.splitlines()[-1])
    if allow:
        assert outcome == {
            "mode": mode, "injected": 1, "key": KEY, "plugin_id": "session-coord-native",
            "status": "delivered", "acquired": True, "poller_stopped": True,
            "host_slot": host_slot,
            "plugin_home_matches": True if host_slot == "gateway" else None,
        }
    else:
        assert outcome == {
            "mode": mode, "injected": 0, "not_sent_attempts": True, "status": "pending",
            "poller_stopped": True,
        }


def exercise(board: str, mode: str, allow: bool, host_slot: str) -> dict:
    from hermes_cli.plugins import PluginManager

    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True)
    shutil.copytree(
        Path(__file__).resolve().parents[1], home / "plugins" / "session-coord-native",
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", ".ruff_cache", "__pycache__"),
    )
    config = {"plugins": {
        "enabled": ["session-coord-native"],
        "entries": {"session-coord-native": {
            "enabled": True,
            "allow_gateway_injection": allow,
            "settings": {"board_script": str(Path(board).resolve()), "delivery": mode},
        }},
    }}
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    checkpoint = home / "checkpoint.json"
    checkpoint.write_text('{"step":"resume"}', encoding="utf-8")

    def sc(*args, expected=0, extra=None):
        completed = subprocess.run(
            [sys.executable, board, *args, "--json"], capture_output=True, text=True,
            encoding="utf-8", timeout=20, check=False, env={**os.environ, **(extra or {})},
        )
        assert completed.returncode == expected, completed.stdout + completed.stderr
        return json.loads(completed.stdout.splitlines()[-1])

    sc("register", "--id", "holder", "--task", "hold")
    sc("claim", "--id", "holder", "--res", "res:first", "--res", "res:second")
    sc("register", "--id", "waiter", "--task", "resume", "--surface", "desktop",
       extra={"HERMES_SESSION_ID": "receiver", "HERMES_SESSION_KEY": KEY})
    sc("claim", "--id", "waiter", "--res", "res:first", "--res", "res:second",
       "--yield", "--checkpoint", str(checkpoint), expected=75)

    manager = PluginManager(scope_key=str(home))
    manager.discover_and_load()
    assert manager.has_hook("pre_llm_call"), "inject delivery did not register its hooks"
    captured = []

    homes = []

    def recorder(*, session_key, content, plugin_id="", plugin_home=None):
        captured.append((session_key, content, plugin_id))
        homes.append(plugin_home)
        return True

    owner = object()
    if host_slot == "gateway":
        manager.set_gateway_message_injector(owner, recorder)
    else:
        manager.set_tui_message_injector(owner, recorder)
    # The turn's route, as the gateway binds it for hooks of that session.
    os.environ["HERMES_SESSION_KEY"] = KEY
    manager.invoke_hook("on_session_open", session_id="receiver", platform="tui", reason="open")
    sc("done", "--id", "holder")

    def poller_alive():
        return any(t.name == "session-coord-inject" and t.is_alive() for t in threading.enumerate())

    deadline = time.monotonic() + 30
    if allow:
        while not captured and time.monotonic() < deadline:
            time.sleep(0.2)
        assert len(captured) == 1, "the wake was not injected"
        key, prompt, plugin_id = captured[0]
        manager.invoke_hook(
            "pre_llm_call", session_id="receiver", user_message=prompt,
            conversation_history=[], is_first_turn=False, model="test", platform="tui",
        )
        [event] = sc("wake-pending", "--profile-home", str(home))["events"]
        continued = sc("continue", "--id", "waiter", "--event", event["event"]["event_id"])
        manager.unload()
        time.sleep(0.5)
        return {
            "mode": mode, "injected": len(captured), "key": key, "plugin_id": plugin_id,
            "status": event["status"], "acquired": continued["acquired"],
            "poller_stopped": not poller_alive(), "host_slot": host_slot,
            "plugin_home_matches": (
                Path(homes[0]).resolve() == home.resolve() if host_slot == "gateway" else None
            ),
        }

    attempts = 0
    while time.monotonic() < deadline:
        with sqlite3.connect(os.environ["HERMES_COORD_DB"]) as conn:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM wake_attempts WHERE outcome='not_sent'"
            ).fetchone()[0]
        if attempts:
            break
        time.sleep(0.2)
    [event] = sc("wake-pending", "--profile-home", str(home))["events"]
    manager.unload()
    time.sleep(0.5)
    return {
        "mode": mode, "injected": len(captured), "not_sent_attempts": attempts > 0,
        "status": event["status"], "poller_stopped": not poller_alive(),
    }


if __name__ == "__main__":
    print(json.dumps(
        exercise(sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]), sort_keys=True
    ))
