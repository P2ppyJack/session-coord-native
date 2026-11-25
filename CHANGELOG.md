# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-11-25

### Added

- Stock-host delivery (`delivery: inject`). On Hermes v0.23.3 or newer without the
  native-turn-source registrar, the plugin leases a board wake and pushes the exact
  prompt through the public `PluginContext.inject_message(..., session_key=...)` call,
  then confirms admission from the `pre_llm_call` hook of the turn that carries it.
  Delivery is limited to sessions the process has observed through its hooks and to
  the wake's recorded `delivery_session_key` (board 2.6.4). Refusals record `not_sent`
  and back off; an unobserved prompt becomes `unknown` and is never resent. A user
  turn, Stop, `/new` or session deletion tombstones and cancels the session's waits.
- `delivery` setting: `auto` (default; native when the registrar exists, otherwise
  inject), `native`, or `inject`.
- `native-check` on a stock host reports `delivery: "inject"`, the
  `allow_gateway_injection` consent key, and `delegation.gateway_mode` as the
  joined-delegation policy.
- `requires_hermes: ">=0.23.3"` and `provides_hooks` in the manifest, so stock
  `hermes plugins validate` passes.
- Tests against a real stock Hermes plugin manager (TUI and gateway injection
  slots, the consent refusal, and `auto`) and against the real board.

### Changed

- The private `hermes_cli.native_turn_sources` import happens only when the host
  provides the registrar; stock hosts import no private Hermes module at load.
- CI validates and tests against stock Hermes v0.23.3 and v0.23.4 instead of a
  patched fork.
- The bundled skill reference copy is synced to board 2.6.4.
- README byline uses the standard project credit.

## [0.3.0] - 2026-11-09

### Added

- Optional `pending_targets(profile_home, surface)` host capability: the adapter
  reports the exact native sessions that have a pending, deliverable wake event,
  so a host can restore a closed session before polling it. Only the board's
  public `wake-pending` payload is read; a failure is inert and returns no
  targets, and a host that does not call the method is unaffected.

## [0.2.0] - 2026-09-25

### Added

- Manifest v2 metadata, project homepage, and explicit runtime API generation.

### Changed

- Hosts without the proposed native-turn-source API now receive a clear warning while the plugin remains inactive; plugin discovery no longer fails.
- Installation guidance now links the proposed Hermes host API, explains its unreleased status, and documents SHA-pinned installation and activation checks.

## [0.1.0] - 2026-09-13

### Added

- Initial standalone native-turn-source adapter for exact `session-coord` wake admission.
- Profile-local admission receipts, user-boundary tombstones, board client, CLI diagnostics, installer integration, and macOS/Linux CI.

[Unreleased]: https://github.com/P2ppyJack/session-coord-native/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/P2ppyJack/session-coord-native/compare/59f221afd021d8c4fcf73202b78cafeea7a650e3...v0.2.0
[0.1.0]: https://github.com/P2ppyJack/session-coord-native/commit/31881eb0e48d0ba6aa90dd2948bda40333bd3498
