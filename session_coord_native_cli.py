"""Public CLI contribution for the session-coord native bridge."""

from __future__ import annotations

import importlib
import json

_MISSING_POLICY_REASON = (
    "Native turn source is registered in this fresh process; the host does not "
    "expose tools.delegate_tool_config.get_delegation_execution_policy, so "
    "delegation wait-for-all cannot be verified."
)
_WATCHDOG_NAME = "session-coord-resume-watchdog"
_WATCHDOG_SCRIPT = "coord_resume_watchdog.py"


def _watchdog_drift(job):
    expected = {
        "name": _WATCHDOG_NAME,
        "schedule": "every 2m",
        "repeat": "forever",
        "deliver": "local",
        "enabled": True,
        "state": "scheduled",
        "script": _WATCHDOG_SCRIPT,
        "no_agent": True,
        "prompt_preview": "",
        "skills": [],
        "model": None,
        "provider": None,
        "base_url": None,
        "attach_to_session": False,
    }
    drift = []
    for field, wanted in expected.items():
        if field not in job:
            drift.append(f"{field}: host readback omitted required field")
        elif job[field] != wanted:
            rendered = (
                repr(wanted) if isinstance(wanted, str) else json.dumps(wanted, sort_keys=True)
            )
            actual = (
                repr(job[field])
                if isinstance(job[field], str)
                else json.dumps(job[field], sort_keys=True)
            )
            drift.append(f"{field}: expected {rendered}, got {actual}")
    if not job.get("job_id"):
        drift.append("job_id: host readback omitted required field")
    if not job.get("next_run_at"):
        drift.append("next_run_at: expected a scheduled occurrence")
    for field in ("monitor_script", "monitor_url", "workdir", "enabled_toolsets"):
        if job.get(field) not in (None, [], ""):
            drift.append(
                f"{field}: expected null/empty, got {json.dumps(job[field], sort_keys=True)}"
            )
    return drift


class SessionCoordCommand:
    def __init__(self, *, ctx, native_handle, surfaces, board_script):
        self.ctx = ctx
        self.native_handle = native_handle
        self.surfaces = tuple(surfaces)
        self.board_script = board_script

    def __call__(self, args):
        if args.session_coord_command == "native-check":
            return self._native_check(args)
        if args.session_coord_command == "watchdog-setup":
            return self._watchdog_setup(args)
        raise ValueError(f"unknown session-coord command: {args.session_coord_command}")

    def _native_check(self, args):
        handle_live = getattr(self.native_handle, "active", False) is True
        wait_supported = False
        wait_for_all = None
        reason = _MISSING_POLICY_REASON
        try:
            policy_module = importlib.import_module("tools.delegate_tool_config")
            policy_getter = policy_module.get_delegation_execution_policy
        except (ImportError, AttributeError):
            policy_getter = None
        if policy_getter is not None:
            try:
                policy = policy_getter()
            except Exception:
                policy = None
                reason = (
                    "Native turn source is registered in this fresh process; the public delegation "
                    "policy probe failed, so delegation wait-for-all cannot be verified."
                )
            if isinstance(policy, dict) and isinstance(policy.get("wait_for_all"), bool):
                wait_supported = True
                wait_for_all = policy["wait_for_all"]
                if wait_for_all:
                    reason = (
                        "Native turn source and joined delegation policy are available in this "
                        'fresh process; restart resident Hermes '
                            'processes to activate installed changes.'
                    )
                else:
                    profile = str(getattr(self.ctx, "profile_name", "default") or "default")
                    reason = (
                        "Native turn source is registered, but delegation wait-for-all is disabled "
                        f"for profile {profile!r}."
                    )
            elif policy is not None:
                reason = (
                    "Native turn source is registered in this fresh process; the public delegation "
                    "policy helper did not return a Boolean wait_for_all value."
                )
        if not handle_live:
            reason = "The native-turn-source registration handle is no longer active."
        payload = {
            "supported": handle_live,
            "surfaces": list(self.surfaces) if handle_live else [],
            "wait_for_all_supported": wait_supported,
            "wait_for_all": wait_for_all,
            "activation": "fresh_process_only",
            "reason": reason,
        }
        print(json.dumps(payload, sort_keys=True) if args.json else reason)
        return 0 if handle_live else 1

    def _watchdog_setup(self, args):
        profile = str(getattr(self.ctx, "profile_name", "default") or "default")
        mode = "check" if args.check else "setup"

        lister = getattr(self.ctx, "list_profile_homes", None)
        profile_discovery_error = None
        if callable(lister):
            try:
                profile_scopes = tuple((str(name), home) for name, home in lister())
            except Exception as exc:
                profile_scopes = ()
                profile_discovery_error = f"profile discovery failed: {exc}"
        else:
            profile_scopes = ()
            profile_discovery_error = "host does not expose complete profile discovery"
        if not profile_scopes:
            profile_discovery_error = profile_discovery_error or "host returned no profile stores"
        default_home = next((home for name, home in profile_scopes if name == "default"), None)
        inspection_scope = "all_profiles" if profile_discovery_error is None else "unavailable"

        def dispatch(args, *, home=None):
            kwargs = {"profile_home": home} if home is not None else {}
            return self.ctx.dispatch_tool("cronjob_manage", args, **kwargs)

        def emit(payload, exit_code):
            print(json.dumps(payload, sort_keys=True) if args.json else payload["reason"])
            return exit_code

        def inspect_jobs():
            combined = []
            for profile_name, home in profile_scopes:
                try:
                    raw = dispatch({"action": "list", "include_disabled": True}, home=home)
                    value = raw if isinstance(raw, dict) else json.loads(raw)
                except Exception:
                    return None
                if (
                    not isinstance(value, dict)
                    or value.get("success") is not True
                    or not isinstance(value.get("jobs"), list)
                ):
                    return None
                for job in value["jobs"]:
                    if isinstance(job, dict):
                        job = dict(job)
                        job["profile"] = profile_name
                    combined.append(job)
            return combined

        def candidates_of(jobs):
            return [
                job
                for job in jobs
                if isinstance(job, dict)
                and (job.get("name") == _WATCHDOG_NAME or job.get("script") == _WATCHDOG_SCRIPT)
            ]

        def base_payload(**fields):
            payload = {
                "ok": False,
                "mode": mode,
                "profile": profile,
                "inspection_scope": inspection_scope,
                "inspected_profiles": [name for name, _home in profile_scopes],
                "status": "unknown",
                "scheduled": False,
                "execution_verified": False,
                "job_id": None,
                "candidates": [],
                "drift": [],
                "reason": "",
            }
            payload.update(fields)
            return payload

        def classify(candidates):
            if len(candidates) > 1:
                return base_payload(
                    status="duplicate",
                    candidates=candidates,
                    drift=[f"expected one machine-global candidate; found {len(candidates)}"],
                    reason=(
                        "Multiple session-coord watchdog candidates exist in the active profile. "
                        'Refusing to create, update, resume, or delete'
                            ' any job; reconcile them explicitly.'
                    ),
                )
            if len(candidates) == 1:
                job = candidates[0]
                drift = _watchdog_drift(job)
                if drift:
                    return base_payload(
                        status="drift",
                        job_id=job.get("job_id"),
                        candidates=candidates,
                        drift=drift,
                        reason=(
                            'The existing session-coord watchdog differs '
                                'from the required semantic job. '
                            'Refusing to alter it automatically; reconcile'
                                ' the reported drift explicitly.'
                        ),
                    )
            return None

        if profile_discovery_error is not None:
            return emit(
                base_payload(
                    status="inspection_failed",
                    reason=(
                        f"{profile_discovery_error}; cannot prove the machine-global "
                        "watchdog is unique, so no scheduler mutation was attempted."
                    ),
                ),
                1,
            )

        jobs = inspect_jobs()
        if jobs is None:
            return emit(
                base_payload(
                    status="inspection_failed",
                    reason=(
                        "cronjob_manage did not return a successful structured list; "
                        "no scheduler mutation was attempted."
                    ),
                ),
                1,
            )
        candidates = candidates_of(jobs)
        refused = classify(candidates)
        if refused is not None:
            return emit(refused, 1)
        if not args.check and profile != "default":
            existing = candidates[0] if candidates else None
            return emit(
                base_payload(
                    status="wrong_profile",
                    scheduled=existing is not None,
                    execution_attempted=False,
                    job_id=existing.get("job_id") if existing is not None else None,
                    candidates=candidates,
                    reason=(
                        "Watchdog setup is allowed only under the default profile; "
                        "no scheduler mutation was attempted."
                    ),
                ),
                1,
            )

        created = False
        create_ambiguous = False
        expected_job_id = None
        if not candidates:
            if args.check:
                return emit(
                    base_payload(
                        status="missing",
                        reason=(
                            "No session-coord resume watchdog exists in any profile store. "
                            "Run watchdog-setup without --check under the default profile "
                            "to create it explicitly."
                        ),
                    ),
                    1,
                )
            if profile != "default":
                return emit(
                    base_payload(
                        status="wrong_profile",
                        reason=(
                            "Watchdog creation is allowed only under the default profile; "
                            "no scheduler mutation was attempted."
                        ),
                    ),
                    1,
                )
            create_args = {
                "action": "create",
                "schedule": "every 2m",
                "name": _WATCHDOG_NAME,
                "script": _WATCHDOG_SCRIPT,
                "no_agent": True,
                "deliver": "local",
                "repeat": 0,
                "prompt": "",
                "skills": [],
                "enabled_toolsets": [],
                "attach_to_session": False,
                "paused": False,
            }
            try:
                raw_create = dispatch(create_args, home=default_home)
                create_result = (
                    raw_create if isinstance(raw_create, dict) else json.loads(raw_create)
                )
            except Exception:
                create_result = None
            created = bool(
                isinstance(create_result, dict)
                and create_result.get("success") is True
                and isinstance(create_result.get("job_id"), str)
                and create_result.get("job_id")
            )
            create_ambiguous = not created
            expected_job_id = create_result.get("job_id") if created else None
            jobs = inspect_jobs()
            if jobs is None:
                return emit(
                    base_payload(
                        status="create_ambiguous" if create_ambiguous else "readback_failed",
                        created=created,
                        create_ambiguous=create_ambiguous,
                        execution_attempted=False,
                        reason=(
                            'The create attempt has no exact scheduler '
                                'readback. It was not retried, '
                            "and execution was not attempted."
                        ),
                    ),
                    1,
                )
            candidates = candidates_of(jobs)
            refused = classify(candidates)
            if refused is not None:
                refused.update(
                    created=created,
                    create_ambiguous=create_ambiguous,
                    execution_attempted=False,
                )
                return emit(refused, 1)
            if not candidates:
                return emit(
                    base_payload(
                        status="create_ambiguous" if create_ambiguous else "readback_missing",
                        created=created,
                        create_ambiguous=create_ambiguous,
                        execution_attempted=False,
                        reason=(
                            "The create attempt did not produce one exact watchdog on readback. "
                            "It was not retried, and execution was not attempted."
                        ),
                    ),
                    1,
                )

        job = candidates[0]
        if expected_job_id is not None and job.get("job_id") != expected_job_id:
            return emit(
                base_payload(
                    status="readback_mismatch",
                    created=created,
                    create_ambiguous=create_ambiguous,
                    execution_attempted=False,
                    job_id=job.get("job_id"),
                    candidates=candidates,
                    drift=[
                        (
                            f"job_id: create returned {expected_job_id!r}, "
                            f"readback returned {job.get('job_id')!r}"
                        )
                    ],
                    reason=(
                        "The created job identity did not match exact readback. "
                        "The create was not retried, and execution was not attempted."
                    ),
                ),
                1,
            )
        drift = _watchdog_drift(job)
        if drift:
            return emit(
                base_payload(
                    status="drift",
                    created=created,
                    create_ambiguous=create_ambiguous,
                    execution_attempted=False,
                    job_id=job.get("job_id"),
                    candidates=candidates,
                    drift=drift,
                    reason=(
                        "The watchdog failed exact semantic readback. "
                        "It was not altered or executed."
                    ),
                ),
                1,
            )
        if create_ambiguous:
            return emit(
                base_payload(
                    status="scheduled_create_ambiguous",
                    scheduled=True,
                    created=False,
                    create_ambiguous=True,
                    execution_attempted=False,
                    job_id=job.get("job_id"),
                    candidates=candidates,
                    reason=(
                        "One exact watchdog exists after an ambiguous create attempt. "
                        'The create was not retried and execution was '
                            'deferred to a later explicit setup.'
                    ),
                ),
                1,
            )

        prior_execution = bool(
            job.get("last_run_at") and job.get("last_status") == "ok" and not job.get("last_error")
        )
        prior_failure = bool(
            job.get("last_run_at") and (job.get("last_status") != "ok" or job.get("last_error"))
        )
        if prior_failure:
            return emit(
                base_payload(
                    status="execution_failed",
                    scheduled=True,
                    execution_attempted=False,
                    execution_verified=False,
                    created=created,
                    create_ambiguous=create_ambiguous,
                    job_id=job.get("job_id"),
                    candidates=candidates,
                    reason=(
                        "The exact watchdog is scheduled, but its latest recorded execution failed."
                    ),
                ),
                1,
            )
        status = "ready" if prior_execution else "scheduled"
        reason = (
            "The exact watchdog is scheduled and has a successful execution receipt."
            if prior_execution
            else (
                "The exact watchdog is scheduled with exact durable readback. "
                "Its first scheduled execution has not completed yet."
            )
        )
        return emit(
            base_payload(
                ok=True,
                status=status,
                scheduled=True,
                execution_verified=prior_execution,
                execution_attempted=False,
                created=created,
                create_ambiguous=create_ambiguous,
                job_id=job.get("job_id"),
                candidates=candidates,
                reason=reason,
            ),
            0,
        )


def configure_cli(parser) -> None:
    commands = parser.add_subparsers(dest="session_coord_command", required=True)

    native = commands.add_parser("native-check", help="Report native continuation readiness")
    native.add_argument("--json", action="store_true")

    watchdog = commands.add_parser(
        "watchdog-setup", help="Check or explicitly configure the shared resume watchdog"
    )
    watchdog.add_argument("--json", action="store_true")
    watchdog.add_argument("--check", action="store_true", help="Read-only inspection")
