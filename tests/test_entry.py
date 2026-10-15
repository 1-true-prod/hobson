"""hobson.py main() entrypoint: stdin dispatch + muted short-circuit."""

import io
import json


def _write_cfg(claude, data):
    (claude / "hobson.json").write_text(json.dumps(data), encoding="utf-8")


def test_main_dispatches_to_engine(claude_home, hobson_entry, monkeypatch):
    _write_cfg(claude_home, {"engine": "say", "muted": False})
    seen = {}

    class FakeEngine:
        def run(self, hook_input):
            seen["hook"] = hook_input

    monkeypatch.setattr(hobson_entry, "load_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(hobson_entry.sys, "stdin",
                        io.StringIO('{"hook_event_name": "Stop"}'))
    hobson_entry.main()
    assert seen["hook"] == {"hook_event_name": "Stop"}


def test_main_muted_short_circuits(claude_home, hobson_entry, monkeypatch):
    _write_cfg(claude_home, {"engine": "say", "muted": True})
    called = {"n": 0}
    monkeypatch.setattr(hobson_entry, "load_engine",
                        lambda cfg: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO("{}"))
    hobson_entry.main()
    assert called["n"] == 0


def test_main_handles_bad_stdin(claude_home, hobson_entry, monkeypatch):
    _write_cfg(claude_home, {"engine": "say", "muted": False})
    seen = {}

    class FakeEngine:
        def run(self, hook_input):
            seen["hook"] = hook_input

    monkeypatch.setattr(hobson_entry, "load_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO("not json at all"))
    hobson_entry.main()
    # Falls back to an empty hook_input rather than crashing.
    assert seen["hook"] == {}


# ── A Stop while muted is still read, by the rules alone ──────────────────
#
# You mute for a meeting; the sessions go on. Skipped unread, the last
# category Hobson knew stood in for every turn since, so nothing knew that a
# session had come to wait on you.

def _muted_stop(claude_home, hobson_entry, monkeypatch, message):
    import session_state
    from home import derive_project_label
    _write_cfg(claude_home, {"engine": "say", "muted": True})
    monkeypatch.setattr(hobson_entry, "load_engine", lambda cfg: (_ for _ in ()).throw(
        AssertionError("no engine while muted")))
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO(json.dumps(
        {"hook_event_name": "Stop", "last_assistant_message": message})))
    hobson_entry.main()
    return session_state.load_session(derive_project_label())


def test_a_question_while_muted_is_recorded_as_one(claude_home, hobson_entry, monkeypatch, fake_ollama):
    state = _muted_stop(claude_home, hobson_entry, monkeypatch,
                        "I drafted both versions. Which one should I ship, A or B?")
    assert state["last_stop_category"] == "question"
    assert state["last_stop_time"] > 0
    assert fake_ollama.urls == [], "no model while muted"
    log = (claude_home / "hobson.log").read_text()
    assert "silenced (muted): Stop, read as question" in log


def test_a_finish_while_muted_is_unknown_not_a_stale_category(claude_home, hobson_entry, monkeypatch):
    import session_state
    from home import derive_project_label
    with session_state.transaction(derive_project_label()) as st:
        st["last_stop_category"] = "question"
    state = _muted_stop(claude_home, hobson_entry, monkeypatch, "I pushed the branch.")
    assert state["last_stop_category"] is None


def test_a_silenced_notification_says_which(claude_home, hobson_entry, monkeypatch):
    _write_cfg(claude_home, {"engine": "say", "muted": True})
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO(json.dumps(
        {"hook_event_name": "Notification", "notification_type": "idle_prompt"})))
    hobson_entry.main()
    assert "silenced (muted): Notification (idle_prompt)" in (claude_home / "hobson.log").read_text()
