"""terminal_jump.py — the face's two buttons: Skip and Take me there.

Both act on a project label, read off the face line that was on screen when
you clicked (Face.swift sends it through presence.py --face-action, run
detached the same way --face-event already is).

Skip is the easy one: it writes the same activity token a typed prompt
writes (nudge.touch_activity), so a running nudge or watchdog stops at its
next check and the face's own "waiting" line takes itself down -- no new
mechanism, just the one UserPromptSubmit already gives every other reader.

Take me there works from what nudge.record_alive last captured for this
project (terminal_info), in two independent best-effort steps:
  - tmux, when a pane was recorded: make the pane current in its window and
    the window current in its session, then switch a client to it -- one
    already showing that session, else the one used most recently. The
    pane's own tty is no terminal window, so the client's tty is the one
    the next step raises. A pane that has since closed, or no client
    attached, fails this step alone.
  - the terminal app: the one that owns that tty, found by walking up from
    the processes on it (login, the shell, a tmux client) to the first .app
    bundle. In iTerm2 or Terminal.app, whose AppleScript dictionaries give a
    session's or tab's tty, that exact tab is selected. Any other app --
    Ghostty, which scripts its terminals but not their ttys, or an IDE -- is
    brought to the front with `open -a`, which needs no permission.

Both steps are "whatever was true last time a tool ran in this project";
a closed window, a quit app, or a pane that no longer exists just means
nothing happens, logged, never an error dialog. The first time the iTerm2
or Terminal.app script runs, macOS asks to let Hobson control that app --
the normal Automation permission prompt, not something this module can
pre-approve.
"""

import os
import subprocess

import log_record
import nudge

# How far up from a process on a terminal's tty to look for its app: login
# or the shell, then the app, with room to spare.
APP_HOPS = 8

_ITERM_SCRIPT = """
on run argv
  set targetTTY to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is targetTTY then
            select s
            select t
            select w
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "no-match"
end run
"""

_TERMINAL_SCRIPT = """
on run argv
  set targetTTY to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is targetTTY then
          set selected of t to true
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "no-match"
end run
"""

# The terminals whose tab can be found by tty, by bundle name (iTerm2's is
# iTerm.app).
_TAB_SCRIPTS = {"iTerm.app": _ITERM_SCRIPT, "Terminal.app": _TERMINAL_SCRIPT}


def _run(cmd, timeout=3.0):
    """True on a zero exit. Never raises: a missing binary, a dead pane, a
    quit app are all "could not", not a crash."""
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _osascript(script, arg, timeout=5.0):
    """The script's `return` value, or None if osascript itself could not
    run (not installed, timed out)."""
    try:
        result = subprocess.run(["osascript", "-", arg], input=script, capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _output(cmd, timeout=3.0):
    """Its output lines on a zero exit, else []. Never raises."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    return result.stdout.splitlines() if result.returncode == 0 else []


def active_tmux_client():
    """(tty, pane) of the tmux client you are looking at, or None: the one
    flagged `focused` (tmux's focus events), else the one used most recently.
    `#{pane_id}` in list-clients is the client's current pane. Activity counts
    typing, not focus, so on its own it misses a window you only clicked."""
    clients = []
    for line in _output(["tmux", "list-clients", "-F",
                         "#{client_activity}\t#{client_flags}\t#{client_tty}\t#{pane_id}"]):
        fields = line.split("\t")
        if len(fields) == 4 and fields[0].isdigit():
            clients.append(("focused" in fields[1].split(","), int(fields[0]), fields[2], fields[3]))
    if not clients:
        return None
    _, _, tty, pane = max(clients)
    return tty, pane


def _tmux_client(pane):
    """The tty of the tmux client to show this pane on: one already showing
    the pane's session, else the one used most recently. None with the pane
    gone or no client attached."""
    # A pane that does not exist prints an empty line and exits 0.
    session = "".join(_output(["tmux", "display-message", "-p", "-t", pane, "#{session_id}"]))
    if not session:
        return None
    clients = []
    for line in _output(["tmux", "list-clients", "-F",
                         "#{client_activity}\t#{session_id}\t#{client_tty}"]):
        fields = line.split("\t")
        if len(fields) == 3 and fields[0].isdigit() and fields[2]:
            clients.append((fields[1] == session, int(fields[0]), fields[2]))
    return max(clients)[2] if clients else None


def _jump_tmux(pane):
    """Make the pane current in its window and session, then switch a client
    to it (_tmux_client). (selected, the client's tty or None); nothing past
    select-window is tried against a pane that is gone."""
    if not _run(["tmux", "select-window", "-t", pane]):
        return False, None
    _run(["tmux", "select-pane", "-t", pane])
    client = _tmux_client(pane)
    if client:
        _run(["tmux", "switch-client", "-c", client, "-t", pane])
    return True, client


def _owning_app(tty):
    """The .app bundle of the terminal that owns `tty`, or None: up from the
    parent of each process on it that is not itself on it (login's parent,
    or the shell's) to the first executable inside an .app. The outermost
    bundle in its path, so an IDE's helper app resolves to the IDE."""
    rows = [line.split() for line in _output(["ps", "-o", "pid=,ppid=", "-t", tty])]
    rows = [row for row in rows if len(row) == 2]
    on_tty = {pid for pid, _ in rows}
    for start in dict.fromkeys(ppid for _, ppid in rows if ppid not in on_tty):
        pid = start
        for _ in range(APP_HOPS):
            found = _output(["ps", "-o", "ppid=,comm=", "-p", pid])
            fields = found[0].split(None, 1) if found else []
            if len(fields) != 2:
                break
            ppid, comm = fields
            if ".app/" in comm:
                return comm[:comm.index(".app/") + len(".app")]
            if ppid in ("0", "1"):
                break
            pid = ppid
    return None


def _raise_terminal(tty):
    """Bring forward the terminal showing `tty`: the exact tab in iTerm2 or
    Terminal.app, the app itself anywhere else. (ok, what happened, for the
    log)."""
    app = _owning_app(tty)
    if app is None:
        return False, f"no app owns {tty}"
    name = os.path.basename(app)
    script = _TAB_SCRIPTS.get(name)
    if script and _osascript(script, tty) == "ok":
        return True, f"{name} tab selected"
    if _run(["open", "-a", app]):
        return True, f"{name} raised"
    return False, f"could not raise {name}"


def take_there(project):
    """"Take me there": best effort, from this project's last recorded
    terminal (nudge.terminal_info, stamped on every tool call). Never
    raises -- a failure here must stay a quiet log line, the same rule the
    face already keeps for everything it shows."""
    alive = nudge.read_alive(project)
    pane, tty = alive.get("tmux_pane"), alive.get("tty")
    if not pane and not tty:
        log_record.write(f"[face] take me there for {project!r}: no terminal recorded")
        return False
    selected, tmux = False, "not in tmux"
    if pane:
        selected, tty = _jump_tmux(pane)
        tmux = f"client {tty}" if tty else "no client attached" if selected else "pane gone"
    raised, terminal = _raise_terminal(tty) if tty else (False, "none to raise")
    log_record.write(f"[face] take me there for {project!r}: tmux {tmux}, terminal {terminal}")
    return selected or raised


def skip(project):
    """"Skip": acknowledge without acting. Touches the same activity token
    a typed prompt does, so should_continue, session_status.waiting_on_you and
    the face's own "typed" check all treat this project as answered."""
    ok = nudge.touch_activity(project)
    log_record.write(f"[face] skipped {project!r}" if ok else f"[face] skip failed for {project!r}")
    return ok
