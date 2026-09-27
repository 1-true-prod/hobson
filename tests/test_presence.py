"""Presence: whether anyone is listening, and what Hobson does about it.

The Swift helper is not run here (like the TTS daemons, it needs hardware);
its state file is written by hand. Everything that decides what is said is
pure or file-backed, and tested as such.
"""

import json
import os
import time

import pytest

import presence


def _state(claude_home, **fields):
    """Write the helper's state file as a live helper would."""
    record = {"state": "present", "source": "input", "ts": time.time(), "since": time.time(),
              "mode": "auto", "camera": "authorized", "pid": 0, "glance_ts": 0}
    record.update(fields)
    (claude_home / "hobson-presence.json").write_text(json.dumps(record))
    return record


def _config(**presence_cfg):
    return {"engine": "say", "personality": "hobson", "presence": presence_cfg}


# ── Reading the sensor ─────────────────────────────────────────────────────

def test_no_helper_is_unknown(claude_home):
    assert presence.audience_of(presence.read_state(), time.time()) == "unknown"


def test_a_stale_file_is_a_dead_helper(claude_home):
    record = _state(claude_home, state="away", ts=time.time() - 60)
    assert presence.audience_of(record, time.time()) == "unknown"


@pytest.mark.parametrize("state", presence.STATES)
def test_a_fresh_file_says_who_is_listening(claude_home, state):
    assert presence.audience_of(_state(claude_home, state=state), time.time()) == state


def test_garbage_is_unknown(claude_home):
    (claude_home / "hobson-presence.json").write_text("{not json")
    assert presence.audience(_config(enabled=True))[0] == "unknown"
    assert presence.audience_of({"state": "asleep", "ts": time.time()}, time.time()) == "unknown"
    assert presence.audience_of({"state": "away", "ts": "soon"}, time.time()) == "unknown"


def test_presence_off_is_unknown_whatever_the_file_says(claude_home):
    _state(claude_home, state="away")
    assert presence.audience(_config(enabled=False))[0] == "unknown"


# ── When to look ───────────────────────────────────────────────────────────

_AUTO = {"mode": "auto"}


def test_idle_and_unconfirmed_asks_for_a_look():
    assert presence.needs_look({"mode": "auto", "camera": "authorized", "state": "present",
                                "source": "idle"}, _AUTO, time.time())


@pytest.mark.parametrize("change", [
    {"source": "input"},                    # a keystroke already answered it
    {"state": "away", "source": "lock"},    # so did a lock
    {"camera": "not-determined"},           # no permission: never a prompt
    {"camera": "denied"},
    {"mode": "signals"},                    # the helper was told no camera
    {"camera_allowed": False},              # the phone is face down
])
def test_no_look_when_it_would_not_help_or_is_not_allowed(change):
    record = {"mode": "auto", "camera": "authorized", "state": "present", "source": "idle"}
    record.update(change)
    assert not presence.needs_look(record, _AUTO, time.time())


def test_a_fresh_look_is_reused_and_a_stale_one_is_not():
    now = time.time()
    record = {"mode": "auto", "camera": "authorized", "state": "present", "source": "camera"}
    assert not presence.needs_look({**record, "glance_ts": now - 5}, _AUTO, now)
    assert presence.needs_look({**record, "glance_ts": now - 60}, _AUTO, now)


def test_continuous_mode_never_asks():
    assert not presence.needs_look({"mode": "continuous", "camera": "authorized",
                                    "state": "present", "source": "idle"},
                                   {"mode": "continuous"}, time.time())


def test_a_look_that_answers_away_holds(claude_home, monkeypatch):
    """The look is a signal to the helper, which rewrites the state file."""
    _state(claude_home, source="idle", pid=4242)
    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        _state(claude_home, state="away", source="camera", pid=4242, glance_ts=time.time() + 1)

    monkeypatch.setattr(presence.os, "kill", fake_kill)
    assert presence.route(_config(enabled=True, mode="auto"), "done", "I shipped it.") is None
    assert sent and sent[0][0] == 4242
    assert [i["phrase"] for i in presence.held()] == ["I shipped it."]


def test_commentary_never_triggers_a_look(claude_home, monkeypatch):
    _state(claude_home, source="idle", pid=4242)
    monkeypatch.setattr(presence.os, "kill", lambda *a: pytest.fail("no look for commentary"))
    assert presence.route(_config(enabled=True), "commentary", "I'm editing it.") == "I'm editing it."


# ── What happens to a phrase ───────────────────────────────────────────────

@pytest.mark.parametrize("state,kind,expected", [
    ("present", "done", "speak"), ("unknown", "waiting", "speak"),
    ("away", "done", "hold"), ("away", "waiting", "hold"), ("away", "broken", "hold"),
    ("call", "stalled", "hold"), ("away", "commentary", "drop"), ("call", "commentary", "drop"),
    ("company", "waiting", "company"), ("company", "done", "hold"),
    ("company", "commentary", "drop"),
    ("away", "briefing", "speak"), ("call", "nudge", "speak"),
])
def test_decision_matrix(state, kind, expected):
    assert presence.decision(state, kind) == expected


def test_route_holds_while_away_and_speaks_while_present(claude_home):
    cfg = _config(enabled=True)
    _state(claude_home, state="away", source="lock")
    assert presence.route(cfg, "done", "I fixed the build.", project="webapp") is None
    _state(claude_home, state="present")
    assert presence.route(cfg, "done", "I fixed the tests.", project="webapp") == "I fixed the tests."
    assert [(i["project"], i["kind"], i["phrase"]) for i in presence.held()] == [
        ("webapp", "done", "I fixed the build.")]


def test_company_hears_a_wait_without_its_details(claude_home):
    _state(claude_home, state="company", source="camera")
    said = presence.route(_config(enabled=True), "waiting",
                          "Careful — this one deletes files. It needs your approval.", project="bank app")
    assert said == presence.COMPANY_LINE
    assert "bank" not in said and "deletes" not in said
    assert presence.held()[0]["phrase"].startswith("Careful")


def test_commentary_is_dropped_not_held(claude_home):
    _state(claude_home, state="away", source="lock")
    assert presence.route(_config(enabled=True), "commentary", "I'm running the tests.") is None
    assert presence.held() == []


def test_a_nudge_pauses_only_for_an_empty_room():
    assert presence.pauses_nudge("away") and presence.pauses_nudge("call")
    assert not any(presence.pauses_nudge(s) for s in ("present", "company", "unknown"))


# ── The salver ─────────────────────────────────────────────────────────────

def test_the_salver_keeps_the_newest_and_forgets_the_old(claude_home):
    presence.hold("p", "done", "ancient", now=time.time() - presence.HELD_MAX_AGE - 10)
    for n in range(presence.MAX_HELD + 5):
        presence.hold("p", "done", f"phrase {n}")
    phrases = [i["phrase"] for i in presence.held()]
    assert len(phrases) == presence.MAX_HELD and "ancient" not in phrases
    assert phrases[-1] == f"phrase {presence.MAX_HELD + 4}"
    assert len(presence.take_held()) == presence.MAX_HELD
    assert presence.held() == []


# ── The briefing ───────────────────────────────────────────────────────────

def _item(kind, phrase, project="webapp", ts=None):
    return {"kind": kind, "phrase": phrase, "project": project, "ts": ts or time.time()}


def test_nothing_held_nothing_said():
    assert presence.compose_briefing([], [], "away") is None


def test_the_briefing_leads_with_what_waits_on_you():
    text = presence.compose_briefing([
        _item("done", "I finished the migration.", ts=1),
        _item("waiting", "Careful — this one deletes files. It needs your approval.", "bank app", ts=2),
    ], [], "away")
    assert text == ("Welcome back. On bank app: Careful — this one deletes files. It needs your "
                    "approval. On webapp: I finished the migration.")


def test_two_told_in_full_and_the_rest_counted():
    items = [_item("done", f"I finished part {n}.", ts=n + 1) for n in range(4)]
    text = presence.compose_briefing(items, [], "away")
    assert text == ("Welcome back. On webapp: I finished part 0. I finished part 1. "
                    "Plus 2 more updates.")


def test_a_sessions_repeated_waits_collapse_to_the_latest():
    text = presence.compose_briefing([_item("waiting", "Claude needs you.", ts=1),
                                      _item("waiting", "Your call, sir.", ts=2)], [], "away")
    assert text == "Welcome back. On webapp: Your call, sir."


def test_a_session_still_waiting_is_named_once():
    text = presence.compose_briefing([_item("waiting", "Your call.", "webapp")],
                                     [("webapp", "answer"), ("site", "approval")], "away")
    assert text == "Welcome back. On webapp: Your call. site is still waiting on your approval."


@pytest.mark.parametrize("came_from,greeting", [
    ("away", "Welcome back."), ("call", "Now that your call's over:"),
    ("company", "Now that it's just you:"),
])
def test_the_greeting_fits_what_ended(came_from, greeting):
    assert presence.compose_briefing([_item("done", "Done.")], [], came_from).startswith(greeting)


def test_before_you_go_only_when_something_waits():
    assert presence.compose_departure([]) is None
    assert presence.compose_departure([("webapp", "approval")]) == (
        "Before you go — webapp is waiting on your approval.")
    assert presence.compose_departure([("a", "you"), ("b", "answer")]) == (
        "Before you go — 2 sessions are waiting on you.")


# ── Who is waiting on you ──────────────────────────────────────────────────

def _session(project, **fields):
    import session_state
    with session_state.transaction(project) as state:
        state.update(fields)


def test_an_unanswered_question_waits(claude_home):
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    assert presence.waiting_on_you() == [("webapp", "answer")]


def test_an_answered_question_does_not(claude_home):
    import nudge
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    with open(nudge.activity_path("webapp"), "w") as f:
        f.write(str(time.time()))
    assert presence.waiting_on_you() == []


def test_a_fresh_permission_request_waits_and_an_old_one_does_not(claude_home):
    import nudge
    _session("webapp", last_stop_time=time.time() - 900)
    nudge.record_alive("webapp", {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    assert presence.waiting_on_you() == [("webapp", "approval")]
    assert presence.waiting_on_you(now=time.time() + presence.PERMISSION_WAIT_SECONDS + 5) == []


def test_a_finished_turn_is_not_waiting(claude_home):
    _session("webapp", last_stop_category="done", last_stop_time=time.time() - 30)
    assert presence.waiting_on_you() == []


# ── Transitions ────────────────────────────────────────────────────────────

@pytest.fixture
def spoken(monkeypatch):
    said = []
    monkeypatch.setattr(presence, "_speak", lambda config, text: said.append(text))
    return said


def test_coming_back_brings_one_briefing_and_empties_the_salver(claude_home, spoken):
    presence.hold("webapp", "done", "I shipped the release.")
    text = presence.on_transition("away", "present", "input", away_for=600, config=_config(enabled=True))
    assert spoken == [text] == ["Welcome back. On webapp: I shipped the release."]
    assert presence.held() == []


def test_coming_back_to_nothing_is_greeted(claude_home, spoken):
    presence.on_transition("away", "present", "input", away_for=1200, config=_config(enabled=True))
    assert spoken == ["Welcome back. All quiet for the last 20 minutes."]


def test_without_greetings_coming_back_to_nothing_says_nothing(claude_home, spoken):
    presence.on_transition("away", "present", "input", away_for=600,
                           config=_config(enabled=True, greetings=False))
    assert spoken == []


def test_a_short_absence_gets_no_greeting(claude_home, spoken):
    presence.on_transition("away", "present", "camera", away_for=12, config=_config(enabled=True))
    assert spoken == []


def test_a_greeting_is_never_the_same_twice_running(claude_home, spoken):
    for _ in range(6):
        presence.on_transition("away", "present", "input", away_for=45, config=_config(enabled=True))
    assert all(line in presence.GREETINGS_NOTHING for line in spoken)
    assert all(a != b for a, b in zip(spoken, spoken[1:]))


def test_the_end_of_a_call_or_company_is_not_an_arrival(claude_home, spoken):
    presence.on_transition("call", "present", "input", away_for=900, config=_config(enabled=True))
    presence.on_transition("company", "present", "camera", away_for=900, config=_config(enabled=True))
    assert spoken == []


def test_leaving_gets_a_farewell_at_most_every_few_minutes(claude_home, spoken):
    cfg = _config(enabled=True, mode="continuous")
    now = time.time()
    presence.on_transition("present", "away", "lock", config=cfg, now=now)
    presence.on_transition("present", "away", "camera", config=cfg, now=now + 60)
    presence.on_transition("present", "away", "camera", config=cfg, now=now + presence.FAREWELL_EVERY + 1)
    assert len(spoken) == 2 and spoken[0] != spoken[1]
    assert all(line in presence.FAREWELLS for line in spoken)


def test_what_waits_beats_a_farewell(claude_home, spoken):
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    presence.on_transition("present", "away", "lock", config=_config(enabled=True))
    assert spoken == ["Before you go — webapp is waiting on an answer from you."]


def test_without_greetings_leaving_says_nothing(claude_home, spoken):
    presence.on_transition("present", "away", "lock", config=_config(enabled=True, greetings=False))
    assert spoken == []


@pytest.mark.parametrize("seconds,said", [
    (300, "5 minutes"), (1190, "20 minutes"), (3500, "58 minutes"), (3700, "an hour"),
    (10533, "3 hours"),
])
def test_durations_are_said_as_people_say_them(seconds, said):
    assert presence._duration(seconds) == said


def test_a_short_absence_does_not_repeat_what_still_waits(claude_home, spoken):
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    presence.on_transition("away", "present", "input", away_for=10, config=_config(enabled=True))
    assert spoken == []
    presence.on_transition("away", "present", "input", away_for=600, config=_config(enabled=True))
    assert spoken == ["Welcome back. webapp is still waiting on an answer from you."]


def test_muted_keeps_the_salver_for_later(claude_home, spoken):
    presence.hold("webapp", "done", "I shipped it.")
    presence.on_transition("away", "present", "input", away_for=600,
                           config={**_config(enabled=True), "muted": True})
    assert spoken == [] and len(presence.held()) == 1


def test_locking_the_screen_mentions_what_waits(claude_home, spoken):
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    presence.on_transition("present", "away", "lock", config=_config(enabled=True))
    assert spoken == ["Before you go — webapp is waiting on an answer from you."]


def test_leaving_seen_late_by_a_look_says_nothing(claude_home, spoken):
    """In auto mode a look notices you gone minutes after you left."""
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    presence.on_transition("present", "away", "camera", config=_config(enabled=True, mode="auto"))
    assert spoken == []
    presence.on_transition("present", "away", "camera", config=_config(enabled=True, mode="continuous"))
    assert len(spoken) == 1


def test_turning_the_phone_over_is_confirmed(claude_home, spoken):
    assert presence.on_switch("down", config=_config(enabled=True)) == "Camera off."
    assert presence.on_switch("up", config=_config(enabled=True)) == "Camera on."
    assert presence.on_switch("up", config={**_config(enabled=True), "muted": True}) is None
    assert spoken == ["Camera off.", "Camera on."]


def test_the_helpers_arguments_parse(claude_home, monkeypatch):
    """The helper runs presence.py detached with output discarded: an
    argument it does not accept would fail without a trace. These are the
    argv presence/main.swift builds (transition, announceSwitch)."""
    calls = []
    monkeypatch.setattr(presence, "on_transition", lambda *a: calls.append(("transition",) + a))
    monkeypatch.setattr(presence, "on_switch", lambda *a: calls.append(("switch",) + a))
    presence.main(["--transition", "away", "present", "--source", "input", "--away-for", "30"])
    presence.main(["--switch", "down"])
    monkeypatch.setattr(presence, "on_wave", lambda *a: calls.append(("wave",)))
    presence.main(["--wave"])
    assert calls == [("transition", "away", "present", "input", 30.0), ("switch", "down"), ("wave",)]


# ── The helper's life ──────────────────────────────────────────────────────

@pytest.fixture
def built(monkeypatch):
    monkeypatch.setattr(presence, "helper_built", lambda: True)


def test_starts_the_helper_through_open(claude_home, no_audio, built):
    assert presence.ensure_running(_config(enabled=True, mode="auto"))
    argv = no_audio["popen"][-1]
    assert argv[:4] == ["open", "-g", "-j", presence.app_path()]
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "auto"
    assert argv[argv.index("--home") + 1] == str(claude_home)


def test_does_not_start_when_off_unbuilt_or_running(claude_home, no_audio, monkeypatch):
    monkeypatch.setattr(presence, "helper_built", lambda: False)
    assert not presence.ensure_running(_config(enabled=True))
    monkeypatch.setattr(presence, "helper_built", lambda: True)
    assert not presence.ensure_running(_config(enabled=False))
    _state(claude_home, mode="auto")
    assert not presence.ensure_running(_config(enabled=True, mode="auto"))
    assert no_audio["popen"] == []


def test_spawns_are_throttled(claude_home, no_audio, built):
    assert presence.ensure_running(_config(enabled=True))
    assert not presence.ensure_running(_config(enabled=True))


def test_a_changed_setting_restarts_the_helper(claude_home, built, monkeypatch):
    _state(claude_home, mode="auto", pid=999, phone_setting=None)
    killed = []
    monkeypatch.setattr(presence.os, "kill", lambda pid, sig: killed.append(pid))
    presence.ensure_running(_config(enabled=True, mode="continuous"))
    presence.ensure_running(_config(enabled=True, mode="auto", phone="auto"))
    assert killed == [999, 999]


def test_the_phone_switch_reaches_the_helper(claude_home, no_audio, built, monkeypatch):
    monkeypatch.setattr(presence, "find_adb", lambda cfg: "/sdk/adb")
    presence.ensure_running(_config(enabled=True, phone="auto"))
    argv = no_audio["popen"][-1]
    assert argv[argv.index("--phone") + 1] == "auto" and argv[argv.index("--adb") + 1] == "/sdk/adb"


# ── The engines' speak seams ───────────────────────────────────────────────

def _stop_transcript(tmp_path, text):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}), encoding="utf-8")
    return str(t)


def test_a_stop_while_away_is_held_and_still_classified(claude_home, no_audio, monkeypatch, tmp_path):
    """The nudge gate and the briefing both need the category, so the Stop is
    read and recorded as usual; only the speaking is held."""
    import session_state
    import stop_outcome
    from engines.say import SayEngine
    from home import derive_project_label
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "broken")
    _state(claude_home, state="away", source="lock")
    SayEngine({"engine": "say", "personality": "hobson", "events": ["stop"]}).run(
        {"hook_event_name": "Stop",
         "transcript_path": _stop_transcript(tmp_path, "The build failed with a linker error.")})
    assert no_audio["say"] == []
    assert session_state.load_session(derive_project_label())["last_stop_category"] == "broken"
    assert [i["kind"] for i in presence.held()] == ["broken"]


def test_a_stop_while_present_is_spoken(claude_home, no_audio, monkeypatch, tmp_path):
    import stop_outcome
    from engines.say import SayEngine
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")
    _state(claude_home, state="present")
    SayEngine({"engine": "say", "personality": "hobson", "events": ["stop"]}).run(
        {"hook_event_name": "Stop", "transcript_path": _stop_transcript(tmp_path, "All done.")})
    assert len(no_audio["say"]) == 1 and presence.held() == []


def test_a_daemon_permission_request_while_on_a_call_is_held(claude_home, no_audio, monkeypatch):
    from engines.pocket_tts_realtime import PocketTTSRealtimeEngine as Engine
    monkeypatch.setattr(Engine, "_daemon_alive", lambda self: False)
    monkeypatch.setattr(Engine, "_start_daemon_background", lambda self: None)
    _state(claude_home, state="call", source="microphone")
    Engine({"engine": "pocket-tts", "personality": "hobson", "events": ["permission"]}).run(
        {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion"})
    assert no_audio["say"] == []
    assert [i["kind"] for i in presence.held()] == ["waiting"]


def test_speak_dynamic_reports_a_hold(claude_home, no_audio):
    from engines.say import SayEngine
    _state(claude_home, state="away", source="lock")
    eng = SayEngine({"engine": "say", "personality": "hobson"})
    assert eng.speak_dynamic("Nothing's moved.", kind="stalled") is False
    assert eng.speak_dynamic("Welcome back.", kind="briefing") is True
    assert no_audio["say"] == ["Welcome back."]


def test_a_broken_presence_module_speaks(claude_home, no_audio, monkeypatch):
    from engines.say import SayEngine
    monkeypatch.setattr(presence, "route", lambda *a, **k: 1 / 0)
    SayEngine({"engine": "say", "personality": "hobson"}).speak_dynamic("Still here.", kind="done")
    assert no_audio["say"] == ["Still here."]


def test_presence_is_on_by_default():
    import home
    assert home.DEFAULT_CONFIG["presence"]["enabled"] is True
    assert home.DEFAULT_CONFIG["presence"]["mode"] == "auto"
    assert home.DEFAULT_CONFIG["presence"]["phone"] is None
    assert home.DEFAULT_CONFIG["presence"]["greetings"] is True


# ── The nudge waits for you ────────────────────────────────────────────────

@pytest.fixture
def fast_nudge(monkeypatch):
    import nudge
    monkeypatch.setattr(nudge, "PRESENCE_POLL_SECONDS", 0)
    monkeypatch.setattr(nudge.time, "sleep", lambda s: None)
    return nudge


def test_a_paused_nudge_resumes_when_you_are_back(claude_home, fast_nudge, monkeypatch):
    states = iter(["away", "away", None])
    monkeypatch.setattr(fast_nudge, "_paused_for", lambda config: next(states))
    started = time.time()
    assert fast_nudge._wait_for_return("webapp", started, started + 60) == "back"


def test_typing_ends_a_paused_nudge(claude_home, fast_nudge, monkeypatch):
    monkeypatch.setattr(fast_nudge, "_paused_for", lambda config: "away")
    started = time.time() - 5
    with open(fast_nudge.activity_path("webapp"), "w") as f:
        f.write(str(time.time()))
    assert fast_nudge._wait_for_return("webapp", started, started + 60) == "cancelled"


def test_a_paused_nudge_gives_up_at_its_ceiling(claude_home, fast_nudge, monkeypatch):
    monkeypatch.setattr(fast_nudge, "_paused_for", lambda config: "call")
    started = time.time()
    assert fast_nudge._wait_for_return("webapp", started, started - 1) == "expired"


def test_a_held_watchdog_line_is_reported_as_held(claude_home, no_audio):
    import nudge
    _state(claude_home, state="away", source="lock")
    assert nudge._speak({"engine": "say", "personality": "hobson"}, "Nothing's moved.",
                        kind="stalled") is False
    _state(claude_home, state="present")
    assert nudge._speak({"engine": "say", "personality": "hobson"}, "Nothing's moved.",
                        kind="stalled") is True


def test_the_preview_window_reaches_the_helper_and_restarts_it(claude_home, no_audio, built, monkeypatch):
    presence.ensure_running(_config(enabled=True, preview=True))
    assert "--preview" in no_audio["popen"][-1]
    _state(claude_home, mode="auto", pid=321, preview=False)
    killed = []
    monkeypatch.setattr(presence.os, "kill", lambda pid, sig: killed.append(pid))
    presence.ensure_running(_config(enabled=True, mode="auto", preview=True))
    assert killed == [321]


def test_the_preview_is_off_by_default():
    import home
    assert home.DEFAULT_CONFIG["presence"]["preview"] is False


# ── Waves ──────────────────────────────────────────────────────────────────

def test_a_wave_is_answered_with_a_hello_when_nothing_is_on(claude_home, spoken):
    said = presence.on_wave(config=_config(enabled=True))
    assert said in presence.WAVE_HELLOS and spoken == [said]


def test_a_wave_brings_what_was_held(claude_home, spoken):
    presence.hold("webapp", "done", "I shipped the release.")
    assert presence.on_wave(config=_config(enabled=True)) == "Hello. On webapp: I shipped the release."
    assert presence.held() == []


def test_a_wave_names_who_is_waiting(claude_home, spoken):
    _session("webapp", last_stop_category="question", last_stop_time=time.time() - 30)
    assert presence.on_wave(config=_config(enabled=True)) == (
        "Hello. webapp is still waiting on an answer from you.")


def test_waving_on_gets_one_answer_every_few_seconds(claude_home, spoken):
    cfg = _config(enabled=True)
    now = time.time()
    presence.on_wave(config=cfg, now=now)
    presence.on_wave(config=cfg, now=now + 3)
    presence.on_wave(config=cfg, now=now + presence.WAVE_EVERY + 1)
    assert len(spoken) == 2 and spoken[0] != spoken[1]


def test_a_wave_while_muted_is_not_answered(claude_home, spoken):
    assert presence.on_wave(config={**_config(enabled=True), "muted": True}) is None
    assert spoken == []
