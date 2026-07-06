import _sensorlib as lib


def test_gmail_handle():
    line = ("- 09:45 [Gmail] messageAdded: from X · subject · snippet "
            "(gmail_message_id=19ed9981dc8f54c8; thread_id=19ed9981dc8f54c8) "
            "_(detected_at=2026-07-06T07:45:00Z; sensor=gmail)_")
    assert lib.source_id(line) == "gmail:19ed9981dc8f54c8"


def test_ics_handle():
    line = "- 10:00 [Calendar (ICS feed)] updated: Standup @ 2026-07-07 (organizer: a@b; ics_uid=ABC-123@cal) _(detected_at=2026-07-06T08:00:00Z; sensor=ics)_"
    assert lib.source_id(line) == "ics:ABC-123@cal"


def test_gcal_handle():
    line = "- 11:00 [Calendar] added: Dentist (gcal_event_id=abc_DEF-123) _(detected_at=2026-07-06T09:00:00Z; sensor=gcal)_"
    assert lib.source_id(line) == "gcal:abc_DEF-123"


def test_github_repo_number():
    line = "- 12:00 [GitHub] review_requested: please review acme/widgets#42 _(detected_at=2026-07-06T10:00:00Z; sensor=github)_"
    assert lib.source_id(line) == "github:acme/widgets#42"


def test_fallback_line_hash_includes_marker():
    body = "07:00 [Weather] forecast: sunny, 20-30C"
    with_marker = f"- {body}"
    # Hashing must include the leading marker: hashing only the body would
    # produce a DIFFERENT id and re-surface duplicates after reformatting.
    sid = lib.source_id(with_marker)
    assert sid.startswith("line:")
    assert sid == f"line:{lib.line_hash(with_marker)}"
    assert sid != f"line:{lib.line_hash(body)}"


def test_legacy_important_marker_hashes_as_written():
    line = "! 09:45 [Gmail] legacy important row with no handle"
    assert lib.source_id(line) == f"line:{lib.line_hash(line)}"


def test_dedupe_same_item_mirrored_in_highlights():
    entry = ("- IMPORTANT 09:45 [Gmail] messageAdded: urgent invoice "
             "(gmail_message_id=aaa111) _(detected_at=2026-07-06T07:45:00Z; sensor=gmail)_")
    # Highlights mirror is byte-identical → identical id → deduped upstream.
    assert lib.source_id(entry) == lib.source_id(entry)
