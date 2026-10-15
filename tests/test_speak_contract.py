"""What each engine family says for each hook event, end to end through
engine.run(): the phrase, its kind, and the path it left by (the playback
line's source). Every phrase reaches the log through log_record.playback, so
this is read from the log, as recap and stats read it.

Both families are driven by the same scenarios, so a difference between them
is written down here rather than discovered.
"""

import json
import time

import pytest

import bark_templates
import engines.daemon as daemon
import log_record
from engines.chatterbox import ChatterboxEngine
from engines.kokoro_realtime import KokoroRealtimeEngine
from engines.say import SayEngine

RISKY = "Careful — this one deletes files. It needs your approval."


def _cfg(engine):
    return {
        "engine": engine,
        "personality": "hobson",
        "events": ["stop", "permission", "notification", "commentary"],
        "cooldown": 0,
        "commentary": {"cooldown": 0, "tools": ["Bash"], "verbosity": "chatty"},
        "ollama": {"model": "m", "url": "http://x"},
        "nudge": {"enabled": True},
    }


class _Resp:
    status = 200

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def live_daemon(monkeypatch):
    def urlopen(req, timeout=None):
        if req.full_url.endswith("/health"):
            return _Resp(b"ok")
        return _Resp(b"RIFF" + b"\0" * 200)
    monkeypatch.setattr(daemon, "urlopen", urlopen)


@pytest.fixture
def model(monkeypatch):
    """The model's phrase for any generated event, and its classification."""
    import phrase_gen
    import stop_outcome
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda event, *a, **k: phrase_gen.Generation("done", f"I handled the {event}."))
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")


def _played():
    """[(source, kind, phrase)] for every phrase played, from the log."""
    out = []
    for rec in log_record.read():
        body = rec.body
        if body.startswith("[") and "] barked" in body:
            body = body.split("] ", 1)[1]
        p = log_record.parse_playback(body)
        if p:
            out.append((p.source, p.kind, p.phrase))
    return out


def _nudged(no_audio):
    return any("nudge.py" in " ".join(map(str, args)) and "--watchdog" not in args
               for args in no_audio["popen"])


def _transcript(tmp_path, text):
    f = tmp_path / "t.jsonl"
    f.write_text(json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    ), encoding="utf-8")
    return str(f)


def _question_phrases():
    group = bark_templates.CATEGORIES["question"]
    return {f"{lead} {tail}" for lead in group["lead"] for tail in group["tail"]}


STATIC = [SayEngine, ChatterboxEngine]
LIVE = [KokoroRealtimeEngine]


def _source(cls):
    """Where a phrase that is not in the cache comes from: every phrase,
    template or not, goes through the same clip (BaseEngine.clip)."""
    return {SayEngine: "say", ChatterboxEngine: "say-fallback: cache miss",
            KokoroRealtimeEngine: "daemon-live"}[cls]


def _engine(cls):
    return cls(_cfg({SayEngine: "say", ChatterboxEngine: "chatterbox",
                     KokoroRealtimeEngine: "kokoro-realtime"}[cls]))


# ── A destructive permission request: a fixed sentence, everywhere ──────────

@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_a_destructive_request_is_the_fixed_warning(cls, no_audio, live_daemon, model):
    _engine(cls).run({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                      "tool_input": {"command": "rm -rf build"}})
    ((source, kind, phrase),) = _played()
    assert (kind, phrase) == ("waiting", RISKY)
    assert source == _source(cls)


# ── A question dialog: a question template, never the model ────────────────

@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_a_question_dialog_is_a_question_template(cls, no_audio, live_daemon, model):
    _engine(cls).run({"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion"})
    ((source, kind, phrase),) = _played()
    assert kind == "waiting" and phrase in _question_phrases()
    assert source == _source(cls)


# ── A plain permission request ──────────────────────────────────────────────

@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_a_plain_permission_request_waits_on_you(cls, no_audio, live_daemon, model):
    _engine(cls).run({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                      "tool_input": {"command": "ls"}})
    ((source, kind, phrase),) = _played()
    assert kind == "waiting"
    if cls in LIVE:
        assert (source, phrase) == ("daemon-live", "I handled the PermissionRequest.")
    else:
        assert source == _source(cls)


# ── Notifications ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_an_idle_prompt_is_a_waiting_template_and_nudges(cls, no_audio, live_daemon, model):
    _engine(cls).run({"hook_event_name": "Notification", "notification_type": "idle_prompt",
                      "message": "Claude is waiting for your input"})
    ((source, kind, phrase),) = _played()
    assert kind == "waiting" and phrase in bark_templates.NOTIFICATION_TEMPLATES
    assert source == _source(cls)
    assert _nudged(no_audio)


@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_an_idle_prompt_after_a_finished_turn_says_nothing(cls, no_audio, live_daemon, model):
    import home
    from session_state import transaction, record_stop_category
    with transaction(home.derive_project_label()) as state:
        record_stop_category(state, "done")
    _engine(cls).run({"hook_event_name": "Notification", "notification_type": "idle_prompt"})
    assert _played() == []
    assert not _nudged(no_audio)


@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_other_news_is_info(cls, no_audio, live_daemon, model):
    _engine(cls).run({"hook_event_name": "Notification", "notification_type": "auth_success",
                      "message": "signed in"})
    ((source, kind, phrase),) = _played()
    assert kind == "info"
    if cls in LIVE:
        assert (source, phrase) == ("daemon-live", "I handled the Notification.")
    else:
        assert phrase in bark_templates.NOTIFICATION_TEMPLATES
    assert not _nudged(no_audio)


# ── Stop ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_a_finished_turn_is_done(cls, no_audio, live_daemon, model, tmp_path):
    _engine(cls).run({"hook_event_name": "Stop",
                      "transcript_path": _transcript(tmp_path, "All tests pass.")})
    ((source, kind, phrase),) = _played()
    assert kind == "done"
    if cls in LIVE:
        assert (source, phrase) == ("daemon-live", "I handled the Stop.")


@pytest.mark.parametrize("cls", STATIC)
def test_a_cached_template_plays_from_the_cache(cls, no_audio, model, tmp_path, monkeypatch):
    eng = _engine(ChatterboxEngine)
    group = bark_templates.CATEGORIES["done"]
    phrases = [f"{lead} {tail}" for lead in group["lead"] for tail in group["tail"]]
    import os
    os.makedirs(eng.cache_dir, exist_ok=True)
    from engines.base import bark_hash
    for p in phrases:
        open(os.path.join(eng.cache_dir, f"{bark_hash(p)}.wav"), "wb").close()
    eng.run({"hook_event_name": "Stop",
             "transcript_path": _transcript(tmp_path, "All tests pass.")})
    ((source, kind, phrase),) = _played()
    assert (source, kind) == ("template-cache", "done") and phrase in phrases


# ── Commentary (chatty: a template on every call) ──────────────────────────

@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_chatty_commentary_is_commentary(cls, no_audio, live_daemon, model):
    _engine(cls).run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "make", "description": "Build it"}})
    ((source, kind, phrase),) = _played()
    assert kind == "commentary"
    assert source == _source(cls)


# ── Pull speech: never dropped by a recent phrase ──────────────────────────

@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_an_answer_right_after_another_phrase_is_still_spoken(cls, no_audio, live_daemon,
                                                              claude_home):
    (claude_home / "hobson.lock").write_text(str(time.time()))
    eng = _engine(cls)
    assert eng.speak("Two sessions finished.", "answer", cold_start=True) is True
    ((source, kind, phrase),) = _played()
    assert (kind, phrase) == ("answer", "Two sessions finished.")


@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_a_phrase_right_after_another_is_dropped_unless_it_cannot_wait(cls, no_audio, live_daemon,
                                                                       claude_home):
    (claude_home / "hobson.lock").write_text(str(time.time()))
    eng = cls(dict(_cfg("x"), cooldown=2.0))
    assert eng.speak("I'm running the tests.", "waiting") is True
    assert _played() == []
    assert eng.speak("The tests pass.", "done", cold_start=True) is True
    assert [k for _, k, _ in _played()] == ["done"]


# ── The template cache, for every phrase on a caching engine ──────────────

def _cache(eng, phrase):
    import os
    from engines.base import bark_hash
    os.makedirs(eng.cache_dir, exist_ok=True)
    path = os.path.join(eng.cache_dir, f"{bark_hash(phrase)}.wav")
    open(path, "wb").close()
    return path


def test_the_destructive_warning_plays_from_the_cache_too(no_audio, model):
    """It used to go to `say` whatever the cache held: only the template
    path read it."""
    eng = _engine(ChatterboxEngine)
    _cache(eng, RISKY)
    eng.run({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
             "tool_input": {"command": "rm -rf build"}})
    assert _played() == [("template-cache", "waiting", RISKY)]


def test_a_template_missing_from_the_cache_is_made_for_next_time_and_commentary_is_not(
        no_audio, monkeypatch):
    eng = _engine(ChatterboxEngine)
    made = []
    monkeypatch.setattr(eng, "backfill", made.append)
    eng.speak("The migration is finished.", "done")
    eng.speak("I'm running the tests.", "commentary")
    eng.speak("Two sessions finished.", "answer", cold_start=True)
    assert made == ["The migration is finished."]


# ── One dispatcher: the bookkeeping is the same on every engine ────────────

@pytest.mark.parametrize("cls", STATIC + LIVE)
@pytest.mark.parametrize("hook", [
    {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}},
    {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion"},
    {"hook_event_name": "Notification", "notification_type": "idle_prompt"},
    {"hook_event_name": "Notification", "notification_type": "auth_success", "message": "hi"},
])
def test_every_engine_marks_the_session_alive_and_records_what_it_said(cls, hook, no_audio,
                                                                      live_daemon, model):
    import home
    from session_state import load_session
    _engine(cls).run(hook)
    state = load_session(home.derive_project_label())
    ((_, _, phrase),) = _played()
    assert state["last_event_time"] > 0
    assert [p for p, _ in state["recent_voiced"]] == [phrase]


@pytest.mark.parametrize("cls", STATIC + LIVE)
def test_the_question_line_rotates_past_what_was_just_said_on_every_engine(cls, no_audio,
                                                                           live_daemon, model):
    eng = _engine(cls)
    hook = {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion"}
    eng.run(hook)
    eng.run(hook)
    first, second = [p for _, _, p in _played()]
    assert first != second
