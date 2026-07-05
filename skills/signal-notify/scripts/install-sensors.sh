#!/usr/bin/env bash
# Legacy name, kept for parity with the original hermes-setup source repo.
# `setup.sh` (`signal-triage setup`) is the canonical entrypoint (FR-16b); this
# is a thin alias so `install-sensors.sh --dry-run` (documented in the PRD's
# functional test plan) keeps working unmodified.
set -euo pipefail
cd "$(dirname "$0")"
exec uv run python setup.py "$@"
