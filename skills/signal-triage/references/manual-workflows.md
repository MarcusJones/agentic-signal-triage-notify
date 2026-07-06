# signal-triage — manual (non-cron) workflows

## Status check ("check signal triage and notify status")

1. Inspect the cron jobs for the whole stack (Layer-1 sensors +
   `signal-triage` + `signal-notify`): enabled/paused, delivery target,
   next/last run, last status, delivery errors.
2. Check the live clock/timezone before judging waking-hours behavior.
3. `$LEDGER pending` — summarize pending `alert` and `create_event`
   separately from `none` bookkeeping (never call `none` rows actionable).
4. Read today's triage file; if the raw daily log has entries newer than the
   triage file, call out that they are untriaged (or held by the debounce —
   run the prefilter script by hand to see its holding notice).
5. Answer compactly: bottom line first, then paused/enabled state, pending
   urgent alerts, pending calendar writes, notable untriaged items.

## Sensor cadence questions ("do the sensors call agents?")

Answer from the live cron inventory (`hermes cron list`), never memory:

- Layer-1 sensors are deterministic no-agent scripts appending to the daily
  log — no LLM.
- The Layer-1.5 prefilters (`triage_prefilter`, `notify_prefilter`) are also
  no-LLM: they gate and batch; the scheduler skips the agent entirely when
  they emit `{"wakeAgent": false}`.
- `signal-triage` / `signal-notify` are the only LLM jobs (judgement /
  dispatch), and only when woken.

## Dry-run / test ("run signal-triage once and show me")

1. `hermes cron run signal-triage` queues for the next scheduler tick; it
   does not return output synchronously. Poll until `last_run_at` advances
   and `last_status` is ok. Note: if the debounce is holding a young batch,
   the run is skipped by the wake gate — run the prefilter script directly
   to see why, or temporarily set `triage.debounce_minutes: 0` in
   policy.yaml.
2. Show the triage file grouped by heading + `$LEDGER pending` (proposed
   actions separate from the human-readable log).
3. If this was a one-shot test, pause the job again afterwards; keep
   `signal-notify` paused unless explicitly enabled.
4. Before suggesting notify resume, inspect pending `create_event` rows for
   over-eager calendar writes so the user can tune policy first.
