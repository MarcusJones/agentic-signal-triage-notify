---
name: signal-notify
description: >-
  Layer 3 of the signal-triage-notify system. Reads PENDING actions proposed
  by signal-triage from the ledger and dispatches them to notification
  channels: creates ATTENDEE-FREE calendar events with reminders, and sends an
  alert on the configured channel for URGENT items. Marks each action
  done/failed. Idempotent. Use for the scheduled signal-notify cron, or when
  asked to "send the pending notifications". Bundles the Layer-1 sensor
  framework this system runs on.
version: 0.2.0
author: signal-triage-notify contributors
license: MIT
metadata:
  hermes:
    tags: [signals, notify, calendar, cron]
    category: productivity
    related_skills: [signal-triage]
    requires_toolsets: [terminal]
    config:
      signals_dir:
        description: >-
          Where the daily raw log and triage view live. Optional — defaults to
          an OS-correct data directory via platformdirs. Set this to point the
          tap at an Obsidian vault or any synced folder, e.g. ~/YourVault/signals.
        required: false
      state_dir:
        description: Where ledger.db and sensor delta state live. Optional, platformdirs default.
        required: false
      policy_path:
        description: Path to policy.yaml. Optional, platformdirs default; seeded on first run.
        required: false
    required_environment_variables:
      - name: SIGNAL_TRIAGE_ICS_URL
        description: Public/secret .ics feed URL for the generic calendar sensor. Only required if you enable ics-poll.
        required: false
      - name: HERMES_GOOGLE_TOKEN
        description: Path to a full-scope Google OAuth token used for calendar writes. Defaults to ~/.hermes/google_token.json.
        required: false
    required_credential_files:
      - name: token.json
        description: Gmail read-only OAuth token, produced by scripts/bootstrap_oauth.py and copied into the resolved state dir. Only required if you enable gmail-poll.
        required: false
---

# signal-notify — Layer 3 (dispatch to channels)

You execute the actions `signal-triage` proposed. You are the only layer
allowed to interrupt the user or write their calendar. Two channels today:
**calendar** and **telegram/chat** (the framework is extensible — new
channels are just new `kind`s).

Config is `<policy_path>` — see `_sensorlib.resolve_paths()` in
`scripts/_sensorlib.py`.

## This skill's bundled framework

This skill directory carries the whole Layer-0/1 sensor framework it depends
on, under `${HERMES_SKILL_DIR}/scripts/`:

- `_sensorlib.py` — shared helpers: path resolution, atomic writes, the daily
  log format, importance heuristics.
- `registry.yaml` + `setup.py` (`setup.sh` / `install-sensors.sh`) —
  declarative sensor/cron registry and idempotent first-run bootstrap
  (builds the venv, seeds `policy.yaml`, syncs `hermes cron` jobs, prints
  credential setup steps). Run `${HERMES_SKILL_DIR}/scripts/setup.sh` once
  after installing this tap.
- `signal_ledger.py` — action queue + idempotency ledger.
- `triage_prefilter.py` / `notify_prefilter.py` (+ `.sh`/`.ps1` launchers) —
  Layer-1.5 deterministic prefilters and wake gates: they do discovery,
  batching, and bookkeeping before any LLM tokens are spent, and skip the
  agent run entirely (`{"wakeAgent": false}`) when there is nothing to do.
- `gcal_write.py` — attendee-free calendar event creation with custom
  reminders and a duplicate guard.
- `bootstrap_oauth.py` — laptop/browser-machine-only Gmail OAuth bootstrap.
- Generic example sensors: `gmail_sensor.py`, `github_sensor.py`,
  `weather_sensor.py`, `ics_sensor.py` (+ their `.sh` launchers).

See `docs/writing-a-sensor.md` to add your own sensor.

## Setup for this run

```bash
SD="${HERMES_SKILL_DIR}/scripts"
LEDGER="uv run --project $SD python $SD/signal_ledger.py"
GCAL="uv run --project $SD python $SD/gcal_write.py"
```

1. **Check the waking-hours window** in `policy.yaml`. If the current local
   time is outside `waking_hours`, do NOT send alerts (calendar writes are
   fine any time). Hold alerts for the next in-window run by leaving them
   pending.

2. **Get pending actions:** `$LEDGER pending` → JSON list of rows with
   `source_id, kind, channel, classification, payload`. If empty, stop: reply
   with a single line beginning `[SILENT]` (e.g. `[SILENT] no pending
   notifications`). On cron this rarely happens — the `notify_prefilter`
   wake gate only wakes you when actionable rows exist.

## Dispatch each pending action

### kind = create_event  (channel: calendar)

Parse `payload`. Build the gcal_write command. The event is ALWAYS
attendee-free — `gcal_write.py` never sets attendees; never add them.

All-day:
```bash
$GCAL create --summary "<summary>" --all-day --date <YYYY-MM-DD> \
      --reminders "<reminders>" ${calendar:+--calendar "<calendar>"} \
      --description "<description>" --dedup-window-days 2
```
Timed:
```bash
$GCAL create --summary "<summary>" --start "<ISO+offset>" --end "<ISO+offset>" \
      --reminders "<reminders>" ${calendar:+--calendar "<calendar>"} --description "<description>"
```
- Omit `--calendar` to use the default (`primary`, or `$HERMES_DEFAULT_CALENDAR`
  if set); pass it only if the payload has a non-empty `calendar`.
- The helper returns JSON `{"status":"created"|"duplicate","id":...}`. Either
  outcome means the calendar is correct — record it:
  `$LEDGER mark --source-id <id> --kind create_event --status done --result-json '<the json>'`
- On error, mark failed:
  `$LEDGER mark --source-id <id> --kind create_event --status failed --result-json '{"error":"..."}'`
  and include it in your final summary so the run's owner can retry.

### kind = alert  (channel: telegram/chat)

- Only if inside waking hours (see step 1).
- The cron delivers the configured channel, so the alert text becomes the
  message via your final response. Collect all alert texts and emit them as
  your final message, one per line, concise, each with its reference link.
- Mark each: `$LEDGER mark --source-id <id> --kind alert --status done --result-json '{"sent":true}'`

### kind = none

Nothing to dispatch (these were settled by triage for idempotency). On cron,
`notify_prefilter` normally settles these deterministically before you wake —
you'll only see stragglers proposed mid-run. Mark any you do see `done` so
`$LEDGER pending` stays reserved for genuinely actionable `alert` /
`create_event` rows:

```bash
$LEDGER mark --source-id <id> --kind none --status done --result-json '{"bookkeeping":true}'
```

## Idempotency & safety

- The ledger guarantees each `(source_id, kind)` is acted on once: `pending`
  only returns `status='proposed'`, and you `mark` them `done` immediately
  after acting. `gcal_write.py` additionally dedups by summary+date as a
  backstop.
- NEVER create an event with attendees. NEVER send invites. (Hard rule.)
- Autonomous on cron — never ask questions. If a payload is malformed, mark
  the action `failed` with the reason and continue.
- If you sent no alerts (none pending, or all out-of-window/calendar), end
  with `[SILENT] <short summary>` so the gateway stays quiet.

## Final response

- If you sent alerts: the final message IS the alert(s) — concise,
  chat-ready.
- Otherwise: `[SILENT] dispatched <N> events, 0 alerts` (or `no pending`).
