"""Escalating, cancellable re-announcement for a session waiting on the user.

Pure logic only — never spawn a real nudge process in a test.
"""

import json
import time

import pytest

from nudge import NUDGE_DELAYS, WAITING_TYPES, should_continue


def test_delays_escalate_and_are_capped():
    assert list(NUDGE_DELAYS) == sorted(NUDGE_DELAYS), "gaps must widen"
    assert len(NUDGE_DELAYS) <= 3, "never nag more than three times"
    assert NUDGE_DELAYS[0] >= 30, "do not pounce immediately"


def test_waiting_types_are_the_two_that_mean_waiting_on_the_user():
    assert set(WAITING_TYPES) == {"idle_prompt", "agent_needs_input"}


def test_stops_once_the_user_has_acted(tmp_path):
    token = tmp_path / "activity"
    started = time.time()
    token.write_text(str(started + 5))       # activity after we started
    assert should_continue(str(token), started, started + 10) is False


def test_continues_while_the_user_stays_silent(tmp_path):
    token = tmp_path / "activity"
    started = time.time()
    token.write_text(str(started - 60))      # activity predates us
    assert should_continue(str(token), started, started + 10) is True


def test_a_missing_activity_file_does_not_stop_the_nudge(tmp_path):
    started = time.time()
    assert should_continue(str(tmp_path / "nope"), started, started + 10) is True


def test_a_corrupt_activity_file_does_not_crash(tmp_path):
    token = tmp_path / "activity"
    token.write_text("not a number")
    started = time.time()
    assert should_continue(str(token), started, started + 10) is True




# ── Engine-level trigger: spawning is guarded and gated by config ──────────


def _popen_calls(no_audio, needle):
    return [c for c in no_audio["popen"] if any(needle in str(a) for a in c)]


def test_waiting_notification_spawns_a_nudge(claude_home, no_audio):
    from engines.say import SayEngine
    eng = SayEngine({"engine": "say", "personality": "hobson", "events": ["notification"]})
    eng.run({"hook_event_name": "Notification", "notification_type": "idle_prompt",
             "message": "waiting"})
    assert len(_popen_calls(no_audio, "nudge.py")) == 1


def test_non_waiting_notification_does_not_spawn_a_nudge(claude_home, no_audio):
    from engines.say import SayEngine
    eng = SayEngine({"engine": "say", "personality": "hobson", "events": ["notification"]})
    eng.run({"hook_event_name": "Notification", "notification_type": "agent_completed",
             "message": "done"})
    assert _popen_calls(no_audio, "nudge.py") == []


def test_nudge_disabled_via_config_never_spawns(claude_home, no_audio):
    from engines.say import SayEngine
    eng = SayEngine({"engine": "say", "personality": "hobson", "events": ["notification"],
                     "nudge": {"enabled": False}})
    eng.run({"hook_event_name": "Notification", "notification_type": "idle_prompt"})
    assert _popen_calls(no_audio, "nudge.py") == []


def test_a_failed_spawn_never_raises(claude_home, monkeypatch):
    from engines.say import SayEngine
    import engines.base as base

    def boom(*a, **k):
        raise OSError("no fork slots")

    monkeypatch.setattr(base.subprocess, "Popen", boom)
    eng = SayEngine({"engine": "say", "personality": "hobson", "events": ["notification"]})
    # Must not raise.
    eng.run({"hook_event_name": "Notification", "notification_type": "idle_prompt"})


# ── UserPromptSubmit hook: records the cancel-activity token ───────────────


def test_user_prompt_submit_records_activity(claude_home, hobson_entry, monkeypatch):
    import io
    import json as _json
    import os

    (claude_home / "hobson.json").write_text(_json.dumps({"engine": "say", "muted": False}))
    monkeypatch.setattr(hobson_entry.sys, "stdin",
                        io.StringIO('{"hook_event_name": "UserPromptSubmit"}'))
    called = {"n": 0}
    monkeypatch.setattr(hobson_entry, "load_engine",
                        lambda cfg: called.__setitem__("n", called["n"] + 1))
    hobson_entry.main()
    assert called["n"] == 0, "UserPromptSubmit must not dispatch to a speaking engine"
    import nudge
    from engines.base import derive_project_label
    activity_file = nudge.activity_path(derive_project_label())
    assert os.path.isfile(activity_file)
    float(open(activity_file).read())  # must parse as a timestamp


def test_user_prompt_submit_records_activity_even_while_muted(claude_home, hobson_entry, monkeypatch):
    import io
    import json as _json
    import os

    (claude_home / "hobson.json").write_text(_json.dumps({"engine": "say", "muted": True}))
    monkeypatch.setattr(hobson_entry.sys, "stdin",
                        io.StringIO('{"hook_event_name": "UserPromptSubmit"}'))
    hobson_entry.main()
    import nudge
    from engines.base import derive_project_label
    assert os.path.isfile(nudge.activity_path(derive_project_label()))


# ── Task 9: the watchdog — notices total silence, pure logic only ─────────


from nudge import watchdog_should_speak  # noqa: E402


def test_speaks_when_activity_has_gone_quiet():
    now = time.time()
    assert watchdog_should_speak(last_event=now - 900, last_stop=now - 1800,
                                 minutes=10, now=now) is True


def test_silent_while_work_is_still_flowing():
    now = time.time()
    assert watchdog_should_speak(last_event=now - 30, last_stop=now - 1800,
                                 minutes=10, now=now) is False


def test_silent_when_the_turn_already_ended():
    """A finished turn is not a hang."""
    now = time.time()
    assert watchdog_should_speak(last_event=now - 900, last_stop=now - 60,
                                 minutes=10, now=now) is False


def test_missing_timestamps_never_raise():
    now = time.time()
    assert watchdog_should_speak(last_event=0, last_stop=0, minutes=10, now=now) is False


def test_watchdog_spawn_is_gated_by_config(claude_home, no_audio, monkeypatch):
    """A commentary flush should ensure a watchdog is running, unless
    watchdog.enabled is False."""
    import phrase_gen
    from engines.say import SayEngine
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I did a thing."))

    eng = SayEngine({
        "engine": "say", "personality": "hobson", "events": ["commentary"],
        "commentary": {"tools": ["bash"], "verbosity": "normal", "cooldown": 0},
        "watchdog": {"enabled": False},
    })
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"description": "run the tests"}})
    assert [c for c in no_audio["popen"] if any("nudge.py" in str(a) for a in c)] == []


def test_watchdog_does_not_spawn_when_one_already_holds_the_lock(
        claude_home, no_audio, monkeypatch):
    """Defense in depth: the parent probes the watchdog's own lock before
    forking a subprocess it already knows would immediately exit."""
    import fcntl
    import os as _os
    import phrase_gen
    from engines.say import SayEngine
    from nudge import _lock_path
    import engines.base as base

    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I did a thing."))
    monkeypatch.setattr(base, "derive_project_label", lambda: "demo")

    lock_path = _lock_path("watchdog", "demo")
    _os.makedirs(_os.path.dirname(lock_path), exist_ok=True)
    held = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        eng = SayEngine({
            "engine": "say", "personality": "hobson", "events": ["commentary"],
            "commentary": {"tools": ["bash"], "verbosity": "normal", "cooldown": 0},
        })
        eng.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"description": "run the tests"}})
        assert _popen_calls(no_audio, "--watchdog") == []
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_watchdog_spawns_on_a_flush_by_default(claude_home, no_audio, monkeypatch):
    import phrase_gen
    from engines.say import SayEngine
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I did a thing."))

    eng = SayEngine({
        "engine": "say", "personality": "hobson", "events": ["commentary"],
        "commentary": {"tools": ["bash"], "verbosity": "normal", "cooldown": 0},
    })
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"description": "run the tests"}})
    watchdog_calls = [c for c in no_audio["popen"]
                      if any("nudge.py" in str(a) for a in c)
                      and any("--watchdog" in str(a) for a in c)]
    assert len(watchdog_calls) == 1


def test_watchdog_spawn_carries_the_parents_in_memory_baseline(
        claude_home, no_audio, monkeypatch):
    """T3: the spawn must carry the parent's in-memory last_event_time, not
    leave the child to re-read a not-yet-saved value from disk — that race
    is why every watchdog used to exit at its first 60s poll."""
    import time
    import phrase_gen
    from engines.say import SayEngine
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I did a thing."))

    before = time.time()
    eng = SayEngine({
        "engine": "say", "personality": "hobson", "events": ["commentary"],
        "commentary": {"tools": ["bash"], "verbosity": "normal", "cooldown": 0},
    })
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"description": "run the tests"}})
    after = time.time()

    watchdog_calls = [c for c in no_audio["popen"]
                      if any("nudge.py" in str(a) for a in c)
                      and any("--watchdog" in str(a) for a in c)]
    assert len(watchdog_calls) == 1
    argv = [str(a) for a in watchdog_calls[0]]
    assert "--baseline" in argv
    baseline = float(argv[argv.index("--baseline") + 1])
    assert before <= baseline <= after


def test_maybe_start_watchdog_omits_baseline_when_none(claude_home, no_audio):
    """Calling with no baseline (the hand-run / legacy path) must not add
    a --baseline flag at all, so _run_watchdog's disk-read fallback kicks
    in instead of parsing a bogus value."""
    from engines.say import SayEngine

    eng = SayEngine({"engine": "say", "personality": "hobson", "events": []})
    eng._maybe_start_watchdog()
    watchdog_calls = [c for c in no_audio["popen"]
                      if any("--watchdog" in str(a) for a in c)]
    assert len(watchdog_calls) == 1
    argv = [str(a) for a in watchdog_calls[0]]
    assert "--baseline" not in argv


def test_main_parses_baseline_and_forwards_to_run_watchdog(monkeypatch):
    import sys
    import nudge as nudge_module

    captured = {}

    def fake_run_watchdog(project, minutes, baseline=None):
        captured["project"] = project
        captured["minutes"] = minutes
        captured["baseline"] = baseline

    monkeypatch.setattr(nudge_module, "_run_watchdog", fake_run_watchdog)
    monkeypatch.setattr(sys, "argv",
                        ["nudge.py", "--watchdog", "--project", "demo",
                         "--minutes", "5", "--baseline", "12345.5"])
    nudge_module.main()
    assert captured == {"project": "demo", "minutes": 5.0, "baseline": 12345.5}



# ── Per-project activity tokens ───────────────────────────────────────────
#
# The token used to be one global file, so typing in ANY session cancelled
# nudges and watchdogs for EVERY project. Working across several tabs, that
# defeated the point: "tab B is waiting on you" went quiet the moment you
# typed in tab A. Keyed per project now, like the lock files beside it.

def test_typing_in_one_project_does_not_cancel_anothers_nudge(claude_home):
    import nudge
    started = time.time() - 10
    with open(nudge.activity_path("project a"), "w") as f:
        f.write(str(time.time()))

    assert nudge.should_continue(nudge.activity_path("project b"), started, time.time())
    assert not nudge.should_continue(nudge.activity_path("project a"), started, time.time())


def test_activity_token_and_lock_share_one_project_key(claude_home):
    """Both are keyed by project; derive them the same way so they cannot drift."""
    import os
    import nudge
    key = lambda p: os.path.basename(p).rsplit("-", 1)[-1].split(".")[0]
    for project in ("hobson", "mobile app, dark mode theming", ""):
        assert key(nudge.activity_path(project)) == key(nudge._lock_path("nudge", project))


def test_distinct_projects_get_distinct_tokens(claude_home):
    import nudge
    assert nudge.activity_path("project a") != nudge.activity_path("project b")


def test_user_prompt_submit_returns_before_loading_config(claude_home, hobson_entry, monkeypatch):
    """CLAUDE.md: UserPromptSubmit is the cheapest path -- it writes the token
    and returns before config or engine load. It used to call load_config()
    first; this pins the documented contract."""
    import io
    monkeypatch.setattr(hobson_entry.sys, "stdin",
                        io.StringIO('{"hook_event_name": "UserPromptSubmit"}'))
    def no_config():
        raise AssertionError("UserPromptSubmit must not load config")
    monkeypatch.setattr(hobson_entry, "load_config", no_config)
    hobson_entry.main()


# ── The parent's argv must be accepted by the child's parser ──────────────
#
# nudge.py runs detached with stderr sent to /dev/null. If the parent passes
# an argument the child's parser does not accept, argparse exits and the
# process dies without a trace -- the same "a feature that never runs" class
# as the watchdog race and the Notification matcher. Round-trip the argv
# engines/base.py actually builds through nudge.main()'s real parser.

def _child_accepts(argv, monkeypatch):
    import sys
    import nudge
    seen = {}
    monkeypatch.setattr(nudge, "_run", lambda *a, **k: seen.setdefault("run", (a, k)))
    monkeypatch.setattr(nudge, "_run_watchdog",
                        lambda *a, **k: seen.setdefault("watchdog", (a, k)))
    script_idx = next(i for i, a in enumerate(argv) if str(a).endswith("nudge.py"))
    monkeypatch.setattr(sys, "argv", ["nudge.py", *argv[script_idx + 1:]])
    nudge.main()  # SystemExit here means the child would have died silently
    return seen


def test_nudge_argv_round_trips_through_the_child_parser(claude_home, no_audio, monkeypatch):
    from engines.say import SayEngine
    eng = SayEngine({"engine": "say", "personality": "hobson", "events": ["notification"]})
    eng.run({"hook_event_name": "Notification", "notification_type": "idle_prompt"})
    [argv] = _popen_calls(no_audio, "nudge.py")
    assert "run" in _child_accepts(argv, monkeypatch)


def test_watchdog_argv_round_trips_through_the_child_parser(claude_home, no_audio, monkeypatch):
    import engines.base as base
    from engines.say import SayEngine
    eng = SayEngine({"engine": "say", "personality": "hobson"})
    eng._maybe_start_watchdog(baseline=1234.5)
    [argv] = _popen_calls(no_audio, "--watchdog")
    seen = _child_accepts(argv, monkeypatch)
    assert "watchdog" in seen
    assert 1234.5 in seen["watchdog"][0] or seen["watchdog"][1].get("baseline") == 1234.5


# ── Realtime engines: a waiting notification is spoken from a template ─────
#
# In ten days of real idle_prompt notifications the model mostly lost the
# meaning -- restating the last completion ("I finished the order history
# view.") or naming the wrong wait ("waiting on your approval") -- and when
# its phrase repeated something recent, the dedup guard silenced the
# announcement outright. The event means one fixed thing; say that.


def _realtime(kind, monkeypatch, spoken, events=("notification", "stop")):
    if kind == "pocket-tts":
        from engines.pocket_tts_realtime import PocketTTSRealtimeEngine as Engine
    else:
        from engines.kokoro_realtime import KokoroRealtimeEngine as Engine
    monkeypatch.setattr(Engine, "speak_dynamic",
                        lambda self, phrase, allow_cold_start=True: spoken.append(phrase))
    monkeypatch.setattr(Engine, "_speak_live",
                        lambda self, phrase, allow_cold_start=True: spoken.append(phrase))
    return Engine({"engine": kind, "personality": "hobson", "events": list(events),
                   "nudge": {"enabled": False}})


_IDLE = {"hook_event_name": "Notification", "notification_type": "idle_prompt",
         "message": "Claude is waiting for your input"}


@pytest.mark.parametrize("kind", ["pocket-tts", "kokoro-realtime"])
def test_a_waiting_notification_skips_the_model(kind, fake_ollama, claude_home, monkeypatch):
    import bark_templates
    spoken = []
    eng = _realtime(kind, monkeypatch, spoken)
    fake_ollama.chat("done | I finished the order history view.")
    eng.run(dict(_IDLE))
    assert fake_ollama.urls == [], "the model is not consulted for a fixed-meaning event"
    assert spoken and spoken[0] in bark_templates.NOTIFICATION_TEMPLATES


def test_the_waiting_template_avoids_one_said_recently(fake_ollama, claude_home, monkeypatch):
    import bark_templates
    import session_state as ss
    from engines.base import derive_project_label
    templates = list(bark_templates.NOTIFICATION_TEMPLATES)
    st = ss.load_session(derive_project_label())
    st["recent_voiced"] = [[t, time.time()] for t in templates[1:]]
    ss.save_session(st)

    spoken = []
    _realtime("pocket-tts", monkeypatch, spoken).run(dict(_IDLE))
    assert spoken == [templates[0]]
    assert ss.load_session(derive_project_label())["recent_voiced"][-1][0] == templates[0]


def test_a_waiting_notification_is_never_silenced_by_repetition(
        fake_ollama, claude_home, monkeypatch):
    import bark_templates
    import session_state as ss
    from engines.base import derive_project_label
    st = ss.load_session(derive_project_label())
    st["recent_voiced"] = [[t, time.time()] for t in bark_templates.NOTIFICATION_TEMPLATES]
    ss.save_session(st)

    spoken = []
    _realtime("pocket-tts", monkeypatch, spoken).run(dict(_IDLE))
    assert len(spoken) == 1, "every template said recently still beats saying nothing"


def test_other_notifications_still_go_to_the_model(fake_ollama, claude_home, monkeypatch):
    spoken = []
    eng = _realtime("pocket-tts", monkeypatch, spoken)
    fake_ollama.chat("done | A background agent wrapped up.")
    eng.run({"hook_event_name": "Notification", "notification_type": "agent_completed",
             "message": "done"})
    assert fake_ollama.urls, "only the blocked-on-you types skip the model"


# ── A nudge only when you are the blocker ──────────────────────────────────
#
# idle_prompt fires whenever the input has sat idle, including after a turn
# that simply finished. All 16 nudges on the first day they worked followed a
# Stop classified "done" -- none a question -- so every finished task you
# walked away from started "I need you for this -- nothing moves until you
# reply", which was not true.


def _seed_last_turn(category):
    import session_state as ss
    from engines.base import derive_project_label
    st = ss.load_session(derive_project_label())
    st["last_stop_category"] = category
    ss.save_session(st)


def _say_engine():
    from engines.say import SayEngine
    return SayEngine({"engine": "say", "personality": "hobson", "events": ["notification", "stop"]})


def test_idle_after_a_finished_turn_does_not_nudge(claude_home, no_audio):
    _seed_last_turn("done")
    _say_engine().run(dict(_IDLE))
    assert _popen_calls(no_audio, "nudge.py") == []


@pytest.mark.parametrize("category", ["question", "broken"])
def test_idle_after_a_question_or_a_failure_nudges(category, claude_home, no_audio):
    _seed_last_turn(category)
    _say_engine().run(dict(_IDLE))
    assert len(_popen_calls(no_audio, "nudge.py")) == 1


def test_idle_after_an_unclassified_turn_still_nudges(claude_home, no_audio):
    """Unknown is not "done": a failed classification must not switch nudges off."""
    _seed_last_turn(None)
    _say_engine().run(dict(_IDLE))
    assert len(_popen_calls(no_audio, "nudge.py")) == 1


def test_idle_while_the_agent_waits_on_its_own_work_does_not_nudge(claude_home, no_audio):
    """Its subagents or build will wake it; you are not what it waits for."""
    _seed_last_turn("working")
    _say_engine().run(dict(_IDLE))
    assert _popen_calls(no_audio, "nudge.py") == []


def test_a_background_agent_waiting_nudges_even_after_a_finished_turn(claude_home, no_audio):
    _seed_last_turn("done")
    _say_engine().run({"hook_event_name": "Notification", "notification_type": "agent_needs_input"})
    assert len(_popen_calls(no_audio, "nudge.py")) == 1


def _last_category():
    import session_state as ss
    from engines.base import derive_project_label
    return ss.load_session(derive_project_label()).get("last_stop_category")


def test_a_static_stop_records_its_raw_category(claude_home, no_audio, monkeypatch, tmp_path):
    import json as _json
    import engines.base as base
    t = tmp_path / "t.jsonl"
    t.write_text(_json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Both approaches are sketched out."}]}}), encoding="utf-8")
    monkeypatch.setattr(base, "classify", lambda *a, **k: "question")
    _say_engine().run({"hook_event_name": "Stop", "transcript_path": str(t)})
    assert _last_category() == "question"

    monkeypatch.setattr(base, "classify", lambda *a, **k: None)  # Ollama down
    _say_engine().run({"hook_event_name": "Stop", "transcript_path": str(t)})
    assert _last_category() is None, "the 'done' it barks by default is not a classification"


def test_a_static_stop_that_asks_is_a_question_without_ollama(
        claude_home, no_audio, monkeypatch, tmp_path):
    """The model called 45 of 46 question-ending Stops "done" -- which also
    told the nudge the turn had finished."""
    import json as _json
    import engines.base as base
    t = tmp_path / "t.jsonl"
    t.write_text(_json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Both are sketched out. **Which one do you want?**"}]}}),
        encoding="utf-8")
    monkeypatch.setattr(base, "classify", lambda *a, **k: "done")
    _say_engine().run({"hook_event_name": "Stop", "transcript_path": str(t)})
    assert _last_category() == "question"


_STILL_WORKING = "Two runs in flight. Waiting on their notifications before the last pair."


def _transcript(tmp_path, text):
    import json as _json
    t = tmp_path / "t.jsonl"
    t.write_text(_json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}), encoding="utf-8")
    return str(t)


def test_a_static_stop_waiting_on_its_own_work_is_not_announced(
        claude_home, no_audio, monkeypatch, tmp_path):
    """Announced as done, a coordinator's every launch buried its one real
    completion: "Rendering hard-case pairs complete", four times in five
    minutes, while the runs were still going."""
    import engines.base as base
    monkeypatch.setattr(base, "classify", lambda *a, **k: pytest.fail("no model call"))
    eng = _say_engine()
    monkeypatch.setattr(eng, "try_bark", lambda phrase: pytest.fail(f"barked {phrase!r}"))
    eng.run({"hook_event_name": "Stop", "transcript_path": _transcript(tmp_path, _STILL_WORKING)})
    assert _last_category() == "working"


@pytest.mark.parametrize("kind", ["pocket-tts", "kokoro-realtime"])
def test_a_realtime_stop_waiting_on_its_own_work_is_not_announced(
        kind, fake_ollama, claude_home, monkeypatch, tmp_path):
    spoken = []
    eng = _realtime(kind, monkeypatch, spoken)
    fake_ollama.chat("done | I finished rendering the hard-case pairs.")
    eng.run({"hook_event_name": "Stop", "transcript_path": _transcript(tmp_path, _STILL_WORKING)})
    assert spoken == [] and fake_ollama.urls == []
    assert _last_category() == "working"


def test_a_realtime_stop_records_its_category(fake_ollama, claude_home, monkeypatch, tmp_path):
    import json as _json
    t = tmp_path / "t.jsonl"
    t.write_text(_json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Which approach do you want?"}]}}), encoding="utf-8")
    spoken = []
    eng = _realtime("pocket-tts", monkeypatch, spoken)
    fake_ollama.chat("question | Which approach do you want?")
    eng.run({"hook_event_name": "Stop", "transcript_path": str(t)})
    assert _last_category() == "question"


# ── The watchdog: waiting on you is not a stall ────────────────────────────
#
# Replayed over 508 real sessions, the watchdog would have said "nothing's
# moved" 53 times and 6 of those were real stalls. ~36 were Claude waiting
# for an answer to a question it asked (AskUserQuestion), ~9 were stretches
# of tools that never reached session state (Read, WebSearch, MCP tools,
# subagents), and 4 were builds within their own Bash timeout. With every
# tool call marking the session alive, user waits exempt, and a Bash call's
# own timeout respected, the same replay fires 13 times -- each a 13-minute
# to 12-hour silence after a call that never returned.

import nudge  # noqa: E402


def test_patience_is_the_configured_minutes_by_default():
    assert nudge.watchdog_patience(10, {"tool": "Edit"}) == 600
    assert nudge.watchdog_patience(10, {}) == 600


@pytest.mark.parametrize("alive", [
    {"event": "PreToolUse", "tool": "AskUserQuestion"},
    {"event": "PreToolUse", "tool": "ExitPlanMode"},
    {"event": "PermissionRequest", "tool": "Bash"},
])
def test_no_patience_limit_while_the_session_waits_on_you(alive):
    assert nudge.watchdog_patience(10, alive) is None


def test_a_long_bash_gets_its_own_timeout_plus_a_minute():
    assert nudge.watchdog_patience(10, {"tool": "Bash", "timeout_ms": 900000}) == 960
    assert nudge.watchdog_patience(10, {"tool": "Bash", "timeout_ms": 30000}) == 600


def _verdict(**kw):
    now = time.time()
    base = dict(state={"last_event_time": now - 900, "last_stop_time": now - 3600},
                alive={}, baseline=now - 900, minutes=10, now=now)
    base.update(kw)
    return nudge.watchdog_verdict(**base)


def test_verdict_speaks_on_a_real_stall():
    assert _verdict() == "speak"


def test_verdict_is_resumed_once_any_tool_ran():
    """A Read the commentary path never records still means the session moved."""
    assert _verdict(alive={"ts": time.time() - 5, "tool": "Read"}) == "resumed"


def test_verdict_waits_while_a_question_is_open():
    t = time.time() - 900
    assert _verdict(state={"last_event_time": t}, alive={"ts": t, "tool": "AskUserQuestion"},
                    baseline=t) == "wait"


def test_verdict_waits_through_a_long_build():
    t = time.time() - 700
    alive = {"ts": t, "tool": "Bash", "timeout_ms": 900000}
    assert _verdict(state={"last_event_time": t}, alive=alive, baseline=t) == "wait"


def test_alive_round_trips_per_project(claude_home):
    nudge.record_alive("proj-a", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                  "tool_input": {"command": "gradle build", "timeout": 900000}})
    got = nudge.read_alive("proj-a")
    assert got["tool"] == "Bash" and got["timeout_ms"] == 900000 and got["event"] == "PreToolUse"
    assert "gradle" not in json.dumps(got), "only the shape of the call, never its contents"
    assert nudge.read_alive("proj-b") == {}


def test_a_corrupt_alive_file_reads_as_nothing(claude_home):
    with open(nudge.alive_path("proj"), "w") as f:
        f.write("{not json")
    assert nudge.read_alive("proj") == {}


@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
def test_the_entrypoint_marks_the_session_alive(event, claude_home, hobson_entry, monkeypatch, no_audio):
    import io
    from engines.base import derive_project_label
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO(json.dumps(
        {"hook_event_name": event, "tool_name": "Read", "agent_id": "sub-1"})))
    hobson_entry.main()
    assert nudge.read_alive(derive_project_label())["event"] == event


# ── A question dialog is announced as a question ───────────────────────────
#
# AskUserQuestion reaches hobson as a PermissionRequest -- 87 of them in the
# log -- and was phrased by the model from "Tool: AskUserQuestion" alone:
# "I finished fixing hobson.", "I'm applying review fixes." The static
# engines said their permission template for a tool called AskUserQuestion.


def _question_phrases():
    import bark_templates
    group = bark_templates.CATEGORIES["question"]
    return {f"{lead} {tail}" for lead in group["lead"] for tail in group["tail"]}


_ASK = {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Which approach?"}]}}


@pytest.mark.parametrize("tool", ["AskUserQuestion", "ExitPlanMode"])
def test_a_static_engine_announces_a_question_dialog_as_a_question(tool, claude_home, no_audio):
    from engines.say import SayEngine
    SayEngine({"engine": "say", "personality": "hobson", "events": ["permission"]}).run(
        {**_ASK, "tool_name": tool})
    spoken = " ".join(map(str, no_audio["say"]))
    assert any(p in spoken for p in _question_phrases())
    assert tool not in spoken


@pytest.mark.parametrize("kind", ["pocket-tts", "kokoro-realtime"])
def test_a_realtime_engine_announces_it_without_the_model(kind, fake_ollama, claude_home, monkeypatch):
    spoken = []
    eng = _realtime(kind, monkeypatch, spoken, events=["permission"])
    fake_ollama.chat("done | I finished fixing hobson.")
    eng.run(dict(_ASK))
    assert fake_ollama.urls == []
    assert len(spoken) == 1 and spoken[0] in _question_phrases()


def test_the_question_line_rotates_past_what_was_just_said(fake_ollama, claude_home, monkeypatch):
    import session_state as ss
    from engines.base import derive_project_label
    phrases = sorted(_question_phrases())
    st = ss.load_session(derive_project_label())
    st["recent_voiced"] = [[p, time.time()] for p in phrases[:6]]
    ss.save_session(st)
    spoken = []
    eng = _realtime("pocket-tts", monkeypatch, spoken, events=["permission"])
    eng.run(dict(_ASK))
    assert spoken and spoken[0] not in phrases[:6]


def test_an_ordinary_permission_still_goes_to_the_model(fake_ollama, claude_home, monkeypatch):
    spoken = []
    eng = _realtime("pocket-tts", monkeypatch, spoken, events=["permission"])
    fake_ollama.chat("question | I need approval to run git status.")
    eng.run({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
             "tool_input": {"command": "git status"}})
    assert fake_ollama.urls, "only question dialogs skip the model"


# ── Nor the one-off "waiting" line, after a finished turn ─────────────────
#
# With nudges gated, each finished turn you walked away from still got two
# announcements: "I finished X", then a minute later "Sir. The terminal
# requires you." 16 of the 18 waiting lines in the two days after the
# template change followed a Stop classified "done".


def test_a_static_engine_says_nothing_for_idle_after_a_finished_turn(claude_home, no_audio):
    _seed_last_turn("done")
    _say_engine().run(dict(_IDLE))
    assert no_audio["say"] == [] and _popen_calls(no_audio, "nudge.py") == []


@pytest.mark.parametrize("category", ["question", "broken", None])
def test_a_static_engine_still_announces_idle_after_anything_else(category, claude_home, no_audio):
    _seed_last_turn(category)
    _say_engine().run(dict(_IDLE))
    assert no_audio["say"]


def test_a_realtime_engine_says_nothing_for_idle_after_a_finished_turn(fake_ollama, claude_home, monkeypatch):
    _seed_last_turn("done")
    spoken = []
    _realtime("pocket-tts", monkeypatch, spoken).run(dict(_IDLE))
    assert spoken == [] and fake_ollama.urls == [], "nor may it fall through to the model"


def test_a_realtime_engine_still_announces_idle_after_a_question(fake_ollama, claude_home, monkeypatch):
    _seed_last_turn("question")
    spoken = []
    _realtime("pocket-tts", monkeypatch, spoken).run(dict(_IDLE))
    assert len(spoken) == 1


def test_a_background_agent_waiting_is_announced_even_after_a_finished_turn(
        fake_ollama, claude_home, monkeypatch):
    _seed_last_turn("done")
    spoken = []
    _realtime("pocket-tts", monkeypatch, spoken).run(
        {"hook_event_name": "Notification", "notification_type": "agent_needs_input"})
    assert len(spoken) == 1
