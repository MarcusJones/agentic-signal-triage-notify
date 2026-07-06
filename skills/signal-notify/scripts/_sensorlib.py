"""Shared library for signal-triage-notify sensors (Layer 1).

Sensors are cheap, no-LLM cron scripts that poll a channel and APPEND
raw-but-sufficient context to the daily signal log. See docs/writing-a-sensor.md
for the entry contract: every line must carry enough context for the hourly
triage LLM to judge importance WITHOUT another network call, OR a
machine-usable `handle:` so the agent can fetch detail on demand. Sensors
NEVER alert; they only write to the log.

Vault-optional, zero-config by default: `resolve_paths()` below picks a safe
OS-correct location for everything (signals, state, policy) with no Obsidian
vault or manual configuration required. See its docstring for the precedence
order.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile

try:  # POSIX file locking; Windows gets a best-effort no-op (sensors are
    import fcntl  # single-writer per file in practice; the lock is belt+braces)
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from platformdirs import user_config_dir, user_data_dir, user_state_dir

# google-auth is imported lazily inside load_creds(): only the OAuth-backed
# sensors need it, and the Layer-1.5 prefilters / tests must import this
# module without the Google stack installed.

APP_NAME = "signal-triage"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]


# ---------------------------------------------------------------------------
# filesystem (atomic, crash-consistent, 0600/0700)
# ---------------------------------------------------------------------------
def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


# ---------------------------------------------------------------------------
# path resolution — vault-optional, XDG-default (FR-13a)
# ---------------------------------------------------------------------------
DEFAULT_POLICY_YAML = """\
# signal-triage-notify policy — safe, conservative starting point.
# Edit freely; signal-triage and signal-notify read this on every run.

waking_hours:
  start: "07:00"
  end: "22:00"
  timezone: "UTC"   # IANA name, e.g. "America/New_York"; UTC is a safe default

routing:
  URGENT: { telegram: true }
  EVENT: { calendar: true }
  ACTION: { telegram: false }
  WATCH: { telegram: false }
  FYI: { telegram: false }
  NOISE: { telegram: false }

watch_terms:
  - security
  - password
  - 2fa

noise_hints:
  - unsubscribe
  - newsletter
  - marketing

calendar:
  default: ""   # empty = notify's default calendar; set to a calendar id to use a dedicated one
  by_type: {}
  reminder_profiles:
    default: "1440,0"   # minutes before: 1 day + day-of

# Layer 1.5 prefilter (triage_prefilter.py). Content-blind batching: the LLM
# wakes only when the OLDEST unjudged item has waited debounce_minutes.
# 30 = responsive default · 90 = cost-optimal · 0 = flush on any new item.
triage:
  debounce_minutes: 30
  max_items: 120   # per-flush cap; truncation is always announced, never silent
"""


def _load_hermes_skill_config(skill_name: str = "signal-triage") -> dict:
    """Tier 1 of resolve_paths(): metadata.hermes.config values Hermes has
    persisted into the user's config.yaml for this skill (non-secret settings
    declared in SKILL.md frontmatter). Returns {} if none configured.
    """
    config_path = Path(os.environ.get("HERMES_CONFIG_PATH", Path.home() / ".hermes" / "config.yaml"))
    if not config_path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return ((data.get("skills") or {}).get(skill_name) or {}).get("config") or {}


_PATHS_CACHE: dict[str, Path] | None = None


def resolve_paths(*, force: bool = False) -> dict[str, Path]:
    """Resolve signals_dir/state_dir/policy_path. Precedence, highest first:

      1. explicit `metadata.hermes.config` value (signals_dir, state_dir, policy_path)
      2. env var (SIGNAL_TRIAGE_SIGNALS_DIR, SIGNAL_TRIAGE_STATE_DIR, SIGNAL_TRIAGE_POLICY)
      3. OS-correct default via `platformdirs` — XDG on Linux, `~/Library/Application
         Support` on macOS, `%LOCALAPPDATA%` on Windows.

    No Obsidian vault, and no config file, is required: a fresh install resolves
    to tier 3 and just works. Obsidian/Logseq/Syncthing users who want signals
    inside a synced vault set one key — `signals_dir: ~/YourVault/signals`.

    Creates missing directories and seeds a default policy.yaml on first run.
    Cached after the first call; pass force=True to re-resolve (mainly for tests).
    """
    global _PATHS_CACHE
    if _PATHS_CACHE is not None and not force:
        return _PATHS_CACHE

    cfg = _load_hermes_skill_config()

    signals_dir = Path(
        cfg.get("signals_dir")
        or os.environ.get("SIGNAL_TRIAGE_SIGNALS_DIR")
        or (Path(user_data_dir(APP_NAME)) / "signals")
    ).expanduser()
    state_dir = Path(
        cfg.get("state_dir") or os.environ.get("SIGNAL_TRIAGE_STATE_DIR") or user_state_dir(APP_NAME)
    ).expanduser()
    policy_path = Path(
        cfg.get("policy_path")
        or os.environ.get("SIGNAL_TRIAGE_POLICY")
        or (Path(user_config_dir(APP_NAME)) / "policy.yaml")
    ).expanduser()

    daily_dir = signals_dir / "daily"
    triage_dir = signals_dir / "triage"
    sensor_state_dir = state_dir / "sensors"
    triage_state_dir = state_dir / "triage"

    for d in (daily_dir, triage_dir, sensor_state_dir, triage_state_dir, policy_path.parent):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)

    if not policy_path.exists():
        atomic_write_text(policy_path, DEFAULT_POLICY_YAML, mode=0o600)

    _PATHS_CACHE = {
        "signals_dir": signals_dir,
        "daily_dir": daily_dir,
        "triage_dir": triage_dir,
        "state_dir": state_dir,
        "sensor_state_dir": sensor_state_dir,
        "triage_state_dir": triage_state_dir,
        "policy_path": policy_path,
    }
    return _PATHS_CACHE


_PATHS = resolve_paths()
STATE_DIR = _PATHS["sensor_state_dir"]
SIGNAL_DIR = _PATHS["signals_dir"]
SIGNAL_LOG_DIR = _PATHS["daily_dir"]
TOKEN_PATH = STATE_DIR / "token.json"
LOCAL_TZ = ZoneInfo(os.environ.get("HERMES_SENSOR_LOCAL_TZ", "UTC"))


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    value = dt or utc_now()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_detected_at(value: Any) -> datetime:
    raw = str(value or utc_iso())
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
    except ValueError:
        return utc_now().astimezone(LOCAL_TZ)


def delta_state_path(sensor: str, name: str = "state.json") -> Path:
    """Per-sensor delta cursor location: <state_dir>/sensors/<sensor>/<name>."""
    return STATE_DIR / sensor / name


def load_delta(sensor: str, name: str = "state.json", default: Any = None) -> Any:
    return read_json(delta_state_path(sensor, name), {} if default is None else default)


def save_delta(sensor: str, data: Any, name: str = "state.json") -> None:
    atomic_write_json(delta_state_path(sensor, name), data, mode=0o600)


WINDOWS_RESERVED_CHARS = re.compile(r'[<>:"/\\|?*]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def windows_safe_filename_part(value: str) -> str:
    safe = WINDOWS_RESERVED_CHARS.sub("-", value).strip(" .")
    if safe.upper().split(".", 1)[0] in WINDOWS_RESERVED_NAMES:
        safe = f"_{safe}"
    return safe or "signal"


# ---------------------------------------------------------------------------
# Google OAuth (read-only sensor token)
# ---------------------------------------------------------------------------
def load_creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    ensure_private_dir(STATE_DIR)
    if not TOKEN_PATH.exists():
        raise SystemExit(
            "Missing OAuth token. Bootstrap on a machine with a browser (see "
            f"bootstrap_oauth.py), then copy token.json to {TOKEN_PATH} and run: "
            f"chmod 600 {TOKEN_PATH}"
        )
    os.chmod(TOKEN_PATH, 0o600)
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            atomic_write_text(TOKEN_PATH, creds.to_json() + "\n", mode=0o600)
        else:
            raise SystemExit("OAuth token is invalid and cannot be refreshed; re-run bootstrap_oauth.py.")
    return creds


# ---------------------------------------------------------------------------
# importance hint (cheap, deterministic). Triage (Layer 2) makes the real call;
# this only flags items into Highlights so the raw log stays scannable.
# ---------------------------------------------------------------------------
IMPORTANT_TERMS = re.compile(
    r"\b(urgent|action required|deadline|due today|overdue|invoice|payment|contract|offer|"
    r"interview|cancelled|canceled|rescheduled|security|password|2fa|mfa|alert|failure|failed)\b",
    re.IGNORECASE,
)


def importance_for(source: str, line: str) -> str:
    if IMPORTANT_TERMS.search(line):
        return "important"
    if source == "ics" and re.search(r"\b(cancelled|canceled|rescheduled|updated|removed)\b", line, re.I):
        return "important"
    return "normal"


# ---------------------------------------------------------------------------
# daily log: scaffold + locked append
# ---------------------------------------------------------------------------
CATEGORY_HEADINGS = {
    "Email": "## Email",
    "Calendar": "## Calendar",
    "GitHub": "## GitHub",
    "Context": "## Context",
    "Other": "## Other sources",
}
LOG_SECTION_ORDER = ["Highlights", "Email", "Calendar", "GitHub", "Context", "Other sources"]

SOURCE_CATEGORY = {
    "gmail": "Email",
    "ics": "Calendar",
    "github": "GitHub",
    "weather": "Context",
}

SOURCE_LABEL = {
    "gmail": "Gmail",
    "ics": "Calendar (ICS feed)",
    "github": "GitHub",
    "weather": "Weather",
}


def signal_section(source: str, category: str | None = None) -> str:
    cat = category or SOURCE_CATEGORY.get(source, "Other")
    return CATEGORY_HEADINGS.get(cat, CATEGORY_HEADINGS["Other"])


def source_label(source: str) -> str:
    return SOURCE_LABEL.get(source, source)


def ensure_log_scaffold(path: Path, date: str) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    parts = [
        f"# Signal Log - {date}\n",
        "A running situational-awareness log for email, calendar, and other "
        "low-level external signals. Entries are compact summaries with enough source "
        "metadata for the hourly triage pass to judge them (or a `handle:` to fetch detail).\n",
        "## Highlights\n",
        "Deterministic keyword hits are mirrored here; the triage layer does the real judging.\n",
    ]
    for sec in LOG_SECTION_ORDER:
        if sec in ("Highlights",):
            continue
        head = "## Other sources" if sec == "Other sources" else f"## {sec}"
        parts.append(head + "\n")
    return "\n".join(parts) + "\n"


def insert_under_heading(text: str, heading: str, entry: str) -> str:
    if heading not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{heading}\n"
    start = text.index(heading) + len(heading)
    next_heading = re.search(r"\n## ", text[start:])
    insert_at = start + next_heading.start() if next_heading else len(text)
    prefix = text[:insert_at].rstrip()
    suffix = text[insert_at:].lstrip("\n")
    updated = prefix + "\n\n" + entry.rstrip() + "\n"
    if suffix:
        updated += "\n" + suffix
    return updated


@contextmanager
def file_lock(lock_path: Path):
    ensure_private_dir(lock_path.parent)
    with lock_path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _format_handle(handles: dict[str, Any] | None) -> str:
    if not handles:
        return ""
    kv = " ".join(f"{k}={v}" for k, v in handles.items() if v not in (None, ""))
    return f" (handle: {kv})" if kv else ""


def append_signal(
    source: str,
    lines: list[str],
    *,
    category: str | None = None,
    handles: dict[str, Any] | None = None,
    detected_at: str | None = None,
    verbs: list[str] | None = None,
) -> Path:
    """Preferred Layer-1 API. Append entries under the right category heading.

    Each entry:
      - HH:MM [<label>] <line><handle> _(detected_at=<iso>; sensor=<source>)_
    Keyword-matched entries stay valid Markdown bullets and are prefixed with
    IMPORTANT:
      - IMPORTANT HH:MM [<label>] <line><handle> _(detected_at=<iso>; sensor=<source>)_
    Important entries are also mirrored into ## Highlights (hint only).
    """
    detected = detected_at or utc_iso()
    return _append(source, lines, category=category, handles=handles, detected_at=detected, verbs=verbs)


def _append(source, lines, *, category, handles, detected_at, verbs) -> Path:
    ensure_private_dir(SIGNAL_LOG_DIR)
    detected_local = parse_detected_at(detected_at)
    date = detected_local.date().isoformat()
    time = detected_local.strftime("%H:%M")
    path = SIGNAL_LOG_DIR / f"{date}.md"
    lock_path = SIGNAL_LOG_DIR / ".signal-log.lock"
    label = source_label(source)
    section = signal_section(source, category)
    handle_str = _format_handle(handles)
    meta = f" _(detected_at={detected_at}; sensor={source})_"

    entries: list[str] = []
    highlights: list[str] = []
    for i, line in enumerate(lines):
        compact = " ".join(str(line).split())
        if not compact:
            continue
        verb = (verbs[i] + ": ") if verbs and i < len(verbs) else ""
        importance = importance_for(source, compact)
        priority = "IMPORTANT " if importance == "important" else ""
        entry = f"- {priority}{time} [{label}] {verb}{compact}{handle_str}{meta}"
        entries.append(entry)
        if importance == "important":
            highlights.append(entry)

    if not entries:
        return path

    with file_lock(lock_path):
        text = ensure_log_scaffold(path, date)
        if highlights:
            text = insert_under_heading(text, "## Highlights", "\n".join(highlights))
        text = insert_under_heading(text, section, "\n".join(entries))
        atomic_write_text(path, text, mode=0o600)
    return path


def write_signal(source: str, frontmatter: dict[str, Any], body_lines: list[str]) -> Path:
    """Back-compat wrapper for the frontmatter+body call shape."""
    return append_signal(
        source,
        body_lines,
        category=SOURCE_CATEGORY.get(source),
        handles=None,
        detected_at=frontmatter.get("detected_at"),
    )


# ---------------------------------------------------------------------------
# Layer 1.5 support: policy loading + stable source ids + entry parsing.
# Single source of truth — the triage SKILL.md conventions table documents
# THIS function; the prefilter and the agent's manual fallback both rely on it.
# ---------------------------------------------------------------------------
def load_policy() -> dict:
    """Parse policy.yaml (seeded on first run). Returns {} on any failure."""
    try:
        import yaml

        return yaml.safe_load(resolve_paths()["policy_path"].read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def line_hash(line: str) -> str:
    """sha1[:12] of the trimmed raw entry line INCLUDING its leading list
    marker. Hashing only the post-marker body creates a different id and
    re-surfaces duplicates — do not change this without a ledger migration."""
    return hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:12]


_SID_PATTERNS = [
    # (regex, id_template) — first match wins. Order matters: specific
    # handles before generic fallbacks.
    (re.compile(r"gmail_message_id=([A-Za-z0-9_-]+)"), "gmail:{0}"),
    (re.compile(r"ics_uid=([^\s;)]+)"), "ics:{0}"),
    (re.compile(r"\bgcal_event_id=([A-Za-z0-9_-]+)"), "gcal:{0}"),
    (re.compile(r"\[GitHub\].*?\b([\w.-]+/[\w.-]+#\d+)"), "github:{0}"),
]


def source_id(line: str) -> str:
    """Derive the stable idempotency id for one raw daily-log entry line.

    Prefers the entry's machine handle; falls back to `line:<sha1[:12]>` of
    the whole trimmed line (safe, but re-surfaces if the line is ever
    reformatted — sensors SHOULD emit a stable handle, see
    docs/writing-a-sensor.md).
    """
    s = line.strip()
    for pattern, template in _SID_PATTERNS:
        m = pattern.search(s)
        if m:
            return template.format(m.group(1))
    return f"line:{line_hash(s)}"


_DETECTED_AT_RE = re.compile(
    r"detected_at=(\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)"
)


def entry_detected_at(line: str) -> datetime | None:
    """Parse the entry's own detected_at stamp (tz-aware); None if absent or
    malformed. Callers treat None as 'old enough' (fail-open to waking)."""
    m = _DETECTED_AT_RE.search(line)
    if not m:
        return None
    raw = m.group(1).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iter_entry_lines(path: Path):
    """Yield trimmed raw entry lines (`- ...` bullets; legacy `! ` markers)
    from a daily log file. Missing file yields nothing."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    for raw in text.splitlines():
        t = raw.strip()
        if (t.startswith("- ") or t.startswith("! ")) and len(t) > 2:
            yield t
