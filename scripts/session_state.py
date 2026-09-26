"""session_state.py — Per-session rolling memory for hobson.

Tracks what's been voiced and what's happened since, so the Ollama prompt
has continuity across events. State is keyed by project label (ties to
worktree/cwd, survives session restarts).

State file: ~/.claude/hobson-sessions/<hash>.json
"""

import hashlib
import json
import os
import time

SESSIONS_DIR = os.path.expanduser("~/.claude/hobson-sessions")
MAX_RECENT_VOICED = 6


def _session_path(project_label):
    key = hashlib.sha256(project_label.encode("utf-8")).hexdigest()[:12]
    return os.path.join(SESSIONS_DIR, f"{key}.json")


def _fresh_state(project_label):
    return {
        "project": project_label,
        "recent_voiced": [],
        "event_counts_since_voice": {},
        "last_voiced_time": 0.0,
        "total_events": 0,
        "total_voiced": 0,
        "pending": [],
        "last_flush_time": 0.0,
        "fingerprints": [],
        "repeat_announced": {},
        "seen_contexts": [],
    }


def load_session(project_label):
    """Load session state from disk, or create a fresh one."""
    path = _session_path(project_label)
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
            # Backfill any fields missing from older on-disk sessions, using
            # _fresh_state() as the single source of truth for the schema.
            for key, default in _fresh_state(project_label).items():
                state.setdefault(key, default)
            # Older sessions hold recent_voiced as bare strings (pre-decay
            # format). Coerce to [phrase, ts] pairs with ts=0.0, which reads
            # as "unknown" everywhere downstream -- unknown stays
            # conservative and dedupes, it is never treated as infinitely
            # old and let through.
            state["recent_voiced"] = [
                [entry, 0.0] if isinstance(entry, str) else entry
                for entry in state.get("recent_voiced", [])
            ]
            return state
    except (FileNotFoundError, json.JSONDecodeError):
        return _fresh_state(project_label)


def save_session(state):
    """Write session state back to disk."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = _session_path(state["project"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def record_voiced(state, phrase):
    """Update state after a phrase was voiced."""
    state["recent_voiced"].append([phrase, time.time()])
    if len(state["recent_voiced"]) > MAX_RECENT_VOICED:
        state["recent_voiced"] = state["recent_voiced"][-MAX_RECENT_VOICED:]
    state["event_counts_since_voice"] = {}
    state["last_voiced_time"] = time.time()
    state["total_voiced"] += 1
    state["total_events"] += 1


def record_skipped(state, event_type):
    """Update state after an event was skipped."""
    counts = state["event_counts_since_voice"]
    counts[event_type] = counts.get(event_type, 0) + 1
    state["total_events"] += 1


def build_session_context(state, task=None):
    """Format session state as a string for the Ollama prompt.

    `task` is the user's latest request (base.request_for_prompt), passed
    for a Stop only. It goes to the local model only, never the decider.
    """
    parts = [f"Task: {task}"] if task else []

    # Recent voiced phrases. Entries are [phrase, ts] pairs; older sessions
    # (or a caller that hasn't gone through load_session's coercion) may
    # still hold bare strings, so accept both shapes here too.
    voiced = state.get("recent_voiced", [])
    if voiced:
        phrases = [v[0] if isinstance(v, (list, tuple)) else v for v in voiced[-6:]]
        quoted = [f'"{p}"' for p in phrases]
        parts.append(f"Recent: {' '.join(quoted)}")
    else:
        parts.append("No announcements yet this session.")

    # Events since last voice
    counts = state.get("event_counts_since_voice", {})
    if counts:
        items = [f"{v} {k}" for k, v in sorted(counts.items())]
        parts.append(f"Since then: {', '.join(items)}.")

    # Time since last announcement
    last_time = state.get("last_voiced_time", 0)
    if last_time > 0:
        delta = time.time() - last_time
        if delta < 60:
            parts.append(f"Last announced {int(delta)}s ago.")
        elif delta < 3600:
            parts.append(f"Last announced {int(delta / 60)} min ago.")
        else:
            parts.append(f"Last announced {int(delta / 3600)}h ago.")

    return "\n".join(parts)


def seconds_since_last_voiced(state):
    """Seconds since the last spoken phrase, or None if nothing spoken yet.

    Used to decay the near-duplicate guard: a repeat two minutes later is not
    what makes repetition grate. None means "unknown", and callers stay
    conservative and dedupe.
    """
    last = state.get("last_voiced_time", 0) or 0
    if not last:
        return None
    return max(0.0, time.time() - last)


MAX_PENDING = 8
PENDING_STALE_SECONDS = 90.0


def record_pending(state, tool, context):
    """Queue a tool call for a later batched utterance.

    Cheap and silent: no Ollama call, no speech. Per-tool-call narration is
    the finest interruption granularity the agent exposes, and fine-grained
    interruption measures worse than interrupting immediately (Iqbal &
    Bailey, CHI 2008).
    """
    pending = state.setdefault("pending", [])
    pending.append({"tool": tool, "context": context, "ts": time.time()})
    if len(pending) > MAX_PENDING:
        del pending[:-MAX_PENDING]
    state["total_events"] += 1


def record_flush_attempt(state):
    """Mark that a flush was attempted, whatever its outcome.

    The gate's timer must measure "time since we last tried", not "time
    since we last succeeded" — otherwise a SKIP or a guard rejection leaves
    the timer tripped and the next single tool call flushes immediately,
    defeating the batching it was meant to enable.
    """
    state["last_flush_time"] = time.time()


def should_flush(state, min_calls, min_seconds):
    """True when the batch has earned an utterance.

    Substance gate: enough happened, or enough time passed with something
    pending. Never fires on an empty queue — elapsed time alone is not news.

    Times off last_flush_time (the last flush *attempt*), not
    last_voiced_time — a flush that spoke nothing must not leave the timer
    tripped. seconds_since_last_voiced() still uses last_voiced_time; it
    drives the near-duplicate guard, which genuinely cares about the last
    time something was actually spoken.

    min_calls/min_seconds have no defaults here on purpose: the sole
    caller (BaseEngine._handle_commentary_llm) always reads them from
    config (commentary.min_tool_calls / commentary.min_seconds, see
    DEFAULT_CONFIG in engines/base.py) and passes both explicitly, so a
    default here would just be a second, driftable copy of that config
    default. Callers must pass both.
    """
    pending = state.get("pending") or []
    if not pending:
        return False
    if len(pending) >= min_calls:
        return True
    last = state.get("last_flush_time", 0) or 0
    if not last:
        return True  # no flush has ever been attempted this session
    return (time.time() - last) >= min_seconds


def take_pending(state, stale_seconds=PENDING_STALE_SECONDS):
    """Return the fresh pending items and clear the queue.

    Stale items are dropped, not spoken: a delayed glance is a wrong glance.
    """
    now = time.time()
    pending = state.get("pending") or []
    fresh = [p for p in pending if (now - p.get("ts", now)) <= stale_seconds]
    state["pending"] = []
    return fresh


MAX_FINGERPRINTS = 20


def action_identity(tool, tool_input):
    """What makes two tool calls *the same action*, as a short digest.

    The full input, minus `description` -- that is Claude's narration about
    the call, not the call. So the same command re-described is one action,
    while two different commands both described as "run the tests" are two.

    Repetition used to be keyed on the short display string from
    extract_context, so any two commands sharing a description or their
    first 40 characters -- `cat` on three different temp files -- counted as
    one command repeated, and every edit to a file as the same edit. That is
    also why the stuck detector built on it ("going in circles") fired on
    work that was going fine; it was removed once a replay of 12,805 real
    tool calls found nothing it caught that was genuinely a loop.

    A digest rather than the text, because the ring is written to disk and
    has no business holding full commands or edit bodies.
    """
    ident = {k: v for k, v in (tool_input or {}).items() if k != "description"}
    blob = json.dumps(ident, sort_keys=True, default=str)
    return hashlib.sha256(f"{(tool or '').lower()}\0{blob}".encode("utf-8")).hexdigest()[:16]


def fingerprint(tool, key):
    """A stable key for "this same piece of work".

    `key` is opaque here. The commentary path passes action_identity(), so
    two calls fingerprint alike only when they are the same action.
    """
    return f"{(tool or '').lower()}:{key or ''}"


def record_fingerprint(state, tool, key):
    """Remember that this work just happened. Cheap: no I/O of its own."""
    ring = state.setdefault("fingerprints", [])
    ring.append({"fp": fingerprint(tool, key), "ts": time.time()})
    if len(ring) > MAX_FINGERPRINTS:
        del ring[:-MAX_FINGERPRINTS]


def repeat_count(state, tool, key, window_seconds=180):
    """How many times this same action happened inside the window, in any
    order. Anomaly mode's "back on the same thing again"."""
    target = fingerprint(tool, key)
    now = time.time()
    return sum(1 for e in (state.get("fingerprints") or [])
               if e.get("fp") == target and (now - e.get("ts", now)) <= window_seconds)


def repeat_already_announced(state, tool, key, within=600):
    """True if this action was already surfaced as a repeat recently.

    An action stays repeated for as long as the agent keeps doing it, so
    without this every further occurrence would force another flush.
    """
    fp = fingerprint(tool, key)
    when = (state.get("repeat_announced") or {}).get(fp, 0)
    return bool(when) and (time.time() - when) <= within


def mark_repeat_announced(state, tool, key):
    """Record that this action's repetition was just announced."""
    state.setdefault("repeat_announced", {})[fingerprint(tool, key)] = time.time()


MAX_SEEN_CONTEXTS = 60


def note_seen(state, context):
    """Record a context; True if this is its first sighting this session."""
    if not context:
        return False
    seen = state.setdefault("seen_contexts", [])
    if context in seen:
        return False
    seen.append(context)
    if len(seen) > MAX_SEEN_CONTEXTS:
        del seen[:-MAX_SEEN_CONTEXTS]
    return True


def anomaly_reason(state, tool, context, cfg, identity=None):
    """Why this event is worth interrupting for in anomaly mode, or None.

    Ignores volume and elapsed time on purpose: in anomaly mode the question
    is not "has enough happened" but "is any of it surprising".

    The repeat reason ("back on the same thing") is gated by
    repeat_already_announced: an action stays repeated for as long as the
    agent keeps doing it, so without this gate anomaly mode would force a
    flush on *every* occurrence of an already-flagged repeat, forever,
    making it the loudest verbosity in the app instead of the quietest. A first-touch
    context can still surface (note_seen) even while a repeat is muted,
    since "first time on this" is a one-shot signal, not a repeat one.
    """
    cfg = cfg or {}
    # Repetition is judged on the action (identity); "first time on this" on
    # what is being touched (context). Callers without an identity keep the
    # old behaviour of judging both on context.
    key = identity if identity is not None else context
    if repeat_already_announced(state, tool, key):
        if note_seen(state, context):
            return "first time on this"
        return None
    n = repeat_count(state, tool, key, cfg.get("repeat_window", 180))
    if n >= 2:
        return f"back on the same thing again ({n} times)"
    if note_seen(state, context):
        return "first time on this"
    return None


def pending_summary(items):
    """One compact line describing a batch, or None if empty.

    Repeats collapse to a count ("2 Edit"); a lone call keeps its context so
    the utterance stays specific — relevance mattered more than timing in
    Iqbal & Bailey's own results.
    """
    if not items:
        return None
    parts = []
    for tool in dict.fromkeys(p["tool"] for p in items):
        group = [p for p in items if p["tool"] == tool]
        if len(group) == 1:
            ctx = group[0].get("context")
            parts.append(f"{tool}: {ctx}" if ctx else tool)
        else:
            contexts = [p["context"] for p in group if p.get("context")]
            head = f"{len(group)} {tool}"
            parts.append(f"{head} ({contexts[-1]})" if contexts else head)
    return "; ".join(parts)
