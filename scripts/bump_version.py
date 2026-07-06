#!/usr/bin/env python3
"""bump_version.py — keep the tap's version strings in lockstep.

The version lives in THREE places that drift without tooling:
  - signal-triage-notify/.well-known/skills/index.json  (every skill entry)
  - skills/signal-triage/SKILL.md   frontmatter `version:`
  - skills/signal-notify/SKILL.md   frontmatter `version:`

Usage:
  scripts/bump_version.py check            # exit 1 if the three disagree (CI)
  scripts/bump_version.py set 0.2.0        # write everywhere + CHANGELOG stub

After `set`, review CHANGELOG.md, commit, then:
  git tag v<version> && git push --tags
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "signal-triage-notify" / ".well-known" / "skills" / "index.json"
SKILLS = [
    ROOT / "skills" / "signal-triage" / "SKILL.md",
    ROOT / "skills" / "signal-notify" / "SKILL.md",
]
CHANGELOG = ROOT / "CHANGELOG.md"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
FRONTMATTER_VERSION = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def collect() -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    data = json.loads(INDEX.read_text(encoding="utf-8"))
    for skill in data.get("skills", []):
        versions.setdefault(skill.get("version", "?"), []).append(f"index.json:{skill.get('name')}")
    for path in SKILLS:
        m = FRONTMATTER_VERSION.search(path.read_text(encoding="utf-8"))
        versions.setdefault(m.group(1) if m else "?", []).append(str(path.relative_to(ROOT)))
    return versions


def cmd_check() -> int:
    versions = collect()
    if len(versions) == 1:
        print(f"version OK: {next(iter(versions))} everywhere")
        return 0
    print("VERSION MISMATCH:")
    for v, where in sorted(versions.items()):
        for w in where:
            print(f"  {v:10} {w}")
    print("fix with: scripts/bump_version.py set <version>")
    return 1


def cmd_set(version: str) -> int:
    if not SEMVER.match(version):
        print(f"not a semver version: {version!r}")
        return 1

    data = json.loads(INDEX.read_text(encoding="utf-8"))
    for skill in data.get("skills", []):
        skill["version"] = version
    INDEX.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    for path in SKILLS:
        text = path.read_text(encoding="utf-8")
        path.write_text(FRONTMATTER_VERSION.sub(f"version: {version}", text, count=1), encoding="utf-8")

    today = datetime.date.today().isoformat()
    header = f"## [{version}] - {today}"
    if CHANGELOG.exists():
        text = CHANGELOG.read_text(encoding="utf-8")
        if header not in text:
            text = text.replace(
                "## [Unreleased]",
                f"## [Unreleased]\n\n{header}\n\n- <fill in>",
                1,
            )
            CHANGELOG.write_text(text, encoding="utf-8")

    print(f"set {version} in index.json + {len(SKILLS)} SKILL.md files; CHANGELOG stub added.")
    print(f"next: edit CHANGELOG.md, commit, then `git tag v{version} && git push --tags`")
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "check":
        return cmd_check()
    if len(sys.argv) >= 3 and sys.argv[1] == "set":
        return cmd_set(sys.argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
