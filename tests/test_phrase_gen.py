"""phrase_gen: _clean_phrase, _parse_response, generate_or_skip, _build_messages."""

import time

import pytest

import phrase_gen


# ── _clean_phrase ──────────────────────────────────────────────────────────

def test_clean_phrase_prepends_i_to_verb_phrase():
    # Subject-less verb phrase (model dropped the leading "I") -> repaired.
    assert phrase_gen._clean_phrase("Finished the whole migration cleanly.") == \
        "I finished the whole migration cleanly."


def test_clean_phrase_prepends_i_on_past_tense_ed():
    assert phrase_gen._clean_phrase("Pushed the commit to origin.") == \
        "I pushed the commit to origin."


def test_clean_phrase_leaves_phrase_with_subject():
    # Already has its own subject — must NOT become "I the build failed."
    assert phrase_gen._clean_phrase("The build failed on line forty.") == \
        "The build failed on line forty."


def test_clean_phrase_leaves_non_verb_lead():
    assert phrase_gen._clean_phrase("Very good sir, the task is complete.") == \
        "Very good sir, the task is complete."


def test_clean_phrase_leaves_ing_lead_untouched():
    # "-ing" excluded — "I running" would be ungrammatical.
    assert phrase_gen._clean_phrase("Running the full test suite.") == \
        "Running the full test suite."


def test_clean_phrase_keeps_existing_i():
    assert phrase_gen._clean_phrase("I finished the migration cleanly.") == \
        "I finished the migration cleanly."


def test_clean_phrase_strips_wrapping_quotes():
    assert phrase_gen._clean_phrase('"I wrapped up the work."') == \
        "I wrapped up the work."


def test_clean_phrase_strips_wrapping_asterisks():
    assert phrase_gen._clean_phrase("**I wrapped up the work.**") == \
        "I wrapped up the work."


def test_clean_phrase_strips_think_tags():
    out = phrase_gen._clean_phrase("<think>hmm</think>I shipped the fix today.")
    assert out == "I shipped the fix today."


def test_clean_phrase_rejects_too_short():
    # word-count gate is MIN_WORDS-MAX_WORDS (4-12)
    assert phrase_gen._clean_phrase("Done.") is None


def test_clean_phrase_rejects_too_long():
    assert phrase_gen._clean_phrase(" ".join(["word"] * 25) + ".") is None


def test_clean_phrase_accepts_exactly_max_words():
    # MAX_WORDS=12 -- the boundary itself must be accepted unchanged, not
    # treated as one-over.
    phrase = "I fixed the login bug in CI after debugging for hours yesterday."
    assert len(phrase.rstrip(".").split()) == 12
    assert phrase_gen._clean_phrase(phrase) == phrase


def test_clean_phrase_salvages_over_budget_by_dropping_lead_in():
    # The model tacked on a throwaway acknowledgment before the real
    # sentence, pushing the combined phrase over MAX_WORDS. The real
    # sentence alone fits, so it is recovered instead of discarding the
    # whole thing.
    raw = "Sure thing. I fixed the login bug in CI after debugging for hours yesterday."
    assert phrase_gen._clean_phrase(raw) == \
        "I fixed the login bug in CI after debugging for hours yesterday."


def test_clean_phrase_over_budget_with_no_recoverable_sentence_returns_none():
    # One long run-on sentence with no earlier clean boundary anywhere in
    # it -- there is nothing shorter to recover, so it must give up rather
    # than hard-truncate.
    raw = " ".join(["word"] * 15) + "."
    assert phrase_gen._clean_phrase(raw) is None


def test_clean_phrase_rejects_mid_word_truncation():
    # Cut off by a token limit before finishing "investigating locally" (or
    # similar) -- ends on a lone letter, not a real word. Word count alone
    # (10, within budget) would let this through, so it needs its own gate.
    raw = "Flaky test failure in Android demo app CI, investigating l"
    assert phrase_gen._clean_phrase(raw) is None


def test_clean_phrase_counts_the_budget_as_the_model_wrote_it():
    # Real Stop that went silent: 10 words as written, 13 once "CLAUDE.md"
    # is spelled out for speech.
    assert phrase_gen._clean_phrase(
        "I updated CLAUDE.md and checked the largest real prompt size.") == \
        "I updated CLAUDE dot M D and checked the largest real prompt size."


def test_clean_phrase_still_counts_the_spoken_words_for_the_minimum():
    # Three words as written, six spoken: accepted before, and still.
    assert phrase_gen._clean_phrase("I checked CLAUDE.md.") == "I checked CLAUDE dot M D."


@pytest.mark.parametrize("raw", [
    "I'm starting on APP-1201.",       # spoken "I'm starting on."
    "Finished APP-1201 and APP-1202.",  # spoken "I finished and."
])
def test_clean_phrase_counts_the_spoken_words_when_normalization_drops_some(raw):
    assert phrase_gen._clean_phrase(raw) is None


def test_clean_phrase_rejects_too_short_regression():
    # MIN_WORDS path must be untouched by the over-budget salvage changes.
    assert phrase_gen._clean_phrase("Fixed it.") is None


# ── _parse_response ────────────────────────────────────────────────────────

def test_parse_response_with_category():
    cat, phrase = phrase_gen._parse_response("broken | The build failed on line forty.")
    assert cat == "broken"
    assert "build failed" in phrase


def test_parse_response_bare_phrase_defaults_done():
    cat, phrase = phrase_gen._parse_response("The deployment finished successfully now.")
    assert cat == "done"


def test_parse_response_skip():
    assert phrase_gen._parse_response("SKIP") is None


def test_parse_response_skip_in_category():
    assert phrase_gen._parse_response("skip | whatever phrase here friend") is None


def test_parse_response_invalid_category_falls_back_to_bare():
    # invalid category before '|' -> treated as bare phrase, default 'done'
    result = phrase_gen._parse_response("banana | The task is now fully complete.")
    assert result is not None
    assert result[0] == "done"


# ── generate_or_skip (mocked Ollama) ───────────────────────────────────────

def test_generate_or_skip_voices(claude_home, fake_ollama):
    fake_ollama.chat("done | I finished the refactor cleanly.")
    cat, phrase = phrase_gen.generate_or_skip("Stop", "detail", "ctx")
    assert cat == "done"
    assert phrase.startswith("I ")
    assert any("/api/chat" in u for u in fake_ollama.urls)


def test_generate_or_skip_skip_returns_none_tuple(claude_home, fake_ollama):
    fake_ollama.chat("SKIP")
    assert phrase_gen.generate_or_skip("PreToolUse", "Read: foo", "ctx") == (None, None)


def test_generate_or_skip_error_returns_none_tuple(claude_home, fake_ollama):
    from urllib.error import URLError
    fake_ollama.error(URLError("down"))
    assert phrase_gen.generate_or_skip("Stop", "d", "c") == (None, None)


# ── _build_messages verbosity selection ────────────────────────────────────

def test_build_messages_uses_verbosity_rules():
    terse = phrase_gen._build_messages("PreToolUse", "Edit: x", "ctx", verbosity="terse")
    normal = phrase_gen._build_messages("PreToolUse", "Edit: x", "ctx", verbosity="normal")
    terse_blob = " ".join(m["content"] for m in terse)
    normal_blob = " ".join(m["content"] for m in normal)
    assert "Most events should be SKIP" in terse_blob
    assert "Lean toward VOICE" in normal_blob


def test_clean_phrase_keeps_legitimate_trailing_digit():
    """A phrase ending on a bare digit is fine when the sentence completed.

    The mid-word truncation guard keys on a trailing single character, but
    "I'm implementing Phase 1." and "I reviewed the spec for Task 2." end
    that way legitimately. Measured against every phrase hobson has
    spoken: keying on the missing full stop as well takes the guard from
    75 false rejections down to 0, while still catching the real ones.
    """
    for good in (
        "I'm implementing Phase 1.",
        "I reviewed the spec for Task 2.",
        "I need to fix the confusion on case 4.",
        "I'm ready for Batch B.",
    ):
        assert phrase_gen._clean_phrase(good) is not None, good


def test_clean_phrase_still_rejects_unterminated_fragment():
    """Truncation leaves no full stop -- that is what separates it."""
    assert phrase_gen._clean_phrase(
        "Flaky test failure in Android demo app CI, investigating l"
    ) is None


def test_a_rejected_phrase_keeps_its_category(fake_ollama, claude_home):
    """The phrase failed a guard; the classification did not. The nudge
    reads a Stop's category, and "done" must not become "unknown" just
    because "I finished it" was said a moment ago."""
    fake_ollama.chat("done | I finished the invoice view.")
    recent = [["I finished the invoice view.", time.time()]]
    assert phrase_gen.generate_or_skip(
        "Stop", "d", "c", recent_voiced=recent, seconds_since_last_voiced=5) == ("done", None)


# ── context window ─────────────────────────────────────────────────────────

def _chat_reporting(monkeypatch, prompt_tokens):
    """Stream one reply whose final chunk reports `prompt_tokens`."""
    import json
    from conftest import _FakeResp
    lines = [json.dumps({"message": {"content": "done | I pushed it."}, "done": False}).encode(),
             json.dumps({"message": {"content": ""}, "done": True,
                         "prompt_eval_count": prompt_tokens}).encode()]
    monkeypatch.setattr(phrase_gen, "urlopen", lambda req, timeout=None: _FakeResp(lines=lines))


def test_a_prompt_at_the_window_edge_is_logged(monkeypatch, claude_home):
    """Past num_ctx Ollama drops the earliest message -- the rules -- silently."""
    _chat_reporting(monkeypatch, phrase_gen.NUM_CTX - 10)
    text, err = phrase_gen._chat([{"role": "user", "content": "x"}], None, 5, None, num_predict=32)
    assert text == "done | I pushed it." and err is None
    assert "prompt at the context limit" in (claude_home / "hobson.log").read_text()


def test_a_prompt_with_room_is_not_logged(monkeypatch, claude_home):
    _chat_reporting(monkeypatch, 900)
    phrase_gen._chat([{"role": "user", "content": "x"}], None, 5, None, num_predict=32)
    log = claude_home / "hobson.log"
    assert not log.exists() or "context limit" not in log.read_text()


# ── a Stop that ends on the agent's question ──────────────────────────────

def _stop(context, **k):
    """A Stop's reading, as stop_outcome.read() would hand it over."""
    from stop_outcome import StopReading
    return StopReading(context=context, last_message=context.split("Last message: ")[-1], **k)


def test_a_turn_ending_on_a_question_is_a_question(fake_ollama, claude_home):
    fake_ollama.chat("done | I sketched both approaches.")
    cat, phrase = phrase_gen.generate_or_skip(
        "Stop", "Last message: Both are sketched. A?", "No announcements yet.",
        stop=_stop("Last message: Both are sketched. A?", awaiting_answer=True))
    assert cat == "question" and phrase == "I sketched both approaches."
    assert "done->question" in (claude_home / "hobson.log").read_text()


def test_a_question_turn_stays_a_question_when_nothing_is_said(fake_ollama, claude_home):
    """The nudge reads the category even when the phrase is skipped."""
    fake_ollama.chat("SKIP")
    assert phrase_gen.generate_or_skip(
        "Stop", "Last message: A?", "ctx",
        stop=_stop("Last message: A?", awaiting_answer=True)) == ("question", None)
    fake_ollama.chat("SKIP")
    assert phrase_gen.generate_or_skip("Stop", "Agent: done.", "ctx") == (None, None)


def test_the_question_note_sits_next_to_the_event():
    msgs = phrase_gen._build_messages("Stop", "Agent: A?", "ctx", awaiting_answer=True)
    plain = phrase_gen._build_messages("Stop", "Agent: A?", "ctx")
    assert "waiting for the answer" in msgs[-1]["content"]
    assert msgs[:-1] == plain[:-1], "the cached prefix is unchanged"


def test_the_task_rule_and_example_are_for_stop_only():
    stop = phrase_gen._build_messages("Stop", "Agent: done.", "Task: x\nctx")
    commentary = phrase_gen._build_messages("PreToolUse", "Bash: push", "ctx", verbosity="normal")
    assert "Task is what the developer asked for" in stop[0]["content"]
    assert any("Session: Task:" in m["content"] for m in stop[2:-1])
    assert "Task" not in commentary[0]["content"]
    assert not any("Task:" in m["content"] for m in commentary)


# ── the Stop retry says what was wrong ────────────────────────────────────

def _scripted_chat(monkeypatch, replies):
    """Answer each _chat call with the next reply; record the messages sent."""
    sent = []
    def fake(messages, *a, **k):
        sent.append((messages, k.get("temperature")))
        return replies[len(sent) - 1], None
    monkeypatch.setattr(phrase_gen, "_chat", fake)
    return sent


def test_an_over_budget_stop_is_retried_with_the_reason(monkeypatch, claude_home):
    long = "done | " + " ".join(["word"] * 15) + "."
    sent = _scripted_chat(monkeypatch, [long, "done | I pushed the voice branch."])
    assert phrase_gen.generate_or_skip("Stop", "Last message: pushed.", "ctx") == \
        ("done", "I pushed the voice branch.")
    assert len(sent) == 2
    retry_msgs, temperature = sent[1]
    assert retry_msgs[-2] == {"role": "assistant", "content": long}
    assert "15 words" in retry_msgs[-1]["content"]
    assert temperature == phrase_gen.STOP_TEMPERATURE


def test_commentary_is_not_retried_and_keeps_its_temperature(monkeypatch, claude_home):
    sent = _scripted_chat(monkeypatch, ["done | " + " ".join(["word"] * 15) + "."])
    assert phrase_gen.generate_or_skip("PreToolUse", "Bash: push", "ctx") == (None, None)
    assert len(sent) == 1 and sent[0][1] == 0.7


def test_a_guard_rejection_is_retried_with_the_reason(monkeypatch, claude_home):
    sent = _scripted_chat(monkeypatch, ["done | I pushed the voice branch.",
                                        "done | I merged the review fixes."])
    recent = [["I pushed the voice branch.", time.time()]]
    assert phrase_gen.generate_or_skip("Stop", "Last message: x", "ctx", recent_voiced=recent) == \
        ("done", "I merged the review fixes.")
    assert "near-duplicate" in sent[1][0][-1]["content"]


def test_an_unfounded_broken_stop_is_retried_then_done(monkeypatch, claude_home):
    """26 "broken" calls on 201 labelled Stops, 2 of them real."""
    sent = _scripted_chat(monkeypatch, ["broken | I hit a problem with the push.",
                                        "broken | I hit a problem with the push."])
    assert phrase_gen.generate_or_skip(
        "Stop", "Last message: Pushed to origin, all tests pass.", "ctx",
        stop=_stop("Last message: Pushed to origin, all tests pass.")) == \
        ("done", "I hit a problem with the push.")
    assert "not broken" in sent[1][0][-1]["content"]


def test_a_broken_stop_that_names_a_failure_stands(monkeypatch, claude_home):
    sent = _scripted_chat(monkeypatch, ["broken | I hit an API error on the retry."])
    assert phrase_gen.generate_or_skip(
        "Stop", "Last message: The API returned an error and I could not recover.", "ctx",
        stop=_stop("Last message: The API returned an error and I could not recover.",
                   failure_reported=True)) == \
        ("broken", "I hit an API error on the retry.")
    assert len(sent) == 1


def test_a_skipped_stop_that_was_checked_is_done(monkeypatch, claude_home):
    """"Unknown" starts a nudge; all 10 skipped, checked Stops were done."""
    _scripted_chat(monkeypatch, ["SKIP", "SKIP", "SKIP"])
    assert phrase_gen.generate_or_skip("Stop", "Last message: x", "c",
                                       stop=_stop("Last message: x")) == ("done", None)
    assert phrase_gen.generate_or_skip("Stop", "Last message: x", "c") == (None, None)
    assert phrase_gen.generate_or_skip("PreToolUse", "Bash: x", "c") == (None, None)
