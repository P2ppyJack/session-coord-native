from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from conftest import FrozenNativeSessionView, FrozenNativeTurnLease


BOARD_SCRIPT_ENV = "SESSION_COORD_TEST_BOARD_SCRIPT"


def _run_board(script: Path, args: list[str], env: dict[str, str], expected_rc: int = 0):
    completed = subprocess.run(
        [sys.executable, str(script), *args, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=20,
    )
    assert completed.returncode == expected_rc, (
        f"board command failed: {args}\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines
    return json.loads(lines[-1])


def test_automatic_target_capture_does_not_guess_profile_label(tmp_path):
    board = runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "skill/multi-session-coordination/scripts/session_coord_wakes.py"
        )
    )
    profile_home = tmp_path / "profiles" / "worker"
    native, transport, target, captured_home, error = board[
        "parse_registration_target"
    ](
        None,
        None,
        None,
        "cli",
        None,
        env={"HERMES_HOME": str(profile_home), "HERMES_SESSION_ID": "session-tip"},
    )
    assert error is None
    assert native == "session-tip"
    assert transport == "hermes"
    assert captured_home == str(profile_home.resolve())
    assert target == {
        "profile_home": str(profile_home.resolve()),
        "kind": "cli",
        "session_id": "session-tip",
        "compression_lineage": ["session-tip"],
    }


def test_real_board_event_is_admitted_and_exactly_acknowledged(
    plugin_loader, tmp_path, monkeypatch
):
    configured = os.environ.get(BOARD_SCRIPT_ENV)
    if not configured:
        pytest.skip(f"set {BOARD_SCRIPT_ENV} to run the external board integration")
    board_script = Path(configured).expanduser().resolve()
    if not board_script.is_file():
        pytest.skip(f"external board script is unavailable: {board_script}")

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    checkpoint = tmp_path / "waiter-checkpoint.json"
    checkpoint.write_text('{"resume": true}\n', encoding="utf-8")
    isolated_env = os.environ.copy()
    isolated_env.update(
        {
            "HERMES_COORD_DB": str(tmp_path / "board.db"),
            "HERMES_COORD_DISABLED_FILE": str(tmp_path / "disabled"),
            "HERMES_COORD_DISABLED": "0",
            "HERMES_COORD_CRON_MANIFEST": str(tmp_path / "cron.json"),
            "HERMES_COORD_CRON_JOBS": str(tmp_path / "jobs.json"),
            "HERMES_COORD_PROFILES_DIR": str(tmp_path / "profiles"),
        }
    )
    isolated_env.pop("HERMES_HOME", None)
    monkeypatch.setenv("HERMES_COORD_DB", isolated_env["HERMES_COORD_DB"])
    monkeypatch.setenv("HERMES_COORD_DISABLED_FILE", isolated_env["HERMES_COORD_DISABLED_FILE"])
    monkeypatch.setenv("HERMES_COORD_DISABLED", "0")
    monkeypatch.setenv("HERMES_COORD_CRON_MANIFEST", isolated_env["HERMES_COORD_CRON_MANIFEST"])
    monkeypatch.setenv("HERMES_COORD_CRON_JOBS", isolated_env["HERMES_COORD_CRON_JOBS"])
    monkeypatch.setenv("HERMES_COORD_PROFILES_DIR", isolated_env["HERMES_COORD_PROFILES_DIR"])

    _run_board(
        board_script,
        ["register", "--id", "holder", "--task", "holder", "--surface", "cli"],
        isolated_env,
    )
    target = {
        "profile": "default",
        "profile_home": str(profile_home.resolve()),
        "kind": "cli",
        "session_id": "native-waiter",
        "compression_lineage": ["native-waiter"],
    }
    _run_board(
        board_script,
        [
            "register",
            "--id",
            "waiter",
            "--task",
            "waiter",
            "--surface",
            "cli",
            "--native-session-id",
            "native-waiter",
            "--wake-transport",
            "hermes",
            "--wake-target-json",
            json.dumps(target, sort_keys=True),
        ],
        isolated_env,
    )
    _run_board(
        board_script,
        ["claim", "--id", "holder", "--res", "res:plugin-integration"],
        isolated_env,
    )
    yielded = _run_board(
        board_script,
        [
            "claim",
            "--id",
            "waiter",
            "--res",
            "res:plugin-integration",
            "--yield",
            "--checkpoint",
            str(checkpoint),
        ],
        isolated_env,
        expected_rc=75,
    )
    assert yielded["yielded"] is True
    _run_board(
        board_script,
        ["release", "--id", "holder", "--res", "res:plugin-integration"],
        isolated_env,
    )

    plugin = plugin_loader()
    source = plugin.SessionCoordNativeSource(
        board_script=board_script,
        lease_type=FrozenNativeTurnLease,
        surfaces=("cli",),
    )
    session = FrozenNativeSessionView(
        profile_home=profile_home,
        profile="default",
        surface="cli",
        session_id="native-waiter",
        session_key="route-waiter",
        compression_lineage=(),
        owner_token="owner-waiter",
    )

    lease = source.poll(session)
    assert lease is not None
    assert lease.prompt
    assert lease.commit(session) is True

    pending = _run_board(
        board_script,
        ["wake-pending", "--profile-home", str(profile_home.resolve())],
        isolated_env,
    )
    assert len(pending["events"]) == 1
    assert pending["events"][0]["status"] == "delivered"
    assert pending["events"][0]["event"]["event_id"] == lease.lease_id.rsplit(":", 1)[0]
