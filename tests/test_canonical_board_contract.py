"""Canonical board and real native host types; no frozen host ABI or model calls."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

native = pytest.importorskip("hermes_cli.native_turn_sources")


def _plugin():
    root = Path(__file__).resolve().parents[1]
    name = "native_contract_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def real_contract(tmp_path, monkeypatch):
    configured = os.environ.get("SESSION_COORD_TEST_BOARD_SCRIPT")
    if not configured:
        pytest.skip("set SESSION_COORD_TEST_BOARD_SCRIPT to the canonical board CLI")
    script = Path(configured).resolve()
    assert script.is_file(), "configured canonical board is missing"
    home = tmp_path / "custom-profile-home"
    home.mkdir()
    for key, value in {
        "HOME": str(tmp_path), "HERMES_HOME": str(home),
        "HERMES_SESSION_ID": "native-receiver", "HERMES_COORD_DB": str(tmp_path / "board.db"),
        "HERMES_COORD_DISABLED": "0", "HERMES_COORD_DISABLED_FILE": str(tmp_path / "disabled"),
        "HERMES_COORD_CRON_MANIFEST": str(tmp_path / "manifest.json"),
        "HERMES_COORD_CRON_JOBS": str(tmp_path / "jobs.json"),
        "HERMES_COORD_PROFILES_DIR": str(tmp_path / "profiles"),
    }.items():
        monkeypatch.setenv(key, value)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text("{}")

    def command(*args, expected=0):
        result = subprocess.run([sys.executable, str(script), *args, "--json"], text=True,
                                capture_output=True, timeout=10, check=False)
        assert result.returncode == expected, (args, result.stdout, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    plugin = _plugin()
    source = plugin.SessionCoordNativeSource(board_script=script, lease_type=native.NativeTurnLease,
                                            surfaces=("cli",), poll_interval_seconds=0)
    view = native.NativeSessionView(profile="custom", profile_home=home,
                                   session_id="native-receiver", surface="cli",
                                   compression_lineage=("native-receiver",),
                                   session_key="native-receiver", owner_token="fixture-owner")
    return command, checkpoint, source, view


@pytest.mark.parametrize("registration", ["automatic", "legacy_explicit", "explicit_metadata"])
def test_canonical_registration_reaches_real_admission_without_guessing_profile(real_contract, registration):
    command, checkpoint, source, view = real_contract
    command("register", "--id", "holder", "--task", "fixture")
    command("claim", "--id", "holder", "--res", "res:contract")
    args = ["register", "--id", "waiter", "--task", "fixture", "--surface", "cli"]
    if registration != "automatic":
        target = {"profile_home": str(view.profile_home), "kind": "cli", "session_id": view.session_id}
        if registration == "explicit_metadata":
            target.update(profile=view.profile, compression_lineage=[view.session_id])
        args.extend(["--wake-target-json", json.dumps(target)])
    command(*args)
    command("claim", "--id", "waiter", "--res", "res:contract", "--yield",
            "--checkpoint", str(checkpoint), expected=75)
    command("release", "--id", "holder", "--res", "res:contract")
    lease = source.poll(view)
    assert isinstance(lease, native.NativeTurnLease)
    admission = native.NativeTurnAdmission(lease, view)
    assert admission.commit() is True
    pending = source.board.pending(view.profile_home)
    assert len(pending) == 1 and pending[0]["board_status"] == "delivered"
    assert source.poll(view) is None, "one receiver admission must not be duplicated"
    command("continue", "--id", "waiter", "--event", pending[0]["event_id"])


def test_legacy_envelope_stays_immutable_and_explicit_identity_mismatches_fail(real_contract):
    command, checkpoint, source, view = real_contract
    command("register", "--id", "holder", "--task", "fixture")
    command("claim", "--id", "holder", "--res", "res:contract")
    command("register", "--id", "waiter", "--task", "fixture", "--surface", "cli")
    command("claim", "--id", "waiter", "--res", "res:contract", "--yield",
            "--checkpoint", str(checkpoint), expected=75)
    command("release", "--id", "holder", "--res", "res:contract")
    event = source.board.pending(view.profile_home)[0]
    event["target"].pop("profile", None)
    event["target"].pop("compression_lineage", None)
    original = deepcopy(event)
    receipts = sys.modules[type(source).__module__.rsplit(".", 1)[0] + ".session_coord_native_receipts"]
    assert receipts.validate_event_for_session(event, view) == original
    assert event == original
    for other in (replace(view, profile_home=view.profile_home / "other"),
                  replace(view, session_id="foreign", compression_lineage=())):
        with pytest.raises(receipts.CoordinationReceiptError):
            receipts.validate_event_for_session(event, other)
    for invalid in ({"profile": "foreign"}, {"profile": None}, {"profile": ""},
                    {"profile_home": ""}, {"profile_home": "."},
                    {"compression_lineage": None}, {"compression_lineage": []},
                    {"compression_lineage": [view.session_id, "foreign"]}):
        changed = deepcopy(event)
        changed["target"].update(invalid)
        with pytest.raises(receipts.CoordinationReceiptError):
            receipts.validate_event_for_session(changed, view)
