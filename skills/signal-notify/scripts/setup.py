#!/usr/bin/env python3
"""signal-triage setup — idempotent first-run bootstrap for the signal-triage-notify tap.

Run via the bundled setup.sh (or the legacy install-sensors.sh alias), or point
a SKILL.md Procedure at:
  ${HERMES_SKILL_DIR}/scripts/setup.sh [--dry-run]

What it does, in order (safe to re-run — matched by name/existence):
  1. Portable-copies these scripts into ~/.hermes/scripts/signal-triage-notify/.
     Hermes only executes no-agent cron scripts from ~/.hermes/scripts/, so this
     step is required even when the skill itself was installed elsewhere via
     `hermes skills install`. Uses Python's shutil for a portable copy,
     avoiding sync tools unavailable under Hermes' Git Bash on native Windows.
  2. Builds the runtime .venv there via `uv`, letting uv resolve the
     bin/ vs Scripts/ interpreter path — never hardcoded.
  3. Resolves signals/state/policy paths (see resolve_paths() in _sensorlib.py)
     and seeds a default policy.yaml if absent.
  4. Idempotently syncs `hermes cron` jobs from registry.yaml — sensors are
     created enabled; signal-triage/signal-notify are created DISABLED so you
     review output before it becomes live.
  5. Prints OAuth (Gmail) / `gh` CLI credential setup steps and a verify
     command that fails loudly if something required is still missing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_SCRIPTS_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path.home() / ".hermes" / "scripts" / "signal-triage-notify"


def log(msg: str) -> None:
    print(msg)


def copy_scripts(dry_run: bool) -> Path:
    log(f"==> copy scripts -> {RUNTIME_DIR} (portable shutil copy)")
    if SKILL_SCRIPTS_DIR == RUNTIME_DIR:
        log("    (already running from the runtime dir, skip)")
        return RUNTIME_DIR
    if dry_run:
        log(f"    (dry-run: would copy {SKILL_SCRIPTS_DIR} -> {RUNTIME_DIR}, excluding .venv/__pycache__)")
        return RUNTIME_DIR
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SKILL_SCRIPTS_DIR,
        RUNTIME_DIR,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "__pycache__"),
    )
    for sh in RUNTIME_DIR.glob("*.sh"):
        sh.chmod(0o755)
    return RUNTIME_DIR


def ensure_venv(runtime_dir: Path, dry_run: bool) -> None:
    venv_dir = runtime_dir / ".venv"
    log(f"==> venv: {venv_dir}")
    if venv_dir.exists():
        log("    (exists, skip)")
        return
    if dry_run:
        log("    (dry-run: would run 'uv venv .venv' + 'uv pip install --python .venv -r pyproject.toml')")
        return
    subprocess.run(["uv", "venv", ".venv"], check=True, cwd=runtime_dir)
    subprocess.run(["uv", "pip", "install", "--python", ".venv", "-r", "pyproject.toml"], check=True, cwd=runtime_dir)


def seed_policy(runtime_dir: Path, dry_run: bool) -> dict:
    log("==> resolving paths (platformdirs default unless overridden)")
    if dry_run:
        log("    (dry-run: would call resolve_paths() to create dirs + seed policy.yaml)")
        return {}
    sys.path.insert(0, str(runtime_dir))
    from _sensorlib import resolve_paths  # noqa: E402

    paths = resolve_paths()
    for key, value in paths.items():
        log(f"    {key}: {value}")
    return paths


def existing_cron_names() -> set:
    try:
        out = subprocess.run(["hermes", "cron", "list", "--json"], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return set()
        jobs = json.loads(out.stdout).get("jobs", [])
        return {j.get("name") for j in jobs if j.get("name")}
    except Exception:
        return set()


def sync_cron(runtime_dir: Path, dry_run: bool) -> None:
    log("==> syncing hermes cron jobs from registry.yaml")
    import yaml

    reg = yaml.safe_load((runtime_dir / "registry.yaml").read_text(encoding="utf-8"))
    existing = existing_cron_names()

    for job in reg.get("sensors", []):
        name = job["name"]
        if name in existing:
            log(f"    = {name} (exists, skip)")
            continue
        script = f"signal-triage-notify/{Path(job['script']).name}"
        log(f"    + {name}  no-agent script={script}  [{job['cadence']}]  enabled")
        if not dry_run:
            subprocess.run(
                ["hermes", "cron", "create", job["cadence"], "--no-agent", "--script", script,
                 "--name", name, "--deliver", job.get("deliver", "local")],
                check=True,
            )

    for job in reg.get("agents", []):
        name = job["name"]
        if name in existing:
            log(f"    = {name} (exists, skip)")
            continue
        prompt = (job.get("prompt") or "").strip()
        log(f"    + {name}  skill={job['skill']}  [{job['cadence']}]  created DISABLED")
        if not dry_run:
            subprocess.run(
                ["hermes", "cron", "create", job["cadence"], prompt, "--skill", job["skill"],
                 "--name", name, "--deliver", job.get("deliver", "local")],
                check=True,
            )
            # Best-effort: pause by name. Verify this subcommand shape against
            # your installed `hermes` CLI — see docs/writing-a-sensor.md.
            subprocess.run(["hermes", "cron", "pause", "--name", name], check=False)


def print_credential_steps(paths: dict) -> None:
    state_dir = paths.get("state_dir", "<state_dir — run without --dry-run to resolve>")
    log(
        f"""
==> credential setup (complete before enabling gmail/github/ics sensors)
  Gmail (read-only history poll):
    1. Create a Desktop OAuth client in Google Cloud Console; enable the Gmail API.
    2. On a machine with a browser (NOT this host):
         uv run python bootstrap_oauth.py
       This writes token.json in the current directory.
    3. Copy it to this host's resolved state dir as token.json, mode 600:
         {state_dir}/sensors/token.json
  GitHub sensor:
    - Requires the `gh` CLI already authenticated (`gh auth status`).
  ICS sensor:
    - Requires SIGNAL_TRIAGE_ICS_URL pointing at a public/secret .ics feed URL.
  Calendar writes (signal-notify):
    - Requires a full-scope Google token — see docs/writing-a-sensor.md.

==> verify (fails loudly if something required is missing)
  uv run python -c "import _sensorlib; _sensorlib.resolve_paths(); print('paths OK')"
  gh auth status || echo 'MISSING: gh auth login'
  test -f {state_dir}/sensors/token.json || echo 'MISSING: Gmail token.json — see steps above'
  test -n "$SIGNAL_TRIAGE_ICS_URL" || echo 'MISSING: SIGNAL_TRIAGE_ICS_URL (only needed for the ICS sensor)'
"""
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print planned actions, change nothing")
    args = parser.parse_args()

    log("signal-triage setup" + (" (dry-run)" if args.dry_run else ""))
    runtime_dir = copy_scripts(args.dry_run)
    ensure_venv(runtime_dir, args.dry_run)
    paths = seed_policy(runtime_dir, args.dry_run)
    sync_cron(runtime_dir, args.dry_run)
    print_credential_steps(paths)
    log("dry-run complete; nothing was changed." if args.dry_run else "done.")


if __name__ == "__main__":
    main()
