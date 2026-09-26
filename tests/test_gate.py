"""gate.py: with the decider on "jev", whether a released batch is spoken.

The count/timer gate still decides when a batch is considered. The decider
then decides whether it is worth hearing, against the chattiness dial, and
Ollama only phrases what it lets through.
"""

import time

import pytest

import gate
import session_state as ss


def test_the_default_dial_asks_for_p_worth_of_0_4():
    assert gate.threshold(gate.DEFAULT_CHATTINESS) == pytest.approx(0.4)


@pytest.mark.parametrize("chattiness, bar", [
    (1.0, 0.0), (0.0, 1.0), (0.8, 0.2), (7, 0.0), (-1, 1.0), ("loud", 0.4), (None, 0.4),
])
def test_the_dial_is_clamped_and_forgiving(chattiness, bar):
    assert gate.threshold(chattiness) == pytest.approx(bar)


# ── worth_probability: what is asked, and what leaves the machine ─────────


@pytest.fixture
def asked(monkeypatch):
    """Record every decider question; answer P(worth) = `asked.p`."""
    import decider

    class Box:
        p = 0.9
        states = []

    def fake_choice(state, instructions, criteria, config=None):
        Box.states.append(state)
        if Box.p is None:
            return None
        return decider.ChoiceResult(choice="worth", probs={"worth": Box.p}, confidence=1.0)

    Box.states = []
    monkeypatch.setattr(decider, "choice", fake_choice)
    return Box


def test_the_decider_scores_the_batch(asked):
    asked.p = 0.73
    assert gate.worth_probability("Bash: Commit the retry fix", {}) == pytest.approx(0.73)
    [state] = asked.states
    assert "Commit the retry fix" in state


def test_the_batch_is_redacted_before_it_leaves(asked):
    gate.worth_probability(
        "Bash: GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123 gh; Edit: /Users/jdoe/app/Invoice.kt", {})
    [state] = asked.states
    assert "ghp_abcdef" not in state and "jdoe" not in state and "Invoice.kt" in state


def test_an_edit_line_count_survives_redaction(asked):
    gate.worth_probability("Edit: InvoiceService.kt (+4/-2 lines)", {})
    [state] = asked.states
    assert "(+4/-2 lines)" in state


def test_no_opinion_and_nothing_to_ask(asked):
    asked.p = None
    assert gate.worth_probability("Bash: run the tests", {}) is None
    assert gate.worth_probability("", {}) is None
    assert len(asked.states) == 1, "an empty batch is never asked about"


def test_the_local_backend_asks_nobody(fake_decider):
    assert gate.worth_probability("Bash: run the tests", {"decider": {"backend": "local"}}) is None
    assert fake_decider.calls() == 0


# ── The engines: held back, or phrased without a veto ─────────────────────


def _engine(verbosity="normal", **commentary):
    from engines.say import SayEngine
    return SayEngine({
        "engine": "say", "personality": "alfred", "events": ["commentary"],
        "commentary": {"tools": ["bash", "edit"], "verbosity": verbosity, "cooldown": 0,
                       "min_tool_calls": 1, "min_seconds": 600.0, **commentary},
    })


@pytest.fixture
def generated(monkeypatch):
    """Record every phrase_gen.generate_or_skip call's verbosity."""
    import phrase_gen
    calls = []

    def fake(*a, **k):
        calls.append(k.get("verbosity"))
        return "done", "I'm committing the retry fix."

    monkeypatch.setattr(phrase_gen, "generate_or_skip", fake)
    return calls


_BASH = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {"command": "git commit -m wip", "description": "Commit the retry fix"}}


def test_below_the_bar_is_held_back_without_ollama(claude_home, no_audio, asked, generated):
    asked.p = 0.2
    _engine().run(dict(_BASH))
    assert generated == [], "a batch the decider holds back never reaches Ollama"
    assert not no_audio["say"]
    log = (claude_home / "claudio.log").read_text()
    assert "held back" in log and "0.20 < 0.40" in log


def test_a_held_back_batch_is_consumed(claude_home, no_audio, asked, generated):
    from engines.base import derive_project_label
    asked.p = 0.2
    _engine().run(dict(_BASH))
    state = ss.load_session(derive_project_label())
    assert state["pending"] == [], "it is not re-offered on the next flush"
    assert state["last_flush_time"], "the flush still counts for the timer gate"


def test_above_the_bar_ollama_phrases_without_a_veto(claude_home, no_audio, asked, generated):
    asked.p = 0.46
    _engine().run(dict(_BASH))
    assert generated == ["decided"]
    assert no_audio["say"]


@pytest.mark.parametrize("verbosity", ["terse", "normal"])
def test_the_dial_replaces_terse_and_normal(verbosity, claude_home, no_audio, asked, generated):
    asked.p = 0.3
    _engine(verbosity, chattiness=0.8).run(dict(_BASH))
    assert generated == ["decided"], "at chattiness 0.8 the bar is 0.2, whatever the verbosity"


def test_no_opinion_keeps_the_old_behaviour(claude_home, no_audio, asked, generated):
    asked.p = None
    _engine("terse").run(dict(_BASH))
    assert generated == ["terse"]


@pytest.mark.parametrize("verbosity", ["chatty", "anomaly"])
def test_chatty_and_anomaly_never_ask(verbosity, claude_home, no_audio, asked, generated):
    _engine(verbosity).run(dict(_BASH))
    assert asked.states == []


def test_a_queued_call_asks_nobody(claude_home, no_audio, asked, generated):
    """The decider is asked once per flush, never per tool call."""
    from engines.base import derive_project_label
    eng = _engine(min_tool_calls=5)
    st = ss.load_session(derive_project_label())
    st["last_voiced_time"] = st["last_flush_time"] = time.time()
    ss.save_session(st)
    for _ in range(3):
        eng.run(dict(_BASH))
    assert asked.states == []


# ── The phrasing prompt once the decider has said "speak" ─────────────────


def test_the_decided_prompt_offers_no_skip():
    from phrase_gen import _build_messages
    messages = _build_messages("PreToolUse", "Bash: Commit the retry fix", "No announcements yet.",
                               verbosity="decided")
    assert all(m["content"] != "SKIP" for m in messages if m["role"] == "assistant")
    assert "SKIP" not in messages[0]["content"].replace("Do not SKIP", "")
    assert "nothing has failed" in messages[0]["content"], (
        "a batch has not run yet; without this the model invents failures for it")


def test_commentary_gets_no_task_line(claude_home, no_audio, asked, monkeypatch, tmp_path):
    """Given the request, commentary spoke it as the action; see _TASK_RULE."""
    import json
    import phrase_gen
    contexts = []
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: (contexts.append(a[2]), ("done", "I'm committing."))[1])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(
        {"type": "user", "message": {"content": "make the retry survive a token refresh"}}))
    asked.p = 0.9
    _engine().run({**_BASH, "transcript_path": str(transcript)})
    assert contexts and "Task:" not in contexts[0]
    assert all("token refresh" not in s for s in asked.states)
