# Contributing

Contributions are welcome — new example sensors, bug fixes, and docs
improvements especially.

## Ground rules

- **No personal or account-specific data.** This is a public tap. Don't
  contribute a sensor hardcoded to a specific company, person, or private
  feed URL — parameterize it via `metadata.hermes.config` or an environment
  variable instead (see `docs/writing-a-sensor.md`).
- **Vault-optional stays vault-optional.** Every path must resolve through
  `_sensorlib.resolve_paths()`. Do not add a code path that requires an
  Obsidian vault or any specific directory layout.
- **Cross-platform.** Hermes' matrix is Linux/macOS/native Windows (Git
  Bash)/WSL. Avoid POSIX-only sync tools or a hardcoded venv `bin/`
  interpreter path — use `uv run`, `platformdirs`, and `shutil` instead.
- **No secrets in commits.** Tokens, credentials, and `.env`-style files are
  never committed. Declare requirements via `required_environment_variables`
  / `required_credential_files` frontmatter instead.

## Adding a sensor

See `docs/writing-a-sensor.md` for the sensor contract. In short: a small
`_sensor.py` + matching `_sensor.sh` launcher under
`skills/signal-notify/scripts/`, a `registry.yaml` entry, and (if it's
generically useful) a mention in the root `README.md`'s sensor list.

## Testing before you open a PR

```bash
# Syntax check
python3 -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('**/*.py', recursive=True)]"

# Dry-run the installer (touches nothing)
bash skills/signal-notify/scripts/install-sensors.sh --dry-run

# Validate the discovery manifest
python3 -c "import json; d=json.load(open('.well-known/skills/index.json')); assert {s['name'] for s in d['skills']} == {'signal-triage','signal-notify'}"
```

## Reporting issues

Open a GitHub issue with: what you expected, what happened, the sensor/skill
involved, and your OS (Linux/macOS/native Windows/WSL) — path resolution bugs
are usually OS-specific.
