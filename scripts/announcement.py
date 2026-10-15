"""announcement.py — the line for an event whose meaning is fixed.

A destructive permission request, a question dialog, a notification that the
session is waiting on you, a permission request itself: each has a template,
never the model. Two callers choose them: the engines, which speak the line
(BaseEngine._fixed_announcement), and the silenced hook, which shows it on the
face (hobson._show_silently). That one may not import an engine, so the lines
are chosen here, and each caller records what it did with them.
"""

import random
import re
from typing import NamedTuple, Optional

import session_status

# Claude Code sends a permission_prompt Notification 6-7 s after a permission
# request nobody has answered yet (92 of 93 in the log, up to 13 s). Within
# this long of a request said for the same session, it is the same wait.
PERMISSION_ECHO_SECONDS = 20


class FixedAnnouncement(NamedTuple):
    """The phrase for a fixed-meaning event, or None when the event is
    handled by saying nothing, and its log label."""
    phrase: Optional[str]
    label: str


def format_tool_name(tool):
    from bark_templates import normalize_tool_name
    tool = normalize_tool_name(tool)
    if not tool:
        return "something important"
    return re.sub(r"\s+", " ", tool)[:80]


def risky_phrase(reason):
    return f"Careful — this one {reason}. It needs your approval."


def question_lines(templates):
    group = templates.CATEGORIES.get("question") or {}
    return [f"{lead} {tail}" for lead in group.get("lead", []) for tail in group.get("tail", [])]


def permission_line(templates, tool):
    """A permission request for `tool`, from the templates. An MCP or
    internal tool name is not read out: it gets a generic line."""
    tool = format_tool_name(tool)
    if "_" in tool:
        return random.choice(templates.GENERIC_PERMISSION_PHRASES)
    return f"{random.choice(templates.PERMISSION_LEADS)} {random.choice(templates.PERMISSION_ACTIONS).format(tool=tool)}"


def fixed_line(hook_input, state, templates, pick, risk=None):
    """For an event whose meaning is fixed -- a destructive permission
    request (`risk`, why it is destructive), a question dialog, a
    notification that the session is waiting on you -- its line, or None if
    this is not one. `pick(candidates, state)` chooses among the templates.
    Reads `state` (this project's session state) and writes nothing."""
    event = hook_input.get("hook_event_name")
    if event == "PermissionRequest" and risk:
        return FixedAnnouncement(risky_phrase(risk), f"[PermissionRequest] destructive ({risk})")
    if session_status.is_question_dialog(hook_input):
        candidates, label = question_lines(templates), "[PermissionRequest] question dialog"
    elif event == "Notification" and hook_input.get("notification_type") == "permission_prompt":
        # Claude Code's reminder of a request it has just sent. Said
        # again, it was the model's, from "waiting for your approval"
        # alone: 48 of 93 never mentioned a wait, 14 claimed a failure.
        from session_state import wait_just_said
        if wait_just_said(state, hook_input.get("session_id"), PERMISSION_ECHO_SECONDS):
            return FixedAnnouncement(None, "[Notification] not announced — the permission request was just said")
        candidates, label = list(templates.NOTIFICATION_TEMPLATES), "[Notification] permission template"
    elif event == "Notification" and session_status.nudge_type(hook_input):
        if session_status.idle_after_settled_turn(hook_input, state):
            # Handled, by saying nothing -- falling through would hand
            # the event to the model, which is worse than either.
            return FixedAnnouncement(None, "[Notification] not announced — the last turn finished")
        candidates, label = list(templates.NOTIFICATION_TEMPLATES), "[Notification] waiting template"
    else:
        return None
    if not candidates:
        return None
    return FixedAnnouncement(pick(candidates, state), label)
