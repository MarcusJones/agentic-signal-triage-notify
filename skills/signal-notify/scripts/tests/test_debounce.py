from datetime import datetime, timedelta, timezone

import _sensorlib as lib


def _line(minutes_old: int, sid: str = "x") -> str:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_old)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    return f"- 09:00 [Gmail] msg (gmail_message_id={sid}) _(detected_at={ts}; sensor=gmail)_"


def test_entry_detected_at_parses_z_suffix():
    dt = lib.entry_detected_at(_line(10))
    assert dt is not None
    age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    assert 9 <= age_min <= 11


def test_entry_detected_at_missing_returns_none():
    assert lib.entry_detected_at("- 09:00 [Gmail] no stamp here") is None


def test_entry_detected_at_malformed_returns_none():
    assert lib.entry_detected_at("- x _(detected_at=2026-13-99Tnonsense; sensor=y)_") is None


def test_debounce_semantics_hold_vs_flush():
    """The prefilter holds while oldest < debounce and flushes at/after it.
    Mirrors triage_prefilter.main()'s arithmetic on entry ages."""
    now = datetime.now(timezone.utc)
    young = [lib.entry_detected_at(_line(5, "a")), lib.entry_detected_at(_line(12, "b"))]
    oldest_min = max((now - a).total_seconds() / 60 for a in young)
    assert oldest_min < 30  # would HOLD at the seeded default

    aged = young + [lib.entry_detected_at(_line(45, "c"))]
    oldest_min = max((now - a).total_seconds() / 60 for a in aged)
    assert oldest_min >= 30  # would FLUSH — the whole batch in one wake-up


def test_unparseable_stamp_fails_open():
    """An entry without a parseable stamp must count as 'old enough':
    all(a is not None) is False → the prefilter skips the hold branch."""
    ages = [lib.entry_detected_at(_line(5)), lib.entry_detected_at("- no stamp")]
    assert not all(a is not None for a in ages)
