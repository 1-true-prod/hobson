#!/usr/bin/env python3
"""hobson.py — Unified entrypoint for hobson voice notifications.

Called by Claude Code hooks (PermissionRequest, Stop, Notification).
Reads config from ~/.claude/hobson.json and dispatches to the active engine.
"""

import json
import os
import sys
import time

# Ensure scripts/ is on the path for bark_templates imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engines.base import (derive_project_label, load_config, log,
                          migrate_legacy_state, silence_reason)


ENGINES = {
    "say": "engines.say.SayEngine",
    "kokoro-realtime": "engines.kokoro_realtime.KokoroRealtimeEngine",
    "chatterbox": "engines.chatterbox.ChatterboxEngine",
    "pocket-tts": "engines.pocket_tts_realtime.PocketTTSRealtimeEngine",
}


def load_engine(config):
    """Instantiate the configured TTS engine."""
    engine_name = config.get("engine", "say")
    engine_path = ENGINES.get(engine_name)
    if not engine_path:
        log(f"unknown engine {engine_name!r}, falling back to say")
        engine_path = ENGINES["say"]

    module_path, class_name = engine_path.rsplit(".", 1)
    import importlib
    module = importlib.import_module(module_path)
    engine_class = getattr(module, class_name)
    return engine_class(config)


def _record_alive(hook_input):
    """Mark this project's session alive (nudge.record_alive). Never raises:
    a failed write costs the watchdog one data point, not the hook."""
    try:
        from nudge import record_alive
        record_alive(derive_project_label(), hook_input)
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
        from nudge import activity_path
        path = activity_path(derive_project_label())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except (ImportError, OSError):
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

    # Four lstat calls once everything has moved; before that, the one time
    # claudio's config, key, log and sessions are taken over as Hobson's.
    migrate_legacy_state()
    config = load_config()
    reason = silence_reason(config)
    if reason:
        log(f"silenced ({reason})")
        return

    engine = load_engine(config)
    engine.run(hook_input)


if __name__ == "__main__":
    main()
