#!/usr/bin/env python3
"""Read-only, generic public-ICS-feed sensor.

Works with any published/public read-only .ics URL (Outlook, Google Calendar
"secret address", Fastmail, etc). Fetches the feed, fingerprints relevant
future/recent events, diffs against the last snapshot, and appends one signal
per run when events are added, updated, cancelled, or removed. No auth, no
calendar writes — this sensor only reads.

Configure via env var (required, no personal default is shipped):
  SIGNAL_TRIAGE_ICS_URL   the public .ics feed URL to poll
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _sensorlib import STATE_DIR, atomic_write_json, read_json, utc_iso, write_signal

SOURCE = "ics"
STATE_PATH = STATE_DIR / "ics-state.json"
FETCH_FAILURE_PATH = STATE_DIR / "ics-fetch-failure.json"
TRANSIENT_FETCH_ALERT_EVERY = 6
# Keep the worst-case fetch retry budget comfortably below typical no-agent
# cron script timeouts so transient stalls are handled by
# handle_fetch_failure() instead of surfacing as scheduler timeouts.
FETCH_TIMEOUT_SECONDS = 20
FETCH_ATTEMPTS = 3
ICS_URL = os.environ.get("SIGNAL_TRIAGE_ICS_URL", "")


DATE_RE = re.compile(r"^(?P<date>\d{8})(?:T(?P<time>\d{6})(?P<utc>Z)?)?$")


def unfold_ics(text: str) -> list[str]:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def split_name_value(line: str) -> tuple[str, str, str]:
    name_params, _, value = line.partition(":")
    name, _, params = name_params.partition(";")
    return name.upper(), params, value


def parse_params(params: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in params.split(";"):
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.upper()] = value.strip('"')
    return result


def unescape(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def parse_ics_datetime(value: str, params: str = "") -> datetime | None:
    match = DATE_RE.match(value.strip())
    if not match:
        return None
    date_part = match.group("date")
    time_part = match.group("time")
    is_utc = bool(match.group("utc"))
    year = int(date_part[0:4])
    month = int(date_part[4:6])
    day = int(date_part[6:8])
    if not time_part:
        return datetime(year, month, day, tzinfo=timezone.utc)
    hour = int(time_part[0:2])
    minute = int(time_part[2:4])
    second = int(time_part[4:6])
    # Public ICS data is sufficient for change detection and human summary even
    # when TZID is present. Use UTC for comparisons if no explicit Z is present.
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc if is_utc else timezone.utc)


def display_ics_datetime(value: str, params: str = "") -> str:
    parsed = parse_ics_datetime(value, params)
    if not parsed:
        return value or "unknown time"
    tzid = parse_params(params).get("TZID")
    suffix = f" {tzid}" if tzid else " UTC"
    if "T" not in value:
        return parsed.date().isoformat()
    return parsed.strftime("%Y-%m-%dT%H:%M") + suffix


def organizer_email(value: str) -> str:
    if not value:
        return "unknown"
    if value.lower().startswith("mailto:"):
        return value[7:]
    return value


def fetch_ics() -> tuple[str, dict[str, str]]:
    if not ICS_URL:
        raise RuntimeError("SIGNAL_TRIAGE_ICS_URL is not set")
    req = Request(
        ICS_URL,
        headers={
            "User-Agent": "signal-triage-notify-ics-sensor/1.0",
            "Accept": "text/calendar,text/plain,*/*",
        },
    )
    last_error = "unknown error"
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", "replace")
                headers = {key.lower(): value for key, value in response.headers.items()}
                FETCH_FAILURE_PATH.unlink(missing_ok=True)
                return body, headers
        except HTTPError as exc:
            # HTTP auth/not-found failures are not transient network blips. Handle
            # them without a Python traceback so no-agent cron reports stay readable.
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(getattr(exc, "reason", exc))
            if attempt < FETCH_ATTEMPTS:
                time.sleep(2 * attempt)
    raise ConnectionError(last_error)


def handle_fetch_failure(message: str, *, permanent: bool = False) -> None:
    now = datetime.now(timezone.utc)
    previous = read_json(FETCH_FAILURE_PATH, {})
    count = int(previous.get("count", 0)) + 1 if isinstance(previous, dict) else 1
    first_seen = previous.get("first_seen") if isinstance(previous, dict) else None
    atomic_write_json(
        FETCH_FAILURE_PATH,
        {
            "count": count,
            "first_seen": first_seen or utc_iso(now),
            "last_seen": utc_iso(now),
            "message": message,
            "permanent": permanent,
        },
        mode=0o600,
    )
    if permanent or count == TRANSIENT_FETCH_ALERT_EVERY or (
        count > TRANSIENT_FETCH_ALERT_EVERY and count % TRANSIENT_FETCH_ALERT_EVERY == 0
    ):
        kind = "permanent" if permanent else f"transient ({count} consecutive polls)"
        print(f"ICS feed fetch failed: {kind}: {message}")


def parse_events(text: str) -> dict[str, dict[str, Any]]:
    lines = unfold_ics(text)
    events: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None

    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current:
                uid = current.get("UID") or current.get("RECURRENCE-ID") or hashlib.sha256(
                    json.dumps(current, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                current["uid"] = uid
                events[uid] = current
            current = None
            continue
        if current is None:
            continue
        name, params, value = split_name_value(line)
        if not name:
            continue
        value = unescape(value)
        if name in {"SUMMARY", "STATUS", "UID", "LOCATION", "DESCRIPTION", "SEQUENCE", "LAST-MODIFIED", "DTSTAMP"}:
            current[name] = value
        elif name in {"DTSTART", "DTEND"}:
            current[name] = value
            current[f"{name}_PARAMS"] = params
        elif name == "ORGANIZER":
            current["ORGANIZER"] = organizer_email(value)
    return events


def event_end(event: dict[str, Any]) -> datetime | None:
    return parse_ics_datetime(event.get("DTEND", ""), event.get("DTEND_PARAMS", ""))


def relevant(event: dict[str, Any], now: datetime) -> bool:
    end = event_end(event)
    if end and end < now - timedelta(hours=24):
        return False
    return True


def fingerprint(event: dict[str, Any]) -> str:
    keep = {
        key: event.get(key)
        for key in [
            "UID",
            "SUMMARY",
            "STATUS",
            "DTSTART",
            "DTSTART_PARAMS",
            "DTEND",
            "DTEND_PARAMS",
            "LOCATION",
            "ORGANIZER",
            "SEQUENCE",
        ]
        if key in event
    }
    payload = json.dumps(keep, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summarize(status: str, event: dict[str, Any]) -> str:
    summary = event.get("SUMMARY") or "(no title)"
    start = display_ics_datetime(event.get("DTSTART", ""), event.get("DTSTART_PARAMS", ""))
    organizer = event.get("ORGANIZER") or "unknown"
    uid = event.get("UID") or event.get("uid") or "unknown"
    return f"{status}: {summary} @ {start} (organizer: {organizer}; ics_uid={uid})"


def build_snapshot(events: dict[str, dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for uid, event in events.items():
        if not relevant(event, now):
            continue
        snapshot[uid] = {
            "fingerprint": fingerprint(event),
            "summary": event.get("SUMMARY") or "(no title)",
            "start": display_ics_datetime(event.get("DTSTART", ""), event.get("DTSTART_PARAMS", "")),
            "organizer": event.get("ORGANIZER") or "unknown",
            "status": event.get("STATUS") or "CONFIRMED",
        }
    return snapshot


def main() -> None:
    try:
        body, headers = fetch_ics()
    except RuntimeError as exc:
        handle_fetch_failure(str(exc), permanent=True)
        return
    except ConnectionError as exc:
        handle_fetch_failure(str(exc), permanent=False)
        return
    events = parse_events(body)
    now = datetime.now(timezone.utc)
    previous = read_json(STATE_PATH, None)
    snapshot = build_snapshot(events, now)

    metadata = {
        "source_url": ICS_URL,
        "fetched_at": utc_iso(now),
        "etag": headers.get("etag"),
        "last_modified": headers.get("last-modified"),
        "event_count": len(snapshot),
    }

    if previous is None:
        atomic_write_json(STATE_PATH, {"metadata": metadata, "events": snapshot}, mode=0o600)
        return

    old_events: dict[str, dict[str, Any]] = previous.get("events", {}) if isinstance(previous, dict) else {}
    lines: list[str] = []

    for uid, item in snapshot.items():
        old = old_events.get(uid)
        event = events.get(uid, {})
        event_status = (event.get("STATUS") or "CONFIRMED").lower()
        if old is None:
            lines.append(summarize(event_status if event_status != "confirmed" else "added", event))
        elif old.get("fingerprint") != item.get("fingerprint"):
            lines.append(summarize(event_status if event_status == "cancelled" else "updated", event))

    for uid, old in old_events.items():
        if uid not in snapshot:
            lines.append(
                f"removed: {old.get('summary', '(no title)')} @ {old.get('start', 'unknown time')} "
                f"(organizer: {old.get('organizer', 'unknown')}; ics_uid={uid})"
            )

    if lines:
        detected_at = utc_iso(now)
        write_signal(
            SOURCE,
            {
                "source": SOURCE,
                "detected_at": detected_at,
                "change_count": len(lines),
            },
            lines,
        )

    atomic_write_json(STATE_PATH, {"metadata": metadata, "events": snapshot}, mode=0o600)


if __name__ == "__main__":
    main()
