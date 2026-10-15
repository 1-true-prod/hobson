"""What each session is doing, as far as you are concerned: waiting on you,
failed, working, finished.

The facts are written in four places -- session state (session_state.py),
the liveness record and the activity token (nudge.py, which the
UserPromptSubmit hook must be able to import cheaply) and a running nudge's
lock -- and read only here. Presence, Ask Hobson and the engines each used to
put them back together for themselves, two of them opening the session files
raw, past the schema session_state backfills.

The rule they all share: a turn that finished, or that ended waiting on the
agent's own work, leaves nothing waiting on you.
"""

import time
from typing import NamedTuple, Optional

import home
import nudge

# The notification types that mean the session is waiting on you:
# nudge.WAITING_TYPES, which start a nudge, plus a permission prompt, which
# does not but is no less a wait.
WAITING_NOTIFICATIONS = tuple(nudge.WAITING_TYPES) + ("permission_prompt",)

# Stop categories after which nothing waits on you: the Stop said it
# finished, or the agent's own subagents or build will wake it.
SETTLED = ("done", "working")

# How old a permission request or a question can be and still count as
# waiting on you. An abandoned session from this morning is not what
# "before you go" means.
PERMISSION_WAIT_SECONDS = 600
QUESTION_WAIT_SECONDS = 2 * 3600

# How recent each counts for Ask Hobson: a failure older than this is
# history, a finish older than this is not news, and a tool call this recent
# is work.
FAILED_WITHIN = 2 * 3600
FINISHED_WITHIN = 3600
WORKING_WITHIN = 120


# ── One hook event ─────────────────────────────────────────────────────────

def nudge_type(hook_input):
    """This Notification's type if it means the session is blocked on you
    and starts a nudge (nudge.WAITING_TYPES), else None."""
    ntype = hook_input.get("notification_type") or ""
    return ntype if ntype in nudge.WAITING_TYPES else None


def is_question_dialog(hook_input):
    """AskUserQuestion or ExitPlanMode, which reach Hobson as a
    PermissionRequest: a question for you, not a request for approval."""
    return (hook_input.get("hook_event_name") == "PermissionRequest"
            and hook_input.get("tool_name") in nudge.USER_WAIT_TOOLS)


def idle_after_settled_turn(hook_input, state=None):
    """True for an idle_prompt after a turn that finished or ended waiting
    on the agent's own work (SETTLED): it fires whenever the input sits idle,
    and nothing is waiting on you. A question, a failure or an unknown ending
    does not match, and neither does agent_needs_input: a background agent
    that asks is blocked by definition. `state` is this project's session
    state when the caller already holds it."""
    if nudge_type(hook_input) != "idle_prompt":
        return False
    try:
        import session_state
        if state is None:
            state = session_state.load_session(home.derive_project_label())
        return session_state.stop_category(state) in SETTLED
    except Exception:
        return False


# ── Every session ──────────────────────────────────────────────────────────

class _Facts(NamedTuple):
    project: str
    category: Optional[str]   # the last Stop's, None when unknown
    last_stop: float
    last_event: float         # any handled event (session_state.mark_event)
    activity: float           # the last prompt you typed (or Skip on the face)
    alive: dict               # the last tool call or permission request
    alive_ts: float
    nudging: bool             # a nudge is running for it now


def _read_float(path):
    try:
        with open(path, encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def _facts():
    """Each session's facts, in the order of its state file's name."""
    import session_state
    for state in session_state.load_all():
        project = state.get("project") or ""
        alive = nudge.read_alive(project)
        yield _Facts(
            project=project,
            category=session_state.stop_category(state),
            last_stop=float(state.get("last_stop_time") or 0),
            last_event=float(state.get("last_event_time") or 0),
            activity=_read_float(nudge.activity_path(project)),
            alive=alive,
            alive_ts=float(alive.get("ts") or 0),
            nudging=nudge.running("nudge", project),
        )


def _need(f, now):
    """What this session waits on you for -- "you", "answer" or "approval"
    -- or None. Answered by anything since: a prompt you typed, a later turn,
    a later tool call.

    Known false positive: an approved Bash call that runs long (a build)
    leaves the liveness record at PermissionRequest until the next tool
    call, so for up to PERMISSION_WAIT_SECONDS it reads as waiting on your
    approval. The window is what bounds it; nothing records the approval."""
    if f.nudging:
        return "you"
    if (f.category == "question" and f.last_stop > max(f.activity, f.alive_ts)
            and now - f.last_stop <= QUESTION_WAIT_SECONDS):
        return "answer"
    asks = f.alive.get("tool") in nudge.USER_WAIT_TOOLS
    if (f.alive_ts > max(f.activity, f.last_stop) and now - f.alive_ts <= PERMISSION_WAIT_SECONDS
            and (f.alive.get("event") == "PermissionRequest" or asks)):
        return "answer" if asks else "approval"
    return None


# What a session can wait on you for, as a sentence ends: "waiting on your approval".
NEED = {"approval": "your approval", "answer": "an answer from you", "you": "you"}


def waiting_on_you(now=None):
    """[(project, need)] for every session blocked on you, most recent first,
    need being "you" (a nudge is running), "answer" or "approval". A session's
    time is its last turn or tool call, whichever is later."""
    now = time.time() if now is None else now
    waits = [(max(f.last_stop, f.alive_ts), f.project, need)
             for f in _facts() for need in [_need(f, now)] if need]
    return [(p, need) for _, p, need in sorted(waits, key=lambda w: -w[0])]


def reading(now=None):
    """Every named session in one of these at most, the most urgent:
    {waiting: [(project, need)], failed: [project], finished: [project],
    working: [project]}, each most recent first (waiting as waiting_on_you
    has it)."""
    now = time.time() if now is None else now
    waits = waiting_on_you(now)
    waiting = {p for p, _ in waits}
    failed, finished, working = [], [], []
    for f in _facts():
        if not f.project or f.project == "unknown" or f.project in waiting:
            continue
        later = max(f.activity, f.alive_ts, f.last_event)
        if now - f.last_event <= WORKING_WITHIN and f.last_event > f.last_stop + 1:
            working.append((f.last_event, f.project))
        elif (f.category == "broken" and now - f.last_stop <= FAILED_WITHIN
              and later <= f.last_stop + 1):
            failed.append((f.last_stop, f.project))
        elif f.category in SETTLED and now - f.last_stop <= FINISHED_WITHIN:
            finished.append((f.last_stop, f.project))
    newest = lambda pairs: [p for _, p in sorted(pairs, reverse=True)]  # noqa: E731
    return {"waiting": waits, "failed": newest(failed), "finished": newest(finished),
            "working": newest(working)}


# ── The menu bar's list ────────────────────────────────────────────────────

_STATE_TEXT = {"failed": "failed", "working": "working", "finished": "finished"}


def menu_json(now=None):
    """What the sensor's menu draws, as a dict ready for json.dumps:
    {"waiting": N, "rows": [{"project", "state", "text"}]}. Rows run most
    urgent first -- waiting, failed, working, finished -- each group most
    recent first, as reading() has them. `waiting` is the badge on the icon.
    A session with no project name has nowhere to jump to and is left out, as
    reading() leaves it out of the rest."""
    seen = reading(now)
    waits = [(p, need) for p, need in seen["waiting"] if p and p != "unknown"]
    rows = [{"project": p, "state": "waiting", "text": f"{p} — waiting on {NEED.get(need, 'you')}"}
            for p, need in waits]
    for state in ("failed", "working", "finished"):
        rows += [{"project": p, "state": state, "text": f"{p} — {_STATE_TEXT[state]}"}
                 for p in seen[state]]
    return {"waiting": len(waits), "rows": rows}


if __name__ == "__main__":
    import json
    import sys
    if sys.argv[1:] == ["--json"]:
        print(json.dumps(menu_json()))
    else:
        sys.exit("usage: session_status.py --json")
