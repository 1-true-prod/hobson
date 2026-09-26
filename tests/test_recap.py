from recap import build_prompt, recent_activity

LINES = [
    "[10:00:00] [claudio] [pocket-tts] [PreToolUse] batch of 3 (m, normal) -> done -> 'I edited the parser.'",
    "[10:02:00] [other proj] [pocket-tts] [Stop] (m) -> done -> 'Not my project.'",
    "[10:05:00] [claudio] gen[Stop] 0.4s attempt=1/1 model=m detail='Bash: run the tests' raw='x' -> done spoken='I ran the tests.'",
    "[09:00:00] [claudio] [pocket-tts] [Stop] (m) -> done -> 'Too old.'",
    "garbage line",
]


def _now(h, m):
    import time
    return time.mktime((2026, 8, 21, h, m, 0, 0, 0, -1))


def test_only_this_project_is_included():
    items = recent_activity(LINES, "claudio", minutes=10, now=_now(10, 6))
    assert not any("Not my project" in i for i in items)


def test_only_the_window_is_included():
    items = recent_activity(LINES, "claudio", minutes=10, now=_now(10, 6))
    assert not any("Too old" in i for i in items)
    assert any("parser" in i for i in items)


def test_junk_lines_never_raise():
    assert recent_activity(["", "garbage", "[oops"], "claudio", now=_now(10, 6)) == []


def test_empty_activity_produces_no_prompt():
    assert build_prompt([]) is None


def test_prompt_mentions_the_activity_and_asks_for_brevity():
    p = build_prompt(["I edited the parser.", "I ran the tests."])
    assert "parser" in p and "tests" in p
    assert "sentence" in p.lower() or "words" in p.lower()


def test_short_recaps_pass_through_untouched():
    from recap import _trim_to_words
    text = "I rewired the flush gate and reran the suite."
    assert _trim_to_words(text) == text


def test_long_recaps_are_trimmed_at_a_sentence_boundary():
    """Prompt rules are a bias, not a guarantee — observed replies ran 41-44
    words against a 35-word instruction, so the cap is enforced in code."""
    from recap import _trim_to_words
    text = ("I rewired the flush gate and reran the suite. " + "then " * 40).strip()
    out = _trim_to_words(text, limit=12)
    assert out.endswith(".")
    assert len(out.split()) <= 12


def test_a_single_runaway_sentence_is_still_capped():
    from recap import _trim_to_words
    out = _trim_to_words("word " * 100, limit=10)
    assert len(out.split()) <= 10
    assert out.endswith(".")


def test_trimming_handles_empty_input():
    from recap import _trim_to_words
    assert _trim_to_words("") == ""
    assert _trim_to_words(None) is None


def test_status_report_phrasing_is_rewritten():
    """The user's words: 'i hate project manager style speaking'."""
    from recap import _strip_pm_speak
    text, stripped = _strip_pm_speak(
        "I've had a big shift with my research, which changed everything, "
        "multiple times actually, before the refactor.")
    assert stripped, "must report what it changed, not silently correct"
    assert "big shift" not in text
    assert "changed everything" not in text
    assert "multiple times actually" not in text


def test_worked_on_becomes_something_concrete():
    from recap import _strip_pm_speak
    text, _ = _strip_pm_speak("I've been working on the flush gate.")
    assert "working on" not in text
    assert "flush gate" in text


def test_plain_speech_is_left_alone():
    from recap import _strip_pm_speak
    original = "Reordered the plan tasks and reran the suite."
    text, stripped = _strip_pm_speak(original)
    assert text == original
    assert stripped == ()


def test_stripping_repairs_spacing_and_capitalisation():
    from recap import _strip_pm_speak
    text, _ = _strip_pm_speak("which changed everything, I reran the suite.")
    assert text[0].isupper()
    assert "  " not in text
    assert " ," not in text
