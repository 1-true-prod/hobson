"""What each session is doing, as far as you are concerned (session_status.py):
read from the four stores it is written to, and nowhere else."""

import fcntl
import json
import time

import pytest

import nudge
import session_status

def _session(project, **fields):
    import session_state
    with session_state.transaction(project) as state:
        state.update(fields)


def test_an_unanswered_question_waits(claude_home):
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    assert session_status.waiting_on_you() == [("webapp", "answer")]


def test_an_answered_question_does_not(claude_home):
    import nudge
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    with open(nudge.activity_path("webapp"), "w") as f:
        f.write(str(time.time()))
    assert session_status.waiting_on_you() == []


def test_a_fresh_permission_request_waits_and_an_old_one_does_not(claude_home):
    import nudge
    _session("webapp", last_stop_time=time.time() - 900)
    nudge.record_alive("webapp", {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    assert session_status.waiting_on_you() == [("webapp", "approval")]
    assert session_status.waiting_on_you(now=time.time() + session_status.PERMISSION_WAIT_SECONDS + 5) == []


def test_a_finished_turn_is_not_waiting(claude_home):
    _session("webapp", last_stop_category="done", last_stop_time=time.time() - 30)
    assert session_status.waiting_on_you() == []


def test_a_running_nudge_waits_on_you(claude_home):
    _session("webapp", last_stop_category="done", last_stop_time=time.time() - 30)
    path = nudge.lock_path("nudge", "webapp")
    with open(path, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert session_status.waiting_on_you() == [("webapp", "you")]
    assert session_status.waiting_on_you() == []


def test_a_session_file_from_an_older_version_is_read(claude_home):
    """Read through session_state's schema, not raw: missing fields and
    bare-string phrases do not drop the session."""
    d = claude_home / "hobson-sessions"
    d.mkdir(exist_ok=True)
    (d / "old.json").write_text(json.dumps({
        "project": "webapp", "recent_voiced": ["I fixed it."],
        "last_stop_category": "question", "last_stop_time": time.time() - 30}))
    (d / "junk.json").write_text("{not json")
    assert session_status.waiting_on_you() == [("webapp", "answer")]


def test_an_unnamed_session_can_wait_but_is_not_reported_otherwise(claude_home):
    now = time.time()
    _session("unknown", last_stop_category="question", last_stop_time=now - 30)
    _session("", last_stop_category="done", last_stop_time=now - 30, last_event_time=now - 30)
    read = session_status.reading(now)
    assert read["waiting"] == [("unknown", "answer")]
    assert read["finished"] == []


@pytest.mark.parametrize("category, settled", [("done", True), ("working", True),
                                               ("question", False), ("broken", False),
                                               (None, False)])
def test_idle_after_a_settled_turn(category, settled, claude_home):
    state = {"last_stop_category": category}
    idle = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}
    asks = {"hook_event_name": "Notification", "notification_type": "agent_needs_input"}
    assert session_status.idle_after_settled_turn(idle, state) is settled
    assert session_status.idle_after_settled_turn(asks, state) is False


def test_a_permission_prompt_waits_but_does_not_nudge():
    prompt = {"hook_event_name": "Notification", "notification_type": "permission_prompt"}
    assert "permission_prompt" in session_status.WAITING_NOTIFICATIONS
    assert set(nudge.WAITING_TYPES) < set(session_status.WAITING_NOTIFICATIONS)
    assert session_status.nudge_type(prompt) is None


@pytest.mark.parametrize("tool, question", [("AskUserQuestion", True), ("ExitPlanMode", True),
                                            ("Bash", False)])
def test_a_question_dialog(tool, question):
    assert session_status.is_question_dialog(
        {"hook_event_name": "PermissionRequest", "tool_name": tool}) is question
    assert not session_status.is_question_dialog({"hook_event_name": "PreToolUse", "tool_name": tool})


def test_the_menu_lists_every_session_most_urgent_first(claude_home):
    now = time.time()
    _session("finished-one", last_stop_category="done", last_stop_time=now - 600)
    _session("working-one", last_stop_time=now - 300, last_event_time=now - 5)
    _session("failed-one", last_stop_category="broken", last_stop_time=now - 900)
    _session("asking-one", last_stop_category="question", last_stop_time=now - 30)
    nudge.record_alive("approving-one", {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    _session("approving-one", last_stop_time=now - 900)
    menu = session_status.menu_json(now)
    assert menu["waiting"] == 2
    rows = [(r["state"], r["text"]) for r in menu["rows"]]
    assert rows[:2] == [
        ("waiting", "approving-one — waiting on your approval"),
        ("waiting", "asking-one — waiting on an answer from you"),
    ]
    assert rows[2:] == [
        ("failed", "failed-one — failed"),
        ("working", "working-one — working"),
        ("finished", "finished-one — finished"),
    ]


def test_the_menu_of_no_sessions_is_empty(claude_home):
    assert session_status.menu_json() == {"waiting": 0, "rows": []}


def test_the_script_prints_the_menu_as_json(claude_home, capsys, monkeypatch):
    import runpy
    _session("asking-one", last_stop_category="question", last_stop_time=time.time() - 30)
    monkeypatch.setattr("sys.argv", ["session_status.py", "--json"])
    runpy.run_path(session_status.__file__, run_name="__main__")
    assert json.loads(capsys.readouterr().out)["rows"][0]["text"] == "asking-one \u2014 waiting on an answer from you"


def test_waiting_sessions_come_most_recent_first(claude_home):
    import home
    now = time.time()
    first, second = sorted(["older", "newer"], key=home.project_key)
    _session(first, last_stop_category="question", last_stop_time=now - 300)
    _session(second, last_stop_category="question", last_stop_time=now - 20)
    assert [p for p, _ in session_status.waiting_on_you(now)] == [second, first]


def test_a_session_without_a_project_is_not_a_row(claude_home):
    now = time.time()
    _session("", last_stop_category="question", last_stop_time=now - 20)
    _session("asking-one", last_stop_category="question", last_stop_time=now - 30)
    menu = session_status.menu_json(now)
    assert [r["project"] for r in menu["rows"]] == ["asking-one"]
    assert menu["waiting"] == 1


def test_an_unknown_need_still_has_a_row(claude_home, monkeypatch):
    monkeypatch.setattr(session_status, "reading", lambda now=None: {
        "waiting": [("webapp", "something-new")], "failed": [], "finished": [], "working": []})
    assert session_status.menu_json()["rows"][0]["text"] == "webapp \u2014 waiting on you"
