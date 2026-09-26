"""T5 — dates in the log.

log() now emits an ISO-style "YYYY-MM-DD HH:MM:SS" stamp. Every parser must
keep accepting the old undated "HH:MM:SS" lines too -- 71,000+ existing lines
have no date and are never migrated, so "date unknown" must stay a distinct,
countable state, never inferred as "today".
"""
import re

import log_analyse


def test_log_writes_dated_format(claude_home, monkeypatch):
    import engines.base as base

    monkeypatch.setattr(base, "derive_project_label", lambda: "claudio")
    base.log("hello")
    text = (claude_home / "claudio.log").read_text()
    assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[claudio\] hello\n$", text)


def test_gen_regex_matches_dated_and_legacy_lines():
    dated = "[2026-09-22 14:26:11] [claudio] [pocket-tts] [Stop] (llama3.2:3b) -> done -> 'I shipped it.'"
    legacy = "[14:26:11] [claudio] [pocket-tts] [Stop] (llama3.2:3b) -> done -> 'I shipped it.'"

    m1 = log_analyse.GEN.match(dated)
    m2 = log_analyse.GEN.match(legacy)
    assert m1 is not None, "dated line must still match GEN"
    assert m2 is not None, "legacy undated line must still match GEN"
    assert m1.group(1) == "2026-09-22"
    assert m2.group(1) is None


def test_trace_regex_matches_dated_and_legacy_lines():
    dated = ("[2026-09-22 15:00:13] [claudio] gen[PreToolUse] 0.59s attempt=1/1 "
              "model=llama3.2:3b detail='x' raw='done | I fixed a Bash issue.' -> done")
    legacy = ("[15:00:13] [claudio] gen[PreToolUse] 0.59s attempt=1/1 "
              "model=llama3.2:3b detail='x' raw='done | I fixed a Bash issue.' -> done")

    m1 = log_analyse.TRACE.match(dated)
    m2 = log_analyse.TRACE.match(legacy)
    assert m1 is not None, "dated trace line must still match TRACE"
    assert m2 is not None, "legacy undated trace line must still match TRACE"
    assert m1.group(1) == "2026-09-22"
    assert m2.group(1) is None


def test_parse_extracts_date_and_none_for_legacy(tmp_path):
    log_path = tmp_path / "claudio.log"
    log_path.write_text(
        "[2026-09-22 14:26:11] [claudio] [pocket-tts] [Stop] (llama3.2:3b) -> done -> 'dated line.'\n"
        "[14:26:11] [claudio] [pocket-tts] [Stop] (llama3.2:3b) -> done -> 'legacy line.'\n"
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


def test_gen_regex_reads_a_flush_the_decider_let_through():
    line = ("[2026-09-25 17:10:00] [claudio] [pocket-tts] [PreToolUse] batch of 5 "
            "(llama3.2:3b, normal, worth=0.72) -> done -> 'I'm pushing the retry fix.'")
    m = log_analyse.GEN.match(line)
    assert m is not None and m.group(8) == "'I'm pushing the retry fix.'"
