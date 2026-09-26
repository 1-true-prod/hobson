# tests/test_log_stats.py
from log_stats import summarize

LINES = [
    "[10:00:00] [proj] [pocket-tts] [PreToolUse] queued (2 pending) Bash: run tests",
    "[10:00:01] [proj] [pocket-tts] [PreToolUse] queued (3 pending) Bash: run more",
    "[10:00:02] [proj] [pocket-tts] [PreToolUse] batch of 5 (llama3.2:3b, normal) -> done -> 'I ran the tests.'",
    "[10:00:03] [proj] [pocket-tts] [PreToolUse] batch of 4 skipped (raw: 'SKIP')",
    "[10:00:04] [proj] [pocket-tts] [PreToolUse] skipped (subagent general-purpose)",
    "[10:00:05] [proj] gen[Stop] 0.5s attempt=1/1 model=m detail='x' raw='y' -> done REJECTED='z' (near-duplicate of a recent phrase)",
    "[10:00:06] [proj] silenced (quiet hours 22-8)",
    "[10:00:07] [proj] [pocket-tts] barked (daemon-live) -> 'I ran the tests.'",
    "[10:00:08] [proj] [pocket-tts] [Stop] (llama3.2:3b) -> done -> 'All finished.'",
]


def test_counts_the_commentary_lifecycle():
    s = summarize(LINES)
    assert s["queued"] == 2
    assert s["flushed"] == 2          # one spoken + one skipped
    assert s["flush_spoken"] == 1
    assert s["flush_skipped"] == 1


def test_counts_suppression_reasons():
    s = summarize(LINES)
    assert s["subagent_suppressed"] == 1
    assert s["silenced"] == 1
    assert s["rejected"] == 1


def test_counts_barks_and_events():
    s = summarize(LINES)
    assert s["barked"] == 1
    assert s["events"]["PreToolUse"] == 5
    assert s["events"]["Stop"] == 1


def test_counts_a_stop_that_is_still_working():
    from engines.base import STILL_WORKING_LOG
    s = summarize([f"[2026-09-26 10:00:00] [proj] [pocket-tts] {STILL_WORKING_LOG}"])
    assert s["stop_working"] == 1


def test_empty_log_is_all_zero_and_never_divides_by_zero():
    s = summarize([])
    assert s["queued"] == 0
    assert s["items_per_flush"] == 0.0


def test_items_per_flush_is_averaged_from_the_batch_lines():
    s = summarize(LINES)
    assert s["items_per_flush"] == 4.5   # batches of 5 and 4


def test_junk_lines_are_ignored():
    assert summarize(["", "garbage", "[bad] [line"])["queued"] == 0


def test_speech_rate_uses_the_batching_path_as_its_denominator():
    """The raw [PreToolUse] tag count is the wrong denominator — it includes
    the pre-batching era, chatty mode, and subagent-suppressed calls, none of
    which emit a 'batch of N' line, so the ratio reads ~0 forever."""
    s = summarize(LINES)
    assert s["tool_calls"] == 4          # 2 queued + 2 flushed
    assert s["speech_per_tool_call"] == 0.25   # 1 spoken of 4


def test_speech_rate_is_zero_not_an_error_on_an_empty_log():
    s = summarize([])
    assert s["tool_calls"] == 0
    assert s["speech_per_tool_call"] == 0.0


PROGRESS_LINES = [
    "[10:00:00] [proj] gen[PreToolUse] 0.5s attempt=1/1 model=m detail='Going in circles — the same Bash 3 times in a row. 3 Bash' raw='x' -> broken spoken='That test keeps failing.'",
    "[10:00:01] [proj] [pocket-tts] [PreToolUse] queued (anomaly, nothing notable) Edit: base.py",
    "[10:00:02] [proj] [nudge] started (waiting) for 'proj': 'waiting for you'",
    "[10:00:03] [proj] [nudge] spoke (1/3) -> 'Still waiting on you here.'",
    "[10:00:04] [proj] [nudge] spoke (2/3) -> 'I need you for this.'",
    "[10:00:05] [proj] [nudge] cancelled (user activity) after step 2",
    "[10:00:06] [proj] [nudge] capped (3/3), giving up on 'proj'",
    "[10:00:07] [proj] [watchdog] started for 'proj' (10m)",
    "[10:00:08] [proj] [watchdog] spoke -> \"Nothing's moved on proj for ten minutes.\"",
]


def test_counts_stuck_and_quiet_anomalies():
    s = summarize(PROGRESS_LINES)
    assert s["stuck"] == 1
    assert s["anomaly_quiet"] == 1


def test_counts_the_nudge_lifecycle():
    s = summarize(PROGRESS_LINES)
    assert s["nudge_started"] == 1
    assert s["nudge_spoke"] == 2
    assert s["nudge_cancelled"] == 1
    assert s["nudge_capped"] == 1


def test_counts_the_watchdog():
    s = summarize(PROGRESS_LINES)
    assert s["watchdog_started"] == 1
    assert s["watchdog_spoke"] == 1


def test_progress_counters_are_zero_on_an_empty_log():
    s = summarize([])
    for key in ("stuck", "anomaly_quiet", "nudge_started", "nudge_spoke",
                "nudge_cancelled", "nudge_capped", "watchdog_started",
                "watchdog_spoke"):
        assert s[key] == 0


def test_a_capped_nudge_is_not_counted_as_spoken():
    """Escalation that gave up must be visible as a cap, not as speech."""
    s = summarize(["[10:00:00] [p] [nudge] capped (3/3), giving up on 'p'"])
    assert s["nudge_capped"] == 1
    assert s["nudge_spoke"] == 0


ERROR_LINES = [
    "[10:00:00] [proj] [pocket-tts] [PreToolUse] batch of 4 ERROR: ollama timed out — timed out",
    "[10:00:01] [proj] [pocket-tts] [Stop] ERROR: ollama unreachable — connection refused",
    "[10:00:02] [proj] [pocket-tts] [PreToolUse] batch of 2 SKIP: model chose not to speak",
    "[10:00:03] [proj] [pocket-tts] [Stop] UNUSABLE: could not parse a phrase from 'blah'",
]


def test_errors_are_counted_separately_from_skips():
    """A timeout logged as 'skipped' reads like working-as-intended — which is
    how 366 real timeouts sat unnoticed in the log."""
    s = summarize(ERROR_LINES)
    assert s["gen_errors"] == 2
    assert s["gen_unusable"] == 1


def test_stale_pre_instrumentation_lines_are_reported_not_counted():
    """The log is append-only across weeks of code changes. Mixing a
    historical numerator with a current-code denominator produces a
    meaningless rate — it read as a 42% timeout rate that was really 0%."""
    old = ["[16:57:37] [kokoro-rt] [PreToolUse] skipped (raw: '(error: timed out)')"]
    s = summarize(old)
    assert s["stale_lines"] == 1
    assert s["gen_errors"] == 0, "a stale line must not inflate the live error count"


def test_current_format_lines_are_not_marked_stale():
    s = summarize(ERROR_LINES)
    assert s["stale_lines"] == 0


def test_a_flush_that_said_nothing_is_never_counted_as_spoken():
    """Only 'batch of N (...) -> category -> phrase' spoke. The verdict lines
    that replaced 'skipped (raw: ...)' carry no 'skipped', and were read as
    speech."""
    s = summarize([
        "[10:00:00] [p] [say] [PreToolUse] batch of 5 SKIP: model chose not to speak",
        "[10:00:01] [p] [say] [PreToolUse] batch of 5 REJECTED: guard rejected — near-duplicate",
        "[10:00:02] [p] [say] [PreToolUse] batch of 5 UNUSABLE: could not parse a phrase from 'x'",
        "[10:00:03] [p] [say] [PreToolUse] batch of 5 ERROR: ollama timed out — t",
        "[10:00:04] [p] [say] [PreToolUse] batch of 5 (m, normal, worth=0.72) -> done -> 'I pushed it.'",
    ])
    assert (s["flush_spoken"], s["flush_skipped"]) == (1, 4)


def test_a_batch_the_decider_held_back_is_its_own_outcome():
    s = summarize(["[10:00:00] [p] [say] [PreToolUse] batch of 5 held back (decider worth=0.01 < 0.40)"])
    assert (s["flushed"], s["flush_held_back"], s["flush_spoken"], s["flush_skipped"]) == (1, 1, 0, 0)
