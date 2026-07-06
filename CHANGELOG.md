# Changelog

All notable changes to this project are documented here. Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: semver.

## [Unreleased]

## [0.2.0] - 2026-07-06

The wake-gate release: the LLM layers stop running on a clock and start
running on demand. Measured on the reference deployment: ~90% fewer LLM
runs at better worst-case alert latency.

### Added
- **Layer 1.5 prefilters** (`triage_prefilter.py`, `notify_prefilter.py` +
  `.sh`/`.ps1` launchers): deterministic discovery, ledger bulk-diff, and
  wake gates — a last stdout line of `{"wakeAgent": false}` skips the
  agent run entirely.
- **Content-blind age debounce** (`triage.debounce_minutes` in policy.yaml,
  seeded 30): triage polls every 15 min free of charge and wakes only when
  the oldest unjudged item has waited long enough — bursts are judged in one
  wake-up. Stateless (ages come from each entry's `detected_at`).
- **Self-settling notify gate**: `kind=none` bookkeeping rows are marked
  done directly in SQLite; the LLM wakes only for actionable alert /
  create_event rows.
- `_sensorlib.source_id()` / `entry_detected_at()` / `iter_entry_lines()` /
  `load_policy()` — id conventions now have a single tested implementation.
- Test suite (`skills/signal-notify/scripts/tests/`, 18 tests, no LLM or
  Hermes install required) and GitHub Actions CI (ubuntu + windows,
  py311/312, shellcheck, PowerShell syntax check, version-consistency gate).
- `scripts/bump_version.py` + this changelog: the version string lives in
  three files; the bump script updates them together and CI fails on drift.
- `skills/signal-triage/references/` (pitfalls, manual workflows) — loaded
  on demand instead of riding along on every run.

### Changed
- signal-triage SKILL.md slimmed to a core (~6.5 KB) with an explicit
  "do NOT load references/ at run start" rule; cadence `*/15 6-23 * * *`.
- signal-notify cadence `5,35 6-23 * * *`; pending alerts dispatch within
  ≤30 min of a triage flush.
- `registry.yaml`: agent jobs gained `script:` (prefilter hook), optional
  `model:`/`provider:` overrides, and a `prefilter: false` opt-out;
  `setup.py` syncs cadence + script onto existing installs.
- `_sensorlib` imports google-auth lazily (only OAuth sensors need it) and
  degrades file locking gracefully on Windows.

### Migration
- Existing installs: re-run `setup.sh` — it attaches the prefilters and new
  cadences to your existing cron jobs. Rollback: `prefilter: false` in
  registry.yaml, or `hermes cron edit <job> --script ""`.

## [0.1.0] - 2026-07-06

Initial public migration: three-layer design (sensors → hourly triage →
notify), vault-optional path resolution, sensor framework with gmail /
github / weather / ics examples, idempotency ledger, agentskills.io tap
layout.
