#!/usr/bin/env python3
"""log_stats.py — Tally what claudio spoke, queued, and suppressed.

Answers the question the log can already answer but nobody reads it to:
how much did claudio actually say, and what stopped it saying more.
"""

import os
import re
import sys

LOG_FILE = os.path.expanduser("~/.claude/claudio.log")

# Negative lookbehind excludes phrase_gen's own trace lines, which use a
# "gen[EventName]" prefix (see phrase_gen.py) that would otherwise collide
# with the real "[EventName]" event tag emitted by BaseEngine._log().
_EVENT_RE = re.compile(r"(?<!gen)\[(Stop|PreToolUse|PermissionRequest|Notification)\]")
_BATCH_RE = re.compile(r"batch of (\d+)")
# Only a flush that produced a phrase has this shape:
#   batch of 5 (llama3.2:3b, normal) -> done -> 'I ran the tests.'
# Every other outcome line -- SKIP:, REJECTED:, UNUSABLE:, ERROR:, the old
# "skipped (raw: ...)" -- is a flush that said nothing. Counting by the
# absence of "skipped" read all 378 of those in the real log as spoken.
_SPOKEN_FLUSH_RE = re.compile(r"batch of \d+ \([^)]*\) -> \w+ -> ")

# "skipped (raw: ...)" is emitted only by builds predating
# describe_generation_failure(), which replaced it with explicit
# SKIP:/ERROR:/UNUSABLE: verdicts. The string is therefore a reliable version
# marker: any line carrying it came from an older build. This matters because
# that old format lumped genuine Ollama timeouts in with ordinary skips — a
# 366-line historical backlog of them read as a 42% failure rate for current
# code, when current code's real rate was 0%. Never fold these into a live rate.
_STALE_MARKER_RE = re.compile(r"skipped \(raw: ")


def summarize(lines):
    """Count log activity. Pure — takes an iterable of lines."""
    s = {
        "queued": 0, "flushed": 0, "flush_spoken": 0, "flush_skipped": 0,
        "flush_held_back": 0, "stop_working": 0,
        "subagent_suppressed": 0, "silenced": 0, "rejected": 0, "barked": 0,
        "events": {}, "items_per_flush": 0.0,
        "tool_calls": 0, "speech_per_tool_call": 0.0,
        # Progress awareness: stuck detection, anomaly mode, nudge, watchdog.
        "gen_errors": 0, "gen_unusable": 0, "stale_lines": 0,
        "stuck": 0, "anomaly_quiet": 0,
        "nudge_started": 0, "nudge_spoke": 0,
        "nudge_cancelled": 0, "nudge_capped": 0,
        "watchdog_started": 0, "watchdog_spoke": 0,
    }
    batch_sizes = []
    for line in lines:
        if not line or "[" not in line:
            continue
        m = _EVENT_RE.search(line)
        if m:
            s["events"][m.group(1)] = s["events"].get(m.group(1), 0) + 1
        if "queued (" in line:
            s["queued"] += 1
        bm = _BATCH_RE.search(line)
        if bm:
            s["flushed"] += 1
            batch_sizes.append(int(bm.group(1)))
            if " held back " in line:
                # The decider judged it not worth hearing (gate.py).
                s["flush_held_back"] += 1
            elif _SPOKEN_FLUSH_RE.search(line):
                s["flush_spoken"] += 1
            else:
                s["flush_skipped"] += 1
        if "(subagent " in line:
            s["subagent_suppressed"] += 1
        if "silenced (" in line:
            s["silenced"] += 1
        if "[Stop] still working" in line:
            # The agent stopped to wait on its own subagents or build.
            s["stop_working"] += 1
        # Matches only the gen[...] trace line from phrase_gen.py
        # ("REJECTED=<phrase>"). The engine's own verdict line for the same
        # event reads "REJECTED: guard rejected — ..." (colon, not equals)
        # via describe_generation_failure(), so it does not also match here
        # — one guard rejection is counted once, not twice.
        if "REJECTED=" in line:
            s["rejected"] += 1
        if _STALE_MARKER_RE.search(line):
            # An old-build line. Report it, never fold it into a live rate.
            s["stale_lines"] += 1
            continue
        if "barked" in line:
            s["barked"] += 1
        if "ERROR:" in line:
            s["gen_errors"] += 1
        if "UNUSABLE:" in line:
            s["gen_unusable"] += 1
        # A stuck flush isn't tagged in the outcome line — it's identifiable
        # by the clause prepended to the context the model was handed, which
        # is why the detail= trace had to land before any of this.
        if "Going in circles" in line:
            s["stuck"] += 1
        if "queued (anomaly" in line:
            s["anomaly_quiet"] += 1
        if "[nudge] started" in line:
            s["nudge_started"] += 1
        if "[nudge] spoke" in line:
            s["nudge_spoke"] += 1
        if "[nudge] cancelled" in line:
            s["nudge_cancelled"] += 1
        if "[nudge] capped" in line:
            s["nudge_capped"] += 1
        if "[watchdog] started" in line:
            s["watchdog_started"] += 1
        if "[watchdog] spoke" in line:
            s["watchdog_spoke"] += 1
    if batch_sizes:
        s["items_per_flush"] = sum(batch_sizes) / len(batch_sizes)
    # Every tool call through the batching path logs either a "queued" line or
    # triggers a flush — so those two together are the tool-call count. Do NOT
    # use the raw [PreToolUse] tag count as the denominator: it also counts the
    # pre-batching era, chatty-mode barks, and subagent-suppressed calls, which
    # never produce a "batch of N" line, so the ratio reads ~0 forever.
    s["tool_calls"] = s["queued"] + s["flushed"]
    if s["tool_calls"]:
        s["speech_per_tool_call"] = s["flush_spoken"] / s["tool_calls"]
    return s


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else LOG_FILE
    try:
        with open(path, errors="replace") as fh:
            s = summarize(fh)
    except FileNotFoundError:
        print(f"No log at {path}")
        return
    ev = s["events"]
    print("Events")
    for name in ("PreToolUse", "Stop", "PermissionRequest", "Notification"):
        if ev.get(name):
            print(f"  {name:18s} {ev[name]:6d}")
    print("\nCommentary")
    print(f"  queued              {s['queued']:6d}")
    print(f"  flushed             {s['flushed']:6d}  ({s['items_per_flush']:.1f} items per flush)")
    print(f"    spoken            {s['flush_spoken']:6d}")
    print(f"    nothing to say    {s['flush_skipped']:6d}")
    if s["flush_held_back"]:
        print(f"    held back (Jev)   {s['flush_held_back']:6d}")
    if s["tool_calls"]:
        print(f"  tool calls batched  {s['tool_calls']:6d}")
        print(f"  speech per tool call {s['speech_per_tool_call']:.2f}   (target ~0.26)")
    print("\nSuppressed")
    print(f"  subagent            {s['subagent_suppressed']:6d}")
    print(f"  muted / quiet hours {s['silenced']:6d}")
    print(f"  guard rejected      {s['rejected']:6d}")
    if s["stop_working"]:
        print(f"  Stop, still working {s['stop_working']:6d}")
    if s["gen_errors"] or s["gen_unusable"]:
        print("\nGeneration failures")
        print(f"  ollama errored      {s['gen_errors']:6d}")
        print(f"  unparseable reply   {s['gen_unusable']:6d}")
    if s["stale_lines"]:
        print(f"\n{s['stale_lines']} line(s) from an older build ignored "
              f"(they predate project tagging; counting them would skew every rate).")

    print("\nProgress")
    print(f"  going in circles    {s['stuck']:6d}")
    print(f"  anomaly, kept quiet {s['anomaly_quiet']:6d}")
    print(f"  nudges started      {s['nudge_started']:6d}")
    print(f"    spoke             {s['nudge_spoke']:6d}")
    print(f"    you answered      {s['nudge_cancelled']:6d}")
    print(f"    gave up (capped)  {s['nudge_capped']:6d}")
    print(f"  watchdogs started   {s['watchdog_started']:6d}")
    print(f"    spoke             {s['watchdog_spoke']:6d}")
    if s["nudge_started"]:
        # A nudge you answered did its job; one that ran out of escalations
        # was talking to nobody. That ratio is the honest measure of whether
        # chasing the user is earning its noise.
        answered = s["nudge_cancelled"] / s["nudge_started"]
        print(f"  nudges you answered {answered:.0%}")

    print(f"\nTotal spoken          {s['barked']:6d}")


if __name__ == "__main__":
    main()
