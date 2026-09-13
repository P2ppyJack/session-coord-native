"""Real discovery -> host dispatch -> board receipts, without a model call.

Run in a subprocess so ABI-isolated unit fixtures cannot substitute host types.
Set SESSION_COORD_TEST_HERMES_ROOT and SESSION_COORD_TEST_BOARD_SCRIPT to enable.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("surface", ["cli", "tui", "gateway"])
def test_public_discovery_admission_and_full_claim_revalidation(tmp_path, surface):
    host = os.environ.get("SESSION_COORD_TEST_HERMES_ROOT")
    board = os.environ.get("SESSION_COORD_TEST_BOARD_SCRIPT")
    if not host or not board:
        pytest.skip("requires a compatible real Hermes checkout and canonical board")
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
        [sys.executable, str(Path(__file__).resolve()), board, surface],
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=40, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "surface": surface, "admitted": 1, "claims": 2,
        "wrong_profile_inert": True, "unload_inert": True,
    }


def exercise(board, surface):
    from hermes_cli.native_turn_sources import (
        NativeSessionView, fence_native_turn_sources, poll_native_turn,
    )
    from hermes_cli.plugins import PluginManager

    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True)
    plugin_root = Path(__file__).resolve().parents[1]
    shutil.copytree(
        plugin_root, home / "plugins" / "session-coord-native",
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", ".ruff_cache", "__pycache__"),
    )
    config = {
        "plugins": {
            "enabled": ["session-coord-native"],
            "entries": {"session-coord-native": {
                "enabled": True, "settings": {"board_script": str(Path(board).resolve())},
            }},
        },
    }
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    checkpoint = home / "checkpoint.json"
    checkpoint.write_text('{"step":"resume"}', encoding="utf-8")

    def sc(args, expected=0):
        result = subprocess.run(
            [sys.executable, board, *args, "--json"], capture_output=True,
            text=True, encoding="utf-8", timeout=15, check=False,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return json.loads(result.stdout.splitlines()[-1])

    session_key = None
    if surface == "gateway":
        from gateway.config import Platform, PlatformConfig
        from gateway.platforms.base import BasePlatformAdapter
        from gateway.platforms.event import MessageEvent
        from gateway.session import SessionSource

        class Adapter(BasePlatformAdapter):
            def __init__(self):
                super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)

            @property
            def name(self):
                return "telegram"

            async def connect(self, *, is_reconnect=False):
                return True

            async def disconnect(self):
                return None

            async def send(self, chat_id, content, reply_to=None, metadata=None):
                raise AssertionError("this test must not send outbound messages")

            async def get_chat_info(self, chat_id):
                return {"id": chat_id, "type": "private"}

        adapter = Adapter()
        origin = SessionSource(platform=Platform.TELEGRAM, chat_id="synthetic", chat_type="dm")
        session_key = adapter._event_session_key(MessageEvent(text="", source=origin))

    target = {
        "profile": "default", "profile_home": str(home.resolve()),
        "kind": "desktop" if surface == "tui" else surface, "session_id": "receiver", "compression_lineage": ["receiver"],
    }
    if session_key:
        target["session_key"] = session_key
    sc(["register", "--id", "holder", "--task", "hold"])
    sc([
        "register", "--id", "waiter", "--task", "resume", "--surface", surface,
        "--native-session-id", "receiver", "--wake-transport", "hermes",
        "--wake-target-json", json.dumps(target),
    ])
    sc(["claim", "--id", "holder", "--res", "res:first", "--res", "res:second"])
    sc([
        "claim", "--id", "waiter", "--res", "res:first", "--res", "res:second",
        "--yield", "--checkpoint", str(checkpoint),
    ], 75)
    sc(["done", "--id", "holder"])
    manager = PluginManager(scope_key=str(home))
    manager.discover_and_load()
    session = NativeSessionView(
        profile="default", profile_home=home, session_id="receiver", surface=surface,
        compression_lineage=("receiver",), session_key=session_key, owner_token="test-owner",
    )
    other = NativeSessionView(
        profile="default", profile_home=home / "other", session_id="receiver", surface=surface,
    )
    assert poll_native_turn(other) is None
    admission = poll_native_turn(session)
    assert admission is not None, "public discovery did not reach the plugin source"
    assert admission.state == "open"
    assert "continue --id waiter --event" in admission.lease.prompt
    admitted = []
    try:
        if surface == "gateway":
            from gateway.platforms.event import MessageEvent, MessageType

            async def dispatch():
                event = MessageEvent(
                    text=admission.lease.prompt, source=origin, message_type=MessageType.TEXT,
                    internal=True, allow_gateway_control=False,
                )
                event._native_turn_admission = admission

                async def handler(received):
                    assert received.is_command() is False
                    assert received._native_turn_admission.commit() is True
                    admitted.append(received.text)

                adapter.set_message_handler(handler)
                assert await adapter.admit_native_turn(event, session_key) is True
                await adapter._session_tasks[session_key]

            asyncio.run(dispatch())
        else:
            # The generic host admission API; resident CLI/TUI loops are a separate gate.
            assert admission.commit() is True
            admitted.append(admission.lease.prompt)
        assert admission.commit() is True  # idempotent receipt, not a second callback
        pending = sc(["wake-pending", "--profile-home", str(home)])
        assert len(pending["events"]) == 1
        event = pending["events"][0]
        assert event["status"] == "delivered"
        continued = sc(["continue", "--id", "waiter", "--event", event["event"]["event_id"]])
        assert continued["acquired"] is True
        held = sc(["status"])["held_claims"]
        assert {item["resource"] for item in held} == {"res:first", "res:second"}
        assert poll_native_turn(session) is None
        # Leave real pending work so unload/disable assertions cannot pass vacuously.
        sc([
            "register", "--id", "later", "--task", "cancel", "--surface", surface,
            "--native-session-id", "receiver", "--wake-transport", "hermes",
            "--wake-target-json", json.dumps(target),
        ])
        sc([
            "claim", "--id", "later", "--res", "res:first", "--res", "res:second",
            "--yield", "--checkpoint", str(checkpoint),
        ], 75)
        sc(["done", "--id", "waiter"])
        later = [item for item in sc(["wake-pending", "--profile-home", str(home)])["events"]
                 if item["status"] == "pending"]
        assert len(later) == 1
        manager.unload()
        assert poll_native_turn(session) is None
        config["plugins"]["enabled"] = []
        config["plugins"]["entries"]["session-coord-native"]["enabled"] = False
        (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        manager.discover_and_load(force=True)
        assert poll_native_turn(session) is None
        config["plugins"]["enabled"] = ["session-coord-native"]
        config["plugins"]["entries"]["session-coord-native"]["enabled"] = True
        (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        manager.discover_and_load(force=True)
        canceled = poll_native_turn(session)
        assert canceled is not None
        fence_native_turn_sources(session, "user_stop")
        assert canceled.commit() is False
        refused = sc([
            "continue", "--id", "later", "--event", later[0]["event"]["event_id"],
        ], 75)
        assert refused["acquired"] is False
    finally:
        manager.unload()
    assert poll_native_turn(session) is None
    print(json.dumps({
        "surface": surface, "admitted": len(admitted), "claims": len(held),
        "wrong_profile_inert": True, "unload_inert": True,
    }))


if __name__ == "__main__":
    exercise(sys.argv[1], sys.argv[2])
