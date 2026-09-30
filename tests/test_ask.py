"""Ask Hobson: what needs me, what failed, what have you been doing.

The menu item itself (presence/main.swift) is checked by hand, with the
rest of the helper.
"""

import fcntl
import json
import threading
import time

import pytest

import ask
import presence

NOW = 1_800_000_000.0


def _session(claude_home, project, **fields):
    d = claude_home / "hobson-sessions"
    d.mkdir(exist_ok=True)
    state = {"project": project, "recent_voiced": [], "last_event_time": 0.0,
             "last_stop_time": 0.0, "last_stop_category": None, **fields}
    (d / f"{abs(hash(project))}.json").write_text(json.dumps(state))


def _config(**extra):
    return {"engine": "say", "personality": "hobson", **extra}


# ── What needs me? ──────────────────────────────────────────────────────────

def test_nothing_at_all_is_all_quiet(claude_home):
    assert ask.compose_status(ask.reading(NOW), []) == ask.QUIET_LINE


def test_each_session_is_read_once_most_urgent_first(claude_home):
    _session(claude_home, "webapp", last_stop_time=NOW - 60, last_event_time=NOW - 60,
             last_stop_category="question")                       # waiting on your answer
    _session(claude_home, "mobile app", last_stop_time=NOW - 600, last_event_time=NOW - 600,
             last_stop_category="broken")                         # failed, nothing since
    _session(claude_home, "docs", last_stop_time=NOW - 900, last_event_time=NOW - 900,
             last_stop_category="done")                           # finished
    _session(claude_home, "hobson", last_stop_time=NOW - 900, last_event_time=NOW - 30,
             last_stop_category="done")                           # at work again since
    _session(claude_home, "old", last_stop_time=NOW - 5 * 3600, last_event_time=NOW - 5 * 3600,
             last_stop_category="broken")                         # history
    read = ask.reading(NOW)
    assert read == {"waiting": [("webapp", "answer")], "failed": ["mobile app"],
                    "finished": ["docs"], "working": ["hobson"]}
    assert ask.compose_status(read, []) == (
        "Webapp is waiting on an answer from you. Mobile app stopped on a failure. "
        "Docs finished, and hobson is still working.")


def test_two_are_told_and_the_rest_counted():
    read = {"waiting": [("a", "you"), ("b", "approval")], "failed": ["c", "d"],
            "finished": ["e", "f", "g"], "working": []}
    assert ask.compose_status(read, []) == (
        "A is waiting on you. B is waiting on your approval. Plus 2 more. "
        "Three sessions finished.")


def test_nothing_urgent_says_so_first():
    read = {"waiting": [], "failed": [], "finished": ["docs"], "working": ["x", "y"]}
    assert ask.compose_status(read, []) == "Nothing needs you. Docs finished, and two are still working."


def test_what_was_held_is_told_once_and_a_held_wait_not_twice():
    read = {"waiting": [("webapp", "answer")], "failed": [], "finished": [], "working": []}
    held = [{"kind": "waiting", "project": "webapp", "phrase": "A question is waiting", "ts": 1},
            {"kind": "done", "project": "docs", "phrase": "I wrote the README", "ts": 2}]
    assert ask.compose_status(read, held) == (
        "Webapp is waiting on an answer from you. On docs: I wrote the README.")


def test_asking_takes_the_held_items_off_the_salver(claude_home, no_audio):
    presence.hold("docs", "done", "I wrote the README.")
    text, how = ask.compose("status", _config(), now=time.time())
    assert text == "On docs: I wrote the README." and "1 held" in how
    assert presence.held() == []


# ── What failed? ────────────────────────────────────────────────────────────

def test_each_failure_with_what_was_said_of_it(claude_home):
    lines = [
        "[2026-01-15 08:00:00] [mobile app] [pocket-tts] [Stop] (m) -> broken -> 'The API build failed.'",
        "[2026-01-15 08:00:01] [mobile app] [pocket-tts] barked (daemon-live) [broken] -> 'The API build failed.'",
        "[2026-01-15 08:01:00] [webapp] [pocket-tts] [Stop] (m) -> done -> 'Not a failure.'",
    ]
    now = time.mktime((2026, 1, 15, 8, 5, 0, 0, 0, -1))
    read = {"waiting": [], "failed": ["mobile app", "webapp"], "finished": [], "working": []}
    assert ask.compose_failed(read, lines, now) == (
        "On mobile app: The API build failed. Webapp stopped on a failure.")
    assert ask.compose_failed({**read, "failed": []}, lines, now) == ask.NO_FAILURES


# ── Answering ───────────────────────────────────────────────────────────────

def test_an_answer_is_spoken_after_one_moment_and_never_held(claude_home, no_audio, no_face):
    presence.hold("docs", "done", "I wrote the README.")
    (claude_home / "hobson-presence.json").write_text(json.dumps(
        {"state": "away", "source": "lock", "ts": time.time(), "pid": 0}))
    text = ask.answer("status", config=_config(presence={"enabled": True}))
    assert text == "On docs: I wrote the README."
    assert no_face[0] == (ask.THINKING_LINE, "thinking", None)
    assert no_audio["say"] == [text]  # away, and spoken all the same


def test_muted_it_is_shown_and_not_spoken_even_with_the_face_off(claude_home, no_audio, no_face):
    text = ask.answer("status", config=_config(muted=True, face={"enabled": False}))
    assert no_audio["say"] == []
    assert no_face == [(ask.THINKING_LINE, "thinking", None, "asked"), (text, "answer", None, "asked")]
    assert "[ask] muted (muted): shown on the face, not spoken" in (claude_home / "hobson.log").read_text()


def test_with_the_face_off_no_one_moment_is_left_on_screen(claude_home, no_audio, no_face):
    ask.answer("status", config=_config(face={"enabled": False}))
    assert no_face == [] and no_audio["say"] == [ask.QUIET_LINE]


def test_a_recap_that_fails_still_answers(claude_home, no_audio, monkeypatch):
    import recap
    monkeypatch.setattr(recap, "across_projects", lambda lines, config, now=None: (None, ["hobson failed (down) after 1.0s"]))
    assert ask.answer("recap", config=_config()) == ask.RECAP_FAILED
    log = (claude_home / "hobson.log").read_text()
    assert "recap failed: hobson failed (down) after 1.0s" in log
    monkeypatch.setattr(recap, "across_projects", lambda lines, config, now=None: ("", []))
    assert ask.answer("recap", config=_config()) == ask.NOTHING_DONE


def test_a_busy_bark_lock_delays_an_answer_rather_than_dropping_it(claude_home):
    lock = open(claude_home / "hobson.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    threading.Timer(0.3, lambda: (fcntl.flock(lock, fcntl.LOCK_UN), lock.close())).start()
    began = time.time()
    assert ask._wait_for_the_bark_lock(timeout=3)
    assert 0.2 < time.time() - began < 2.5


def test_a_lock_that_stays_busy_gives_up_in_time(claude_home):
    lock = open(claude_home / "hobson.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        assert not ask._wait_for_the_bark_lock(timeout=0.3)
    finally:
        lock.close()


def test_every_step_is_logged_and_none_reads_as_speech(claude_home, no_audio):
    import log_record
    ask.answer("failed", source="menu", config=_config())
    lines = (claude_home / "hobson.log").read_text().splitlines()
    ask_lines = [line for line in lines if "[ask]" in line]
    assert "[ask] asked 'What failed?' (menu)" in ask_lines[0]
    assert "[ask] answer to 'What failed?' in" in ask_lines[1] and repr(ask.NO_FAILURES) in ask_lines[1]
    assert all(log_record.tail_phrase(r.body) is None
               for r in log_record.records(ask_lines))


def test_the_menu_asks_through_presence(claude_home, monkeypatch):
    asked = []
    monkeypatch.setattr(ask, "answer", lambda q, source="menu": asked.append((q, source)))
    presence.main(["--ask", "recap"])
    assert asked == [("recap", "menu")]


def test_an_unknown_question_is_answered_as_status(claude_home, no_audio):
    assert ask.answer("anything", config=_config()) == ask.QUIET_LINE


@pytest.mark.parametrize("kind", ["answer", "thinking"])
def test_an_answer_names_no_project_on_the_face(kind):
    import face
    record = face.compose({}, "On webapp: fixed it.", kind, None, "hobson", NOW)
    assert record["project"] is None
