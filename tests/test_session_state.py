"""session_state: schema, backfill, record_*, build_session_context, round-trip."""

import json

import session_state as ss


def test_fresh_state_schema():
    s = ss._fresh_state("proj")
    assert set(s) == {
        "project", "recent_voiced", "event_counts_since_voice",
        "last_voiced_time", "total_events", "total_voiced", "pending",
        "last_flush_time", "fingerprints", "repeat_announced", "seen_contexts",
        "last_event_time", "last_stop_time", "last_stop_category",
        "last_wait_time", "last_wait_session",
    }


def test_load_session_fresh_when_missing(claude_home):
    s = ss.load_session("newproj")
    assert s["project"] == "newproj"
    assert s["total_events"] == 0


def test_load_session_backfills_missing_fields(claude_home):
    # Simulate an old session file missing newer fields.
    path = ss._session_path("proj")
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"project": "proj", "total_events": 5}, f)
    s = ss.load_session("proj")
    assert s["total_events"] == 5  # preserved
    assert s["recent_voiced"] == []  # backfilled
    assert s["total_voiced"] == 0


def test_record_voiced_updates_counts_and_resets():
    s = ss._fresh_state("p")
    s["event_counts_since_voice"] = {"Edit": 3}
    ss.record_voiced(s, "I shipped it.")
    assert len(s["recent_voiced"]) == 1
    phrase, ts = s["recent_voiced"][0]
    assert phrase == "I shipped it."
    assert ts > 0  # real timestamp, not the 0.0 "unknown" sentinel
    assert s["event_counts_since_voice"] == {}
    assert s["total_voiced"] == 1
    assert s["total_events"] == 1
    assert s["last_voiced_time"] > 0


def test_record_voiced_trims_to_max():
    s = ss._fresh_state("p")
    for i in range(ss.MAX_RECENT_VOICED + 5):
        ss.record_voiced(s, f"phrase {i}")
    assert len(s["recent_voiced"]) == ss.MAX_RECENT_VOICED
    # keeps the most recent
    assert s["recent_voiced"][-1][0] == f"phrase {ss.MAX_RECENT_VOICED + 4}"


def test_record_skipped_increments():
    s = ss._fresh_state("p")
    ss.record_skipped(s, "PreToolUse")
    ss.record_skipped(s, "PreToolUse")
    assert s["event_counts_since_voice"]["PreToolUse"] == 2
    assert s["total_events"] == 2


def test_build_session_context_empty():
    ctx = ss.build_session_context(ss._fresh_state("p"))
    assert "No announcements yet" in ctx


def test_build_session_context_with_history():
    s = ss._fresh_state("p")
    ss.record_voiced(s, "I did a thing.")
    ss.record_skipped(s, "Edit")
    ctx = ss.build_session_context(s)
    assert "Edit" in ctx
    assert "Last announced" in ctx
    assert "No announcements yet" not in ctx


def test_the_model_is_not_shown_what_was_said_before():
    """Shown its last six phrases, of any age, llama3.2:3b reused them: 67% of
    off-batch commentary took its words from one, and paired replays of real
    Stops carried a stale phrase 33 times with it and never without. The
    phrases stay in the state for the duplicate guards, which read them."""
    s = ss._fresh_state("p")
    ss.record_voiced(s, "I fixed the invoice sync.")
    ctx = ss.build_session_context(s, task="tidy the release notes")
    assert "invoice" not in ctx and "Recent" not in ctx
    assert s["recent_voiced"][0][0] == "I fixed the invoice sync."


def test_save_load_round_trip(claude_home):
    s = ss.load_session("rt")
    ss.record_voiced(s, "hello there friend")
    ss.save_session(s)
    again = ss.load_session("rt")
    assert again["recent_voiced"][0][0] == "hello there friend"
    assert again["total_voiced"] == 1


def test_legacy_bare_string_recent_voiced_coerces_on_load(claude_home):
    # Old on-disk sessions hold recent_voiced as bare strings. Loading must
    # coerce them to [phrase, 0.0] -- ts=0.0 reads as "unknown", which stays
    # conservative (dedupe), never "infinitely old" (let through).
    path = ss._session_path("legacy")
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"project": "legacy", "recent_voiced": ["an old bare phrase"]}, f)
    s = ss.load_session("legacy")
    assert s["recent_voiced"] == [["an old bare phrase", 0.0]]

    # Round-trips cleanly through save/load too.
    ss.save_session(s)
    again = ss.load_session("legacy")
    assert again["recent_voiced"] == [["an old bare phrase", 0.0]]


def test_build_session_context_leads_with_the_task():
    s = ss._fresh_state("p")
    ss.record_voiced(s, "I started on the nudge fix.")
    ctx = ss.build_session_context(s, task="stop the nudge repeating itself")
    assert ctx.splitlines()[0] == "Task: stop the nudge repeating itself"
    assert "Task:" not in ss.build_session_context(s)


def test_a_wait_just_said_is_matched_by_session_and_time():
    s = ss._fresh_state("proj")
    assert not ss.wait_just_said(s, "a", 20, now=100.0)
    ss.record_wait_said(s, "a", now=100.0)
    assert ss.wait_just_said(s, "a", 20, now=107.0)
    assert not ss.wait_just_said(s, "b", 20, now=107.0), "another session's wait"
    assert ss.wait_just_said(s, None, 20, now=107.0), "no session id: the project decides"
    assert not ss.wait_just_said(s, "a", 20, now=121.0), "too long ago"
    ss.record_wait_said(s, None, now=200.0)
    assert ss.wait_just_said(s, "b", 20, now=205.0)
