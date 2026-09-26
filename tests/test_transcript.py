"""find_transcript, last_assistant_text, last_transcript_context."""

import json
import os

import pytest

import engines.base as base


def _projects_dir(claude_home):
    p = claude_home / "projects"
    p.mkdir()
    return p


def test_find_transcript_prefers_transcript_path(claude_home, tmp_path):
    f = tmp_path / "direct.jsonl"
    f.write_text("{}", encoding="utf-8")
    assert base.find_transcript({"transcript_path": str(f)}) == str(f)


def test_find_transcript_ignores_missing_transcript_path(claude_home):
    projects = _projects_dir(claude_home)
    proj = projects / "myproj"
    proj.mkdir()
    target = proj / "abc123.jsonl"
    target.write_text("{}", encoding="utf-8")
    got = base.find_transcript({"transcript_path": "/no/such.jsonl", "session_id": "abc123"})
    assert got == str(target)


def test_find_transcript_by_session_id(claude_home):
    projects = _projects_dir(claude_home)
    proj = projects / "p"
    proj.mkdir()
    target = proj / "sess.jsonl"
    target.write_text("{}", encoding="utf-8")
    assert base.find_transcript({"session_id": "sess"}) == str(target)


def test_find_transcript_mtime_fallback(claude_home):
    projects = _projects_dir(claude_home)
    proj = projects / "p"
    proj.mkdir()
    old = proj / "old.jsonl"
    new = proj / "new.jsonl"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    os.utime(str(old), (1, 1))
    os.utime(str(new), (10_000_000, 10_000_000))
    assert base.find_transcript({}) == str(new)


def test_find_transcript_none_when_empty(claude_home):
    _projects_dir(claude_home)
    assert base.find_transcript({}) is None


def _jsonl(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def test_last_assistant_text_returns_last_text(tmp_path):
    f = tmp_path / "t.jsonl"
    _jsonl(f, [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "last"}]}},
    ])
    assert base.last_assistant_text(str(f)) == "last"


def test_last_assistant_text_survives_non_dict_blocks(tmp_path):
    f = tmp_path / "t.jsonl"
    _jsonl(f, [
        {"type": "assistant", "message": {"content": "a plain string"}},
        {"type": "assistant", "message": {"content": [1, 2, {"type": "text", "text": "ok"}]}},
    ])
    # Must not raise AttributeError on the int/str blocks.
    assert base.last_assistant_text(str(f)) == "ok"


def test_last_transcript_context_collects_turns(tmp_path):
    f = tmp_path / "t.jsonl"
    _jsonl(f, [
        {"type": "user", "message": {"content": "do the thing"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I did the thing"}]}},
    ])
    ctx = base.last_transcript_context(str(f), max_turns=4)
    assert "do the thing" in ctx
    assert "I did the thing" in ctx


# ── What counts as a turn, and what the work is for ────────────────────────


def _prompt(text, **extra):
    return {"type": "user", "message": {"content": text}, **extra}


def _said(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def test_injected_user_entries_are_not_turns(tmp_path):
    """The Stop that finished the Jev gate was spoken as "I finished grabbing
    attention in claudio": the grab-attention skill's body had taken a turn."""
    f = tmp_path / "t.jsonl"
    _jsonl(f, [
        _prompt("let jev decide which batches are worth hearing"),
        _said("On it."),
        _prompt("Base directory for this skill: grab-attention", isMeta=True),
        _prompt("<command-name>/clear</command-name>"),
        _prompt("<task-notification> <task-id>b9z</task-id> build finished",
                origin={"kind": "task-notification"}),
        _prompt("summary of the earlier conversation", isCompactSummary=True),
        _prompt("<bash-input>pwd</bash-input>"),
        {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        _said("Jev now decides which batches get spoken."),
    ])
    ctx = base.last_transcript_context(str(f), max_turns=4)
    assert "grab-attention" not in ctx
    assert "/clear" not in ctx and "interrupted" not in ctx
    assert "task-" not in ctx and "summary" not in ctx and "pwd" not in ctx
    assert ctx.splitlines() == [
        "Earlier: User: let jev decide which batches are worth hearing | Agent: On it.",
        "Last message: Jev now decides which batches get spoken.",
    ]


def test_a_long_turn_keeps_its_ending():
    """The verdict is at the end: snippet(text)[:300] cut the question off."""
    opening = "I traced the flake to the token refresh racing the login call. " * 6
    turn = opening + "Should I move the refresh behind the mutex or retry the call?"
    out = base.condense_turn(turn, 300)
    assert len(out) <= 300
    assert out.endswith("retry the call?")
    assert out.startswith("I traced the flake")


def test_a_short_turn_is_unchanged():
    assert base.condense_turn("  Pushed   to origin.  ") == "Pushed to origin."


def test_one_long_final_sentence_still_fits():
    out = base.condense_turn("word " * 200 + "end?", 300)
    assert len(out) <= 300 and out.endswith("end?")


def test_the_request_is_the_last_prompt_that_says_something(tmp_path):
    f = tmp_path / "t.jsonl"
    _jsonl(f, [
        _prompt("stop the nudge repeating the same sentence"),
        _said("Should I also cap the number of nudges?"),
        _prompt("yes"),
        _prompt("Another Claude session sent a message: hand-back", isMeta=True),
        _said("Done."),
    ])
    assert base.last_user_request(str(f)) == "stop the nudge repeating the same sentence"


def test_the_request_leaves_pasted_text_and_reminders_out(tmp_path):
    f = tmp_path / "t.jsonl"
    _jsonl(f, [_prompt(
        "fix what this log shows <pasted_content id=\"a1\">Traceback ... 400 lines"
        "</pasted_content><system-reminder>be brief</system-reminder>")])
    assert base.last_user_request(str(f)) == "fix what this log shows [pasted text]"


def test_the_request_is_read_from_the_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_REQUEST_TAIL_BYTES", 200)
    f = tmp_path / "t.jsonl"
    _jsonl(f, [_prompt("an old request far back in the file")]
           + [_said("working on it " * 3)] * 20)
    assert base.last_user_request(str(f)) is None
    assert base.last_user_request(None) is None
    assert base.last_user_request(str(tmp_path / "missing.jsonl")) is None


def test_the_task_line_comes_only_from_this_hooks_transcript(tmp_path, claude_home):
    """find_transcript's last resort is the newest transcript of any project,
    which would voice another tab's request."""
    other = claude_home / "projects" / "other"
    other.mkdir(parents=True)
    _jsonl(other / "s.jsonl", [_prompt("another tab's request entirely")])
    assert base.request_for_prompt({"session_id": "nope"}) is None

    f = tmp_path / "t.jsonl"
    _jsonl(f, [_prompt("stop the nudge repeating the same sentence")])
    hook = {"transcript_path": str(f)}
    assert base.request_for_prompt(hook) == "stop the nudge repeating the same sentence"
    # A Stop whose turns already hold the request does not get it twice.
    assert base.request_for_prompt(
        hook, "User: stop the nudge repeating the same sentence\nAgent: done") is None


def test_a_stop_awaits_an_answer_only_when_the_agent_asks_last():
    assert base.stop_awaits_answer("Earlier: User: pick one\nLast message: Both done. A or B?")
    assert base.stop_awaits_answer("Last message: **Go?**")
    assert not base.stop_awaits_answer("Last message: Should I? I did. It works. All pushed. Done.")
    assert not base.stop_awaits_answer("Earlier: Agent: A or B?\nLast message: Went with A.")
    assert not base.stop_awaits_answer(None)


@pytest.mark.parametrize("ending", [
    "Waiting for your go-ahead on the commit: `approve` or `approve + resolve`.",
    "Tell me the two codes and I will prepare the branch.",
    "Annotate what you want and close the tab.",
    "Say **go** and I run it; or tell me to cut it.",
    "The restore deletes uncommitted work, so you must decide.",
    "My recommendation: A. It matches the name.",
    "Which one? The OBS stream is still worth keeping. I should have measured.",
])
def test_waiting_on_the_developer_without_a_trailing_question(ending):
    assert base.awaits_developer("I looked into it and found the cause. " + ending)


@pytest.mark.parametrize("ending", [
    "I will not post it without your approval.",
    "I sent you a notification that this is done.",
    "Say the word and I will do the hardening pass, or leave it as-is.",
    "Pushed to origin. All 578 tests pass.",
    "Want me to draft the Slack reply?",
    "Should I also run the code review on 5950?",
    "See the thread (https://x.slack.com/p1?thread_ts=17) for context.",
])
def test_finished_work_with_an_offer_is_not_waiting(ending):
    assert not base.awaits_developer("I finished the review. " + ending)


# Real endings, from Stops hand-labelled as waiting on the agent's own work.
@pytest.mark.parametrize("ending", [
    "Three images-arm runs in flight. Waiting on their notifications before launching the pair.",
    "Compile running. Once it is green I will install and verify on the emulator.",
    "Still waiting on the data/domain and presentation/Compose verifiers.",
    "Leaderboard report is in. Two more agents (Quests, Profile) still running.",
    "Heartbeats only (15). Waiter alive, no reports yet. Nothing to act on.",
    "Holding.",
])
def test_waiting_on_its_own_work(ending):
    assert base.works_on_its_own("The first arm got it right. " + ending)


@pytest.mark.parametrize("ending", [
    # A process left running is not work being waited on.
    "The emulator is still running with mocks off; say the word to shut it down.",
    # Other people, not the agent: the work is finished.
    "The PR is up and waiting for review.",
    "The QA companion PR is still waiting on another agent via the handoff doc.",
    # The wait is on you.
    "Waiting on the Plannotator close for task 1. Nothing else to run until it returns.",
    "The review agent is still running. Which of the two fixes do you want first?",
    "Holding. Nothing further from me until you reply.",
    "Pushed to origin. All 620 tests pass.",
    # Waits on you, phrased as a when (found in review).
    "Reinstalled on the emulator. Please confirm when it looks right.",
    "Run `make deploy` and let me know when it finishes.",
    "Kick off the release build on your machine; tell me when it lands.",
    "Payload validated. Standing by on [3] until `acli` is installed.",
    # The opposite of work in flight, or a description of code.
    "Nothing in flight.",
    "You flipped the flag while the retry was in flight, so the commit landed unsigned.",
    "All tests pass. Nothing else to run.",
    "I reviewed the overnight logs. Nothing actionable.",
    "Fixed the race: the lock is now released after the build finishes.",
    "Done. The dev server is running in the background on port 3000.",
    "PR #412 is open and waiting on reviewers.",
    "Any agent running `pgrep` here will see those instructions.",
    # Real Stops: a question, even an offer, or a plain "from you", is a turn to hear.
    "My census is a raw field count. Want me to wait for it, or act on the above now?",
    "The index worker is still running in its own terminal. Want me to dispatch APP-1204 now?",
    "Reviewer running in background. Meanwhile still need from you: the new repo name.",
    "Still holding on your nod: the three one-hop transitions and the follow-up ticket.",
])
def test_not_waiting_on_its_own_work(ending):
    assert not base.works_on_its_own("The first arm got it right. " + ending)


def test_a_stop_works_on_its_own_only_when_its_last_message_says_so():
    assert base.stop_works_on_its_own("Earlier: User: go\nLast message: Holding.")
    assert not base.stop_works_on_its_own("Earlier: Agent: Build running.\nLast message: Done.")
    assert not base.stop_works_on_its_own(None)


def test_the_last_message_label_does_not_open_its_first_sentence():
    """An offer opening the message is still an offer, not a question."""
    assert not base.stop_awaits_answer("Last message: Want me to draft the Slack reply?")


def test_agent_text_is_read_as_prose():
    """One real Stop's ending was its "Sources:" list of three URLs."""
    assert base._prose(
        "Fixed.\n```\nx = 1\n```\nSee [the notes](https://a.b/c) or https://x.y/z.\n\n"
        "**Sources:**\n- [A](https://a)\n- [B](https://b)"
    ) == "Fixed. [code] See the notes or a link."
