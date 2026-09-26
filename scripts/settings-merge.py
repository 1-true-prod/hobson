#!/usr/bin/env python3
"""settings-merge.py — Safely merge hobson hooks into ~/.claude/settings.json.

Used by install.sh. Can also be run standalone:
    python3 settings-merge.py              # merge hooks
    python3 settings-merge.py --remove     # remove hooks (used by uninstall)
    python3 settings-merge.py --check      # check if hooks are installed
"""

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime

# Claude Code reads its settings from $CLAUDE_CONFIG_DIR when that is set, so
# hooks written to ~/.claude would never run for someone who relocated it.
CLAUDE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
SETTINGS_FILE = os.path.join(CLAUDE_CONFIG_DIR, "settings.json")

# The hooks we inject — command points to the unified entrypoint. The
# interpreter is the one the installer checked, by absolute path: Claude Code
# runs hooks with its own PATH, and when launched from the desktop app or an
# IDE that PATH can resolve `python3` to something else entirely -- on a Mac
# without the Command Line Tools, to a stub that pops an install dialog on
# every hook. Bare `python3` remains the default for a manual run.
HOOK_COMMAND_TEMPLATE = '"{python}" "{install_dir}/scripts/hobson.py"'
DEFAULT_PYTHON = "python3"

HOOKS_TO_INJECT = {
    "PermissionRequest": [{
        "hooks": [{
            "type": "command",
            "command": None,  # filled in at runtime
            "async": True,
        }]
    }],
    "Stop": [{
        "hooks": [{
            "type": "command",
            "command": None,
            "async": True,
        }]
    }],
    "Notification": [{
        # permission_prompt is spoken on directly. idle_prompt and
        # agent_needs_input are nudge.WAITING_TYPES -- the signal that a
        # session is blocked on the user. The matcher is applied by Claude
        # Code before hobson runs, so a type left off this list never
        # arrives at all: nudges shipped with both of theirs filtered out
        # here and never fired once. tests/test_settings_merge.py pins this
        # list to WAITING_TYPES so they cannot drift apart again.
        "matcher": "permission_prompt|idle_prompt|agent_needs_input",
        "hooks": [{
            "type": "command",
            "command": None,
            "async": True,
        }]
    }],
    "PreToolUse": [{
        "hooks": [{
            "type": "command",
            "command": None,
            "async": True,
        }]
    }],
    "UserPromptSubmit": [{
        "hooks": [{
            "type": "command",
            "command": None,
            "async": True,
        }]
    }],
}

# Old installs added this to permissions.allow. It is no longer added, and
# merge and uninstall both take it out: hooks run outside the permission
# system, so it only ever let Claude itself run `say` unprompted -- and
# `say -o <file>` writes anywhere.
PERMISSION_ENTRY = "Bash(say:*)"


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            settings = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        print(f"ERROR: {SETTINGS_FILE} contains invalid JSON", file=sys.stderr)
        sys.exit(1)
    if not isinstance(settings, dict):
        print(f"ERROR: {SETTINGS_FILE} is not a JSON object", file=sys.stderr)
        sys.exit(1)
    return settings


def _hooks_of(settings):
    """settings["hooks"] as a dict, created if absent or null."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    return hooks


def _entries(hooks, event):
    """One event's hook entries as a list; null or a stray value reads as none."""
    entries = hooks.get(event)
    return entries if isinstance(entries, list) else []


def _drop_say_permission(settings):
    """Remove the grant old installs added. True if it was there."""
    permissions = settings.get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    if isinstance(allow, list) and PERMISSION_ENTRY in allow:
        allow.remove(PERMISSION_ENTRY)
        return True
    return False


def save_settings(settings):
    # Write atomically: a crash or disk-full mid-write must never leave the
    # user's global settings.json as truncated/invalid JSON (it gates *all* of
    # Claude Code's hooks, not just hobson's).
    #
    # Write through a symlink, not over it: dotfile managers (stow, chezmoi,
    # a synced repo) keep settings.json as a link, and os.replace on the link
    # itself would silently swap it for a local copy the user never sees.
    target = os.path.realpath(SETTINGS_FILE)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(target):
        shutil.copymode(target, tmp)  # keep a 600 settings.json at 600
    os.replace(tmp, target)


def backup_settings():
    if not os.path.isfile(SETTINGS_FILE):
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{SETTINGS_FILE}.backup-{ts}"
    shutil.copy2(SETTINGS_FILE, backup)
    return backup


# What marks a hook as Hobson's: its entrypoint, not the word. Matching any
# command containing "claudio" (the old name) claimed the hooks of anyone
# whose home is /Users/claudio -- install replaced them, uninstall deleted
# them -- and Hobson is a surname too.
# claudio.py is the entrypoint's name before 0.3.0: a hook still running it
# is Hobson's (install replaces it, uninstall removes it), just out of date.
_OUR_ENTRYPOINT = re.compile(r"[/\\]scripts[/\\](?:hobson|claudio)\.py\b")
_OLD_ENTRYPOINT = re.compile(r"[/\\]scripts[/\\]claudio\.py\b")
_LEGACY_MARKERS = ("claude-bark", "voice-bark")  # names from before the rename


def _is_our_hook(hook_entry):
    """Check if a hook entry belongs to hobson (or legacy claude-bark)."""
    if not isinstance(hook_entry, dict):
        return False
    for h in hook_entry.get("hooks") or []:
        cmd = (h.get("command") or "") if isinstance(h, dict) else ""
        if _OUR_ENTRYPOINT.search(cmd) or any(m in cmd for m in _LEGACY_MARKERS):
            return True
    return False


def merge_hooks(install_dir, python=DEFAULT_PYTHON):
    """Add hobson hooks to settings.json."""
    command = HOOK_COMMAND_TEMPLATE.format(python=python, install_dir=install_dir)

    settings = load_settings()
    before = json.dumps(settings, sort_keys=True)

    if _drop_say_permission(settings):
        print(f"  Removed permission {PERMISSION_ENTRY} (added by older hobson installs)")

    # Merge hooks
    hooks = _hooks_of(settings)
    for event, new_entries in HOOKS_TO_INJECT.items():
        existing = _entries(hooks, event)

        # Remove any existing hobson/claude-bark hooks
        existing = [e for e in existing if not _is_our_hook(e)]

        # Add our hooks with the correct command
        for entry in new_entries:
            entry_copy = json.loads(json.dumps(entry))  # deep copy
            for h in entry_copy.get("hooks", []):
                if h.get("command") is None:
                    h["command"] = command
            existing.append(entry_copy)

        hooks[event] = existing

    # Re-running the installer is how hobson updates, so an unchanged file
    # is left alone: no rewrite, and no backup piling up per run.
    if json.dumps(settings, sort_keys=True) == before:
        print(f"  Hooks already current in {SETTINGS_FILE}")
        return

    backup = backup_settings()
    if backup:
        print(f"  Backed up settings to {backup}")
    save_settings(settings)
    print(f"  Hooks written to {SETTINGS_FILE}: {', '.join(HOOKS_TO_INJECT)}")


def remove_hooks():
    """Remove hobson hooks from settings.json."""
    if not os.path.isfile(SETTINGS_FILE):
        print("  No settings.json found, nothing to remove")
        return

    settings = load_settings()
    hooks = settings.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    removed = []

    for event in list(hooks.keys()):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue  # not a shape hobson ever wrote; leave it be
        kept = [e for e in entries if not _is_our_hook(e)]
        if len(kept) < len(entries):
            removed.append(event)
            # Drop an event hobson emptied; one that was already empty is theirs
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]

    if _drop_say_permission(settings):
        removed.append(f"permission:{PERMISSION_ENTRY}")

    if removed:
        backup = backup_settings()
        if backup:
            print(f"  Backed up settings to {backup}")
        save_settings(settings)
        print(f"  Removed: {', '.join(removed)}")
    else:
        print("  No hobson hooks found in settings")


def _shape(entry):
    """An entry's behaviour, ignoring the command path.

    The command differs by install location and by quoting era, neither of
    which is staleness. The matcher and hook flags are what change what
    hobson receives, so those are what an install is compared on.
    """
    return {
        "matcher": entry.get("matcher") or "",
        "hooks": [{k: v for k, v in h.items() if k != "command"}
                  for h in entry.get("hooks") or [] if isinstance(h, dict)],
    }


def _interpreter(command):
    """The absolute interpreter path a hook command starts with, if any."""
    try:
        argv = shlex.split(command or "")
    except ValueError:
        return None
    return argv[0] if argv and os.path.isabs(argv[0]) else None


def check_hooks():
    """True only if every hobson hook is installed and current.

    Finding *some* hobson hooks is not enough. This used to return True on
    any match, and `hobson doctor` trusts the exit code -- so a machine
    that had never received UserPromptSubmit (the hook that cancels a nudge
    the moment the user types) reported "Hooks installed" for a month.
    Now each expected event must be present, and shaped the way the current
    installer would write it; anything else is named, with the fix.
    """
    if not os.path.isfile(SETTINGS_FILE):
        print("not installed")
        return False

    installed = load_settings().get("hooks")
    installed = installed if isinstance(installed, dict) else {}
    found, missing, stale, gone = [], [], [], set()

    for event, expected_entries in HOOKS_TO_INJECT.items():
        ours = [e for e in _entries(installed, event) if _is_our_hook(e)]
        if not ours:
            missing.append(event)
            continue
        found.append(event)
        if [_shape(e) for e in ours] != [_shape(e) for e in expected_entries] \
                or any(_OLD_ENTRYPOINT.search(h.get("command") or "")
                       for e in ours for h in e.get("hooks") or [] if isinstance(h, dict)):
            stale.append(event)
        for e in ours:
            for h in e.get("hooks") or []:
                interpreter = _interpreter(h.get("command") if isinstance(h, dict) else "")
                if interpreter and not os.access(interpreter, os.X_OK):
                    gone.add(interpreter)

    if not found:
        print("not installed")
        return False

    print(f"installed: {', '.join(found)}")
    if missing:
        print(f"missing: {', '.join(missing)}")
    if stale:
        print(f"out of date: {', '.join(stale)}")
    if gone:
        # A hook whose interpreter was uninstalled fails inside Claude Code
        # without a word; this is the only place it can be seen.
        print(f"interpreter missing: {', '.join(sorted(gone))}")
    if missing or stale or gone:
        print("fix: re-run settings-merge.py (it replaces hobson's hooks in place)")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Manage hobson hooks in settings.json")
    parser.add_argument("--remove", action="store_true", help="Remove hooks")
    parser.add_argument("--check", action="store_true", help="Check if hooks are installed")
    parser.add_argument("--install-dir", default=None,
                        help="hobson install directory (for hook command paths)")
    parser.add_argument("--python", default=DEFAULT_PYTHON,
                        help="interpreter the hooks run (absolute path recommended)")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check_hooks() else 1)
    elif args.remove:
        remove_hooks()
    else:
        install_dir = args.install_dir
        if not install_dir:
            # Default: parent of scripts/ directory
            install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        merge_hooks(install_dir, python=args.python)


if __name__ == "__main__":
    main()
