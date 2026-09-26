"""Engine registry + per-engine run() dispatch.

Two families behave differently:
  - BaseEngine (say, chatterbox): templates for Permission/Notification, classify for Stop.
  - KokoroRealtimeEngine: every non-PreToolUse event goes through generate_or_skip.
"""

import json

import pytest

import engines.base as base


def _cfg(engine="say"):
    return {
        "engine": engine,
        "personality": "hobson",
        "events": ["stop", "permission", "notification", "commentary"],
        "cooldown": 0,
        "commentary": {"cooldown": 0, "tools": ["Bash", "Edit", "Write", "Agent"],
                       "verbosity": "normal"},
        "ollama": {"model": "m", "url": "http://x"},
        "kokoro": {"daemon_port": 19849},
    }


def _transcript(tmp_path, text="all done"):
    f = tmp_path / "t.jsonl"
    f.write_text(json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    ), encoding="utf-8")
    return str(f)


# ── registry ────────────────────────────────────────────────────────────────

def test_load_engine_known(claude_home, hobson_entry):
    eng = hobson_entry.load_engine(_cfg("say"))
    assert eng.engine_name == "say"


def test_load_engine_unknown_falls_back_to_say(claude_home, hobson_entry):
    eng = hobson_entry.load_engine(_cfg("nonsense-engine"))
    assert eng.engine_name == "say"


# ── BaseEngine (say) dispatch ────────────────────────────────────────────────

def test_say_stop_classifies_and_barks(claude_home, no_audio, monkeypatch, tmp_path):
    from engines.say import SayEngine
    monkeypatch.setattr(base, "classify", lambda *a, **k: "done")
    eng = SayEngine(_cfg("say"))
    eng.run({"hook_event_name": "Stop", "transcript_path": _transcript(tmp_path)})
    assert no_audio["say"], "expected a spoken phrase on Stop"


def test_say_permission_barks(claude_home, no_audio):
    from engines.say import SayEngine
    eng = SayEngine(_cfg("say"))
    eng.run({"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    assert no_audio["say"]


def test_say_notification_barks(claude_home, no_audio):
    from engines.say import SayEngine
    eng = SayEngine(_cfg("say"))
    eng.run({"hook_event_name": "Notification", "message": "needs input"})
    assert no_audio["say"]


def test_say_disabled_event_silent(claude_home, no_audio):
    from engines.say import SayEngine
    cfg = _cfg("say")
    cfg["events"] = ["stop"]
    eng = SayEngine(cfg)
    eng.run({"hook_event_name": "Notification", "message": "x"})
    assert not no_audio["say"]


# ── ChatterboxEngine ─────────────────────────────────────────────────────────

def test_chatterbox_cache_miss_falls_back_to_say(claude_home, no_audio):
    from engines.chatterbox import ChatterboxEngine
    eng = ChatterboxEngine(_cfg("chatterbox"))
    # No cached WAVs exist -> say fallback.
    eng.run({"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    assert no_audio["say"]


# ── KokoroRealtimeEngine dispatch ────────────────────────────────────────────

def _kokoro(monkeypatch, recorded):
    from engines.kokoro_realtime import KokoroRealtimeEngine
    eng = KokoroRealtimeEngine(_cfg("kokoro-realtime"))
    monkeypatch.setattr(eng, "_speak_live", lambda phrase, **k: recorded.append(phrase))
    return eng


def test_kokoro_permission_routes_through_generate(claude_home, monkeypatch):
    import phrase_gen
    monkeypatch.setattr(phrase_gen, "generate_or_skip", lambda *a, **k: ("done", "I asked permission."))
    spoken = []
    eng = _kokoro(monkeypatch, spoken)
    eng.run({"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    assert spoken == ["I asked permission."]


def test_kokoro_notification_routes_through_generate(claude_home, monkeypatch):
    import phrase_gen
    monkeypatch.setattr(phrase_gen, "generate_or_skip", lambda *a, **k: ("done", "I have a notification."))
    spoken = []
    eng = _kokoro(monkeypatch, spoken)
    eng.run({"hook_event_name": "Notification", "message": "hey"})
    assert spoken == ["I have a notification."]


def test_kokoro_stop_uses_transcript_and_generate(claude_home, monkeypatch, tmp_path):
    import phrase_gen
    seen = {}

    def fake_gen(event, detail, ctx, **k):
        seen["event"] = event
        seen["detail"] = detail
        return ("done", "I finished everything.")

    monkeypatch.setattr(phrase_gen, "generate_or_skip", fake_gen)
    spoken = []
    eng = _kokoro(monkeypatch, spoken)
    eng.run({"hook_event_name": "Stop", "transcript_path": _transcript(tmp_path, "wrapped up")})
    assert spoken == ["I finished everything."]
    assert seen["event"] == "Stop"
    assert "wrapped up" in seen["detail"]


def test_kokoro_describe_event_branches(claude_home):
    from engines.kokoro_realtime import KokoroRealtimeEngine
    eng = KokoroRealtimeEngine(_cfg("kokoro-realtime"))
    assert eng._describe_event(
        {"hook_event_name": "PermissionRequest", "tool_name": "Bash"}).startswith("Tool:")
    assert eng._describe_event(
        {"hook_event_name": "Notification", "message": "hey"}) == "Message: hey"
    assert eng._describe_event(
        {"hook_event_name": "PreToolUse", "tool_name": "Edit",
         "tool_input": {"file_path": "/a/base.py"}}) == "Edit: base.py"
    assert eng._describe_event({"hook_event_name": "Unknown"}) is None


def test_kokoro_skip_does_not_speak(claude_home, monkeypatch):
    import phrase_gen
    monkeypatch.setattr(phrase_gen, "generate_or_skip", lambda *a, **k: (None, None))
    spoken = []
    eng = _kokoro(monkeypatch, spoken)
    eng.run({"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    assert spoken == []


@pytest.mark.parametrize("engine", ["kokoro-realtime", "pocket-tts"])
def test_a_realtime_stop_that_asks_is_flagged_and_gets_the_task(engine, claude_home,
                                                                 monkeypatch, tmp_path):
    import json
    import phrase_gen
    seen = {}

    def fake_gen(event, detail, ctx, **k):
        seen.update(ctx=ctx, awaiting=k.get("awaiting_answer"))
        return ("question", "I need your pick on the cat's stats.")

    monkeypatch.setattr(phrase_gen, "generate_or_skip", fake_gen)
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": "design the cat's stats for the game"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Sketched."}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "One"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Two"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hidden stats: A?"}]}},
    ]), encoding="utf-8")
    if engine == "pocket-tts":
        from engines.pocket_tts_realtime import PocketTTSRealtimeEngine as Eng
    else:
        from engines.kokoro_realtime import KokoroRealtimeEngine as Eng
    eng = Eng(_cfg(engine))
    monkeypatch.setattr(eng, "_speak_live", lambda phrase, **k: None)
    eng.run({"hook_event_name": "Stop", "transcript_path": str(t)})
    assert seen["awaiting"] is True
    # The request fell out of the four-turn window, so it rides as the Task.
    assert seen["ctx"].startswith("Task: design the cat's stats for the game")
