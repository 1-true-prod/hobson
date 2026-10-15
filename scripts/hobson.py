#!/usr/bin/env python3
"""hobson.py — Unified entrypoint for hobson voice notifications.

Called by Claude Code hooks (PermissionRequest, Stop, Notification).
Reads config from ~/.claude/hobson.json and dispatches to the active engine.
"""

import json
import os
import sys

# Ensure scripts/ is on the path for bark_templates imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home
import log_record


ENGINES = {
    "say": "engines.say.SayEngine",
    "kokoro-realtime": "engines.kokoro_realtime.KokoroRealtimeEngine",
    "chatterbox": "engines.chatterbox.ChatterboxEngine",
    "pocket-tts": "engines.pocket_tts_realtime.PocketTTSRealtimeEngine",
}


def engine_class(engine_name):
    """The class ENGINES names for engine_name, or None if it names none."""
    engine_path = ENGINES.get(engine_name)
    if not engine_path:
        return None
    module_path, class_name = engine_path.rsplit(".", 1)
    import importlib
    return getattr(importlib.import_module(module_path), class_name)


def load_engine(config):
    """Instantiate the configured TTS engine."""
    engine_name = config.get("engine", "say")
    cls = engine_class(engine_name)
    if cls is None:
        log_record.write(f"unknown engine {engine_name!r}, falling back to say")
        cls = engine_class("say")
    return cls(config)


def _record_alive(hook_input):
    """Mark this project's session alive (nudge.record_alive). Never raises:
    a failed write costs the watchdog one data point, not the hook."""
    try:
        from nudge import record_alive
        record_alive(home.derive_project_label(), hook_input)
    except (ImportError, OSError):
        pass


def _record_activity():
    """Write a fresh timestamp for this project's nudge/watchdog to cancel against.

    Runs unconditionally — even while muted — because it only records that
    the user typed something; it never itself speaks. Keyed per project, so
    typing here acknowledges this session only, not every other session that
    may be waiting on the user.
    """
    try:
        from nudge import touch_activity
        touch_activity(home.derive_project_label())
    except ImportError:
        pass


def main():
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}

    # The cheapest path, and it must stay that way: record the activity
    # token and return before config or engine load. This used to call
    # load_config() first, on every single prompt, for no reason.
    if hook_input.get("hook_event_name") == "UserPromptSubmit":
        _record_activity()
        return

    # Every tool call and permission request marks the session alive for
    # the watchdog -- any tool, main session or subagent, muted or not.
    if hook_input.get("hook_event_name") in ("PreToolUse", "PermissionRequest"):
        _record_alive(hook_input)

    # One lstat once everything has moved; before that, the one time a config
    # under the old name is taken over as Hobson's.
    home.migrate_legacy_state()
    config = home.load_config()
    reason = home.silence_reason(config)
    if reason:
        log_record.write(f"silenced ({reason}): {_silenced_what(hook_input, config)}")
        _ensure_presence(config)
        return

    _ensure_presence(config)
    engine = load_engine(config)
    engine.run(hook_input)


def _silenced_what(hook_input, config):
    """What a silenced hook was, for its log line -- and a Stop is still
    read, by the rules alone (_read_silent_stop), and what waits on you is
    still shown (_show_silently)."""
    event = hook_input.get("hook_event_name") or "unknown event"
    category = None
    if event == "Notification":
        what = f"Notification ({hook_input.get('notification_type') or 'untyped'})"
    elif event == "Stop" and home.event_enabled(config, "Stop"):
        category = _read_silent_stop(hook_input)
        what = f"Stop, read as {category}"
    else:
        what = event
    shown = _show_silently(hook_input, config, category)
    if event == "Stop" and home.event_enabled(config, "Stop"):
        shown += _show_notes_silently(hook_input, config, shown == ", shown")
    return what + shown


def _show_silently(hook_input, config, stop_category=None):
    """While Hobson is silenced (muted, quiet hours), what waits on you is
    still shown on the face, without a sound: a permission request, a
    waiting notification, a Stop the rules read as a question. The user
    mutes for meetings, and Hobson used to show nothing then either. The
    line is the fixed one the engines speak (announcement), with no engine,
    no model and nothing over the network: a command the local risk rules
    cannot place gets the ordinary permission line. Silenced, the helper
    senses nothing (face only), so company is never known and the line is
    shown whole. Returns the log line's ending: ", shown", ", not shown
    (why)", ", not signalled (see [face])", or "" for nothing to show."""
    event = hook_input.get("hook_event_name")
    if (event not in ("Stop", "PermissionRequest", "Notification")
            or not home.event_enabled(config, event)
            or (event == "Stop" and stop_category != "question")):
        return ""
    try:
        import face
        from session_state import transaction, record_wait_shown
        if not face.settings(config).get("enabled"):
            return ", not shown (face off)"
        project = home.derive_project_label()
        with transaction(project) as state:
            phrase = _silent_line(hook_input, state, config)
        if not phrase:
            return ""
        if not face.deliver(config, phrase, "waiting", project=project, silent=True):
            return ", not signalled (see [face])"
        if event == "PermissionRequest":
            with transaction(project) as state:
                record_wait_shown(state, hook_input.get("session_id"))
        return ", shown"
    except Exception as e:
        return f", not shown ({e})"


def _show_notes_silently(hook_input, config, wait_shown):
    """A silenced Stop still claims the notes cc-plugin-you-should-know has
    left (plugin_notes.claim, as the engines do), so unmuting does not read
    out what the meeting went through, and shows the first one on the face,
    its line only: no model, no engine, no words in the log. A wait this
    Stop has just put on the face keeps the window; the notes are claimed
    all the same. Returns the log line's ending."""
    try:
        import face
        import plugin_notes
        notes = plugin_notes.claim()
        if not notes:
            return ""
        first, rest = notes[0], notes[1:]
        ending = f", {len(notes)} notes claimed" if rest else ", note claimed"
        if wait_shown:
            return ending + ", not shown (a wait is on the face)"
        if not face.settings(config).get("enabled"):
            return ending + ", not shown (face off)"
        current = hook_input.get("session_id")
        project = home.derive_project_label()
        if first.session != current:
            project = plugin_notes.project_of(first.session) or project
        line = log_record.Unlogged(first.line, f"note {first.tag}, {first.length} chars")
        if not face.deliver(config, line, "note", project=project, silent=True):
            return ending + ", not signalled (see [face])"
        return ending + f", note {first.tag} shown"
    except Exception as e:
        return f", notes not shown ({e})"


def _silent_line(hook_input, state, config):
    """The line _show_silently shows, or None. Every one waits on you.
    Two gates the engines do not have: Claude Code's reminder of a request
    just shown says nothing (it would replace the specific line with a
    vaguer one), and an idle prompt is shown only after a Stop read as a
    question -- the silent read cannot tell a finished turn from an unknown
    one, and every finished turn of a meeting would show "needs you"."""
    import random
    import announcement
    import bark_templates
    from session_state import stop_category, wait_just_shown
    pick = lambda candidates, state: random.choice(candidates)
    event = hook_input.get("hook_event_name")
    if event == "Stop":
        lines = announcement.question_lines(bark_templates)
        return pick(lines, state) if lines else None
    ntype = hook_input.get("notification_type")
    if event == "Notification" and ntype == "permission_prompt" and wait_just_shown(
            state, hook_input.get("session_id"), announcement.PERMISSION_ECHO_SECONDS):
        return None
    if event == "Notification" and ntype == "idle_prompt" and stop_category(state) != "question":
        return None
    risk = None
    if event == "PermissionRequest":
        try:
            from risk import permission_risk
            risk = permission_risk(hook_input.get("tool_name"), hook_input.get("tool_input"),
                                   config, remote=False)
        except Exception:
            risk = None
    chosen = announcement.fixed_line(hook_input, state, bark_templates, pick, risk)
    if chosen is not None:
        return chosen.phrase
    if event == "PermissionRequest":
        return announcement.permission_line(bark_templates, hook_input.get("tool_name"))
    return None


def _read_silent_stop(hook_input):
    """A Stop while Hobson is silent: read by the rules alone -- no model,
    nothing said, no nudge -- and its category recorded, so what waits on you
    is still known when you unmute. Muted, Hobson used to skip the Stop
    unread, and the last category it knew stood in for every turn since.
    Returns the category, "unknown" when the rules cannot tell."""
    try:
        import stop_outcome
        from session_state import transaction, begin_stop, record_stop_category
        reading = stop_outcome.read(hook_input)
        category = None
        if reading is not None and reading.still_working:
            category = "working"
        elif reading is not None and reading.awaiting_answer:
            category = "question"
        with transaction(home.derive_project_label()) as state:
            begin_stop(state)
            record_stop_category(state, category)
        return category or "unknown"
    except Exception as e:
        return f"unread ({e})"


def _ensure_presence(config):
    """Start the presence sensor if it should be running and is not. Never
    raises: without it, Hobson speaks exactly as he did before it existed."""
    try:
        import presence
        presence.ensure_running(config)
    except Exception as e:
        log_record.write(f"[presence] could not start the sensor ({e})")


if __name__ == "__main__":
    main()
