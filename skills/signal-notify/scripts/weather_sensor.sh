#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec uv run python weather_sensor.py
