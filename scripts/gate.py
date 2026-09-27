"""gate.py — is a batch of tool calls worth a spoken update?

The commentary batch gate (5 calls or 60s) decides when to *consider*
speaking. What to do with a batch it releases used to be Ollama's call, made
in the same breath as phrasing it. With the decider backend on "jev", this
asks first, once per flush, and a batch below the chattiness threshold is
held back without calling Ollama at all.

Calibrated against the user's written rule for what to voice — "skip
routine events, voice completions, errors, questions" — applied by hand,
blind to both gates, over 140 real logged batches (80, then a held-out 60).
Seven were worth hearing, all finishing actions: commits, pushes, a merge,
a revert, a confirmed uninstall. Ollama spoke on 106 of the 140 and caught
5 of the 7. At P(worth) >= 0.4 this spoke on about a third as many and
caught all 7, the lowest scoring 0.46 and 0.49; its ranking (AUC) was 0.91
on both sets. Its misses lean the other way: starting big work (dispatching
an agent, booting an emulator) scores high.

A PreToolUse batch describes what the agent is about to do, never how it
turned out — hobson does not hook tool results — so "tests passed" or "it
failed" cannot appear here; the Stop announcement is where those land.
"""

# Where the chattiness dial sits by default: a threshold of 0.4. Leaning
# quiet on purpose -- the user's standing complaint is that models blab.
DEFAULT_CHATTINESS = 0.6

_CRITERIA = {
    "worth": "a meaningful step the user would want a short spoken update about: finishing, "
             "starting or changing something substantive, a result, a failure, a decision point",
    "routine": "routine plumbing: looking things up, reading, small checks, reruns, bookkeeping; "
               "not worth interrupting for",
}


def threshold(chattiness):
    """The P(worth) a batch needs, for a chattiness in [0, 1]: 1 is every
    batch the count/timer gate releases, 0 is next to nothing."""
    try:
        c = float(chattiness)
    except (TypeError, ValueError):
        c = DEFAULT_CHATTINESS
    return 1.0 - min(1.0, max(0.0, c))


def worth_probability(batch_detail, config):
    """P(worth a spoken update) for a batch summary, per the decider, or
    None for no opinion (backend local, any failure, no probability).

    The summary carries Bash descriptions and the first 40 characters of
    commands, so it goes as a decider.Summary: redacted, 220 characters at
    most, exactly as it went out in the calibration.
    """
    if not batch_detail:
        return None
    try:
        import decider
    except ImportError:
        return None
    return decider.probability(
        "worth",
        ["A coding agent just did this batch of work (a summary of its tool calls):\n\n",
         decider.Summary(batch_detail)],
        "Decide whether this batch deserves a short spoken update to a user who is not "
        "watching the screen.",
        _CRITERIA,
        config=config,
    )
