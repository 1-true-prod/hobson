import pytest

from recap import build_prompt, recent_activity

LINES = [
    "[10:00:00] [hobson] [pocket-tts] [PreToolUse] batch of 3 (m, normal) -> done -> 'I edited the parser.'",
    "[10:02:00] [other proj] [pocket-tts] [Stop] (m) -> done -> 'Not my project.'",
    "[10:05:00] [hobson] gen[Stop] 0.4s attempt=1/1 model=m detail='Bash: run the tests' raw='x' -> done spoken='I ran the tests.'",
    "[09:00:00] [hobson] [pocket-tts] [Stop] (m) -> done -> 'Too old.'",
    "garbage line",
]


def _now(h, m):
    import time
    return time.mktime((2026, 8, 21, h, m, 0, 0, 0, -1))


def test_only_this_project_is_included():
    items = recent_activity(LINES, "hobson", minutes=10, now=_now(10, 6))
    assert not any("Not my project" in i for i in items)


def test_only_the_window_is_included():
    items = recent_activity(LINES, "hobson", minutes=10, now=_now(10, 6))
    assert not any("Too old" in i for i in items)
    assert any("parser" in i for i in items)


def test_junk_lines_never_raise():
    assert recent_activity(["", "garbage", "[oops"], "hobson", now=_now(10, 6)) == []


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


# Every line written since the log was dated starts "[YYYY-MM-DD HH:MM:SS]".
# recap matched only the undated form, so it found nothing at all.
DATED = [
    "[2026-08-21 10:04:00] [hobson] gen[Stop] 0.4s attempt=1/2 model=m "
    "detail='Last message: Pushed.' raw='done | I pushed the branch.' -> done spoken='I pushed the branch.'",
    "[2026-08-21 10:04:00] [hobson] [pocket-tts] [Stop] (m) -> done -> 'I pushed the branch.'",
    "[2026-08-21 10:04:01] [hobson] [pocket-tts] barked (daemon-live) -> 'I pushed the branch.'",
    "[2026-08-20 10:05:00] [hobson] [pocket-tts] [Stop] (m) -> done -> 'Yesterday, same clock time.'",
]


def test_dated_lines_are_read():
    items = recent_activity(DATED, "hobson", minutes=10, now=_now(10, 6))
    assert "I pushed the branch." in items


def test_a_phrase_logged_three_ways_is_one_item():
    """The trace, the event line and the playback line all carry it."""
    items = recent_activity(DATED, "hobson", minutes=10, now=_now(10, 6))
    assert items.count("I pushed the branch.") == 1


def test_a_dated_line_keeps_its_own_date():
    """Reconstructing the date from the clock time would put yesterday's
    10:05 inside a 10:06 window."""
    items = recent_activity(DATED, "hobson", minutes=10, now=_now(10, 6))
    assert not any("Yesterday" in i for i in items)


def test_dated_and_legacy_lines_mix():
    items = recent_activity(LINES + DATED, "hobson", minutes=10, now=_now(10, 6))
    assert any("parser" in i for i in items) and "I pushed the branch." in items


# ── Hobson's own talk is not work ───────────────────────────────────────────

OWN = [
    "[2026-08-21 10:01:00] [hobson] [pocket-tts] barked (daemon-live) [done] -> 'I pushed the branch.'",
    "[2026-08-21 10:02:00] [unknown] [presence] briefing (1 held) -> 'Welcome back. On hobson: I pushed the branch.'",
    "[2026-08-21 10:02:01] [hobson] [pocket-tts] barked (daemon-live) -> 'Welcome back. On hobson: I pushed the branch.'",
    "[2026-08-21 10:03:00] [hobson] [pocket-tts] barked (daemon-live) [answer] -> 'On hobson: pushed the branch.'",
    "[2026-08-21 10:03:30] [hobson] [nudge] spoke (1/3) -> 'Hobson is still waiting on you.'",
    "[2026-08-21 10:04:00] [hobson] [pocket-tts] barked (daemon-live) [nudge] -> 'Hobson is still waiting on you.'",
]


def test_what_hobson_said_for_himself_is_not_activity():
    """A briefing, an answer and a nudge were read back as work: the next
    recap retold them. The old playback line has no kind, but the line that
    composed the briefing does."""
    items = recent_activity(OWN, "hobson", minutes=10, now=_now(10, 6))
    assert items == ["I pushed the branch."]


def test_two_recaps_in_a_row_do_not_retell_each_other(monkeypatch):
    import recap
    first = list(OWN)
    monkeypatch.setattr(recap, "summarise", lambda prompt, config, timeout, limit=0, check=None: (
        "Pushed the branch." if "pushed the branch" in prompt.lower() else "Retold the recap.", None))
    text, _ = recap.across_projects(first, {}, now=_now(10, 6))
    assert text == "On hobson: pushed the branch."
    first.append("[2026-08-21 10:05:00] [hobson] [pocket-tts] barked (daemon-live) [answer] -> " + repr(text))
    again, _ = recap.across_projects(first, {}, now=_now(10, 6))
    assert again == text


# ── Across projects ─────────────────────────────────────────────────────────

MANY = [
    "[2026-08-21 10:00:00] [webapp] [pocket-tts] [Stop] (m) -> done -> 'I fixed the login redirect.'",
    "[2026-08-21 10:10:00] [hobson] [pocket-tts] [Stop] (m) -> done -> 'I shipped the face.'",
    "[2026-08-21 10:20:00] [mobile app] [pocket-tts] [Stop] (m) -> broken -> 'The build failed.'",
    "[2026-08-21 10:25:00] [docs] [pocket-tts] [Stop] (m) -> done -> 'I wrote the README.'",
    "[2026-08-21 09:00:00] [old] [pocket-tts] [Stop] (m) -> done -> 'Long ago.'",
    "[2026-08-21 10:26:00] [unknown] [face] shown 'I wrote the README.' for docs, 3.0s, following audio 2.1s, gone: said",
]


def test_projects_come_most_recent_first_three_at_most():
    from recap import recent_by_project
    groups = recent_by_project(MANY, minutes=30, now=_now(10, 27))
    assert [p for p, _ in groups] == ["docs", "mobile app", "hobson"]
    assert groups[0][1] == ["I wrote the README."]


def _fake_summaries(monkeypatch, replies):
    import recap
    calls = []

    def summarise(prompt, config, timeout, limit=0, check=None):
        project = next(p for p in replies if replies[p][0] in prompt)
        calls.append((project, timeout))
        text, err = replies[project][1]
        why = check(text) if text and check else None
        return (None, why) if why else (text, err)

    monkeypatch.setattr(recap, "summarise", summarise)
    return calls


def test_one_call_per_project_joined_in_code(monkeypatch):
    import recap
    _fake_summaries(monkeypatch, {
        "docs": ("README", ("Wrote the README.", None)),
        "mobile app": ("build failed", ("The build failed on the API.", None)),
        "hobson": ("face", ("Shipped the face.", None)),
    })
    text, notes = recap.across_projects(MANY, {}, now=_now(10, 27), clock=iter(range(100)).__next__)
    assert text == ("On docs: wrote the README. On mobile app: the build failed on the API. "
                    "On hobson: shipped the face.")
    assert notes == ["docs 1.0s", "mobile app 1.0s", "hobson 1.0s"]


def test_a_failed_call_says_what_the_session_said_itself(monkeypatch):
    """Its own latest line is true: it was spoken. A project with nothing
    sayable is left out, and nothing at all is None."""
    import recap
    _fake_summaries(monkeypatch, {
        "docs": ("README", (None, "timed out")),
        "mobile app": ("build failed", ("The build failed.", None)),
        "hobson": ("face", (None, "timed out")),
    })
    text, notes = recap.across_projects(MANY, {}, now=_now(10, 27), clock=iter(range(100)).__next__)
    assert text == "On docs: I wrote the README. On mobile app: the build failed. On hobson: I shipped the face."
    assert notes[0] == "docs said as logged, the model's reply dropped (timed out) after 1.0s"
    summary_only = ["[2026-08-21 10:25:00] [docs] gen[PreToolUse] 0.4s attempt=1/1 model=m "
                    "detail='2 Bash (run it); Write: a.py' raw='SKIP' -> SKIP"]
    _fake_summaries(monkeypatch, {"docs": ("Bash", (None, "down"))})
    assert recap.across_projects(summary_only, {}, now=_now(10, 27))[0] is None
    assert recap.across_projects([], {}, now=_now(10, 27)) == ("", [])


def test_a_reply_about_something_not_in_the_notes_is_not_said(monkeypatch):
    """Live: "reran tests on odio dot P Y, flush gate dot P Y" for a session
    whose notes said neither."""
    import recap
    notes = ["I found Odio's info and phone numbers."]
    assert recap._not_in_the_notes("Found Odio's info and phone numbers.", notes) is None
    assert recap._not_in_the_notes("Reran tests on odio.py and the flush gate.", notes) == ["flus", "gate"]
    _fake_summaries(monkeypatch, {"docs": ("README", ("Reran tests on the flush gate module.", None))})
    text, got = recap.across_projects(MANY[3:4], {}, now=_now(10, 27))
    assert text == "On docs: I wrote the README." and "not in the notes" in got[0]


def test_the_whole_recap_keeps_to_one_budget(monkeypatch):
    """Three calls at 20s each would outlast "One moment." by a long way."""
    import recap
    calls = _fake_summaries(monkeypatch, {
        "docs": ("README", ("Wrote the README.", None)),
        "mobile app": ("build failed", ("The build failed.", None)),
        "hobson": ("face", ("Shipped the face.", None)),
    })
    # The deadline; then per project: time left, start, end. Docs 0-15s, mobile 15-24.5s,
    # and 0.5s left for hobson.
    ticks = iter([0, 0, 0, 15, 15, 15, 24.5, 24.5])
    text, notes = recap.across_projects(MANY, {}, now=_now(10, 27), clock=lambda: next(ticks))
    assert [c[0] for c in calls] == ["docs", "mobile app"]
    assert calls[1][1] == pytest.approx(10.0)
    assert notes[-1] == "hobson out of time" and text.count("On ") == 2


def test_a_summary_goes_through_the_model_and_is_capped(fake_ollama):
    import recap
    fake_ollama.chat("Fixed the login redirect and reran every single one of the tests in the suite again today.")
    text, err = recap.summarise("p", {"ollama": {"model": "m", "url": "http://x"}}, 5, limit=8)
    assert err is None and len(text.split()) <= 8


def test_a_reply_that_copies_the_prompts_example_is_dropped(monkeypatch):
    """Live: llama3.2:3b said "fixed the login redirect and reran the suite",
    the prompt's example, of a project that had done neither."""
    import recap
    prompt = recap.build_project_prompt(["I'm adding thinking modes to the page."])
    assert "login redirect" not in prompt and "flush gate" not in prompt  # no examples to copy
    assert not recap._echoes_the_prompt("Added thinking modes to the page.", prompt)
    assert recap._echoes_the_prompt("Used only what the notes say about the page.", prompt)
