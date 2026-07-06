# Writing a sensor

A Layer-1 sensor is a small, no-LLM Python script that polls one source and
appends compact entries to the daily signal log via `_sensorlib.append_signal()`.
Sensors never alert, never write a calendar, and never call an LLM — they only
observe and record.

## Contract

Every appended line must carry enough context for the hourly triage pass to
judge importance **without another network call**, or a machine-usable
`handle:` key so triage can fetch detail on demand when the line is too thin.

```python
from _sensorlib import append_signal, load_delta, save_delta

def main() -> None:
    state = load_delta("my-sensor", default={})
    # ... poll your source, diff against state ...
    new_lines = ["some new item description (handle: my_id=123)"]
    if new_lines:
        append_signal("my-sensor", new_lines, category="Context", verbs=["seen"])
    save_delta("my-sensor", {"cursor": "..."})

if __name__ == "__main__":
    main()
```

- `append_signal(source, lines, *, category=None, handles=None, detected_at=None, verbs=None)`
  writes under the right `## <Category>` heading in the daily log, mirrors
  keyword-important hits into `## Highlights`, and is safe to call
  concurrently (file-locked).
- `load_delta(sensor, default={})` / `save_delta(sensor, data)` persist your
  sensor's own cursor/state under `<state_dir>/sensors/<sensor>/state.json` —
  use this instead of inventing your own state file location.
- Add your source to `SOURCE_CATEGORY` / `SOURCE_LABEL` in `_sensorlib.py` if
  you want a dedicated `## <Category>` heading and display label; otherwise it
  falls under `## Other sources`.
- Keep the script idempotent: re-running it with no new upstream data must be
  a no-op (no duplicate log lines, no state corruption).

## Give your entries a stable handle

The Layer-1.5 prefilter derives an idempotency id per entry via
`_sensorlib.source_id()`. Entries that carry a unique machine handle
(`gmail_message_id=…`, `ics_uid=…`, or your own `mysensor_id=…` added to the
patterns in `_sensorlib._SID_PATTERNS`) get exact-once triage for free.
Entries without one fall back to `line:<sha1[:12]>` of the whole trimmed
line — safe, but the item re-surfaces if the line is ever reformatted, and
context-summary sensors that re-emit the same body each poll will be judged
once per distinct wording. If your sensor updates the same logical item over
time, a stable handle is strongly recommended.

Also: always pass `detected_at` (`append_signal` stamps it for you) — the
prefilter's batch debounce reads it; entries without a parseable stamp are
treated as "old enough" and flush immediately.

## Wrapper script

Ship a matching `.sh` launcher so Hermes' no-agent cron runner can execute it
via bash on any OS (Hermes runs shell through Git Bash on native Windows):

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec uv run python my_sensor.py
```

Using `uv run` — instead of hardcoding the venv's POSIX interpreter path —
sidesteps the `bin/` vs `Scripts/` split between POSIX and native Windows venvs.

## Register it

Add an entry to `registry.yaml` under `sensors:` with a unique `name`,
`script` (the `.sh` launcher filename), `cadence` (cron expression),
`category`, and `deliver: local`. Re-run `setup.sh` (idempotent) to sync the
new `hermes cron` job.

## Path resolution — do not hardcode paths

Never hardcode a home-relative notes/vault directory, `~/.hermes`, or any
OS-specific data directory. Always go through `_sensorlib.resolve_paths()`
(directly or via the
already-exported `SIGNAL_LOG_DIR` / `STATE_DIR` module constants), so your
sensor respects the same config → env → `platformdirs` precedence as the rest
of the tap. See the README's "Works with zero config" section and FR-13a in
the project's design notes for the full precedence order.

## OAuth-backed sensors

If your sensor needs an OAuth token (like `gmail_sensor.py`), keep the
bootstrap flow (`bootstrap_oauth.py`-style) strictly browser-machine-only —
never attempt an interactive OAuth flow on a headless host. Declare the
credential file in your skill's `required_credential_files` frontmatter so
users know it's needed before they enable the cron job.

## CLI verification

The tap layout (`SKILL.md` leaf dirs, `${HERMES_SKILL_DIR}`, `external_dirs`,
`.well-known/skills/index.json`) is confirmed by official Hermes docs. The
exact `hermes skills install <owner>/<repo>/<skill>` / tap-add / publish
subcommand spelling can drift between CLI releases — before relying on the
commands in this repo's README, run `hermes skills --help` (or `hermes
--help`) on your installed version and adjust if the verbs differ.
