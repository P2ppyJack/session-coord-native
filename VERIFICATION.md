# Local verification and support boundaries

Prepared by Hermes (agentic AI assistant) under the direction of Tobias Musser

## Current-base host verification

Hermes base: `5dea46d13deec9549bdc2ea703ae9201d733c28d`.

- Native continuation: **142 passed, 0 failed, 1 skipped**, 11 files through the
  canonical `scripts/run_tests.sh`. The skip is an existing Windows-only CLI
  initialization case on macOS, not an excluded native-continuation failure.
- Joined delegation, on a separate branch: **52 passed, 0 failed**, 3 files,
  including the existing asynchronous-delegation controls.
- Expanded delegation/schema checks: **125 passed, 1 failed**. The failure is
  `TestDelegateTask::test_child_dedicated_db_follows_parents_db_path`, whose raw
  temporary-path string comparison differs between macOS alias and resolved forms.
  An unchanged-delegation-code control reproduces the identical failure (80 passed,
  1 failed). It is a known baseline limitation, not reported as a green run.
- The optional-skill overlay also passed **12 tests on the new current-main base**.
- Branch-boundary regression: failure of child creation leaves the parent session
  unchanged and does not fence it; successful creation fences before switching.
- Windows footguns: **0 findings** over the native change set.
- Exact pinned Ruff 0.16.7 checks pass on both production components.

## Current-base component verification

The exact proposed board and plugin sources were re-tested against the combined
host candidate (`6b7a3ef05121c8b27598898c0b181b6a209b8889`):

- **macOS: 130 passed, 0 skipped** across 22 isolated test files.
- **Linux: 130 passed, 0 skipped** across the same 22 files.
- The plugin CI command `python -m pytest -q tests` additionally passed all **53**
  cases in one process, guarding against test-order and import-isolation differences.
- Expanded delegation/schema checks on Linux: **126 passed, 0 failed**; the macOS
  baseline exception above is retained explicitly.

## Receipt-validation repair gate

The board now requires an exact durable disposition receipt before `not_sent` or
`canceled` may mutate leased or unknown wake state. Incorrect receipts leave the
database unchanged. The repair passed an independent code re-review.

- Current repaired components: **166 passed, 0 skipped on macOS**, and **166 passed,
  0 skipped on Linux** (23 test files, including 36 new receipt regressions).
- Existing wake CLI selftests: **18 passed on Linux/Python 3.8**.
- The plugin bundle's two stale shell selftests were synchronized to the canonical
  copies and passed pinned ShellCheck.
- **Native Windows receipt-repair acceptance passed:** all six expected
  interpreter/step pairs completed, using Python 3.8.10, 3.11.9 and 3.13.15.
  Each interpreter passed **107 pytest cases with 6 documented skips**, plus
  **18 wake CLI selftests**. Startup, noninteractive input, artifact hashes and
  success/failure exit controls were verified. The report identifies Windows
  build 26200 and AMD64 interpreter processes; it is not native ARM64 certification.
- This rerun covers the entire standalone pytest suite and wake CLI selftests,
  not a repeat of the earlier full 33-step workflow. The tested board source is
  `1d2396f1b1378592cb9a3d16488e902e7ba403d6`; its production and test files remain
  byte-identical. Publication notes and CI environment placement were updated separately.
  Verified payload SHA-256:
  `bce60ace50d3819b1247ab47d17bb70f53c832b42c7ef0da2247c3e040b45f86`.

## Previously executed platform acceptance

These scopes are retained separately; they are not added together.

| Scope | Result |
|---|---|
| macOS components against the prior compatible host candidate | 77 board/installer + 53 plugin tests passed; no skips |
| Linux components against that host candidate | 77 board/installer + 53 plugin tests passed; no skips |
| Real Windows standalone workflow | All 33 interpreter/step pairs passed |
| Windows pytest under each of Python 3.8.10, 3.11.9, 3.13.15 | 71 passed, 6 skipped |
| Existing optional-skill PR exact-base overlay contract | 12 passed |
| Bandit production-component scan | No outstanding findings |
| ShellCheck of guard, wrappers and shell selftests | No findings |

The Windows payload was bound to SHA-256
`9f843730fdba04bf02df790634ae759ad144f76412ce40b2c5598ece60ddcdfb`.
That result predates the receipt-validation repair. The receipt engine, its wake
selftest fixtures and new regression file are covered by the accepted receipt-repair
rerun above. This old archive hash is historical evidence, not the current candidate's identity.

Windows skips: one optional-plugin lifecycle test lacked a real host/plugin
configuration; one prompt test requires a POSIX pseudo-terminal; four folder-with-
spaces cases are explicitly POSIX-only. Git Bash shell-suite success is not a
substitute for the unexecuted Windows-specific prompt or spaced-path scenarios.
The original pytest report recorded counts without `-rs`; this breakdown is
reconciled against the tested source conditions.

The optional native plugin declares **macOS/Linux**, not Windows support. Standalone
Windows coordination is tested; complete Windows native-plugin feature parity is
not claimed.

## Resident behavior

An earlier authorized installed candidate was exercised through actual Desktop /
shared TUI backend, interactive classic CLI and a verified self-only gateway route.
Release/TTL continuation, exact receiver admission/consumption, cancellation,
cleanup and strict joined-delegation behavior were verified within those scopes.
Those observations are historical installed-runtime evidence, not a new deployment
of these publication commits or a certification of every surface/OS combination.

## Reproduction and publication status

The board's `.github/workflows/tests.yml` contains the portable workflow. Standalone
component pytest is `python -m pytest -q tests`; real optional-plugin lifecycle
checks additionally need explicit compatible host/plugin paths. Plugin tests use
isolated homes and fixtures, with real-host paths supplied for integration coverage.
Hermes changes use the canonical `scripts/run_tests.sh` against their declared base.

This source is proposed for review, not marked stable. Hosted CI results belong to
the exact pushed commit and must be read from GitHub; local green checks never imply
remote success. Maintainer/API agreement and whole-submission review remain merge
gates. No private transcripts, credentials, local machine paths or preparation logs
are distributed. No production installation or process restart is performed by
publishing these changes.

---
Hermes analyzed and drafted; Tobias Musser supplied business context, adjudicated
judgment calls, and corrected conclusions.
