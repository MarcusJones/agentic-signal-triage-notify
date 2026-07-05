#!/usr/bin/env python3
"""GitHub sensor — surfaces things that need your attention.

Uses the already-authenticated `gh` CLI (no extra token wiring). Emits:
  - review-requested PRs
  - unread notifications (mentions, review requests, CI failures)

Idempotent: keyed by notification/PR id; only new items are emitted.
"""

from __future__ import annotations

import json
import subprocess

from _sensorlib import append_signal, load_delta, save_delta


def gh_api(path: str) -> list | dict:
    out = subprocess.run(
        ["gh", "api", path, "--paginate"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        return []
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return []


def gh_search(query: str) -> list:
    out = subprocess.run(
        ["gh", "search", "prs" if "is:pr" in query else "issues", query,
         "--json", "title,url,repository,number", "--limit", "30"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        return []
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return []


def main() -> None:
    state = load_delta("github", default={})
    first_run = "seen" not in state
    seen = set(state.get("seen", []))
    new_seen: list[str] = []
    lines: list[str] = []
    verbs: list[str] = []

    # Notifications (mentions, review requests, CI). Unread only.
    notifs = gh_api("/notifications")
    if isinstance(notifs, list):
        for n in notifs:
            nid = f"notif:{n.get('id')}"
            new_seen.append(nid)
            if nid in seen or first_run:
                continue
            subj = n.get("subject", {})
            repo = n.get("repository", {}).get("full_name", "")
            reason = n.get("reason", "")
            lines.append(f"{repo}: {subj.get('type')} '{subj.get('title')}' ({reason})")
            verbs.append("notification")

    # Review-requested PRs (even if notification was already read).
    for pr in gh_search("is:pr is:open review-requested:@me"):
        key = f"review:{pr.get('repository', {}).get('nameWithOwner','')}#{pr.get('number')}"
        new_seen.append(key)
        if key in seen or first_run:
            continue
        repo = pr.get("repository", {}).get("nameWithOwner", "")
        lines.append(f"review requested: {repo}#{pr.get('number')} {pr.get('title')}")
        verbs.append("review-request")

    if lines:
        append_signal("github", lines, category="GitHub", verbs=verbs)

    merged = list(dict.fromkeys(new_seen + list(seen)))[:500]
    save_delta("github", {"seen": merged})


if __name__ == "__main__":
    main()
