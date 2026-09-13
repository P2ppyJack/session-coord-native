from __future__ import annotations

import argparse
import json
from pathlib import Path

from test_registration import FrozenHostContext


class DispatchContext(FrozenHostContext):
    def __init__(self, replies, *, profile_name="default"):
        super().__init__()
        self.profile_name = profile_name
        self.replies = list(replies)
        self.dispatches = []

    def list_profile_homes(self):
        return ((self.profile_name, Path.home() / ".hermes"),)

    def dispatch_tool(self, name, args, **_kwargs):
        self.dispatches.append((name, args))
        if not self.replies:
            raise AssertionError("unexpected dispatch")
        return self.replies.pop(0)


def _command(plugin_loader, ctx):
    plugin = plugin_loader()
    plugin.register(ctx)
    registered = ctx.cli_calls[0]
    parser = argparse.ArgumentParser()
    registered["setup_fn"](parser)
    return registered["handler_fn"], parser


def _job(job_id, **updates):
    job = {
        "job_id": job_id,
        "name": "session-coord-resume-watchdog",
        "schedule": "every 2m",
        "repeat": "forever",
        "deliver": "local",
        "enabled": True,
        "state": "scheduled",
        "script": "coord_resume_watchdog.py",
        "no_agent": True,
        "prompt_preview": "",
        "skills": [],
        "model": None,
        "provider": None,
        "base_url": None,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
    }
    job.update(updates)
    return job


def test_watchdog_check_is_read_only_and_includes_disabled_jobs(
    plugin_loader, capsys
):
    ctx = DispatchContext([json.dumps({"success": True, "count": 0, "jobs": []})])
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json", "--check"]))

    assert result == 1
    assert ctx.dispatches == [
        ("cronjob_manage", {"action": "list", "include_disabled": True})
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "mode": "check",
        "profile": "default",
        "inspection_scope": "all_profiles",
        "inspected_profiles": ["default"],
        "status": "missing",
        "scheduled": False,
        "execution_verified": False,
        "job_id": None,
        "candidates": [],
        "drift": [],
        "reason": (
            "No session-coord resume watchdog exists in any profile store. "
            "Run watchdog-setup without --check under the default profile to create it explicitly."
        ),
    }


def test_watchdog_refuses_duplicates_even_when_one_is_disabled(plugin_loader, capsys):
    jobs = [_job("job-a"), _job("job-b", enabled=False, state="paused")]
    ctx = DispatchContext([json.dumps({"success": True, "count": 2, "jobs": jobs})])
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json"]))

    assert result == 1
    assert len(ctx.dispatches) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "duplicate"
    assert payload["scheduled"] is False
    assert payload["execution_verified"] is False
    assert [candidate["job_id"] for candidate in payload["candidates"]] == ["job-a", "job-b"]
    assert payload["drift"] == ["expected one machine-global candidate; found 2"]
    assert "Refusing to create, update, resume, or delete" in payload["reason"]


def test_watchdog_detects_cross_profile_disabled_duplicate(plugin_loader, capsys, tmp_path):
    class MultiProfileContext(DispatchContext):
        def list_profile_homes(self):
            return (
                ("default", tmp_path / "root"),
                ("worker", tmp_path / "root" / "profiles" / "worker"),
            )

    ctx = MultiProfileContext(
        [
            json.dumps({"success": True, "jobs": [_job("root-job")]}),
            json.dumps(
                {
                    "success": True,
                    "jobs": [_job("worker-job", enabled=False, state="paused")],
                }
            ),
        ]
    )
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json", "--check"]))

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["inspection_scope"] == "all_profiles"
    assert payload["inspected_profiles"] == ["default", "worker"]
    assert payload["status"] == "duplicate"
    assert [(row["profile"], row["job_id"]) for row in payload["candidates"]] == [
        ("default", "root-job"),
        ("worker", "worker-job"),
    ]
    assert len(ctx.dispatches) == 2


def test_watchdog_refuses_semantic_drift_and_requires_exact_readback(plugin_loader, capsys):
    job = _job(
        "job-drift",
        script="/tmp/coord_resume_watchdog.py",
        enabled=False,
        state="paused",
    )
    ctx = DispatchContext([json.dumps({"success": True, "count": 1, "jobs": [job]})])
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json"]))

    assert result == 1
    assert len(ctx.dispatches) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "drift"
    assert payload["scheduled"] is False
    assert payload["job_id"] == "job-drift"
    assert any(
        item.startswith("script: expected 'coord_resume_watchdog.py'")
        for item in payload["drift"]
    )
    assert any(item.startswith("enabled: expected true") for item in payload["drift"])
    assert "Refusing to alter it automatically" in payload["reason"]


def test_watchdog_check_distinguishes_scheduled_from_execution_verified(
    plugin_loader, capsys
):
    job = _job(
        "job-ready",
        next_run_at="2026-09-10T18:00:00+00:00",
        attach_to_session=False,
    )
    ctx = DispatchContext([json.dumps({"success": True, "count": 1, "jobs": [job]})])
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json", "--check"]))

    assert result == 0
    assert len(ctx.dispatches) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "scheduled"
    assert payload["scheduled"] is True
    assert payload["execution_verified"] is False
    assert payload["job_id"] == "job-ready"
    assert payload["drift"] == []
    assert "exact durable readback" in payload["reason"]


def test_watchdog_check_reports_latest_execution_failure(plugin_loader, capsys):
    failed = _job(
        "job-failed",
        next_run_at="2026-09-10T18:00:00+00:00",
        last_run_at="2026-09-10T17:58:00+00:00",
        last_status="error",
        last_error="script exited non-zero",
        attach_to_session=False,
    )
    ctx = DispatchContext(
        [json.dumps({"success": True, "count": 1, "jobs": [failed]})]
    )
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json", "--check"]))

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "execution_failed"
    assert payload["scheduled"] is True
    assert payload["execution_attempted"] is False
    assert payload["execution_verified"] is False


def test_watchdog_setup_creates_once_and_requires_exact_readback_without_running(
    plugin_loader, capsys
):
    scheduled = _job(
        "job-new",
        next_run_at="2026-09-10T18:00:00+00:00",
        attach_to_session=False,
    )
    ctx = DispatchContext(
        [
            json.dumps({"success": True, "count": 0, "jobs": []}),
            json.dumps({"success": True, "job_id": "job-new", "job": scheduled}),
            json.dumps({"success": True, "count": 1, "jobs": [scheduled]}),
        ]
    )
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json"]))

    assert result == 0
    assert ctx.dispatches == [
        ("cronjob_manage", {"action": "list", "include_disabled": True}),
        (
            "cronjob_manage",
            {
                "action": "create",
                "schedule": "every 2m",
                "name": "session-coord-resume-watchdog",
                "script": "coord_resume_watchdog.py",
                "no_agent": True,
                "deliver": "local",
                "repeat": 0,
                "prompt": "",
                "skills": [],
                "enabled_toolsets": [],
                "attach_to_session": False,
                "paused": False,
            },
        ),
        ("cronjob_manage", {"action": "list", "include_disabled": True}),
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "scheduled"
    assert payload["scheduled"] is True
    assert payload["execution_attempted"] is False
    assert payload["execution_verified"] is False
    assert payload["created"] is True
    assert payload["create_ambiguous"] is False
    assert payload["job_id"] == "job-new"


def test_watchdog_setup_never_mutates_a_nondefault_profile(plugin_loader, capsys):
    job = _job(
        "job-bot",
        next_run_at="2026-09-10T18:00:00+00:00",
        attach_to_session=False,
    )
    ctx = DispatchContext(
        [json.dumps({"success": True, "count": 1, "jobs": [job]})],
        profile_name="bot-a",
    )
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json"]))

    assert result == 1
    assert ctx.dispatches == [
        ("cronjob_manage", {"action": "list", "include_disabled": True})
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "wrong_profile"
    assert payload["scheduled"] is True
    assert payload["execution_attempted"] is False
    assert "only under the default profile" in payload["reason"]


def test_watchdog_setup_does_not_retry_or_run_after_ambiguous_create(
    plugin_loader, capsys
):
    scheduled = _job(
        "job-partial",
        next_run_at="2026-09-10T18:00:00+00:00",
        attach_to_session=False,
    )
    ctx = DispatchContext(
        [
            json.dumps({"success": True, "count": 0, "jobs": []}),
            json.dumps(
                {
                    "success": False,
                    "persisted": True,
                    "job_id": "job-partial",
                    "error": "scheduler registration outcome ambiguous",
                }
            ),
            json.dumps({"success": True, "count": 1, "jobs": [scheduled]}),
        ]
    )
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json"]))

    assert result == 1
    assert [args["action"] for _, args in ctx.dispatches] == ["list", "create", "list"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "scheduled_create_ambiguous"
    assert payload["scheduled"] is True
    assert payload["created"] is False
    assert payload["create_ambiguous"] is True
    assert payload["execution_attempted"] is False
    assert "not retried" in payload["reason"]


def test_watchdog_setup_fails_closed_when_profile_discovery_fails(
    plugin_loader, capsys
):
    class BrokenDiscoveryContext(DispatchContext):
        discovery_calls = 0

        def list_profile_homes(self):
            self.discovery_calls += 1
            if self.discovery_calls == 1:
                return super().list_profile_homes()
            raise OSError("unavailable")

    ctx = BrokenDiscoveryContext([])
    handler, parser = _command(plugin_loader, ctx)

    result = handler(parser.parse_args(["watchdog-setup", "--json"]))

    assert result == 1
    assert ctx.dispatches == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "inspection_failed"
    assert payload["inspection_scope"] == "unavailable"
    assert payload["inspected_profiles"] == []
    assert "no scheduler mutation" in payload["reason"]
