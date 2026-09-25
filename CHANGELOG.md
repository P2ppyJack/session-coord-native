# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
