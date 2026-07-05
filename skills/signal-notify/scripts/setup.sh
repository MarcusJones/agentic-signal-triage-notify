#!/usr/bin/env bash
# Canonical first-run bootstrap entrypoint (FR-16b). See setup.py for details.
set -euo pipefail
cd "$(dirname "$0")"
exec uv run python setup.py "$@"
