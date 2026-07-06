"""End-to-end prefilter tests: fixture daily log + fixture ledger → stdout.

Runs the real triage_prefilter / notify_prefilter mains with resolved paths
pointed at the tmp sandbox (see conftest). Protects the prompt contract the
signal-triage skill depends on.
"""

import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import _sensorlib as lib
import notify_prefilter
import triage_prefilter

PATHS = lib.resolve_paths()
LEDGER = PATHS["triage_state_dir"] / "ledger.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  source_id TEXT NOT NULL, kind TEXT NOT NULL, channel TEXT,
  status TEXT NOT NULL, classification TEXT, payload TEXT,
  created_at TEXT, executed_at TEXT, result TEXT,
  PRIMARY KEY (source_id, kind)
);
"""


def _write_log(lines):
    day = datetime.now().astimezone().date().isoformat()
    text = f"# Signal Log - {day}\n\n## Email\n\n" + "\n".join(lines) + "\n"
    (PATHS["daily_dir"] / f"{day}.md").write_text(text, encoding="utf-8")


def _entry(sid, minutes_old):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_old)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    return f"- 09:00 [Gmail] msg {sid} (gmail_message_id={sid}) _(detected_at={ts}; sensor=gmail)_"


def _reset_ledger(rows=()):
    LEDGER.unlink(missing_ok=True)
    con = sqlite3.connect(LEDGER)
    con.executescript(SCHEMA)
    for sid, kind, status in rows:
        con.execute(
            "insert into actions (source_id, kind, status) values (?,?,?)",
            (sid, kind, status),
        )
    con.commit()
    con.close()


def _run(main):
    buf = io.StringIO()
    with redirect_stdout(buf):
        main()
    return buf.getvalue()


def test_triage_all_known_gates_off():
    _write_log([_entry("aaa", 120)])
    _reset_ledger([("gmail:aaa", "none", "done")])
    out = _run(triage_prefilter.main)
    assert "NO NEW ITEMS" in out
    assert out.strip().splitlines()[-1] == '{"wakeAgent": false}'


def test_triage_young_batch_holds():
    _write_log([_entry("bbb", 5)])
    _reset_ledger()
    out = _run(triage_prefilter.main)
    assert "HOLDING BATCH" in out
    assert out.strip().splitlines()[-1] == '{"wakeAgent": false}'


def test_triage_aged_batch_flushes_with_ids():
    _write_log([_entry("ccc", 5), _entry("ddd", 500)])
    _reset_ledger()
    out = _run(triage_prefilter.main)
    assert "NEW ITEMS (2)" in out
    assert "[gmail:ccc]" in out and "[gmail:ddd]" in out
    assert "wakeAgent" not in out  # flushed → no gate line
    assert "=== POLICY" in out and "END PREFILTER" in out


def test_triage_missing_ledger_wakes_with_diagnostic():
    _write_log([_entry("eee", 5)])
    LEDGER.unlink(missing_ok=True)
    out = _run(triage_prefilter.main)
    assert "LEDGER READ FAILED" in out
    assert "wakeAgent" not in out  # fail-open: agent wakes


def test_notify_settles_bookkeeping_and_gates_off():
    _reset_ledger([("gmail:x", "none", "proposed"), ("gmail:y", "alert", "done")])
    out = _run(notify_prefilter.main)
    assert "settled 1 bookkeeping row" in out
    assert out.strip().splitlines()[-1] == json.dumps({"wakeAgent": False})
    con = sqlite3.connect(LEDGER)
    status, result = con.execute(
        "select status, result from actions where source_id='gmail:x'"
    ).fetchone()
    con.close()
    assert status == "done"
    assert json.loads(result)["bookkeeping"] is True


def test_notify_wakes_for_actionable_rows():
    _reset_ledger([("gmail:z", "alert", "proposed")])
    out = _run(notify_prefilter.main)
    assert "PENDING ACTIONABLE LEDGER ROWS" in out
    assert "wakeAgent" not in out
