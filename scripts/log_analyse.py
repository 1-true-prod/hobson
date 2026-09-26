#!/usr/bin/env python3
"""log_analyse.py — measure voice-quality defect rates from claudio.log.

Reproduces the baseline numbers in plans/2026-08-19-coworker-voice-design.md
so every rate is a command rather than a one-off.

    python3 scripts/log_analyse.py              # last 300 generations
    python3 scripts/log_analyse.py --n 1000
    python3 scripts/log_analyse.py --project claudio
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phrase_gen

LOG = os.path.expanduser("~/.claude/claudio.log")

# Matches the old (untagged), new (project-tagged), and batched (commentary
# batching, e.g. "batch of 5 (model, verbosity) -> ...") line formats.
# The leading timestamp carries an optional "YYYY-MM-DD " date prefix (added
# when dates were put in the log) -- older lines have only "HH:MM:SS" and are
# "date unknown", never "today". Group 1 is the date (or None), group 2 the
# time.
GEN = re.compile(
    r"^\[(?:(\d{4}-\d{2}-\d{2}) )?(\d\d:\d\d:\d\d)\] (?:\[([^\]]+)\] )?\[([^\]]+)\] \[(\w+)\] "
    r"(?:batch of \d+ )?\(([^)]*)\) -> (\w+) -> (.+)$"
)

# Matches the "gen[...]" trace line generate_or_skip() writes for every
# Ollama call -- the only place the model's *unrepaired* raw output survives.
# e.g. [2026-09-22 15:00:13] [claudio] gen[PreToolUse] 0.59s attempt=1/1
#      model=llama3.2:3b detail='...' raw='done | I fixed a Bash issue on
#      claudio.' -> done ...
# (older lines lack the date prefix -- see GEN above.)
TRACE = re.compile(
    r"^\[(?:(\d{4}-\d{2}-\d{2}) )?(\d\d:\d\d:\d\d)\] (?:\[([^\]]+)\] )?gen\[(\w+)\] .*?"
    r"raw=(['\"])(.*?)\5 -> "
)


def parse(path, project=None):
    """Returns rows of (date, event, phrase). date is None for legacy
    (pre-dating) lines -- "date unknown", never "today"."""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = GEN.match(line.rstrip("\n"))
            if not m:
                continue
            date, _time, proj, _engine, event, _model, _cat, phrase = m.groups()
            if project and proj != project:
                continue
            phrase = phrase.strip()
            if phrase[:1] in "\"'":
                phrase = phrase[1:-1] if phrase[-1:] in "\"'" else phrase[1:]
            rows.append((date, event, phrase))
    return rows


def parse_raw(path, project=None):
    """Pre-guard, pre-repair phrases straight from the model, via raw= traces.

    Returns rows of (date, event, phrase); date is None for legacy lines.
    """
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = TRACE.match(line.rstrip("\n"))
            if not m:
                continue
            date, _time, proj, event, _quote, raw = m.groups()
            if project and proj != project:
                continue
            if not raw:
                continue
            # raw is "category | phrase" (same shape _parse_response expects)
            # or, rarely, a bare phrase with no category.
            phrase = raw.split("|", 1)[1].strip() if "|" in raw else raw.strip()
            if not phrase:
                continue
            rows.append((date, event, phrase))
    return rows


def filter_since(rows, since):
    """Keep rows dated on/after `since` (a "YYYY-MM-DD" string).

    Rows with an unknown date (legacy, pre-date lines) can never be
    date-filtered -- they are excluded from the kept set but counted
    separately so the exclusion is visible rather than silently dropping
    most of the log's history.

    Returns (kept_rows, undated_excluded_count).
    """
    if not since:
        return rows, 0
    kept = []
    undated_excluded = 0
    for date, event, phrase in rows:
        if date is None:
            undated_excluded += 1
            continue
        if date < since:
            continue
        kept.append((date, event, phrase))
    return kept, undated_excluded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--project", default=None)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                     help="only consider rows dated on/after this date. "
                          "Undated (legacy, pre-dating) rows can't be "
                          "date-filtered and are excluded -- the count "
                          "excluded is printed.")
    args = ap.parse_args()

    all_rows = parse(args.log, args.project)
    all_raw_rows = parse_raw(args.log, args.project)

    undated_excluded = 0
    raw_undated_excluded = 0
    if args.since:
        all_rows, undated_excluded = filter_since(all_rows, args.since)
        all_raw_rows, raw_undated_excluded = filter_since(all_raw_rows, args.since)
        print(f"--since {args.since}: excluded {undated_excluded} undated (legacy) "
              f"generation row(s) and {raw_undated_excluded} undated trace row(s) "
              f"that cannot be date-filtered")

    rows = all_rows[-args.n:]
    if not rows:
        sys.exit("no generation lines found")

    raw_rows = all_raw_rows[-args.n:]

    pre = [p for _, e, p in rows if e == "PreToolUse"]
    phrases = [p for _, _, p in rows]
    pre_raw = [p for _, e, p in raw_rows if e == "PreToolUse"]

    def pct(n, d):
        return f"{n}/{d} = {100 * n / d:.1f}%" if d else "n/a"

    # Post-repair past tense on PreToolUse is not measured here: _repair_phrase()
    # fixes tense before a phrase can be logged as spoken/rejected, and
    # _guard_reject_reason() rejects anything repair missed, so that rate is
    # structurally 0/N regardless of real model behaviour. See "past tense from
    # model (pre-repair)" below for the metric that can actually vary.
    dupes = sum(
        phrase_gen.is_near_duplicate(phrases[i], phrases[max(0, i - 6):i])
        for i in range(len(phrases))
    )
    pipes = sum("|" in p for p in phrases)
    tickets = sum(bool(re.search(r"\b[A-Z]{2,6}-\d+\b", p)) for p in phrases)
    idents = sum(bool(re.search(r"\b[a-z]+[A-Z][a-zA-Z]*\b", p)) for p in phrases)
    hexes = sum(bool(re.search(r"\b[0-9a-f]{8,}\b", p)) for p in phrases)

    words = [w for p in phrases for w in re.findall(r"[A-Za-z']+", p)]
    mean_len = sum(len(w) for w in words) / len(words) if words else 0

    past_pre_repair = sum(phrase_gen.looks_past_tense(p) for p in pre_raw)

    window_note = (
        f"last {args.n} matching rows"
        if len(all_rows) >= args.n
        else f"only {len(all_rows)} matching rows exist in the log (requested --n {args.n})"
    )
    raw_window_note = (
        f"last {args.n} matching trace rows"
        if len(all_raw_rows) >= args.n
        else f"only {len(all_raw_rows)} matching trace rows exist in the log (requested --n {args.n})"
    )
    print(f"analysed {len(rows)} generations ({len(pre)} PreToolUse) -- {window_note}")
    print(f"trace rows for pre-repair measurement: {len(raw_rows)} ({len(pre_raw)} PreToolUse) -- {raw_window_note}\n")
    print(f"  past tense from model (pre-repair): {pct(past_pre_repair, len(pre_raw))}")
    print(f"  near-duplicate (window 6): {pct(dupes, len(phrases))}")
    print(f"  separator spoken         : {pct(pipes, len(phrases))}")
    print(f"  ticket ID present        : {pct(tickets, len(phrases))}")
    print(f"  camelCase identifier     : {pct(idents, len(phrases))}")
    print(f"  hex id present           : {pct(hexes, len(phrases))}")
    print(f"  mean word length         : {mean_len:.2f} chars")


if __name__ == "__main__":
    main()
