"""Focus: the session in the pane you are looking at gets its own commentary pace."""

import json
import time

import pytest

import home
import phrase_gen
import presence
import session_state as ss
import terminal_jump
from log_stats import summarize

GHOSTTY = "/Applications/Ghostty.app"
BASH = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/", "description": "Run the tests"}}


def _no_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)


def _app_of_by_tty(owners):
    calls = []

    def app_of(tty):
        calls.append(tty)
        return owners.get(tty)

    app_of.calls = calls
    return app_of


def test_watching_the_active_clients_pane_with_its_app_in_front():
    app_of = _app_of_by_tty({"/dev/ttys001": GHOSTTY})
    assert presence.watching({"tmux_pane": "%7"}, GHOSTTY, ("/dev/ttys001", "%7"), app_of) is True


def test_not_watching_when_another_app_is_in_front():
    app_of = _app_of_by_tty({"/dev/ttys001": GHOSTTY})
    assert presence.watching({"tmux_pane": "%7"}, "/Applications/Safari.app",
                             ("/dev/ttys001", "%7"), app_of) is False


def test_a_different_pane_is_not_watched_and_costs_no_lookup():
    app_of = _app_of_by_tty({"/dev/ttys001": GHOSTTY})
    assert presence.watching({"tmux_pane": "%7"}, GHOSTTY, ("/dev/ttys001", "%9"), app_of) is False
    assert app_of.calls == []


def test_not_watching_with_no_tmux_client():
    app_of = _app_of_by_tty({"/dev/ttys001": GHOSTTY})
    assert presence.watching({"tmux_pane": "%7"}, GHOSTTY, None, app_of) is False
    assert app_of.calls == []


def test_a_tty_session_is_watched_when_its_terminal_is_in_front():
    app_of = _app_of_by_tty({"/dev/ttys004": GHOSTTY})
    assert presence.watching({"tty": "/dev/ttys004"}, GHOSTTY, None, app_of) is True
    assert presence.watching({"tty": "/dev/ttys004"}, "/Applications/Safari.app", None, app_of) is False


def test_nothing_known_about_where_it_runs_is_not_watched():
    app_of = _app_of_by_tty({"/dev/ttys004": GHOSTTY})
    assert presence.watching({}, GHOSTTY, ("/dev/ttys004", "%7"), app_of) is False


def test_no_frontmost_app_is_not_watched():
    app_of = _app_of_by_tty({"/dev/ttys004": GHOSTTY})
    assert presence.watching({"tty": "/dev/ttys004"}, None, None, app_of) is False
    assert app_of.calls == []


def _write_presence(state="present", frontmost=GHOSTTY):
    record = {"state": state, "source": "input", "ts": time.time(), "mode": "signals"}
    if frontmost is not None:
        record["frontmost"] = frontmost
    with open(home.path("hobson-presence.json"), "w", encoding="utf-8") as f:
        json.dump(record, f)


@pytest.fixture
def tmux(monkeypatch):
    """A stubbed terminal_jump._output: two clients, and ps rows leading each
    tty to its app. Returns the commands run and the clients, to be changed."""
    _no_tmux(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "%7")
    box = {"ran": [], "clients": [("100", "", "/dev/ttys002", "%7"), ("200", "", "/dev/ttys001", "%7")]}
    apps = {"50": "/Applications/Ghostty.app/Contents/MacOS/ghostty",
            "60": "/Applications/Other.app/Contents/MacOS/other"}

    def output(cmd, timeout=3.0):
        box["ran"].append(cmd)
        if cmd[:2] == ["tmux", "list-clients"]:
            return ["\t".join(c) for c in box["clients"]]
        if cmd[0] == "ps" and "-t" in cmd:
            return {"/dev/ttys001": ["100 50"], "/dev/ttys002": ["200 60"]}[cmd[-1]]
        if cmd[0] == "ps" and "-p" in cmd:
            return [f"1 {apps[cmd[-1]]}"]
        return []

    monkeypatch.setattr(terminal_jump, "_output", output)
    return box


def test_watched_end_to_end_with_the_newest_client_on_this_pane(claude_home, tmux):
    _write_presence()
    assert presence.watched({}, "demo") is True


def test_watched_is_false_when_the_newest_client_is_on_another_pane(claude_home, tmux):
    _write_presence()
    tmux["clients"][1] = ("200", "", "/dev/ttys001", "%9")
    assert presence.watched({}, "demo") is False


def test_watched_is_false_when_the_newest_clients_app_is_not_in_front(claude_home, tmux):
    _write_presence()
    tmux["clients"][1] = ("200", "", "/dev/ttys002", "%7")
    assert presence.watched({}, "demo") is False


def test_the_focused_client_is_the_one_you_look_at_even_if_another_typed_later(claude_home, tmux):
    _write_presence()
    tmux["clients"][0] = ("100", "attached,focused,UTF-8", "/dev/ttys002", "%9")
    tmux["clients"][1] = ("200", "attached,UTF-8", "/dev/ttys001", "%7")
    assert terminal_jump.active_tmux_client() == ("/dev/ttys002", "%9")
    assert presence.watched({}, "demo") is False


def test_with_no_client_focused_the_most_recently_active_one_is_used(claude_home, tmux):
    tmux["clients"][0] = ("100", "attached", "/dev/ttys002", "%9")
    tmux["clients"][1] = ("200", "attached", "/dev/ttys001", "%7")
    assert terminal_jump.active_tmux_client() == ("/dev/ttys001", "%7")


def test_watched_asks_nothing_of_the_system_when_you_are_away(claude_home, tmux):
    _write_presence(state="away")
    assert presence.watched({}, "demo") is False
    assert tmux["ran"] == []


def test_watched_asks_nothing_of_the_system_with_no_frontmost_app(claude_home, tmux):
    _write_presence(frontmost=None)
    assert presence.watched({}, "demo") is False
    assert tmux["ran"] == []


def test_watched_is_false_when_focus_is_off(claude_home, tmux):
    _write_presence()
    assert presence.watched({"focus": {"enabled": False}}, "demo") is False
    assert tmux["ran"] == []


def _cfg(**commentary):
    return {
        "engine": "say", "personality": "hobson", "events": ["commentary"],
        "cooldown": 0.0,
        "commentary": {"cooldown": 8.0, "tools": ["Bash"], "verbosity": "normal", **commentary},
        "ollama": {"model": "m", "url": "http://x"},
    }


def _engine(cfg=None):
    from engines.say import SayEngine
    return SayEngine(cfg or _cfg())


@pytest.fixture
def prompts(monkeypatch):
    """Answer each model call with a phrase about the tests, keeping each
    prompt. The first is the one a lone call is expected to say."""
    sent = []
    phrases = ["I'm running the tests.", "Now testing the new build.",
               "I'm rerunning the unit tests.", "Another test pass is starting."]

    def chat(messages, *a, **k):
        sent.append(messages)
        return f"done | {phrases[(len(sent) - 1) % len(phrases)]}", None

    monkeypatch.setattr(phrase_gen, "_chat", chat)
    return sent


def _primed(project, seconds_ago=10):
    with ss.transaction(project) as state:
        state["last_flush_time"] = time.time() - seconds_ago


def test_a_watched_session_flushes_at_two_calls_and_says_so(claude_home, no_audio, prompts, monkeypatch):
    monkeypatch.setattr(presence, "watched", lambda *a, **k: True)
    _primed(home.derive_project_label())
    eng = _engine()
    eng.run(dict(BASH))
    assert prompts == []
    eng.run(dict(BASH))
    assert len(prompts) == 1
    assert no_audio["say"] == ["I'm running the tests."]
    log = open(home.log_file(), encoding="utf-8").read()
    assert "flush (watched)" in log
    assert "normal, watched" in log


def test_an_unwatched_session_with_the_same_calls_only_queues(claude_home, no_audio, prompts, monkeypatch):
    monkeypatch.setattr(presence, "watched", lambda *a, **k: False)
    _primed(home.derive_project_label())
    eng = _engine()
    eng.run(dict(BASH))
    eng.run(dict(BASH))
    assert prompts == []
    assert len(ss.load_session(home.derive_project_label())["pending"]) == 2


def test_a_watched_flush_does_not_starve_an_unwatched_session(claude_home, no_audio, prompts, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(presence, "watched", lambda config, project, now=None: project == "alpha")
    labels = {"project": "bravo"}
    monkeypatch.setattr(home, "derive_project_label", lambda: labels["project"])
    eng = _engine()

    def call(project, at):
        labels["project"] = project
        clock[0] = 1_000_000.0 + at
        eng.run(dict(BASH))

    call("bravo", -20)
    for at in (1, 2, 3, 4):
        call("bravo", at)
    assert len(ss.load_session("bravo")["pending"]) == 4

    call("alpha", 0)
    call("alpha", 4)
    call("alpha", 5)
    assert len(no_audio["say"]) == 3
    assert ss.load_session("alpha")["pending"] == []

    call("bravo", 6)
    assert ss.load_session("bravo")["pending"] == []
    assert len(no_audio["say"]) == 4


@pytest.fixture
def two_clocks(claude_home, no_audio, prompts, monkeypatch):
    """Several projects on one fake clock. `watched` names the watched
    projects; run(project, at) makes one call and says whether it flushed."""
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    box = {"watched": set(), "project": None}
    monkeypatch.setattr(presence, "watched", lambda config, project, now=None: project in box["watched"])
    monkeypatch.setattr(home, "derive_project_label", lambda: box["project"])
    eng = _engine()

    def run(project, at):
        box["project"] = project
        clock[0] = 1_000_000.0 + at
        before = len(prompts)
        eng.run(dict(BASH))
        return len(prompts) > before

    box["run"] = run
    return box


def test_a_watched_session_keeps_its_pace_beside_a_busy_unwatched_one(two_clocks):
    two_clocks["watched"] = {"alpha"}
    run = two_clocks["run"]
    alpha, bravo = [], []
    for second in range(20):
        if run("alpha", second):
            alpha.append(second)
        if run("bravo", second + 0.5):
            bravo.append(second + 0.5)
    assert alpha == [0, 4, 8, 12, 16]
    assert bravo == [0.5, 8.5, 16.5]


def test_two_watched_sessions_share_one_pace(two_clocks):
    two_clocks["watched"] = {"alpha", "charlie"}
    run = two_clocks["run"]
    flushes = []
    for second in range(20):
        calls = [("alpha", second)] if second < 10 else []
        for project, at in calls + [("charlie", second + 0.5)]:
            if run(project, at):
                flushes.append((at, project))
    assert flushes == [(0, "alpha"), (4, "alpha"), (8, "alpha"),
                       (12.5, "charlie"), (16.5, "charlie")]


def test_a_watched_session_waits_for_its_own_last_flush(two_clocks):
    run = two_clocks["run"]
    assert run("alpha", 0) is True
    two_clocks["watched"] = {"alpha"}
    assert run("alpha", 1) is False
    assert run("alpha", 3.9) is False
    assert run("alpha", 4) is True


def test_a_watched_session_never_asks_the_decider_and_gets_the_short_budget(
        claude_home, no_audio, prompts, monkeypatch):
    import gate

    def refuse(*a, **k):
        raise AssertionError("the decider was consulted for a watched session")

    monkeypatch.setattr(gate, "worth_probability", refuse)
    monkeypatch.setattr(presence, "watched", lambda *a, **k: True)
    _primed(home.derive_project_label())
    eng = _engine()
    eng.run(dict(BASH))
    eng.run(dict(BASH))
    prompt = prompts[0][0]["content"]
    assert "Rules: 2-6 words" in prompt
    assert "Do not SKIP it." in prompt
    assert prompts[0][-1]["content"].endswith("Say it in 2-6 words.")


def test_only_a_watched_budget_is_restated_after_the_event():
    watched = phrase_gen._build_messages("PreToolUse", "2 Bash (Run the tests)", "No announcements yet.",
                                         verbosity="decided", words=(2, 6))
    usual = phrase_gen._build_messages("PreToolUse", "2 Bash (Run the tests)", "No announcements yet.",
                                       verbosity="decided")
    assert watched[-1]["content"] == ("Session: No announcements yet.\n\n"
                                      "Event: [PreToolUse] 2 Bash (Run the tests)\n\nSay it in 2-6 words.")
    assert usual[-1]["content"] == "Session: No announcements yet.\n\nEvent: [PreToolUse] 2 Bash (Run the tests)"


def test_the_word_budget_reaches_the_guard():
    assert phrase_gen._clean_phrase("I'm testing.", words=(2, 6)) == "I'm testing."
    assert phrase_gen._clean_phrase("I'm testing.") is None
    eight = "I'm running the tests on the settings screen."
    assert phrase_gen._clean_phrase(eight) == eight
    assert phrase_gen._clean_phrase(eight, words=(2, 6)) is None


def test_the_budget_holds_through_generate_or_skip(monkeypatch):
    replies = ["done | I'm running the tests on the settings screen.", "done | I'm testing."]
    monkeypatch.setattr(phrase_gen, "_chat", lambda *a, **k: (replies[0], None))
    args = ("PreToolUse", "2 Bash (Run the tests)", "No announcements yet.")
    assert phrase_gen.generate_or_skip(*args, verbosity="decided").phrase is not None
    assert phrase_gen.generate_or_skip(*args, verbosity="decided", words=(2, 6)).phrase is None
    monkeypatch.setattr(phrase_gen, "_chat", lambda *a, **k: (replies[1], None))
    assert phrase_gen.generate_or_skip(*args, verbosity="decided", words=(2, 6)).phrase == "I'm testing."
    assert phrase_gen.generate_or_skip(*args, verbosity="decided").phrase is None


def test_a_reply_over_the_budget_is_measured_against_it():
    eight = "done | I'm running the tests on the settings screen."
    assert phrase_gen._words_over_budget(eight) is None
    assert phrase_gen._words_over_budget(eight, (2, 6)) == 8


def test_the_retry_after_an_over_budget_reply_names_the_calls_budget(monkeypatch):
    seen = []
    replies = iter(["done | I'm running the tests on the settings screen.", "done | I'm testing."])

    def chat(messages, *a, **k):
        seen.append(messages)
        return next(replies), None

    monkeypatch.setattr(phrase_gen, "_chat", chat)
    got = phrase_gen.generate_or_skip("Stop", "Last message: done.", "No announcements yet.", words=(2, 6))
    assert got.phrase == "I'm testing."
    assert seen[1][-1]["content"] == "That is 8 words. Say it in at most 6 words."


def test_stats_count_watched_flushes():
    lines = [
        "[2026-10-15 10:00:00] [demo] [PreToolUse] flush (watched)\n",
        "[2026-10-15 10:00:01] [demo] [say] [PreToolUse] (m, normal, watched) -> done -> 'I ran it.'\n",
        "[2026-10-15 10:00:09] [demo] [PreToolUse] flush (watched)\n",
    ]
    assert summarize(lines)["flush_watched"] == 2
