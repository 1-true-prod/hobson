"""Controls that keep hobson quiet: subagent suppression, timed mute, quiet hours."""

import time

import engines.base as base
from engines.base import parse_duration, silence_reason


def _cfg(**over):
    cfg = {
        "engine": "say", "personality": "hobson", "events": ["commentary", "stop"],
        "commentary": {"tools": ["bash", "edit"], "verbosity": "normal", "cooldown": 0,
                       "min_tool_calls": 1, "min_seconds": 60.0},
    }
    cfg.update(over)
    return cfg


def _engine(cfg):
    from engines.say import SayEngine
    return SayEngine(cfg)


def test_subagent_event_is_detected_by_agent_id():
    eng = _engine(_cfg())
    assert eng._is_subagent_event({"agent_id": "a1", "agent_type": "general-purpose"})
    assert not eng._is_subagent_event({})
    assert not eng._is_subagent_event({"agent_id": ""})


def test_commentary_from_a_subagent_is_suppressed(claude_home, no_audio, monkeypatch):
    """1,114 PreToolUse events fired in ~22 min of background worker activity.
    A subagent's tool calls must not narrate."""
    import phrase_gen
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I edited the parser."))
    eng = _engine(_cfg())
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "/a/x.py"},
             "agent_id": "a1", "agent_type": "general-purpose"})
    assert no_audio["say"] == []


def test_commentary_from_the_main_session_still_speaks(claude_home, no_audio, monkeypatch):
    import phrase_gen
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I edited the parser."))
    eng = _engine(_cfg())
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "/a/x.py"}})
    assert len(no_audio["say"]) == 1


def test_subagent_suppression_can_be_turned_off(claude_home, no_audio, monkeypatch):
    import phrase_gen
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I edited the parser."))
    cfg = _cfg()
    cfg["commentary"]["suppress_subagents"] = False
    eng = _engine(cfg)
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "/a/x.py"}, "agent_id": "a1"})
    assert len(no_audio["say"]) == 1


def test_a_subagent_stop_still_speaks(claude_home, no_audio, monkeypatch):
    """Deliberate asymmetry: 'the background worker finished' is worth hearing;
    its 200 intermediate tool calls are not."""
    eng = _engine(_cfg())
    eng.run({"hook_event_name": "Stop", "agent_id": "a1", "agent_type": "general-purpose"})
    assert no_audio["say"], "a subagent's Stop must not be suppressed"


# ── Timed mute + quiet hours ────────────────────────────────────────────────


def test_nothing_silences_a_default_config():
    assert silence_reason({}) is None


def test_permanent_mute_silences():
    assert silence_reason({"muted": True}) == "muted"


def test_a_future_timed_mute_silences():
    reason = silence_reason({"mute_until": time.time() + 300})
    assert reason and "muted" in reason


def test_an_expired_timed_mute_does_not_silence():
    assert silence_reason({"mute_until": time.time() - 1}) is None


def test_quiet_hours_inside_a_normal_range():
    # 13:00, quiet 9->17
    now = time.mktime((2026, 8, 20, 13, 0, 0, 0, 0, -1))
    assert silence_reason({"quiet_hours": [9, 17]}, now=now)


def test_quiet_hours_outside_a_normal_range():
    now = time.mktime((2026, 8, 20, 8, 0, 0, 0, 0, -1))
    assert silence_reason({"quiet_hours": [9, 17]}, now=now) is None


def test_quiet_hours_wrapping_over_midnight_before_and_after():
    """22->8 must silence at 23:00 AND at 03:00, and not at noon."""
    late = time.mktime((2026, 8, 20, 23, 0, 0, 0, 0, -1))
    early = time.mktime((2026, 8, 20, 3, 0, 0, 0, 0, -1))
    noon = time.mktime((2026, 8, 20, 12, 0, 0, 0, 0, -1))
    assert silence_reason({"quiet_hours": [22, 8]}, now=late)
    assert silence_reason({"quiet_hours": [22, 8]}, now=early)
    assert silence_reason({"quiet_hours": [22, 8]}, now=noon) is None


def test_malformed_quiet_hours_never_raises():
    """A hook must not die on a hand-edited config."""
    for bad in ("nonsense", [], [1], [1, 2, 3], {"a": 1}, None):
        assert silence_reason({"quiet_hours": bad}) is None


def test_duration_parsing():
    assert parse_duration("45") == 45 * 60      # bare number means minutes
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 30 * 60
    assert parse_duration("2h") == 2 * 3600
    assert parse_duration("1.5h") == 5400
    for bad in ("", "abc", "-5", "5x", None):
        assert parse_duration(bad) is None


def test_entrypoint_stays_silent_when_muted(claude_home, hobson_entry, monkeypatch):
    """The silence check must run before any engine work."""
    import io
    import json
    (claude_home / "hobson.json").write_text(json.dumps({"mute_until": time.time() + 300}))
    called = []
    monkeypatch.setattr(hobson_entry, "load_engine", lambda cfg: called.append(cfg))
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO('{"hook_event_name":"Stop"}'))
    hobson_entry.main()
    assert called == [], "engine must not be constructed while silenced"


def test_daemon_idle_timeout_outlives_normal_gaps_between_utterances():
    """Measured on 11,173 real gaps between spoken phrases: a 180s idle
    timeout expires during 4.1% of them, and every expiry costs the next
    utterance a fallback to macOS `say` (commentary passes
    allow_cold_start=False by design). 600s cuts that to 1.1%; beyond it the
    curve flattens while RAM stays pinned longer.
    """
    from engines.base import DEFAULT_CONFIG
    for engine in ("pocket_tts", "kokoro"):
        timeout = DEFAULT_CONFIG[engine]["daemon_idle_timeout"]
        assert timeout >= 600, f"{engine} idle timeout {timeout}s re-opens the gap"
