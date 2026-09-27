"""Session state under overlapping hooks.

Every hook is async, so hooks for one project overlap: parallel tool calls,
a commentary flush waiting on Ollama, a Stop being phrased. A hook that
loaded the state, waited on a model and then saved its copy threw away
whatever the others wrote in the meantime -- queued tool calls, a phrase
just spoken, a Stop's category. And a reader that caught the file between
truncate and write read "no session", then saved one.
"""

import fcntl
import os
import subprocess
import sys
import time

import pytest

import home
import session_state as ss

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
# The autouse no_audio fixture replaces subprocess.Popen for every test;
# these children must really run. Captured at import, before any fixture.
_REAL_POPEN = subprocess.Popen


# ── transaction() ───────────────────────────────────────────────────────────

def test_a_transaction_saves_what_it_changed(claude_home):
    with ss.transaction("demo") as st:
        ss.record_pending(st, "Bash", "run the tests")
    assert ss.load_session("demo")["pending"][0]["context"] == "run the tests"


def test_a_transaction_that_raises_saves_nothing(claude_home):
    with pytest.raises(RuntimeError):
        with ss.transaction("demo") as st:
            ss.record_pending(st, "Bash", "run the tests")
            raise RuntimeError("halfway")
    assert ss.load_session("demo")["pending"] == []


def test_interleaved_hooks_keep_both_writes(claude_home):
    """What a slow hook does to the state lands on the state as it is when
    it commits, not as it was when the hook began."""
    with ss.transaction("demo") as st:
        ss.record_pending(st, "Bash", "first")
    # ...a model call here, during which another hook commits...
    with ss.transaction("demo") as st:
        ss.record_pending(st, "Edit", "second")
    with ss.transaction("demo") as st:
        ss.record_voiced(st, "I ran the tests.")
    st = ss.load_session("demo")
    assert [p["context"] for p in st["pending"]] == ["first", "second"]
    assert st["recent_voiced"][0][0] == "I ran the tests."


def test_parallel_processes_lose_no_write(claude_home):
    """Eight hooks at once, 25 tool calls each: every one is counted."""
    child = (
        "import sys; sys.path.insert(0, sys.argv[1]); import session_state as ss\n"
        "for i in range(25):\n"
        "    with ss.transaction('demo') as st:\n"
        "        ss.record_pending(st, 'Bash', sys.argv[2] + str(i))\n"
    )
    procs = [_REAL_POPEN([sys.executable, "-c", child, SCRIPTS, f"p{n}-"],
                              env={**os.environ, "HOME": str(claude_home.parent)})
             for n in range(8)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert ss.load_session("demo")["total_events"] == 200


def test_a_busy_lock_is_waited_for_then_given_up_on(claude_home, monkeypatch):
    """A hook must never hang behind a stuck one; it goes ahead unlocked
    and says so."""
    monkeypatch.setattr(ss, "LOCK_TIMEOUT_SECONDS", 0.05)
    os.makedirs(home.sessions_dir(), exist_ok=True)
    holder = open(ss._lock_path("demo"), "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with ss.transaction("demo") as st:
            ss.record_pending(st, "Bash", "anyway")
        assert time.monotonic() - started < 2
    finally:
        holder.close()
    assert ss.load_session("demo")["pending"][0]["context"] == "anyway"
    assert "session lock busy" in (claude_home / "hobson.log").read_text()


# ── save_session() never leaves half a file ─────────────────────────────────

def test_a_save_that_fails_midway_leaves_the_last_state(claude_home, monkeypatch):
    st = ss.load_session("demo")
    st["total_events"] = 7
    ss.save_session(st)

    def dump_then_fail(obj, f, **k):
        f.write('{"project": "de')
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(ss.json, "dump", dump_then_fail)
        with pytest.raises(OSError):
            ss.save_session(st)
    assert ss.load_session("demo")["total_events"] == 7
    assert [f for f in os.listdir(home.sessions_dir()) if ".tmp" in f] == []


# ── the keys it owns ────────────────────────────────────────────────────────

def test_a_fresh_session_knows_its_timestamps_and_last_stop():
    st = ss._fresh_state("demo")
    assert st["last_event_time"] == 0.0 and st["last_stop_time"] == 0.0
    assert st["last_stop_category"] is None


def test_begin_stop_drops_the_queue_and_forgets_the_last_category():
    st = ss._fresh_state("demo")
    ss.record_pending(st, "Bash", "a")
    ss.record_pending(st, "Edit", "b")
    ss.record_stop_category(st, "done")
    assert ss.begin_stop(st, now=100.0) == 2
    assert st["pending"] == []
    assert ss.stop_category(st) is None, "a stale 'done' must not stand in for this turn"
    assert st["last_stop_time"] == 100.0 and st["last_event_time"] == 100.0


def test_mark_event_moves_the_liveness_clock():
    st = ss._fresh_state("demo")
    ss.mark_event(st, now=42.0)
    assert st["last_event_time"] == 42.0


# ── the engines keep other hooks' writes ────────────────────────────────────

def _pocket(monkeypatch, spoken, **commentary):
    from engines.pocket_tts_realtime import PocketTTSRealtimeEngine
    monkeypatch.setattr(PocketTTSRealtimeEngine, "speak_dynamic",
                        lambda self, phrase, allow_cold_start=False, **k: spoken.append(phrase))
    monkeypatch.setattr(PocketTTSRealtimeEngine, "_speak_live",
                        lambda self, phrase, allow_cold_start=False, **k: spoken.append(phrase))
    return PocketTTSRealtimeEngine({
        "engine": "pocket-tts", "personality": "hobson",
        "events": ["commentary", "stop", "permission", "notification"],
        "nudge": {"enabled": False}, "watchdog": {"enabled": False},
        "commentary": {"tools": ["bash", "edit"], "verbosity": "normal", "cooldown": 0,
                       "min_tool_calls": 3, "min_seconds": 600.0, **commentary},
    })


def _while_generating(monkeypatch, meanwhile, reply=("done", "I edited three files.")):
    """Run `meanwhile` -- another hook -- while the model is thinking."""
    import phrase_gen

    def slow_model(*a, **k):
        meanwhile()
        return reply

    monkeypatch.setattr(phrase_gen, "generate_or_skip", slow_model)


def _project():
    from home import derive_project_label
    return derive_project_label()


def test_a_commentary_flush_keeps_tool_calls_queued_while_it_spoke(claude_home, monkeypatch):
    spoken = []
    eng = _pocket(monkeypatch, spoken)
    with ss.transaction(_project()) as st:
        st["last_flush_time"] = st["last_voiced_time"] = time.time()
    other = _pocket(monkeypatch, spoken)  # one class: its patches are shared
    _while_generating(monkeypatch, lambda: other.run(
        {"hook_event_name": "PreToolUse", "tool_name": "Edit",
         "tool_input": {"file_path": "/repo/queued_meanwhile.py"}}))
    for name in ("a.py", "b.py", "c.py"):
        eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                 "tool_input": {"file_path": f"/repo/{name}"}})
    assert spoken == ["I edited three files."]
    st = ss.load_session(_project())
    assert [p["context"] for p in st["pending"]] == ["queued_meanwhile.py"]
    assert st["recent_voiced"][-1][0] == "I edited three files."


def test_a_stop_keeps_a_phrase_spoken_while_it_was_phrased(claude_home, monkeypatch, tmp_path):
    import json
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Pushed the branch. All tests pass."}]}}), encoding="utf-8")

    def meanwhile():  # another hook, as every hook wrote before transactions
        st = ss.load_session(_project())
        ss.record_voiced(st, "Said by another hook.")
        ss.save_session(st)

    _while_generating(monkeypatch, meanwhile, reply=("done", "I pushed the branch."))
    _pocket(monkeypatch, []).run({"hook_event_name": "Stop", "transcript_path": str(t)})
    st = ss.load_session(_project())
    assert [v[0] for v in st["recent_voiced"]] == ["Said by another hook.", "I pushed the branch."]
    assert ss.stop_category(st) == "done"


def test_a_permission_request_keeps_what_others_wrote_while_it_was_phrased(
        claude_home, monkeypatch):
    def meanwhile():  # another hook, as every hook wrote before transactions
        st = ss.load_session(_project())
        ss.record_pending(st, "Bash", "queued meanwhile")
        ss.save_session(st)

    _while_generating(monkeypatch, meanwhile, reply=("question", "I need your approval here."))
    spoken = []
    _pocket(monkeypatch, spoken).run({"hook_event_name": "PermissionRequest", "tool_name": "Edit"})
    assert spoken == ["I need your approval here."]
    st = ss.load_session(_project())
    assert [p["context"] for p in st["pending"]] == ["queued meanwhile"]
