"""log_record.py: every log line written one way, and read back one way.

The golden lines are copied from a real hobson.log.
"""

import re
import subprocess
import sys
import time

import log_record

_REAL_POPEN = subprocess.Popen  # the autouse no_audio fixture replaces it

PLAYBACK = "[2026-09-22 16:23:14] [claudio] [pocket-tts] barked (daemon-live) -> \"I'm hitting a problem with logging analysis.\""
UNTAGGED = "[14:26:16] [kokoro-rt] barked (daemon-live) -> \"Oh look, it works. Don't touch it again.\""
BARE = "[11:36:37] classify (qwen3.5:4b) -> no match (raw: 'done<end_of_turn')"
NUDGE = "[2026-09-24 14:25:15] [web client app] [nudge] skipped — a nudge is already running for 'web client app'"
TRACE = ("[2026-09-22 16:27:22] [claudio] gen[PreToolUse] 0.40s attempt=1/1 model=llama3.2:3b "
         "detail='Bash: git status --short -- scripts/ tests/ &&' raw=\"done | I'm running short "
         "scripts and tests.\" -> done  spoken=\"I'm running short scripts and tests.\"")
FLUSH = ("[2026-09-25 17:22:47] [web client app] [pocket-tts] [PreToolUse] batch of 1 "
         "(llama3.2:3b, normal, worth=0.52) -> done -> \"I'm writing the stuck webcam capture issue report.\"")
STOP = "[2026-09-22 16:29:09] [claudio] [pocket-tts] [Stop] (llama3.2:3b) -> done -> \"I've nailed down the Jev API shape.\""


# ── The envelope ───────────────────────────────────────────────────────────

def test_a_dated_engine_line():
    r = log_record.parse(PLAYBACK)
    assert (r.date, r.time, r.project, r.engine) == ("2026-09-22", "16:23:14", "claudio", "pocket-tts")
    assert r.body.startswith("barked (daemon-live)")
    assert r.stamp == "[2026-09-22 16:23:14]"


def test_a_line_from_before_project_tags_names_its_engine_first():
    r = log_record.parse(UNTAGGED)
    assert (r.date, r.project, r.engine) == (None, None, "kokoro-rt")
    assert r.stamp == "[14:26:16]"


def test_a_line_with_no_tags_at_all():
    r = log_record.parse(BARE)
    assert (r.project, r.engine) == (None, None)
    assert r.body.startswith("classify (qwen3.5:4b)")


def test_a_message_tag_is_not_an_engine():
    """The monitor once read [nudge] as the engine, and a one-word project
    ("hobson") as one before that."""
    r = log_record.parse(NUDGE)
    assert (r.project, r.engine) == ("web client app", None)
    assert r.body.startswith("[nudge] skipped")
    one_word = log_record.parse("[2026-09-26 10:00:00] [hobson] silenced (muted)")
    assert (one_word.project, one_word.engine, one_word.body) == ("hobson", None, "silenced (muted)")


def test_every_engine_tag_is_known():
    """A new engine whose tag is missing here would have it read as part of
    the message, and its lines would drop out of every reader."""
    import importlib
    import hobson
    for path in hobson.ENGINES.values():
        module, cls = path.rsplit(".", 1)
        tag = getattr(importlib.import_module(module), cls).engine_name
        assert tag in log_record.ENGINE_TAGS, tag


def test_junk_is_not_a_record():
    for junk in ("", "garbage", "[bad] [line", "[10:00] [p] too short a stamp"):
        assert log_record.parse(junk) is None


def test_the_timestamp_of_a_dated_and_an_undated_record():
    dated = log_record.parse(STOP)
    assert dated.timestamp() == time.mktime((2026, 9, 22, 16, 29, 9, 0, 0, -1))
    now = time.mktime((2026, 9, 26, 10, 0, 0, 0, 0, -1))
    today = log_record.parse("[09:59:00] [p] earlier today")
    assert today.timestamp(now) == now - 60
    # Later than now on the clock: it was yesterday, not the future.
    yesterday = log_record.parse("[10:01:00] [p] late last night")
    assert yesterday.timestamp(now) == now + 60 - 86400


# ── Writing ────────────────────────────────────────────────────────────────

def test_write_stamps_the_date_time_and_project(claude_home, monkeypatch):
    import home
    monkeypatch.setattr(home, "derive_project_label", lambda: "hobson")
    log_record.write("hello")
    text = (claude_home / "hobson.log").read_text()
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[hobson\] hello\n", text)


def test_a_message_with_line_breaks_is_one_record(claude_home):
    """A Bash command with no description is its own context, newlines and
    all; 178 lines of the real log were the rest of such a command."""
    log_record.write("[PreToolUse] queued (1 pending) Bash: set -e\nTMP=$(mktemp -d)\r\nls")
    lines = (claude_home / "hobson.log").read_text().splitlines()
    assert len(lines) == 1
    assert log_record.parse(lines[0]).body == "[PreToolUse] queued (1 pending) Bash: set -e TMP=$(mktemp -d) ls"


def test_lines_written_before_folding_are_folded_back_on_reading():
    lines = ["[16:11:11] [claudio] [pocket-tts] [PreToolUse] queued (3 pending) Bash: set -e\n",
             "TMPHOME=$(mktemp -d)\n",
             "[16:11:12] [claudio] silenced (muted)\n"]
    records = list(log_record.records(lines))
    assert [r.body for r in records] == [
        "[PreToolUse] queued (3 pending) Bash: set -e TMPHOME=$(mktemp -d)", "silenced (muted)"]


def test_read_yields_the_records_of_a_file(tmp_path):
    path = tmp_path / "hobson.log"
    path.write_text("\n".join((PLAYBACK, STOP, "")))
    assert [r.engine for r in log_record.read(str(path))] == ["pocket-tts", "pocket-tts"]


# ── The shapes, each written and read back ─────────────────────────────────

def test_playback_round_trip_and_golden_line():
    for phrase in ("I ran the tests.", "I'm done.", 'He said "no" and it\'s fine.'):
        body = log_record.playback("say-fallback: daemon down", phrase)
        assert log_record.parse_playback(body) == ("say-fallback: daemon down", phrase)
    assert log_record.parse_playback(log_record.parse(PLAYBACK).body) == (
        "daemon-live", "I'm hitting a problem with logging analysis.")


def test_playback_is_only_a_played_phrase():
    """Counted as "barked" anywhere in the line, a trace whose detail quoted
    the word was a phrase spoken."""
    assert log_record.parse_playback(log_record.parse(TRACE).body) is None
    assert log_record.parse_playback("skipped (cooldown) -> 'x'") is None


def test_outcome_round_trip_and_golden_lines():
    body = log_record.outcome("PreToolUse", "llama3.2:3b, normal, worth=0.72", "done",
                              "I'm pushing the retry fix.", batch=5)
    assert log_record.parse_outcome(body) == (
        "PreToolUse", 5, "llama3.2:3b, normal, worth=0.72", "done", "I'm pushing the retry fix.")
    assert log_record.parse_outcome(log_record.parse(FLUSH).body).batch == 1
    stop = log_record.parse_outcome(log_record.parse(STOP).body)
    assert (stop.event, stop.batch, stop.notes, stop.phrase) == (
        "Stop", None, "llama3.2:3b", "I've nailed down the Jev API shape.")


def test_outcome_is_not_a_failed_flush():
    assert log_record.parse_outcome("[PreToolUse] batch of 5 SKIP: model declined") is None


def test_trace_round_trip_and_golden_line():
    start = log_record.trace("Stop", 0.531, 1, 2, "llama3.2:3b", "Agent: it's done", "done | I'm done.")
    spoken = log_record.parse_trace(f"{start} {log_record.verdict('done', [], 'I am done.')}")
    assert (spoken.event, spoken.model, spoken.detail, spoken.raw) == (
        "Stop", "llama3.2:3b", "Agent: it's done", "done | I'm done.")
    assert (spoken.spoken, spoken.rejected) == ("I am done.", None)
    rejected = log_record.parse_trace(
        f"{start} {log_record.verdict('done', ['(tense)'], 'I am done.', 'near-duplicate')}")
    assert (rejected.spoken, rejected.rejected) == (None, "I am done.")
    skipped = log_record.parse_trace(f"{start} -> SKIP")
    assert (skipped.result, skipped.spoken, skipped.rejected) == ("-> SKIP", None, None)
    golden = log_record.parse_trace(log_record.parse(TRACE).body)
    assert golden.spoken == "I'm running short scripts and tests."
    assert golden.detail == "Bash: git status --short -- scripts/ tests/ &&"


def test_an_old_trace_has_no_detail():
    old = log_record.parse_trace(
        "gen[Stop] 0.84s attempt=1/2 model=llama3.2:3b raw='done | I fixed it.' -> SKIP")
    assert (old.detail, old.raw, old.result) == (None, "done | I fixed it.", "-> SKIP")


def test_the_phrase_ending_a_line():
    assert log_record.tail_phrase("template (done) -> \"It's done.\"") == "It's done."
    assert log_record.tail_phrase("[nudge] started for 'p': 'x'") is None


# ── For the monitor ────────────────────────────────────────────────────────

def test_fields_for_the_monitor():
    out = _REAL_POPEN([sys.executable, log_record.__file__, "--fields"],
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stdout, _ = out.communicate("\n".join((PLAYBACK, NUDGE, "TMPHOME=$(mktemp -d)", "")).encode())
    rows = [row.split("\x1f") for row in stdout.decode().splitlines()]
    assert rows[0][:2] == ["[2026-09-22 16:23:14]", "pocket-tts"]
    assert rows[0][2].startswith("barked (daemon-live)") and rows[0][3] == PLAYBACK
    assert rows[1][1] == "" and rows[1][2].startswith("[nudge] skipped")
    assert rows[2] == ["", "", "", "TMPHOME=$(mktemp -d)"]


# ── End to end ─────────────────────────────────────────────────────────────

def test_what_an_engine_writes_every_reader_reads(fake_ollama, claude_home, monkeypatch):
    """One spoken commentary flush: stats counts it spoken and played, recap
    reads its phrase, and log_analyse its outcome and its trace."""
    import home
    import log_analyse
    import log_stats
    import recap
    from engines.say import SayEngine
    monkeypatch.setattr(home, "derive_project_label", lambda: "hobson")
    fake_ollama.chat("done | I'm running the login tests.")
    engine = SayEngine({"engine": "say", "events": ["commentary"], "personality": "hobson",
                        "commentary": {"tools": ["bash"], "verbosity": "normal", "cooldown": 0,
                                       "min_tool_calls": 1, "min_seconds": 60.0}})
    engine.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"description": "Run the login tests"}})

    path = str(claude_home / "hobson.log")
    lines = open(path, encoding="utf-8").readlines()
    stats = log_stats.summarize(lines)
    assert (stats["flush_spoken"], stats["barked"]) == (1, 1)
    assert recap.recent_activity(lines, "hobson") == ["I'm running the login tests."]
    assert [row[1:] for row in log_analyse.parse(path)] == [("PreToolUse", "I'm running the login tests.")]
    assert [row[1:] for row in log_analyse.parse_raw(path)] == [("PreToolUse", "I'm running the login tests.")]
