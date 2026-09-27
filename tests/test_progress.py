"""Repetition as the signal for 'this is going badly'."""

import time

import session_state as ss


def _state():
    return ss._fresh_state("demo")


def test_the_same_work_produces_the_same_fingerprint():
    assert ss.fingerprint("Edit", "base.py") == ss.fingerprint("edit", "base.py")
    assert ss.fingerprint("Edit", "base.py") != ss.fingerprint("Edit", "other.py")


def test_a_missing_context_still_fingerprints():
    assert ss.fingerprint("Bash", None)


def test_repeat_count_climbs_with_repetition():
    st = _state()
    for _ in range(3):
        ss.record_fingerprint(st, "Bash", "run the tests")
    assert ss.repeat_count(st, "Bash", "run the tests") == 3


def test_unrelated_work_does_not_count():
    st = _state()
    ss.record_fingerprint(st, "Bash", "run the tests")
    ss.record_fingerprint(st, "Edit", "base.py")
    assert ss.repeat_count(st, "Bash", "run the tests") == 1


def test_old_repeats_fall_out_of_the_window():
    st = _state()
    ss.record_fingerprint(st, "Bash", "run the tests")
    st["fingerprints"][0]["ts"] = time.time() - 600
    ss.record_fingerprint(st, "Bash", "run the tests")
    assert ss.repeat_count(st, "Bash", "run the tests", window_seconds=180) == 1


def test_the_ring_is_capped():
    st = _state()
    for i in range(ss.MAX_FINGERPRINTS + 10):
        ss.record_fingerprint(st, "Bash", f"cmd {i}")
    assert len(st["fingerprints"]) == ss.MAX_FINGERPRINTS


def test_fingerprints_survive_a_disk_round_trip(claude_home):
    st = _state()
    ss.record_fingerprint(st, "Bash", "keep me")
    ss.save_session(st)
    assert ss.load_session("demo")["fingerprints"]


def test_old_sessions_without_fingerprints_are_backfilled(claude_home):
    st = _state()
    del st["fingerprints"]
    ss.save_session(st)
    assert ss.load_session("demo")["fingerprints"] == []


# ── Repetition does not force a flush ────────────────────────────────────
#
# A stuck detector ("going in circles") used to force a flush when one
# action repeated three times. Replayed over 12,805 real tool calls it
# caught nothing that was genuinely a loop, and it was removed. Repetition
# now only matters to anomaly mode's "back on the same thing again".


def test_a_repeat_is_announced_only_once():
    """Otherwise every further repeat force-flushes anomaly mode."""
    st = _state()
    for _ in range(4):
        ss.record_fingerprint(st, "Bash", "run the tests")
    assert not ss.repeat_already_announced(st, "Bash", "run the tests")
    ss.mark_repeat_announced(st, "Bash", "run the tests")
    assert ss.repeat_already_announced(st, "Bash", "run the tests")


def test_repetition_does_not_force_a_flush_in_normal_mode(
        claude_home, no_audio, monkeypatch):
    import phrase_gen
    from engines.say import SayEngine
    from home import derive_project_label
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("broken", "That test keeps failing."))
    eng = SayEngine({
        "engine": "say", "personality": "hobson", "events": ["commentary"],
        "commentary": {"tools": ["bash"], "verbosity": "normal", "cooldown": 0,
                       "min_tool_calls": 5, "min_seconds": 600.0},
    })
    # Past the first-ever-call bootstrap flush, which would speak on its own
    # and make this assertion meaningless.
    st = ss.load_session(derive_project_label())
    st["last_voiced_time"] = st["last_flush_time"] = time.time()
    ss.save_session(st)

    evt = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
           "tool_input": {"command": "pytest tests/", "description": "run the tests"}}
    for _ in range(4):
        eng.run(dict(evt))
    assert not no_audio["say"], "4 identical calls stay batched below min_tool_calls=5"


# ── Task 8: anomaly mode ─────────────────────────────────────────────────


def test_first_touch_of_a_file_is_an_anomaly():
    st = _state()
    assert ss.anomaly_reason(st, "Edit", "payments.py", {})


def test_the_second_touch_of_the_same_file_is_not():
    st = _state()
    ss.anomaly_reason(st, "Edit", "payments.py", {})
    ss.record_fingerprint(st, "Edit", "payments.py")
    reason = ss.anomaly_reason(st, "Edit", "payments.py", {})
    assert reason is None or "again" in reason.lower()


def test_repetition_is_an_anomaly():
    st = _state()
    for _ in range(3):
        ss.record_fingerprint(st, "Bash", "run the tests")
    ss.note_seen(st, "run the tests")
    assert ss.anomaly_reason(st, "Bash", "run the tests", {})


def test_anomaly_mode_stays_silent_on_routine_work(claude_home, no_audio, monkeypatch):
    import phrase_gen
    from engines.say import SayEngine
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I did a thing."))
    eng = SayEngine({
        "engine": "say", "personality": "hobson", "events": ["commentary"],
        "commentary": {"tools": ["edit"], "verbosity": "anomaly", "cooldown": 0},
    })
    # first touch of each file is notable; a repeat of an already-seen file is not
    for name in ("a.py", "a.py", "a.py"):
        eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                 "tool_input": {"file_path": f"/r/{name}"}})
    assert len(no_audio["say"]) <= 2, "routine repeats must not each speak"


def test_a_repeat_anomaly_is_also_announced_only_once(claude_home, no_audio, monkeypatch):
    """Otherwise a repeated action force-flushes anomaly mode on every
    single occurrence forever."""
    import phrase_gen
    from engines.say import SayEngine
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I did a thing."))
    eng = SayEngine({
        "engine": "say", "personality": "hobson", "events": ["commentary"],
        "commentary": {"tools": ["bash"], "verbosity": "anomaly", "cooldown": 0},
    })
    for _ in range(6):
        eng.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"description": "run the tests"}})
    assert len(no_audio["say"]) <= 2, \
        "a fingerprint that stays repeated must not speak on every occurrence"


# ── A call's identity is the action, not its display text ────────────────
#
# Repetition used to be keyed on extract_context's short string, so `cat`
# on three different temp files -- all "cat /private/tmp/claude-501/
# -Users-dev-" -- read as one command three times.

_TMP = "/private/tmp/claude-501/-Users-dev-Dev-hobson/d68de1f9/tasks/"


def _bash(cmd, description="run it"):
    return ss.action_identity("Bash", {"command": cmd, "description": description})


def test_commands_sharing_a_long_prefix_are_different_actions():
    a, b = f"cat {_TMP}a1.output", f"cat {_TMP}b2.output"
    assert a[:40] == b[:40], "precondition: these collided under the old 40-char context"
    assert _bash(a) != _bash(b)


def test_narration_does_not_change_the_action():
    """The description is Claude's prose about the command, not the command."""
    cmd = "git status"
    assert _bash(cmd, "check status") == _bash(cmd, "see what changed")
    assert _bash("pytest a", "run the tests") != _bash("pytest b", "run the tests")


def test_identity_stores_a_digest_not_the_command_itself():
    """The ring persists to disk; it should not hold full commands or edit bodies."""
    secret = "curl -H 'Authorization: Bearer sk-live-abc123' https://x"
    ident = _bash(secret)
    assert "sk-live" not in ident and "curl" not in ident
    assert len(ident) == len(_bash("ls"))

