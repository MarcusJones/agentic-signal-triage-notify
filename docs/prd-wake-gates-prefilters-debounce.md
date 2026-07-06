# PRD — Wake gates, deterministic prefilters, and the poll-fast/flush-slow cadence

**Status:** draft for iteration
**Target version:** 0.2.0
**Depends on:** current tap layout (registry.yaml-driven setup, `_sensorlib.resolve_paths()`, `signal_ledger.py` schema)

## 1. Summary

Layer 2 (triage) and Layer 3 (notify) currently wake an LLM on a fixed hourly
schedule regardless of whether there is anything to judge. This PRD introduces
a deterministic **Layer 1.5** between the sensors and the LLM: cheap pre-run
scripts that do discovery, dedup, and batching *before any tokens are spent*,
and skip the LLM run entirely when it has nothing to do. The result, measured
on the reference deployment (2026-07-06): **~90% fewer LLM runs at strictly
better worst-case latency** than the hourly design this repo currently ships.

The headline inversion: once empty polls are free, **cadence stops being the
cost driver — the wake-up is**. So we poll *more* often (every 15 min) and
flush *less* often (age-debounced batches), which improves both cost and
latency simultaneously.

## 2. Motivation (measured, not vibes)

Production measurements that motivated this (7 days of session accounting on
the reference deployment, before optimization):

- Triage + notify were **36 of 42 daily agent runs** — 73% of all LLM input
  tokens consumed on the host.
- A typical hourly triage run: 40–105k input tokens across 7–10 tool
  round-trips — of which **~95% was rediscovery**: reading the daily log,
  re-deriving source ids, and re-checking items already settled in the
  ledger (routinely 23 of 24 items).
- Most notify runs found zero pending actions and still paid a full agent
  session to learn that.

Anatomy of the waste per run: (system prompt ≈ 4k tokens of tool schemas +
skill text ≈ 6k tokens) × every round-trip, plus the round-trips themselves
being spent on deterministic work.

## 3. Goals / non-goals

**Goals**

1. Zero-token scheduled runs when there is nothing to judge or dispatch.
2. Deterministic discovery: the LLM receives the delta, not the haystack.
3. Content-blind batching with a bounded, configurable latency (no urgency
   keywords, no "looks important" heuristics — those rot silently and
   require planning ahead).
4. Everything configurable through `policy.yaml`; sensible defaults; one-line
   escape hatch back to v0.1 behavior.
5. Unit-testable without an LLM or a Hermes install (pure Python + fixtures).

**Non-goals**

- Changing the three-layer architecture, the ledger schema, or the sensor
  entry contract (both are already sufficient — see §4.2).
- Per-item urgency classification outside the LLM. Explicitly rejected: the
  prefilter must never decide *importance*, only *newness* and *age*.
- Real-time push. The floor remains sensor cadence + debounce.

## 4. Design

### 4.1 Component: `triage_prefilter.py` (new, Layer 1.5)

A pre-run script attached to the `signal-triage` cron job via the Hermes
`script` hook (stdout is injected at the top of the agent prompt; supported
today, used by `registry.yaml` → `setup.py` sync).

Responsibilities, in order:

1. Read today's daily log (+ yesterday's within the first hour after
   midnight) from the resolved `signals_dir`.
2. Derive a **stable source_id per entry** (see §4.2) and dedupe within the
   log (Highlights mirrors).
3. Bulk-diff against the ledger (read-only SQLite: `select source_id from
   actions`) — never one subprocess per line.
4. **Debounce** (§4.3): if the oldest unjudged item is younger than
   `debounce_minutes`, print a holding notice and emit the wake gate.
5. Otherwise print the prefilter block: new items with ids pre-derived, plus
   `policy.yaml` verbatim, capped at `max_items` with an explicit truncation
   notice (no silent caps).

Failure posture: **never exits non-zero, never blocks the run**. Ledger
unreadable / crash → print a diagnostic line; the skill instructs the agent
to fall back to the v0.1 manual procedure. Fail-open in the waking direction,
always.

### 4.2 Source-id derivation moves into `_sensorlib.py`

Today the id conventions live as prose in the triage SKILL.md. With a script
also deriving them, prose is no longer an acceptable source of truth — drift
between skill text and prefilter code re-surfaces duplicates.

- New: `_sensorlib.source_id(line) -> str` — single implementation, imported
  by the prefilter, unit-tested against fixture logs.
- Convention: prefer the entry's `handle:` key (`gmail_message_id=…` →
  `gmail:<id>`, etc.); sensors that emit *context summaries* which repeat
  under new poll ids (the company-availability pattern) get special-cased
  semantic ids; fallback is `line:<sha1[:12]>` of the trimmed raw line
  **including its leading list marker**.
- `docs/writing-a-sensor.md` gains a section: *"Give your entries a stable
  handle"* — a sensor whose entries carry a unique handle gets exact-once
  triage for free; one that doesn't falls back to line hashing (safe but
  re-surfaces on any reformatting).
- The SKILL.md conventions table stays, but is demoted to documentation of
  what `_sensorlib.source_id()` does, with a "keep in sync" pointer.

### 4.3 Age-based debounce (poll fast, flush slow)

- Triage cadence in `registry.yaml` changes from hourly to `*/15 6-23 * * *`
  (the poll is free when gated).
- The prefilter wakes the agent only when the **oldest** unjudged item has
  waited ≥ `debounce_minutes` (default **90**, configurable in
  `policy.yaml`). Bursts accumulate into one wake-up; a lone item flushes
  ~90 min after arrival; a quiet day costs zero LLM runs.
- **Stateless**: item age comes from each entry's own `detected_at` stamp
  (already mandated by the sensor contract). No timer files, no lost state.
  Entries with an unparseable `detected_at` count as "old enough" (fail-open
  to waking, never to holding forever).
- **Content-blind by design.** The debounce looks at counts and timestamps
  only. This is a deliberate product decision, not a TODO.

Latency envelope (defaults): sensor cadence + ≤ debounce + ≤ poll interval —
worst case ≈ 2h for a lone item, typical much less for bursts; both bounded
and uniform across item types.

### 4.4 Wake gate

- Convention: if the last non-empty stdout line of the pre-run script is
  `{"wakeAgent": false}`, the Hermes cron scheduler skips the agent run
  entirely (no LLM call, no delivery). Also: empty stdout skips the run.
- Portability note for the README: on runtimes without this convention the
  prefilter still works — the agent wakes, sees "NO NEW ITEMS", and exits in
  one cheap call. The gate is an optimization, not a correctness dependency.

### 4.5 Component: `notify_prefilter.sh` (new)

- **Self-settles bookkeeping**: rows with `kind='none'` and
  `status='proposed'` are marked done directly in SQLite (identical format
  to `signal_ledger.py cmd_mark`, `{"bookkeeping": true, "via":
  "notify_prefilter"}`). Waking an LLM to run a mechanical UPDATE is waste.
- Wakes the agent **only** when actionable rows (`alert`, `create_event`)
  are pending; otherwise gates off.
- Notify cadence in `registry.yaml`: `5,35 6-23 * * *` — pending alerts
  dispatch within ≤30 min of a triage flush (better than the hourly v0.1
  worst case).
- Ledger access failure → wake the agent (fail-open).

### 4.6 Skill diet: core + references, with an anti-eager-load rule

- `skills/signal-triage/SKILL.md` slims to the core (identity, paths, id
  conventions table, procedure, rules; ≈6.5 KB) and gains the prefilter
  contract: trust the block for discovery; `NO NEW ITEMS` → verify cheaply,
  `[SILENT]`; `CRASHED` → manual fallback; **the final `seen` guard before
  proposing is always required** (the prefilter is a discovery optimization,
  not the idempotency authority).
- Operational lore moves to `skills/signal-triage/references/pitfalls.md`
  and `references/manual-workflows.md`.
- ⚠️ Field-tested pitfall: a friendly "see references/" pointer makes agents
  **eagerly load every reference file at run start**, costing more than the
  split saves. The pointer MUST be phrased as a prohibition with explicit
  triggers: *"Do NOT load references/ at run start; open pitfalls.md only
  when <recovery/batch/duplicate> actually occurs."*

### 4.7 Config surface (`policy.yaml` additions)

```yaml
triage:
  debounce_minutes: 90   # 0 = flush on any new item (v0.1-like immediacy)
  max_items: 120         # per-flush cap; truncation is always announced
```

Registry additions: optional per-job `model:` / `provider:` fields on layer
2/3 jobs, synced by `setup.py`. Cheap-model routing is host-specific, so the
default is unset (inherit), with README guidance: scheduled classification
does not need the flagship model; on gated endpoints (e.g. ChatGPT-account
Codex auth) probe the model allow-list empirically — it shifts.

### 4.8 Escape hatch / rollback

`hermes cron edit signal-triage --script ""` restores v0.1 behavior at
runtime; `setup.py` respects a `prefilter: false` registry override so a
user can opt out declaratively. Document both in the README.

## 5. Migration

- `setup.py` sync updates existing installs' cron jobs (cadence + script
  fields) idempotently — it already owns those jobs per registry.yaml.
- Version bump: skills + index.json → 0.2.0.
- README: replace "hourly LLM sweep" framing; update the mermaid diagram
  with the Layer 1.5 gate box; add the measured before/after table.
- `docs/architecture.md`: new section "Wake gates & the prefilter layer";
  `docs/writing-a-sensor.md`: stable-handle guidance (§4.2).

## 6. Testing (CI-able, no LLM required)

1. `test_source_id.py` — fixture log lines → expected ids (every sensor the
   tap ships, plus the line-hash fallback and marker-inclusion rule).
2. `test_debounce.py` — synthetic `detected_at` ages vs `debounce_minutes`:
   hold / flush / fail-open-on-unparseable / ledger-unreadable-wakes.
3. `test_notify_gate.py` — tmp SQLite ledger: none-only → settle + gate off;
   alert pending → wake; corrupt db → wake.
4. Golden-output test: fixture log + fixture ledger → exact prefilter stdout
   (protects the prompt contract the skill depends on).
5. GitHub Actions: pytest on 3.11/3.12, shellcheck on the wrappers.

## 7. Success metrics

From the reference deployment (to reproduce post-merge and publish in the
README):

| Metric | v0.1 (hourly) | v0.2 target |
|---|---|---|
| Scheduled LLM runs/day (triage+notify) | 36 | — (72+36 free polls) |
| Actual LLM wake-ups/day | 36 | ~6–12 (bursts + flushes) |
| Input tokens/run | 40–105k | 25–35k |
| Worst-case alert latency | ~1h triage + up to 1h notify | ≤ debounce + 30 min |
| Empty-day cost | 36 full sessions | 0 tokens |

## 8. Open questions (iterate here)

1. **Where does the prefilter live?** All scripts currently ship under
   `skills/signal-notify/scripts/` (the "sensor framework" home). Proposal:
   keep them there for tap-layout simplicity, referenced by both skills —
   but a `scripts/` top-level shared home is cleaner if agentskills.io
   allows non-skill dirs. Decide before implementation.
2. **Debounce default**: 90 min suits a personal-assistant profile. Should
   the seeded `policy.yaml` default to 90 or 30? (Reference deployment: 90.)
3. **Per-sensor debounce exemptions?** e.g. a flood-warning sensor might
   warrant `debounce_minutes: 0` for its category. This reintroduces
   content-awareness through configuration (acceptable? it's declarative,
   not heuristic). Deferred unless a real need appears.
4. **Non-Hermes runtimes**: is documenting the graceful degradation (§4.4)
   enough for v0.2, or do we want a generic cron wrapper example?
5. **Windows**: the wrapper scripts are bash; the Python cores are portable.
   Ship `.ps1` wrappers now or defer to a contributor?

## 9. Provenance

Pattern extracted from the reference deployment's optimization pass
(2026-07-06). The general write-up (framework-agnostic checklist, full
measurements) lives in the operator's infra repo as
`docs/agent-cron-token-optimization.md`; this PRD is the tap-specific
implementation plan. Reference implementations of both prefilters exist and
are battle-tested on that host — porting them here is mostly path resolution
(`_sensorlib.resolve_paths()`) plus the §4.2 refactor and tests.
