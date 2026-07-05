#!/usr/bin/env python3
"""Action queue + idempotency ledger for the triage/notify layers.

This is the central anti-double-alert / anti-double-create mechanism. Triage
proposes actions; notify executes pending ones and marks them done. A row is
keyed by (source_id, kind) so re-runs never duplicate.

SQLite at <state_dir>/triage/ledger.db (see _sensorlib.resolve_paths()).
Called from the agent skills via the terminal so the reasoning stays cheap and
the safety logic lives in code.

Usage:
  signal_ledger.py init
  signal_ledger.py seen   --source-id gmail:19e7...                  # exit 0 if known, 3 if new
  signal_ledger.py propose --source-id ID --kind alert|create_event|none \
                           --channel telegram|calendar --classification URGENT \
                           --payload-json '{"text":"..."}'           # idempotent insert
  signal_ledger.py pending [--channel calendar]                      # JSON list of status=proposed
  signal_ledger.py mark   --source-id ID --kind KIND --status done|failed|skipped \
                           [--result-json '{"event_id":"..."}']
  signal_ledger.py recent [--days 7]                                 # JSON, for audit
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from _sensorlib import resolve_paths

DB = Path(os.environ.get("HERMES_TRIAGE_LEDGER") or (resolve_paths()["triage_state_dir"] / "ledger.db"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(DB.parent, 0o700)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  source_id      TEXT NOT NULL,
  kind           TEXT NOT NULL,
  channel        TEXT,
  status         TEXT NOT NULL DEFAULT 'proposed',
  classification TEXT,
  payload        TEXT,
  created_at     TEXT,
  executed_at    TEXT,
  result         TEXT,
  PRIMARY KEY (source_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_source ON actions(source_id);
"""


def cmd_init(con, args):
    con.executescript(SCHEMA)
    con.commit()
    print("ledger initialized at", DB)


def cmd_seen(con, args):
    row = con.execute("SELECT 1 FROM actions WHERE source_id=? LIMIT 1", (args.source_id,)).fetchone()
    if row:
        print("known")
        sys.exit(0)
    print("new")
    sys.exit(3)


def cmd_propose(con, args):
    # Idempotent: do nothing if this (source_id, kind) already exists in any
    # terminal or pending state — prevents re-proposing an item already acted on.
    existing = con.execute(
        "SELECT status FROM actions WHERE source_id=? AND kind=?",
        (args.source_id, args.kind),
    ).fetchone()
    if existing:
        print(f"skip (exists: {existing['status']})")
        return
    con.execute(
        "INSERT INTO actions (source_id, kind, channel, status, classification, payload, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (args.source_id, args.kind, args.channel, "proposed", args.classification,
         args.payload_json, _now()),
    )
    con.commit()
    print("proposed")


def cmd_pending(con, args):
    sql = "SELECT * FROM actions WHERE status='proposed'"
    params: list = []
    if args.channel:
        sql += " AND channel=?"
        params.append(args.channel)
    sql += " ORDER BY created_at"
    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    print(json.dumps(rows, indent=2))


def cmd_mark(con, args):
    con.execute(
        "UPDATE actions SET status=?, executed_at=?, result=? WHERE source_id=? AND kind=?",
        (args.status, _now(), args.result_json, args.source_id, args.kind),
    )
    con.commit()
    print("marked", args.status)


def cmd_recent(con, args):
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM actions ORDER BY created_at DESC LIMIT 200"
    ).fetchall()]
    print(json.dumps(rows, indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("seen"); s.add_argument("--source-id", required=True)
    s = sub.add_parser("propose")
    s.add_argument("--source-id", required=True)
    s.add_argument("--kind", required=True)
    s.add_argument("--channel")
    s.add_argument("--classification")
    s.add_argument("--payload-json", default="{}")
    s = sub.add_parser("pending"); s.add_argument("--channel")
    s = sub.add_parser("mark")
    s.add_argument("--source-id", required=True)
    s.add_argument("--kind", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--result-json", default="{}")
    s = sub.add_parser("recent"); s.add_argument("--days", type=int, default=7)
    args = p.parse_args()

    con = connect()
    {
        "init": cmd_init, "seen": cmd_seen, "propose": cmd_propose,
        "pending": cmd_pending, "mark": cmd_mark, "recent": cmd_recent,
    }[args.cmd](con, args)


if __name__ == "__main__":
    main()
