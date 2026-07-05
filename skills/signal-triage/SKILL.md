---
name: signal-triage
description: >-
  Layer 2 of the signal-triage-notify system. Hourly LLM sweep that reads the
  day's raw situational-awareness log, classifies NEW items by importance,
  writes a surfaced triage view, and PROPOSES actions into the ledger. Never
  sends notifications or writes the calendar — that is signal-notify's job.
  Use for the scheduled signal-triage cron, or when asked to "triage the
  signals" / "what needs my attention".
version: 0.1.0
author: signal-triage-notify contributors
license: MIT
metadata:
  hermes:
    tags: [signals, triage, awareness, cron]
    category: productivity
    related_skills: [signal-notify]
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
    required_environment_variables: []
    required_credential_files: []
---

# signal-triage — Layer 2 (judge & surface)

You read the cheap raw signals collected by the Layer-1 sensors (bundled with
`signal-notify`, see `${HERMES_SKILL_DIR}/../signal-notify/scripts/`) and
decide what actually matters. You do NOT notify the user or touch their
calendar here — you only classify, surface, and **propose** actions into the
ledger. `signal-notify` executes them. Keep this run cheap: it is scheduled
hourly.

See `docs/architecture.md` in this repo for the full three-layer design, and
`docs/writing-a-sensor.md` for the raw-log entry contract.

## Paths (resolved via `resolve_paths()` — see `_sensorlib.py`)

All paths below are **vault-optional** and resolve automatically; no manual
configuration is required for a fresh install:

- Raw log (input):  `<signals_dir>/daily/$(date +%F).md`
- Triage log (output): `<signals_dir>/triage/$(date +%F).md`
- Policy/config: `<policy_path>` (seeded with sane defaults on first run)
- Ledger CLI: `${HERMES_SKILL_DIR}/../signal-notify/scripts/signal_ledger.py` (run with `uv run`)
- Watermark: `<state_dir>/triage/watermark`

Shorthand for this run:

```bash
SD="${HERMES_SKILL_DIR}/../signal-notify/scripts"
LEDGER="uv run --project $SD python $SD/signal_ledger.py"
PATHS="$(uv run --project $SD python -c 'import _sensorlib,json,sys; sys.path.insert(0,\"'"$SD"'\"); print(json.dumps({k:str(v) for k,v in _sensorlib.resolve_paths().items()}))')"
```

(Or simpler: read `signals_dir`/`state_dir`/`policy_path` once per run and reuse them — see `setup.py --dry-run` output for the resolved values on this host.)

## Procedure

1. **Read policy** — `<policy_path>` (default `policy.yaml` seeded by
   `resolve_paths()`). It defines the classification→channel routing, the
   conservative alert bar, watch terms, reminder profiles, and the calendar
   map. Honour it; do not hardcode.

2. **Read today's raw log** — `<signals_dir>/daily/$(date +%F).md`. Near
   midnight also read yesterday's file so late-night items aren't missed. Each
   entry looks like:
   `- HH:MM [Source] verb: summary (handle: key=val …) _(detected_at=…; sensor=…)_`

3. **Find NEW items.** For each entry derive a stable `source_id`:
   - prefer the handle id emitted by the sensor: `gmail:<gmail_message_id>`,
     `github:<repo#num>`, `ics:<ics_uid>`.
   - if no reliable unique id exists, use `line:<sha1 of the raw line, first 12 chars>`.
   **Hash the raw markdown entry exactly as it appears after trimming
   surrounding whitespace, including its leading Markdown marker.** Sensor
   entries always use a valid bullet (`- ...`), with deterministic highlights
   represented as `- IMPORTANT ...`.
   Dedupe repeated raw-log entries by `source_id` before judging. The daily log
   often mirrors the same item in `## Highlights` and its source section;
   process the first occurrence only so one upstream item does not get
   multiple ledger proposals or repeated triage lines in the same run.
   Skip any item where `$LEDGER seen --source-id <id>` prints `known` (exit 0).
   Only process items it reports `new` (exit 3). This is what keeps the run
   incremental and idempotent — never re-judge settled items.

4. **Classify** each new item into exactly one primary class:
   `URGENT` · `EVENT` · `ACTION` · `WATCH` · `FYI` · `NOISE`.
   - URGENT = time-sensitive AND consequential (deadline today/tomorrow,
     payment due, cancellation/reschedule, security alert, a direct ask
     needing a same-day reply).
   - EVENT = implies a dated calendar item (appointment, meeting, deadline
     date). Extract: title, date, all-day vs timed, and event_type for
     reminder + calendar routing.
   - WATCH = matches a `watch_terms` entry but isn't itself urgent.
   - NOISE = newsletters/marketing/bulk (use `noise_hints`). Drop from surfacing.
   When unsure between two, pick the lower-interrupt class (prefer FYI over
   URGENT). Be conservative: the bar for URGENT is high.

5. **Pull detail only when needed.** If the log line is too thin to classify,
   use the handle to fetch just that item (e.g. a Gmail message fetch, or a
   calendar event detail lookup). Do this sparingly; most items are decidable
   from the line. Hard cap: at most ~10 detail fetches per run; if you'd
   exceed it, classify the rest from the line and note it.

6. **Cluster** obviously-related items (same thread / meeting / topic) into
   one surfaced entry so the user sees one thing, not five.

7. **Write the triage log** — append to `<signals_dir>/triage/$(date +%F).md`
   under `## URGENT / ## EVENT / ## ACTION / ## WATCH / ## FYI` headings
   (create the file with those headings if missing). One line per surfaced
   item:
   `- [<class>] <one-line what + why it matters> · src: <source_id> · proposed: <action|none>`
   Do NOT write NOISE to the triage log.

8. **Propose actions** into the ledger (idempotent; safe to repeat):
   Immediately before proposing/writing a batch, re-run `$LEDGER seen --source-id <id>`
   for the source IDs you plan to emit and drop any that have become `known`.
   This final guard prevents duplicate output if a manual run, retry, or
   overlapping cron invocation settled an item after your initial scan.
   - URGENT (routing.telegram true):
     `$LEDGER propose --source-id <id> --kind alert --channel telegram --classification URGENT --payload-json '{"text":"<concise alert>","ref":"<handle/url>"}'`
   - EVENT (routing.calendar true): pick `event_type`, look up the calendar id
     (`calendar.by_type[event_type]` or `calendar.default`) and the reminder
     profile (`reminder_profiles[event_type]` or `default`):
     `$LEDGER propose --source-id <id> --kind create_event --channel calendar --classification EVENT --payload-json '{"summary":"...","all_day":true,"date":"YYYY-MM-DD","reminders":"4320,0","calendar":"<id or empty=default>","description":"src ref"}'`
     For a timed event use `{"all_day":false,"start":"<ISO+offset>","end":"<ISO+offset>",...}`.
   - Everything else (ACTION/WATCH/FYI): record so it isn't re-judged:
     `$LEDGER propose --source-id <id> --kind none --classification <CLASS> --payload-json '{}'`
   - NOISE: also record as `--kind none --classification NOISE` so it's settled.

9. **Advance the watermark** — write the max `detected_at` you processed to
   `<state_dir>/triage/watermark` (plain ISO line). Used only as an efficiency
   hint; the ledger `seen` check is the real idempotency guard. Before
   writing, read the existing watermark and only advance it if your max
   processed timestamp is newer.

10. **Finish quietly.** This job delivers `local`. End with a one-line summary
    like `triaged 7 new (1 URGENT, 2 EVENT, 4 FYI); proposed 1 alert + 2 events`.
    If the scheduled job instruction includes silent suppression and there are
    genuinely zero fresh items after the final ledger re-check, respond
    exactly `[SILENT]` and nothing else. Do not send a notification. Do not
    run signal-notify.

## Manual status-check workflow

When asked to "check signal triage and notify status" or similar:

1. Inspect cron jobs for the whole signal stack, especially Layer-1 sensors
   plus `signal-triage` and `signal-notify`. Report enabled/paused state,
   delivery target, next run, last run, last status, and delivery errors.
2. Check the live clock/timezone before judging waking-hours behavior.
3. Check the ledger with `$LEDGER pending` and summarize pending `alert` and
   `create_event` actions separately from `none` bookkeeping rows.
4. Read today's triage file and summarize surfaced items by heading/count; if
   the raw daily log has entries newer than the triage watermark or triage
   file mtime, call out that they have not been triaged yet.
5. Keep the answer compact: bottom line first, then paused/enabled state,
   pending urgent alerts, pending calendar writes, and any notable untriaged
   raw items.

## Architecture note: custom signal system, not stock Hermes

- `<signals_dir>/daily/YYYY-MM-DD.md` is the custom Layer-1 raw sensor
  handoff, written by the bundled no-agent sensor scripts.
- `<signals_dir>/triage/YYYY-MM-DD.md` is the LLM-summarized attention view,
  grouped by URGENT/EVENT/ACTION/WATCH/FYI.
- `<state_dir>/triage/ledger.db` is a custom SQLite action/idempotency ledger
  for this signal stack, managed by `signal_ledger.py`. It is **not** part of
  a default Hermes Agent install.

Do not treat `pending` rows with `kind=none` as actionable notifications. They
are bookkeeping rows that allow triage to mark items as settled/idempotent.

## Performance + recovery pitfalls

- Raw logs can grow while triage is running (Layer-1 sensors may append new
  rows during the pass). After writing/proposing and advancing the watermark,
  run one final bulk check against the current daily log + ledger for
  `remaining_new`. If new rows appeared, process them before finishing; only
  return `[SILENT]` or a final count after this check is zero.
- Large raw logs can contain hundreds of rows. Avoid spawning `$LEDGER seen`
  once per raw line from a code-execution tool; it can exceed typical tool
  timeouts and leave a partially-written run. Prefer a single terminal script
  that reads the ledger SQLite file directly to bulk-prefilter known
  `source_id`s, then still perform the required final `$LEDGER seen` guard
  only for the small batch you are about to emit. The ledger schema stores
  rows in an `actions` table (`source_id`, `kind`, `classification`, `status`,
  `payload`, timestamps).
- If a triage run times out after proposing rows but before writing all
  surfaced lines or advancing the watermark, recover idempotently: query the
  ledger for rows created in this run window, compare them against today's
  triage file, add any missing human-visible FYI/ACTION/WATCH lines, then
  advance the watermark to the max processed `detected_at`.

## Rules

- Conservative alert bar: when in doubt, it is not URGENT.
- Never invent dates. If an EVENT's date is ambiguous, classify it ACTION
  ("needs a date") instead of guessing — a wrong calendar entry is worse than
  none.
- Idempotency first: always gate on `$LEDGER seen` before proposing.
- Cheap: prefer the log line over fetching; respect the detail-fetch cap.
- Routine context-only weather forecasts are bookkeeping/NOISE unless they
  indicate disruptive or severe conditions; mark them settled with `kind none`
  but do not surface ordinary daily forecasts in the triage log.
- You are autonomous on the cron run — never ask questions; degrade and note.

## Cron execution pitfalls

- In scheduled cron mode, prefer normal tools (read/write file, terminal) over
  arbitrary code execution for this workflow — the signal stack only needs
  file reads, ledger CLI calls, and small shell probes, and cron approval
  guards may treat piped/interpreted shell as unsafe.
- When advancing `<state_dir>/triage/watermark`, use a file-writing tool (or
  another approval-safe write path) rather than shell redirection into a
  dotfile-style path — some cron approval policies flag that as an unsafe
  overwrite even though the watermark write is intended.
