"""Test bootstrap: isolate all resolved paths into a tmp dir BEFORE
_sensorlib is imported (its module level calls resolve_paths())."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="stn-tests-"))
os.environ.setdefault("SIGNAL_TRIAGE_SIGNALS_DIR", str(_TMP / "signals"))
os.environ.setdefault("SIGNAL_TRIAGE_STATE_DIR", str(_TMP / "state"))
os.environ.setdefault("SIGNAL_TRIAGE_POLICY", str(_TMP / "policy.yaml"))
# Keep resolve_paths() away from any real Hermes config on the host/CI.
os.environ.setdefault("HERMES_CONFIG_PATH", str(_TMP / "no-such-config.yaml"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
