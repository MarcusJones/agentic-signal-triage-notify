#!/usr/bin/env python3
"""Gmail native history-delta sensor.

Read-only: uses this tap's own OAuth token (gmail.readonly scope, see
bootstrap_oauth.py) to poll Gmail's history API and append new messages and
label changes to the daily signal log. Never writes to the mailbox.
"""

from __future__ import annotations

import html
import re
from typing import Any

from _sensorlib import STATE_DIR, atomic_write_text, load_creds, read_text, utc_iso, write_signal
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HISTORY_ID_PATH = STATE_DIR / "gmail-history-id"
SPAM_TRASH = {"SPAM", "TRASH"}
HISTORY_TYPES = ["messageAdded", "labelAdded", "labelRemoved"]


def clean(value: str | None, limit: int | None = None) -> str:
    text = html.unescape(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"[\u200b-\u200f\ufeff\u00ad]", "", text)
    text = " ".join(text.split())
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def header(headers: list[dict[str, str]], name: str) -> str:
    wanted = name.lower()
    for item in headers:
        if item.get("name", "").lower() == wanted:
            return item.get("value", "")
    return ""


def load_labels(service: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    response = service.users().labels().list(userId="me").execute()
    for item in response.get("labels", []):
        label_id = item.get("id")
        if label_id:
            labels[label_id] = item.get("name") or label_id
    return labels


def message_details(service: Any, msg_id: str, cache: dict[str, dict[str, str]]) -> dict[str, str]:
    if msg_id in cache:
        return cache[msg_id]
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "To", "Subject", "Date"],
    ).execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    details = {
        "id": msg_id,
        "thread_id": clean(msg.get("threadId")),
        "from": clean(header(headers, "From")),
        "to": clean(header(headers, "To")),
        "subject": clean(header(headers, "Subject")) or "(no subject)",
        "date": clean(header(headers, "Date")),
        "snippet": clean(msg.get("snippet"), limit=200),
    }
    cache[msg_id] = details
    return details


def iter_history(service: Any, start_history_id: str) -> tuple[list[dict[str, Any]], str | None]:
    records: list[dict[str, Any]] = []
    page_token = None
    latest_history_id = None
    while True:
        request = service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=HISTORY_TYPES,
            pageToken=page_token,
        )
        response = request.execute()
        latest_history_id = response.get("historyId") or latest_history_id
        records.extend(response.get("history", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return records, latest_history_id


def collect_changes(service: Any, records: list[dict[str, Any]], labels: dict[str, str]) -> list[str]:
    msg_cache: dict[str, dict[str, str]] = {}
    lines: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for record in records:
        for item in record.get("messagesAdded", []):
            msg = item.get("message", {})
            msg_id = msg.get("id")
            if not msg_id:
                continue
            details = message_details(service, msg_id, msg_cache)
            key = ("messageAdded", msg_id, "")
            if key not in seen:
                lines.append(
                    f"messageAdded: from {details['from']} · {details['subject']} · {details['snippet']} (gmail_message_id={details['id']}; thread_id={details['thread_id']})"
                )
                seen.add(key)

        for bucket, event_name, arrow in [
            ("labelsAdded", "labelAdded", "→"),
            ("labelsRemoved", "labelRemoved", "←"),
        ]:
            for item in record.get(bucket, []):
                msg = item.get("message", {})
                msg_id = msg.get("id")
                if not msg_id:
                    continue
                details = message_details(service, msg_id, msg_cache)
                for label_id in item.get("labelIds", []):
                    if label_id in SPAM_TRASH:
                        continue
                    label_name = labels.get(label_id, label_id)
                    key = (event_name, msg_id, label_id)
                    if key in seen:
                        continue
                    lines.append(f"{event_name}: {details['subject']} · from {details['from']} {arrow} {label_name} (gmail_message_id={details['id']}; thread_id={details['thread_id']})")
                    seen.add(key)

    return lines


def seed(service: Any) -> None:
    profile = service.users().getProfile(userId="me").execute()
    history_id = profile.get("historyId")
    if not history_id:
        raise SystemExit("Gmail profile did not include historyId")
    atomic_write_text(HISTORY_ID_PATH, str(history_id) + "\n", mode=0o600)


def main() -> None:
    creds = load_creds()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    labels = load_labels(service)

    start_history_id = read_text(HISTORY_ID_PATH)
    if not start_history_id:
        seed(service)
        return

    try:
        records, latest_history_id = iter_history(service, start_history_id)
    except HttpError as exc:
        if exc.resp.status in {404, 410}:
            profile = service.users().getProfile(userId="me").execute()
            new_history_id = str(profile.get("historyId"))
            detected_at = utc_iso()
            write_signal(
                "gmail",
                {
                    "source": "gmail",
                    "detected_at": detected_at,
                    "change_count": 1,
                    "resync": True,
                },
                [f"Gmail history expired or invalid ({exc.resp.status}); reset baseline to historyId {new_history_id}."],
            )
            atomic_write_text(HISTORY_ID_PATH, new_history_id + "\n", mode=0o600)
            return
        raise

    if not latest_history_id:
        return

    lines = collect_changes(service, records, labels)
    if lines:
        detected_at = utc_iso()
        write_signal(
            "gmail",
            {
                "source": "gmail",
                "detected_at": detected_at,
                "change_count": len(lines),
            },
            lines,
        )

    atomic_write_text(HISTORY_ID_PATH, str(latest_history_id) + "\n", mode=0o600)


if __name__ == "__main__":
    main()
