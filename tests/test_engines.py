"""Engine registry + per-engine run() dispatch.

Two families behave differently:
  - BaseEngine (say, chatterbox): templates for Permission/Notification, classify for Stop.
  - KokoroRealtimeEngine: every non-PreToolUse event goes through generate_or_skip.
"""

import json

import pytest

import engines.base as base

# Captured before the autouse no_audio fixture swaps it out for every test.
_REAL_SAY_WITH_VOLUME = base.BaseEngine._say_with_volume


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
    import stop_outcome
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")
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
        seen.update(ctx=ctx, awaiting=k["stop"].awaiting_answer)
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


@pytest.mark.parametrize("phrase", ["-o/Users/me/notes.txt", "--help", "I pushed the branch."])
def test_say_reads_the_phrase_as_text_never_as_an_option(phrase, no_audio, monkeypatch):
    """A phrase is model output. Before `--`, "-o<path>" wrote audio over that file."""
    import shlex
    from engines.say import SayEngine
    monkeypatch.setattr(base.BaseEngine, "_say_with_volume", _REAL_SAY_WITH_VOLUME)
    SayEngine(_cfg())._say_with_volume(phrase)
    [say] = [a for a in no_audio["popen"] if a[0] == "say"]
    assert say[-2:] == ["--", phrase] and say.index("--") > say.index("-o")
    # Played from the rendered clip; the phrase never reaches a shell.
    [played] = [a for a in no_audio["popen"] if a[:2] == ["sh", "-c"]]
    words = shlex.split(played[2])
    assert words[0] == "afplay" and say[say.index("-o") + 1] in words and phrase not in words


# ── The face: every played phrase, once, after the voice ─────────────────────

def _face_after_voice(monkeypatch, no_audio, seen):
    """A face.show that records what was already spawned when it was called."""
    import face

    def show(config, phrase, kind, audio=None, **k):
        seen.append({"phrase": phrase, "kind": kind, "audio": audio,
                     "spawned": [list(a) for a in no_audio["popen"]]})
        return True

    monkeypatch.setattr(face, "show", show)


def _cached(claude_home, phrase):
    cache = claude_home / "voice-cache-chatterbox"
    cache.mkdir(exist_ok=True)
    clip = cache / f"{base.bark_hash(phrase)}.wav"
    clip.write_bytes(b"RIFF")
    return clip


def test_a_cached_template_goes_on_the_face_after_afplay(claude_home, no_audio, monkeypatch):
    from engines.chatterbox import ChatterboxEngine
    phrase = "The migration is finished."
    clip = _cached(claude_home, phrase)
    seen = []
    _face_after_voice(monkeypatch, no_audio, seen)
    ChatterboxEngine(_cfg("chatterbox")).try_bark(phrase, kind="done")
    [shown] = seen
    assert (shown["phrase"], shown["kind"], shown["audio"]) == (phrase, "done", str(clip))
    assert shown["spawned"][-1][0] == "afplay" and shown["spawned"][-1][-1] == str(clip)


def test_the_say_path_renders_first_then_plays_and_shows_the_clip(no_audio, monkeypatch):
    """The face needs the audio to move to, so say renders before playback."""
    from engines.say import SayEngine
    monkeypatch.setattr(base.BaseEngine, "_say_with_volume", _REAL_SAY_WITH_VOLUME)
    seen = []
    _face_after_voice(monkeypatch, no_audio, seen)
    SayEngine(_cfg()).speak_dynamic("I pushed the branch.", kind="waiting")
    [shown] = seen
    say, played = shown["spawned"]
    assert say[0] == "say" and shown["audio"] == say[say.index("-o") + 1]
    assert played[:2] == ["sh", "-c"] and "rm -f" in played[2]
    assert (shown["phrase"], shown["kind"]) == ("I pushed the branch.", "waiting")


def test_a_say_that_cannot_render_plays_and_shows_nothing(no_audio, monkeypatch):
    from engines.say import SayEngine

    class FailingSay:
        def __init__(self, args, *a, **k):
            no_audio["popen"].append(args)

        def wait(self, *a, **k):
            return 1

    monkeypatch.setattr(base.BaseEngine, "_say_with_volume", _REAL_SAY_WITH_VOLUME)
    monkeypatch.setattr(base.subprocess, "Popen", FailingSay)
    seen = []
    _face_after_voice(monkeypatch, no_audio, seen)
    SayEngine(_cfg())._say_with_volume("I pushed the branch.")
    assert [a[0] for a in no_audio["popen"]] == ["say"] and seen == []


def test_a_failing_face_still_plays_and_logs_the_bark(claude_home, no_audio, monkeypatch):
    import face
    from engines.chatterbox import ChatterboxEngine

    def broken(*a, **k):
        raise RuntimeError("no face today")

    monkeypatch.setattr(face, "show", broken)
    phrase = "The migration is finished."
    _cached(claude_home, phrase)
    ChatterboxEngine(_cfg("chatterbox")).try_bark(phrase, kind="done")
    assert no_audio["popen"][-1][0] == "afplay"
    log = (claude_home / "hobson.log").read_text()
    assert "face failed (no face today)" in log and "barked (template-cache)" in log


def test_a_phrase_presence_holds_never_reaches_the_face(no_audio, no_face, monkeypatch):
    import presence
    from engines.say import SayEngine
    monkeypatch.setattr(presence, "route", lambda config, kind, phrase, project=None: None)
    monkeypatch.setattr(base.BaseEngine, "_say_with_volume", _REAL_SAY_WITH_VOLUME)
    eng = SayEngine(_cfg())
    eng.try_bark("The migration is finished.", kind="done")
    assert eng.speak_dynamic("I pushed the branch.", kind="waiting") is False
    assert no_audio["popen"] == [] and no_face == []
