#!/usr/bin/env python3
"""Browser-machine-only OAuth bootstrap for the Gmail delta sensor.

Run this on a machine with a browser, not on a headless server host.
"""

from __future__ import annotations

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from _sensorlib import SCOPES


def main() -> None:
    cwd = Path.cwd()
    credentials_path = cwd / "credentials.json"
    token_path = cwd / "token.json"

    if not credentials_path.exists():
        raise SystemExit(
            "Missing credentials.json in the current directory.\n\n"
            "Create a Desktop OAuth client in Google Cloud, enable the Gmail API, "
            "download the client JSON, and save it here as credentials.json."
        )

    print("Starting browser OAuth flow with InstalledAppFlow.run_local_server(port=0).")
    print("Scopes:")
    for scope in SCOPES:
        print(f"  - {scope}")

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json() + "\n", encoding="utf-8")
    token_path.chmod(0o600)

    print(f"\nWrote {token_path}")
    print("\nNow copy it to the host's resolved state directory as token.json, mode 600.")
    print("Run `uv run python setup.py` first if you haven't, to see the resolved path — e.g.:")
    print("  scp token.json <host>:<state_dir>/sensors/token.json")
    print("  ssh <host> 'chmod 600 <state_dir>/sensors/token.json'")
    print("\nDo not run this browser OAuth flow on a headless host.")


if __name__ == "__main__":
    main()
