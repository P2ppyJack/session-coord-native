# session-coord-native

`session-coord-native` is a standalone Hermes plugin that admits exact `session-coord` wake events through Hermes's native-turn-source API. The coordination board remains framework-neutral and authoritative for waits, leases, and results; the plugin stores only receiver-side admission receipts and user-boundary tombstones.

## Requirements

- A Hermes build that exposes `PluginContext.register_native_turn_source(...)`, `NativeSessionView`, and `NativeTurnLease`. For version 0.2.0, that public host API is proposed in [NousResearch/hermes-agent#110232](https://github.com/NousResearch/hermes-agent/pull/110232); it is not yet in a released Hermes version.
- The framework-neutral `session_coord.py` and `coord_resume_watchdog.py` scripts in the machine-root Hermes `scripts/` directory, or an explicit `board_script` plugin setting.
- Python 3.11 or newer on macOS or Linux, matching the compatible Hermes host's supported runtime. Receipt locking uses `fcntl`.

No model, provider, network, or messaging call is made by plugin registration, native readiness checks, wake polling, or the watchdog itself.

### Older Hermes hosts

The plugin probes the host API at registration time. If the module or context method is unavailable, registration logs exactly:

```text
This Hermes host does not provide the public native-turn-source API
```

The plugin then remains inactive without registering its CLI command or native source. Hermes continues loading, and the standalone coordination board remains usable. Upgrade to a Hermes build containing the API, restart the resident Hermes process, and run `hermes session-coord native-check --json` before relying on automatic continuation.

## Install, change your choice, or remove

Use the canonical board installer. It explains standalone versus automatic
continuation, defaults to **do not install/change the plugin**, and offers
install/upgrade, plugin-only removal, and cancel. The board remains usable
without this plugin.

```bash
python3 /path/to/session-coord/install.py
# Explicit alternatives:
python3 /path/to/session-coord/install.py --plugin keep
python3 /path/to/session-coord/install.py --plugin install --plugin-path /path/to/session-coord-native
python3 /path/to/session-coord/install.py --plugin remove
```

This checkout's `install.py` delegates to the same installer and supplies its
own plugin path. It discovers a sibling `session-coord/install.py`, or accepts
`--board-installer /path/to/session-coord/install.py`. The old independent
copy/force-overwrite path is retired; its legacy `--force`, `--enable`, and
`--json` flags are not accepted.

Rerun and choose install to add the plugin to an existing standalone board or
upgrade from a reviewed, clean Git checkout. The installer uses Hermes's
standard SHA-pinned plugin installation, Doctor, enablement, configuration and
native-check commands. It does not weaken compatibility or trust checks.
A locally edited/unverifiable existing plugin is preserved for manual review.

Advanced users who already installed the board may use the standard Hermes
plugin lifecycle directly. Replace the placeholder with a reviewed 40-character
commit ID; do not install an unreviewed moving branch:

```bash
hermes plugins install P2ppyJack/session-coord-native --ref <40-character-commit-sha>
hermes plugins enable session-coord-native
hermes plugins doctor session-coord-native --ci
```

Restart resident Hermes processes after enabling. On a compatible host,
`hermes session-coord native-check --json` must report `"supported": true`
before automatic continuation is considered active.

Removal uses Hermes's disable/remove operations, verifies a recovery copy,
and removes only the native managed instruction block. Board files, claims,
database, skills, receipts, shared watchdog, other profiles, and the shared
joined-delegation setting remain. Already-absent removal is a no-op.

Successful setup/removal changes disk state, not already-running processes.
Activation remains `fresh_process_only`: close/restart resident processes
through the normal operator-controlled lifecycle. Pending waits are preserved
for manual recovery after removal; they are not cancelled or replayed.

## Settings

Settings are read through `PluginContext.get_config` from the plugin's settings subtree:

- `board_script`: optional path to `session_coord.py`. Empty uses the machine-root `~/.hermes/scripts/session_coord.py`, even for a named profile.
- `supported_surfaces`: non-empty unique subset of `cli`, `tui`, and `gateway`. Default: all three.

Example:

```bash
hermes -p PROFILE config set plugins.entries.session-coord-native.settings.supported_surfaces '["cli", "gateway"]'
hermes -p PROFILE config set plugins.entries.session-coord-native.settings.board_script /absolute/path/to/session_coord.py
```

## Native admission behavior

The host calls the source only at its proven idle/owned turn boundary. The plugin then:

1. reads candidate events through `wake-pending`;
2. validates the exact profile home, receiver session, session key, surface, and board target kind; supplied profile labels and lineage must also match the host;
3. acquires one event with `wake-lease`;
4. writes an `attempting` receiver receipt under the target profile home;
5. after the host reserves the turn slot, verifies the same native owner and commits an `admitted` receipt;
6. acknowledges the exact event and attempt through `wake-result`.

Legacy targets may omit the profile label and compression lineage. The exact
absolute profile home and receiver session remain mandatory; absent lineage is
checked as the single known receiver, without changing the stored event. A label
or lineage that is present but invalid is rejected, not guessed or repaired.

`not_sent` can be reconciled and retried. `attempting` or `unknown` is never guessed safe from transcript absence and is not blindly retried. Stop, new-session, session-switch, close, and exit boundaries create durable tombstones and invoke board cancellation before identity rotation. A receipt proves prompt admission, not completion of the resumed task.

## Shared watchdog

Inspect without mutation:

```bash
hermes -p default session-coord watchdog-setup --json --check
```

Explicitly create or verify the one machine-global watchdog:

```bash
hermes -p default session-coord watchdog-setup --json
```

The semantic job is fixed:

- name `session-coord-resume-watchdog`;
- schedule `every 2m`, recurring forever;
- relative script `coord_resume_watchdog.py`;
- `no_agent: true`;
- local success and failure delivery;
- enabled, with no model, provider, monitor, or attached session.

Inspection includes disabled jobs across all existing profile stores. Setup
refuses duplicates and semantic drift, never deletes or rewrites a candidate,
and never retries an ambiguous create. It requires exact list readback and does
not force-run a newly scheduled job from the fresh CLI process; the latest
recorded execution health is reported separately. Setup mutation is restricted
to the `default` profile because the board and watchdog are machine-global.

## Failure behavior

A host without the proposed native-turn-source API gets the warning documented
above and an inert plugin, not a failed Hermes startup. On a compatible host, a
missing, malformed, timed-out, or failing board command is inert to the host
turn. Invalid exact targets are ignored. Native receipts are written atomically
with profile-local locking. Scheduler setup failures return nonzero structured
output and do not trigger automatic cleanup or a second create.

## Tests

Unit tests use isolated profile homes, frozen host protocol types, subprocess board fixtures, and a fake `dispatch_tool` scheduler boundary:

```bash
python -m pytest -q tests
```

The optional end-to-end board test is enabled by an explicit script path and still uses an isolated board database and profile home:

```bash
SESSION_COORD_TEST_BOARD_SCRIPT=/absolute/path/to/session_coord.py \
  python -m pytest -q tests/test_reference_integration.py
```

To verify the canonical board against the proposed host types, lease admission,
durable receipt, and exact-event continuation, run:

```bash
PYTHONPATH=/absolute/path/to/prepared/hermes \
SESSION_COORD_TEST_BOARD_SCRIPT=/absolute/path/to/canonical/session_coord.py \
  python -m pytest -q tests/test_canonical_board_contract.py
```

## License

Licensed under the MIT License. See [LICENSE](LICENSE).

## Credits

Prepared by Hermes (agentic AI assistant) under the direction of Tobias Musser.
