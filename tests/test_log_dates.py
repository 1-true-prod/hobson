"""T5 — dates in the log.

The log is written with an ISO-style "YYYY-MM-DD HH:MM:SS" stamp. Every reader
must keep accepting the old undated "HH:MM:SS" lines too -- 71,000+ existing
lines have no date and are never migrated, so "date unknown" must stay a
distinct, countable state, never inferred as "today".
"""
import log_analyse


def test_parse_extracts_date_and_none_for_legacy(tmp_path):
    log_path = tmp_path / "hobson.log"
    log_path.write_text(
        "[2026-09-22 14:26:11] [hobson] [pocket-tts] [Stop] (llama3.2:3b) -> done -> 'dated line.'\n"
        "[14:26:11] [hobson] [pocket-tts] [Stop] (llama3.2:3b) -> done -> 'legacy line.'\n"
    )
    rows = log_analyse.parse(str(log_path))
    assert rows[0][0] == "2026-09-22"
    assert rows[0][1:] == ("Stop", "dated line.")
    assert rows[1][0] is None
    assert rows[1][1:] == ("Stop", "legacy line.")


def test_since_filters_dated_rows_and_reports_undated_excluded():
    rows = [
        ("2026-09-01", "Stop", "too old"),
        ("2026-09-20", "Stop", "kept"),
        (None, "Stop", "legacy, can't be dated"),
    ]
    kept, undated_excluded = log_analyse.filter_since(rows, "2026-09-10")
    assert kept == [("2026-09-20", "Stop", "kept")]
    assert undated_excluded == 1, "the undated row must be counted, not silently dropped"


def test_since_none_is_a_noop_and_keeps_undated_rows():
    rows = [("2026-09-01", "Stop", "dated"), (None, "Stop", "legacy")]
    kept, undated_excluded = log_analyse.filter_since(rows, None)
    assert kept == rows
    assert undated_excluded == 0


def test_parse_reads_a_flush_the_decider_let_through(tmp_path):
    log_path = tmp_path / "hobson.log"
    log_path.write_text("[2026-09-25 17:10:00] [hobson] [pocket-tts] [PreToolUse] batch of 5 "
                        "(llama3.2:3b, normal, worth=0.72) -> done -> \"I'm pushing the retry fix.\"\n")
    assert log_analyse.parse(str(log_path)) == [("2026-09-25", "PreToolUse", "I'm pushing the retry fix.")]


def test_parse_raw_extracts_date_and_none_for_legacy(tmp_path):
    log_path = tmp_path / "hobson.log"
    trace = ("gen[PreToolUse] 0.59s attempt=1/1 model=llama3.2:3b detail='x' "
             "raw='done | I fixed a Bash issue.' -> done")
    log_path.write_text(f"[2026-09-22 15:00:13] [hobson] {trace}\n[15:00:13] [hobson] {trace}\n")
    assert log_analyse.parse_raw(str(log_path)) == [
        ("2026-09-22", "PreToolUse", "I fixed a Bash issue."), (None, "PreToolUse", "I fixed a Bash issue.")]
