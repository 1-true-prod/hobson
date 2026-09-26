#!/usr/bin/env python3
"""nudge.py — Escalating, cancellable re-announcement for a session waiting
on the user, plus (Task 9) a `--watchdog` hang detector.

Runs as a short-lived detached process, spawned by engines/base.py the same
way afplay survives a hook process exiting. Every utterance re-checks
silence_reason() and a user-activity token, so muting or typing a prompt
stops it immediately.

Design rules (do not relax — a repeat-until-acknowledged feature is the
fastest route to the user disabling claudio entirely):
  - Escalating gaps, never constant (NUDGE_DELAYS).
  - Hard cap of len(NUDGE_DELAYS) re-announcements, then silence forever.
  - Instant cancel: the UserPromptSubmit hook writes this project's
    activity token (activity_path); any timestamp there newer than this
    nudge's start time cancels it. Per project, not global -- typing in one
    session must not silence another session that is waiting on you.
  - Obeys silence_reason() before every utterance.
  - One nudge per project (a lock file); a second request while one is
    running is dropped, not queued.
  - Only for a session blocked on the user. A nudge means "you are what
    this session is waiting on" and nothing else -- see the note on
    _WAITING_PHRASES for why there is no stuck nudge.
  - Never the same sentence twice — phrasing escalates in directness.
"""

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engines.base import (  # noqa: E402
    derive_project_label, load_config, log, silence_reason,
)

LOCK_DIR = os.path.expanduser("~/.claude")

# Escalating gaps in seconds; length is the hard cap on re-announcements.
# Overridable via config "nudge.delays" — see _run().
NUDGE_DELAYS = (45, 120, 300)

# The two documented Claude Code notification_type values that mean
# "the session is waiting on you" (see NOTIFICATION_LABELS in base.py).
WAITING_TYPES = ("idle_prompt", "agent_needs_input")

# Escalating in directness. Never the same sentence twice.
#
# There is deliberately no stuck variant. One existed, but nothing ever
# spawned it, and when the question was examined against the log the case
# against wiring it up was decisive: all 56 "going in circles" episodes
# ended with the agent finishing on its own, median three minutes later,
# none with it asking for help. A repeating nudge would have nagged about 56
# problems that solved themselves -- and a stuck agent that does give up
# asks, which is idle_prompt, which is this nudge already.
_WAITING_PHRASES = [
    "Whenever you have a moment, I'm still waiting on you.",
    "Still waiting on you here.",
    "I need you for this — nothing moves until you reply.",
]

def nudge_schedule(delays=NUDGE_DELAYS):
    """The escalating gap schedule, exposed for testing."""
    return tuple(delays)


def _project_key(project):
    """The short stable key every per-project file in LOCK_DIR is named by."""
    return hashlib.sha256((project or "").encode("utf-8")).hexdigest()[:12]


def _lock_path(kind, project):
    """kind is 'nudge' or 'watchdog' — separate lock namespaces, since a
    project may reasonably have one of each running at once."""
    return os.path.join(LOCK_DIR, f"claudio-{kind}-{_project_key(project)}.lock")


def activity_path(project):
    """This project's user-activity token: written by the UserPromptSubmit
    hook, read by this project's nudge and watchdog to cancel on typing.

    Per project on purpose. It used to be one global file, so typing in any
    session cancelled every project's nudge -- which, across several tabs,
    silenced "this session is waiting on you" at exactly the moment you were
    busy somewhere else.
    """
    return os.path.join(LOCK_DIR, f"claudio-activity-{_project_key(project)}")


# Tools that park the session on the user. While one is the last thing that
# happened, silence is the user deciding, not a hang.
USER_WAIT_TOOLS = ("AskUserQuestion", "ExitPlanMode")


def alive_path(project):
    """This project's liveness record: the last tool call or permission
    request, any tool, main session or subagent. Written by the entrypoint on
    every PreToolUse/PermissionRequest, read by the watchdog.

    A file of its own, not a session_state field: the commentary path only
    loads session state for the tools it narrates, so a stretch of Read,
    WebSearch or MCP calls -- or a subagent's whole run -- used to look like
    total silence. And a write on every tool call, subagents included, must
    not read-modify-write the file that holds the commentary queue.
    """
    return os.path.join(LOCK_DIR, f"claudio-alive-{_project_key(project)}")


def record_alive(project, hook_input):
    """Record the shape of this call -- when, which event, which tool, its
    timeout -- never its contents. Atomic, so a reader never sees half."""
    tool_input = hook_input.get("tool_input")
    timeout = tool_input.get("timeout") if isinstance(tool_input, dict) else None
    record = {"ts": time.time(), "event": hook_input.get("hook_event_name") or "",
              "tool": hook_input.get("tool_name") or ""}
    if isinstance(timeout, (int, float)):
        record["timeout_ms"] = timeout
    path = alive_path(project)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    os.replace(tmp, path)


def read_alive(project):
    """The liveness record, or {} if there is none or it cannot be read."""
    try:
        with open(alive_path(project), encoding="utf-8") as f:
            record = json.load(f)
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def should_continue(state_path, started_at, now):
    """False once user activity is newer than `started_at`.

    A missing or corrupt activity file must never stop the nudge — that
    would silently disable it for anyone who hasn't touched
    UserPromptSubmit yet (a fresh install, or the file simply not existing).
    """
    try:
        with open(state_path, encoding="utf-8") as f:
            last_activity = float(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return True
    return last_activity <= started_at


def _phrase_for(step):
    return _WAITING_PHRASES[min(step, len(_WAITING_PHRASES) - 1)]


def _speak(config, text):
    import claudio
    engine = claudio.load_engine(config)
    engine.speak_dynamic(text, allow_cold_start=True)


def _run(project, subject):
    """The escalating re-announcement loop. Never called from tests."""
    config = load_config()
    lock_path = _lock_path("nudge", project)

    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"[nudge] skipped — a nudge is already running for {project!r}")
        fd.close()
        return

    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()

    delays = tuple(config.get("nudge", {}).get("delays") or NUDGE_DELAYS)
    started_at = time.time()
    log(f"[nudge] started for {project!r}: {subject!r}")

    try:
        for step, delay in enumerate(delays):
            time.sleep(delay)

            if not should_continue(activity_path(project), started_at, time.time()):
                log(f"[nudge] cancelled (user activity) after step {step + 1}")
                return

            # Re-read config on every step, not just at startup — a live
            # `claudio off` mid-escalation must be able to silence an
            # already-running nudge, not just future ones.
            live_config = load_config()
            reason = silence_reason(live_config)
            if reason:
                log(f"[nudge] silenced ({reason}), skipping step {step + 1}")
                continue

            phrase = _phrase_for(step)
            _speak(live_config, phrase)
            log(f"[nudge] spoke ({step + 1}/{len(delays)}) -> {phrase!r}")

        log(f"[nudge] capped ({len(delays)}/{len(delays)}), giving up on {project!r}")
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()
        try:
            os.remove(lock_path)
        except OSError:
            pass


def watchdog_should_speak(last_event, last_stop, minutes, now):
    """True when activity has gone quiet for `minutes` with no Stop since.

    Pure counting — no model call. A finished turn
    (last_stop at or after last_event) is not a hang: Stop is the normal
    end of work, not a failure to notice. A missing/zero last_event means
    no session data yet, which must never raise or false-trigger.
    """
    if not last_event:
        return False
    if (now - last_event) < minutes * 60:
        return False
    if last_stop and last_stop >= last_event:
        return False
    return True


def watchdog_patience(minutes, alive):
    """Seconds of silence that mean a stall, or None while the session is
    waiting on the user -- an open question, plan approval or permission
    prompt is the user's move, and "nothing's moved" would be the wrong
    thing to say about it. A Bash call gets at least its own timeout plus a
    minute: a build the agent expected to take fifteen minutes is not stuck
    at ten.
    """
    alive = alive or {}
    if alive.get("event") == "PermissionRequest" or alive.get("tool") in USER_WAIT_TOOLS:
        return None
    patience = minutes * 60
    timeout_ms = alive.get("timeout_ms")
    if alive.get("tool") == "Bash" and isinstance(timeout_ms, (int, float)):
        patience = max(patience, timeout_ms / 1000 + 60)
    return patience


def watchdog_verdict(state, alive, baseline, minutes, now):
    """One watchdog poll: "resumed" (exit, the session moved), "wait", or
    "speak". Pure, so the loop around it stays thin enough to trust.

    Replayed over 508 real sessions, the watchdog as it was would have said
    "nothing's moved" 53 times, 6 of them real stalls: ~36 were Claude
    waiting on an answer to its own question, ~9 stretches of tools that
    never reached session state, 4 builds inside their own timeout. This
    verdict fires 13 times on the same replay, each a 13-minute to 12-hour
    silence after a call that never returned.
    """
    alive = alive or {}
    last_event = max(state.get("last_event_time", 0) or 0, alive.get("ts", 0) or 0)
    if last_event > baseline:
        return "resumed"
    patience = watchdog_patience(minutes, alive)
    if patience is None:
        return "wait"
    if watchdog_should_speak(last_event or baseline, state.get("last_stop_time", 0) or 0,
                             patience / 60, now):
        return "speak"
    return "wait"


def _run_watchdog(project, minutes, baseline=None):
    """Wake every 60s; announce once if nothing has moved, then exit.

    Hard cap of one announcement per watchdog — it is a hang detector, not
    a heartbeat. Never called from tests.

    `baseline` is the parent's in-memory last_event_time at spawn time. The
    parent saves that value to disk only *after* spawning this process (see
    engines/base.py's _handle_commentary_llm), so re-reading it from disk
    here would race the parent's own save and always see a stale (pre-flush)
    value — making every watchdog compare against a timestamp its own parent
    is about to overwrite, and exit unconditionally on the first poll. When
    the caller supplies a baseline explicitly, trust it instead. Fall back to
    the disk read only when no baseline was given (e.g. a hand-run
    `python3 scripts/nudge.py --watchdog`).
    """
    lock_path = _lock_path("watchdog", project)
    try:
        fd = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log(f"[watchdog] already running for {project!r}, exiting")
        return

    from session_state import load_session

    started_at = time.time()
    if baseline is not None:
        baseline_event = baseline
    else:
        baseline_event = load_session(project).get("last_event_time", 0) or 0
    log(f"[watchdog] started for {project!r} ({minutes}m)")

    try:
        while True:
            time.sleep(60)
            now = time.time()

            if not should_continue(activity_path(project), started_at, now):
                log(f"[watchdog] cancelled (user activity) for {project!r}")
                return

            verdict = watchdog_verdict(load_session(project), read_alive(project),
                                       baseline_event or started_at, minutes, now)
            if verdict == "resumed":
                log(f"[watchdog] activity resumed, exiting for {project!r}")
                return
            if verdict == "wait":
                continue

            live_config = load_config()
            reason = silence_reason(live_config)
            if reason:
                log(f"[watchdog] silenced ({reason}), exiting for {project!r}")
                return

            phrase = f"Nothing's moved on {project} for {round(minutes)} minutes."
            _speak(live_config, phrase)
            log(f"[watchdog] spoke -> {phrase!r}")
            return
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()
        try:
            os.remove(lock_path)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=None)
    parser.add_argument("--subject", default="")
    parser.add_argument("--watchdog", action="store_true",
                        help="Run as a hang detector instead of a nudge.")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--baseline", type=float, default=None,
                        help="Parent's in-memory last_event_time at spawn "
                             "time, to avoid racing the parent's own "
                             "post-spawn save_session().")
    args = parser.parse_args()

    project = args.project or derive_project_label()

    if args.watchdog:
        _run_watchdog(project, args.minutes, baseline=args.baseline)
        return

    _run(project, args.subject)


if __name__ == "__main__":
    main()
