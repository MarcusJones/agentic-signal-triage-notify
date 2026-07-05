#!/usr/bin/env python3
"""Calendar WRITE helper for the notify layer (Layer 3).

Self-contained so it survives skill updates. Uses a full-scope Google token
(default: ~/.hermes/google_token.json, override with HERMES_GOOGLE_TOKEN), NOT
the read-only sensor token. Creates ATTENDEE-FREE events only — it never sets
an `attendees` field, so no invitation is ever sent to a third party.

Features the bundled Google Workspace tooling typically lacks:
  - custom reminder overrides (e.g. birthday: 3 days before + day-of)
  - all-day (date-only) events
  - duplicate guard (skip if a same-summary event already exists in a window)
  - ensure a purpose-specific calendar exists, return its id

Usage:
  gcal_write.py list-calendars
  gcal_write.py ensure-calendar --name "Signals · Birthdays"      # prints calendar id
  gcal_write.py create --summary "Anna's birthday" --all-day --date 2026-07-03 \
                --reminders "4320,0" --calendar <id> [--description "..."] \
                [--dedup-window-days 3]
  gcal_write.py create --summary "Dentist" --start 2026-06-05T14:00:00+02:00 \
                --end 2026-06-05T15:00:00+02:00 --reminders "1440,120" --calendar <id>

Output is JSON: {"status": "created"|"duplicate", "id": ..., "htmlLink": ...}
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN = Path(os.environ.get("HERMES_GOOGLE_TOKEN", Path.home() / ".hermes" / "google_token.json"))
SCOPES = ["https://www.googleapis.com/auth/calendar"]
# "primary" is Google's alias for the authenticated user's own calendar — no
# account-specific calendar id is shipped. Set HERMES_DEFAULT_CALENDAR to a
# calendar id to route events to a dedicated calendar instead.
DEFAULT_CALENDAR = os.environ.get("HERMES_DEFAULT_CALENDAR", "primary")


def service():
    if not TOKEN.exists():
        raise SystemExit(f"Missing Google token at {TOKEN}; see docs/writing-a-sensor.md for OAuth setup.")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def cmd_list_calendars(svc, args):
    items = svc.calendarList().list().execute().get("items", [])
    print(json.dumps([
        {"id": c.get("id"), "summary": c.get("summary"),
         "accessRole": c.get("accessRole"), "primary": c.get("primary", False)}
        for c in items
    ], indent=2))


def cmd_ensure_calendar(svc, args):
    for c in svc.calendarList().list().execute().get("items", []):
        if (c.get("summary") or "").strip() == args.name.strip():
            print(c.get("id"))
            return
    created = svc.calendars().insert(body={"summary": args.name, "timeZone": args.timezone}).execute()
    print(created.get("id"))


def _reminders(spec: str | None) -> dict:
    if not spec:
        return {"useDefault": True}
    overrides = [{"method": "popup", "minutes": int(m)} for m in spec.split(",") if m.strip()]
    return {"useDefault": False, "overrides": overrides}


def _duplicate_exists(svc, calendar_id: str, summary: str, anchor: datetime, window_days: int) -> dict | None:
    tmin = (anchor - timedelta(days=window_days)).isoformat()
    tmax = (anchor + timedelta(days=window_days)).isoformat()
    try:
        events = svc.events().list(
            calendarId=calendar_id, timeMin=_z(tmin), timeMax=_z(tmax),
            q=summary, singleEvents=True, maxResults=20,
        ).execute().get("items", [])
    except Exception:
        return None
    for ev in events:
        if (ev.get("summary") or "").strip().lower() == summary.strip().lower():
            return ev
    return None


def _z(iso: str) -> str:
    # events.list needs an RFC3339 with offset; add Z if naive.
    return iso if ("+" in iso[10:] or iso.endswith("Z")) else iso + "Z"


def cmd_create(svc, args):
    calendar_id = args.calendar or DEFAULT_CALENDAR
    summary = args.summary
    body: dict = {"summary": summary, "reminders": _reminders(args.reminders)}
    if args.description:
        body["description"] = args.description

    if args.all_day:
        if not args.date:
            raise SystemExit("--all-day requires --date YYYY-MM-DD")
        d = date.fromisoformat(args.date)
        body["start"] = {"date": d.isoformat()}
        body["end"] = {"date": (d + timedelta(days=1)).isoformat()}
        anchor = datetime.fromisoformat(args.date + "T00:00:00+00:00")
    else:
        if not (args.start and args.end):
            raise SystemExit("timed event requires --start and --end (ISO 8601 with offset)")
        body["start"] = {"dateTime": args.start}
        body["end"] = {"dateTime": args.end}
        anchor = datetime.fromisoformat(args.start.replace("Z", "+00:00"))

    if args.dedup_window_days > 0:
        dup = _duplicate_exists(svc, calendar_id, summary, anchor, args.dedup_window_days)
        if dup:
            print(json.dumps({"status": "duplicate", "id": dup.get("id"), "htmlLink": dup.get("htmlLink")}))
            return

    # NOTE: no `attendees` key is ever set — attendee-free by hard rule.
    created = svc.events().insert(calendarId=calendar_id, body=body).execute()
    print(json.dumps({"status": "created", "id": created.get("id"),
                      "htmlLink": created.get("htmlLink"), "calendar": calendar_id}))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-calendars")
    s = sub.add_parser("ensure-calendar")
    s.add_argument("--name", required=True)
    s.add_argument("--timezone", default="UTC")
    s = sub.add_parser("create")
    s.add_argument("--summary", required=True)
    s.add_argument("--all-day", action="store_true")
    s.add_argument("--date")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--reminders", help='comma minutes-before, e.g. "4320,0"')
    s.add_argument("--calendar")
    s.add_argument("--description")
    s.add_argument("--dedup-window-days", type=int, default=2)
    args = p.parse_args()

    svc = service()
    {
        "list-calendars": cmd_list_calendars,
        "ensure-calendar": cmd_ensure_calendar,
        "create": cmd_create,
    }[args.cmd](svc, args)


if __name__ == "__main__":
    main()
