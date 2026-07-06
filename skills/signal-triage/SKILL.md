---
name: signal-triage
description: >-
  Layer 2 of the signal-triage-notify system. Scheduled LLM sweep that
  classifies NEW items from the raw situational-awareness log, writes a
  surfaced triage view, and PROPOSES actions into the ledger. A deterministic
  Layer-1.5 prefilter does discovery and batching before any tokens are
  spent. Never sends notifications or writes the calendar — that is
  signal-notify's job. Use for the scheduled signal-triage cron, or when
  asked to "triage the signals" / "what needs my attention".
version: 0.2.0
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

You judge the cheap raw signals collected by the Layer-1 sensors. You do NOT
notify the user or touch their calendar — you classify, surface, and
**propose** into the ledger; `signal-notify` executes. Keep runs cheap.

**Do NOT load `references/` files at run start** — they cost tokens and are
rarely needed. Open them only when their trigger actually occurs:
`references/pitfalls.md` → an interrupted/failed run, apparent duplicates
re-surfacing, or a batch of related documents; `references/manual-workflows.md`
→ the user asked for a status check or dry-run (not cron). A normal cron run
needs NONE of them.

## Paths + shorthand

All paths are **vault-optional** and resolve automatically via
`resolve_paths()` in the bundled `_sensorlib.py`:

- Raw log (input): `<signals_dir>/daily/$(date +%F).md`
- Triage log (output): `<signals_dir>/triage/$(date +%F).md`
- Policy: `<policy_path>` (seeded with sane defaults on first run)
- Watermark: `<state_dir>/triage/watermark`

```bash
SD="${HERMES_SKILL_DIR}/../signal-notify/scripts"
LEDGER="uv run --project $SD python $SD/signal_ledger.py"
```

## The prefilter block (cron runs)

The cron job runs `triage_prefilter.sh` (Layer 1.5, no LLM) as its script
hook and injects its output at the top of your prompt: a
`=== TRIAGE PREFILTER ===` block listing the **NEW items with source_ids
already derived**, plus the policy verbatim. When there is nothing to judge
— or the batch is younger than the policy's `triage.debounce_minutes` — the
scheduler never wakes you at all (wake gate), so you only ever see runs with
work to do or a failure notice.

- Trust it for discovery: do NOT re-read the daily log or re-derive ids for
  listed items. Skip straight to classification.
- It says `LEDGER READ FAILED` / `CRASHED` → fall back to the manual
  procedure (steps 2m/3m below).
- The final ledger `seen` guard before proposing is ALWAYS required — the
  prefilter is a discovery optimization, not the idempotency authority.

## Source-id conventions

Implemented by `_sensorlib.source_id()` — that function is the single source
of truth; this table documents it. Prefer the entry's machine handle:

| Row contains | source_id |
|---|---|
| `gmail_message_id=<id>` | `gmail:<id>` |
| `ics_uid=<uid>` | `ics:<uid>` |
| `gcal_event_id=<id>` | `gcal:<id>` |
| `[GitHub] … owner/repo#123` | `github:owner/repo#123` |
| anything else | `line:<sha1 first 12>` — hash of the trimmed raw line INCLUDING its leading `- ` marker |

Dedupe by source_id before judging: the daily log mirrors important items in
`## Highlights`; process the first occurrence only.

## Procedure

1. **Policy** — read it from the prefilter block (manual runs: read
   `<policy_path>`). It defines routing, the conservative alert bar, watch
   terms, reminder profiles, and the calendar map. Honour it; do not
   hardcode.
2. **New items** — from the prefilter block.
   - *(2m — manual fallback only)* Read today's raw log (+ yesterday's near
     midnight); derive `source_id` per the table above.
   - *(3m — manual fallback only)* Skip anything `$LEDGER seen --source-id
     <id>` reports `known` (exit 0); process only `new` (exit 3).
3. **Classify** each new item into exactly one primary class:
   - **URGENT** — time-sensitive AND consequential (deadline today/tomorrow,
     payment due, cancellation/reschedule, security alert, a direct ask
     needing a same-day reply). The bar is HIGH.
   - **EVENT** — implies a dated calendar item; extract title, date, all-day
     vs timed, and event_type for reminder + calendar routing.
   - **ACTION** — needs the user, not urgent. **WATCH** — matches a
     `watch_terms` entry. **FYI** — worth knowing. **NOISE** — newsletters /
     marketing / bulk (use `noise_hints`); never written to the triage log.
   - When unsure between two, pick the lower-interrupt class.
4. **Pull detail only when needed** — if a line is too thin to classify, use
   its `handle:` to fetch just that item. Hard cap ~10 fetches per run;
   classify the rest from the line and note it.
5. **Cluster** obviously-related items (same thread / meeting / topic) into
   one surfaced entry so the user sees one thing, not five.
6. **Write the triage log** — append under `## URGENT / ## EVENT / ## ACTION
   / ## WATCH / ## FYI` (create the file with those headings if missing):
   `- [<class>] <one-line what + why it matters> · src: <source_id> · proposed: <action|none>`
   Do NOT write NOISE to the triage log.
7. **Propose actions** into the ledger. Immediately before emitting, re-run
   `$LEDGER seen --source-id <id>` for each id and drop any now-`known`
   (this guard is what makes retries and overlapping runs safe):
   - URGENT → `$LEDGER propose --source-id <id> --kind alert --channel telegram --classification URGENT --payload-json '{"text":"<concise alert>","ref":"<handle/url>"}'`
   - EVENT → pick `event_type`, look up the calendar
     (`calendar.by_type[event_type]` or `calendar.default`) and reminder
     profile (`reminder_profiles[event_type]` or `default`):
     `$LEDGER propose --source-id <id> --kind create_event --channel calendar --classification EVENT --payload-json '{"summary":"...","all_day":true,"date":"YYYY-MM-DD","reminders":"1440,0","calendar":"<id or empty=default>","description":"src ref"}'`
     Timed events: `{"all_day":false,"start":"<ISO+offset>","end":"<ISO+offset>",…}`.
   - ACTION/WATCH/FYI/NOISE → `$LEDGER propose --source-id <id> --kind none --classification <CLASS> --payload-json '{}'` so they're settled and never re-judged.
8. **Advance the watermark** — write the max processed `detected_at` to
   `<state_dir>/triage/watermark` (plain ISO line; efficiency hint only —
   the ledger is the real idempotency guard). Only advance forward.
9. **Final sweep** — logs grow mid-run: one last check for entries newer
   than what you processed; handle them before finishing.
10. **Finish quietly** — end with one line:
    `triaged N new (x URGENT, y EVENT, z FYI); proposed …`. Zero fresh items
    after the final re-check → respond exactly `[SILENT]`. Never send
    notifications; never run signal-notify.

## Rules

- Conservative alert bar — when in doubt, it is not URGENT.
- Never invent dates; ambiguous date → ACTION ("needs a date"), not a guess
  — a wrong calendar entry is worse than none.
- Idempotency first — the final ledger `seen` gate is non-negotiable.
- Cheap — prefer the prefilter/log line over fetching detail.
- Autonomous on cron — never ask questions; degrade and note.
