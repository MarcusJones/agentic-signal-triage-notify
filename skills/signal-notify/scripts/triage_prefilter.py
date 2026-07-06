#!/usr/bin/env python3
"""triage_prefilter.py — deterministic Layer-1.5 pre-pass for signal-triage.

Attached to the signal-triage cron job as its `script` hook: stdout is
injected at the top of the agent's prompt. Does the grunt work the LLM used
to burn tool round-trips on:

  1. read today's (and, within the first hour after midnight, yesterday's)
     daily log from the resolved signals_dir
  2. derive a stable source_id per entry (_sensorlib.source_id — the single
     source of truth for id conventions)
  3. dedupe within the log (Highlights mirrors) and bulk-diff against the
     ledger (read-only SQLite — never one subprocess per line)
  4. debounce: hold the batch (wakeAgent:false) until the OLDEST unjudged
     item has waited `triage.debounce_minutes` (policy.yaml; content-blind —
     counts and timestamps only, never importance)
  5. otherwise print the prefilter block: new items with ids pre-derived,
     plus policy.yaml verbatim

Wake-gate convention (Hermes cron scheduler): if the last non-empty stdout
line is `{"wakeAgent": false}`, the agent run is skipped entirely. On
runtimes without this convention the block degrades gracefully — the agent
wakes, reads the holding notice, and exits cheaply.

The agent MUST still run its final `seen` guard before proposing — this
prefilter is a discovery optimization, not the idempotency authority.

Failure posture: never exits non-zero, never blocks the run. Any crash
prints a diagnostic block; the skill instructs the agent to fall back to the
manual procedure. Fail-open in the waking direction, always.
"""

from __future__ import annotations

import datetime
import sqlite3
import sys

from _sensorlib import (
    entry_detected_at,
    iter_entry_lines,
    load_policy,
    resolve_paths,
    source_id,
)

DEFAULT_DEBOUNCE_MINUTES = 30
DEFAULT_MAX_ITEMS = 120


def main() -> None:
    paths = resolve_paths()
    policy = load_policy()
    triage_cfg = policy.get("triage") or {}
    debounce_minutes = int(triage_cfg.get("debounce_minutes", DEFAULT_DEBOUNCE_MINUTES))
    max_items = int(triage_cfg.get("max_items", DEFAULT_MAX_ITEMS))

    now = datetime.datetime.now().astimezone()
    days = [now.date()]
    if now.hour < 1:  # near midnight: include yesterday so late items aren't orphaned
        days.append((now - datetime.timedelta(days=1)).date())

    seen_in_log: set[str] = set()
    items: list[tuple[str, str]] = []  # (sid, line), first occurrence, log order
    total_lines = 0
    for day in days:
        for line in iter_entry_lines(paths["daily_dir"] / f"{day.isoformat()}.md"):
            total_lines += 1
            sid = source_id(line)
            if sid in seen_in_log:
                continue
            seen_in_log.add(sid)
            items.append((sid, line))

    ledger_db = paths["triage_state_dir"] / "ledger.db"
    known: set[str] = set()
    ledger_ok = False
    try:
        con = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
        known = {r[0] for r in con.execute("select source_id from actions")}
        con.close()
        ledger_ok = True
    except Exception as e:  # noqa: BLE001 - deliberately broad, fail-open
        print(
            f"=== TRIAGE PREFILTER: LEDGER READ FAILED ({e}) — "
            "fall back to the manual `seen` scanning procedure ==="
        )

    fresh = [(sid, line) for sid, line in items if sid not in known]

    print(f"=== TRIAGE PREFILTER (deterministic, generated {now.isoformat(timespec='seconds')}) ===")
    print(
        f"Scanned: {', '.join(d.isoformat() for d in days)} — {total_lines} entry lines, "
        f"{len(items)} distinct source_ids, {len(items) - len(fresh)} already settled in ledger."
    )

    if not fresh and ledger_ok:
        # Nothing new, ledger authoritative → skip the agent entirely.
        print("NO NEW ITEMS — agent run skipped by wake gate.")
        print('{"wakeAgent": false}')
        return

    if not fresh:
        print("NO NEW ITEMS (ledger unreadable — waking agent to verify manually).")
    elif ledger_ok and debounce_minutes > 0:
        # Content-blind age debounce. Stateless: ages come from each entry's
        # own detected_at stamp. Unparseable stamps count as "old enough"
        # (fail-open to waking — never risk holding forever).
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ages = [entry_detected_at(line) for _, line in fresh]
        if all(a is not None for a in ages):
            oldest_min = max((now_utc - a).total_seconds() / 60 for a in ages)
            if oldest_min < debounce_minutes:
                print(
                    f"HOLDING BATCH: {len(fresh)} new item(s), oldest {oldest_min:.0f} min "
                    f"< {debounce_minutes} min debounce — agent run skipped by wake gate."
                )
                print('{"wakeAgent": false}')
                return

    if fresh:
        truncated = len(fresh) > max_items
        shown = fresh[:max_items]
        header = f"NEW ITEMS ({len(fresh)}"
        if truncated:
            header += f" — TRUNCATED to first {max_items}"
        print(header + "):")
        for sid, line in shown:
            print(f"[{sid}] {line}")
        if truncated:
            print(
                f"...plus {len(fresh) - max_items} more — process the above, "
                "then re-run discovery for the rest."
            )

    print()
    print(f"=== POLICY ({paths['policy_path']}) ===")
    try:
        print(paths["policy_path"].read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"(policy read failed: {e} — read it yourself)")
    print(
        "=== END PREFILTER — classify the NEW ITEMS above; "
        "final ledger `seen` guard still required before proposing ==="
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - never break the cron prompt
        print(
            f"=== TRIAGE PREFILTER CRASHED: {type(e).__name__}: {e} — "
            "use the manual skill procedure ==="
        )
    sys.exit(0)
