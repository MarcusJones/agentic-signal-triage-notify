#!/usr/bin/env python3
"""notify_prefilter.py — wake gate for the signal-notify cron job.

1. Self-settles pure-bookkeeping ledger rows (kind='none',
   status='proposed' → 'done'): that is deterministic, exactly what the
   agent would do mechanically, and not worth an LLM wake-up. The write
   format matches signal_ledger.py `mark` byte-for-byte.
2. Wakes the agent ONLY when actionable rows (alert / create_event) are
   pending; otherwise emits {"wakeAgent": false} and the scheduler skips
   the LLM run entirely.

Failure posture: ledger unreadable → wake the agent (fail-open). Never
exits non-zero.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone

from _sensorlib import resolve_paths


def main() -> None:
    ledger_db = resolve_paths()["triage_state_dir"] / "ledger.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        con = sqlite3.connect(ledger_db)
        settled = con.execute(
            "update actions set status='done', executed_at=?, result=? "
            "where status='proposed' and kind='none'",
            (now, json.dumps({"bookkeeping": True, "via": "notify_prefilter"})),
        ).rowcount
        con.commit()
        rows = con.execute(
            "select kind, count(*) from actions where status='proposed' group by kind"
        ).fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001 - fail-open: wake the agent
        print(f"notify prefilter: ledger access failed ({e}) — proceed manually per the skill.")
        return

    counts = {kind: n for kind, n in rows}
    actionable = counts.get("alert", 0) + counts.get("create_event", 0)
    if actionable == 0:
        extra = f" (settled {settled} bookkeeping row(s))" if settled else ""
        print(f"No actionable pending ledger rows{extra} — agent run skipped by wake gate.")
        print(json.dumps({"wakeAgent": False}))
    else:
        print(
            f"PENDING ACTIONABLE LEDGER ROWS: {json.dumps(counts)} "
            f"(bookkeeping already settled by prefilter: {settled}). "
            "Dispatch per the signal-notify skill; mark every row done/failed."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"notify prefilter crashed: {type(e).__name__}: {e} — proceed manually per the skill.")
    sys.exit(0)
