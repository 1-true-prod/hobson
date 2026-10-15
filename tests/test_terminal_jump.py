"""The face's Skip / Take me there buttons: scripts/terminal_jump.py.

Every subprocess call (tmux, osascript) is mocked -- this module must never
shell out for real in a test, same rule as afplay/say elsewhere.
"""

import json
import time

import pytest

import nudge
import terminal_jump


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def no_subprocess(monkeypatch):
    """Record every subprocess.run call instead of making it. `results`
    maps a command's leading tokens to the _Result it should get back --
    the longest prefix that matches wins, so a test can script tmux
    succeeding and osascript failing (or the reverse) in the same case; an
    unlisted command succeeds with no output."""
    box = {"calls": [], "results": {}}

    def fake_run(cmd, **kwargs):
        box["calls"].append(cmd)
        prefixes = [key for key in box["results"] if tuple(cmd[:len(key)]) == key]
        return box["results"][max(prefixes, key=len)] if prefixes else _Result()

    monkeypatch.setattr(terminal_jump.subprocess, "run", fake_run)
    return box


def _log(claude_home):
    path = claude_home / "hobson.log"
    return path.read_text() if path.exists() else ""


def _seed_alive(project, **fields):
    with open(nudge.alive_path(project), "w", encoding="utf-8") as f:
        json.dump(fields, f)


# ── Skip ─────────────────────────────────────────────────────────────────


def test_skip_touches_the_activity_token(claude_home):
    started = time.time() - 10
    assert terminal_jump.skip("proj")
    assert nudge.should_continue(nudge.activity_path("proj"), started, time.time()) is False


def test_skip_logs_the_outcome(claude_home):
    terminal_jump.skip("webapp")
    assert "[face] skipped 'webapp'" in _log(claude_home)


def test_skip_failure_is_logged_and_does_not_raise(claude_home, monkeypatch):
    monkeypatch.setattr(nudge, "touch_activity", lambda project, now=None: False)
    assert terminal_jump.skip("webapp") is False
    assert "[face] skip failed for 'webapp'" in _log(claude_home)


# ── Take me there: nothing recorded for this project ──────────────────────


def test_take_there_with_nothing_recorded_does_nothing(claude_home, no_subprocess):
    assert terminal_jump.take_there("unknown-project") is False
    assert no_subprocess["calls"] == []
    assert "no terminal recorded" in _log(claude_home)


# ── _tmux_client: which client shows the pane ──────────────────────────────

_CLIENTS = ("tmux", "list-clients")
_SESSION = ("tmux", "display-message")


def test_tmux_client_prefers_one_already_showing_the_pane_session(no_subprocess):
    no_subprocess["results"][_SESSION] = _Result(stdout="$3\n")
    no_subprocess["results"][_CLIENTS] = _Result(
        stdout="900\t$1\t/dev/ttys000\n500\t$3\t/dev/ttys002\n")
    assert terminal_jump._tmux_client("%7") == "/dev/ttys002"


def test_tmux_client_else_takes_the_one_used_most_recently(no_subprocess):
    """The usual case: one terminal, its client on some other session."""
    no_subprocess["results"][_SESSION] = _Result(stdout="$3\n")
    no_subprocess["results"][_CLIENTS] = _Result(
        stdout="500\t$1\t/dev/ttys000\n900\t$2\t/dev/ttys002\n")
    assert terminal_jump._tmux_client("%7") == "/dev/ttys002"


def test_tmux_client_none_attached(no_subprocess):
    no_subprocess["results"][_SESSION] = _Result(stdout="$3\n")
    no_subprocess["results"][_CLIENTS] = _Result(stdout="")
    assert terminal_jump._tmux_client("%7") is None


def test_tmux_client_for_a_missing_pane_is_none(no_subprocess):
    """tmux answers display-message for a pane that does not exist with an
    empty line and exit 0 (seen live): that is not a session to match."""
    no_subprocess["results"][_SESSION] = _Result(stdout="\n")
    no_subprocess["results"][_CLIENTS] = _Result(stdout="900\t$1\t/dev/ttys000\n")
    assert terminal_jump._tmux_client("%7") is None


# ── _jump_tmux ─────────────────────────────────────────────────────────────


def test_jump_tmux_selects_window_and_pane_then_switches_the_client(no_subprocess):
    no_subprocess["results"][_SESSION] = _Result(stdout="$3\n")
    no_subprocess["results"][_CLIENTS] = _Result(stdout="900\t$1\t/dev/ttys000\n")
    assert terminal_jump._jump_tmux("%7") == (True, "/dev/ttys000")
    calls = no_subprocess["calls"]
    assert calls[:2] == [["tmux", "select-window", "-t", "%7"], ["tmux", "select-pane", "-t", "%7"]]
    assert calls[-1] == ["tmux", "switch-client", "-c", "/dev/ttys000", "-t", "%7"]


def test_jump_tmux_with_no_client_only_selects(no_subprocess):
    """A detached session: the pane is made current for whoever attaches,
    and there is no client to switch or terminal to raise."""
    no_subprocess["results"][_SESSION] = _Result(stdout="$3\n")
    no_subprocess["results"][_CLIENTS] = _Result(stdout="")
    assert terminal_jump._jump_tmux("%7") == (True, None)
    assert not [c for c in no_subprocess["calls"] if c[1] == "switch-client"]


def test_jump_tmux_gives_up_once_select_window_fails(no_subprocess):
    """A pane that has since closed: select-window fails, and nothing else
    is attempted against a target that no longer exists."""
    no_subprocess["results"][("tmux", "select-window")] = _Result(returncode=1)
    assert terminal_jump._jump_tmux("%7") == (False, None)
    assert no_subprocess["calls"] == [["tmux", "select-window", "-t", "%7"]]


# ── _owning_app: the app behind a tty ──────────────────────────────────────


def _processes(box, on_tty, parents):
    """Script `ps`: the pid/ppid rows on the tty, then each ancestor's
    ppid and executable by pid."""
    box["results"][("ps", "-o", "pid=,ppid=")] = _Result(
        stdout="".join(f"{pid} {ppid}\n" for pid, ppid in on_tty))
    for pid, (ppid, comm) in parents.items():
        box["results"][("ps", "-o", "ppid=,comm=", "-p", pid)] = _Result(stdout=f"{ppid} {comm}\n")


def test_owning_app_walks_from_the_terminal_login_to_its_app(no_subprocess):
    """As found live: Ghostty -> login -> zsh -> the tmux client."""
    _processes(no_subprocess, on_tty=[("3401", "3337"), ("3404", "3401"), ("36322", "3404")],
               parents={"3337": ("1", "/Applications/Ghostty.app/Contents/MacOS/ghostty")})
    assert terminal_jump._owning_app("/dev/ttys000") == "/Applications/Ghostty.app"


def test_owning_app_takes_the_outermost_bundle(no_subprocess):
    """An IDE's shell runs under a helper app inside the IDE's bundle; the
    helper cannot be brought forward, the IDE can."""
    helper = "/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper"
    _processes(no_subprocess, on_tty=[("50", "40")], parents={"40": ("30", helper)})
    assert terminal_jump._owning_app("/dev/ttys001") == "/Applications/Visual Studio Code.app"


def test_owning_app_none_without_an_app_above(no_subprocess):
    """A tmux pane's tty: its processes descend from the tmux server, which
    launchd started, and no app."""
    _processes(no_subprocess, on_tty=[("80", "70")],
               parents={"70": ("1", "tmux"), "1": ("0", "/sbin/launchd")})
    assert terminal_jump._owning_app("/dev/ttys004") is None


def test_owning_app_none_for_a_tty_nobody_is_on(no_subprocess):
    no_subprocess["results"][("ps", "-o", "pid=,ppid=")] = _Result(returncode=1)
    assert terminal_jump._owning_app("/dev/ttys999") is None


# ── _raise_terminal ────────────────────────────────────────────────────────


def test_raise_terminal_brings_ghostty_forward_without_a_script(no_subprocess, monkeypatch):
    """Ghostty's AppleScript has no tty to match a terminal by: the app is
    brought forward, and no Automation prompt is spent on a script."""
    monkeypatch.setattr(terminal_jump, "_owning_app", lambda tty: "/Applications/Ghostty.app")
    assert terminal_jump._raise_terminal("/dev/ttys000") == (True, "Ghostty.app raised")
    assert no_subprocess["calls"] == [["open", "-a", "/Applications/Ghostty.app"]]


def test_raise_terminal_selects_the_iterm_tab_by_tty(no_subprocess, monkeypatch):
    monkeypatch.setattr(terminal_jump, "_owning_app", lambda tty: "/Applications/iTerm.app")
    no_subprocess["results"][("osascript", "-")] = _Result(stdout="ok\n")
    assert terminal_jump._raise_terminal("/dev/ttys003") == (True, "iTerm.app tab selected")
    assert no_subprocess["calls"] == [["osascript", "-", "/dev/ttys003"]]


def test_raise_terminal_uses_terminal_app_script_for_terminal_app(monkeypatch):
    seen = []
    monkeypatch.setattr(terminal_jump, "_owning_app",
                        lambda tty: "/System/Applications/Utilities/Terminal.app")
    monkeypatch.setattr(terminal_jump, "_osascript", lambda script, arg: seen.append(script) or "ok")
    assert terminal_jump._raise_terminal("/dev/ttys003")[0]
    assert seen == [terminal_jump._TERMINAL_SCRIPT]


def test_raise_terminal_falls_back_to_the_app_when_no_tab_matches(no_subprocess, monkeypatch):
    monkeypatch.setattr(terminal_jump, "_owning_app", lambda tty: "/Applications/iTerm.app")
    no_subprocess["results"][("osascript", "-")] = _Result(stdout="no-match\n")
    assert terminal_jump._raise_terminal("/dev/ttys003") == (True, "iTerm.app raised")
    assert no_subprocess["calls"][-1] == ["open", "-a", "/Applications/iTerm.app"]


def test_a_nonzero_osascript_exit_is_not_a_match(no_subprocess, monkeypatch):
    """osascript itself failing (missing, timed out) must read as "no
    match", never as whatever stray stdout it left behind."""
    monkeypatch.setattr(terminal_jump, "_owning_app", lambda tty: "/Applications/iTerm.app")
    no_subprocess["results"][("osascript", "-")] = _Result(returncode=1, stdout="ok\n")
    assert terminal_jump._raise_terminal("/dev/ttys003") == (True, "iTerm.app raised")


def test_raise_terminal_reports_an_app_it_could_not_raise(no_subprocess, monkeypatch):
    monkeypatch.setattr(terminal_jump, "_owning_app", lambda tty: "/Applications/Ghostty.app")
    no_subprocess["results"][("open", "-a")] = _Result(returncode=1)
    assert terminal_jump._raise_terminal("/dev/ttys000") == (False, "could not raise Ghostty.app")


def test_raise_terminal_with_no_app_does_nothing(no_subprocess, monkeypatch):
    monkeypatch.setattr(terminal_jump, "_owning_app", lambda tty: None)
    assert terminal_jump._raise_terminal("/dev/ttys004") == (False, "no app owns /dev/ttys004")
    assert no_subprocess["calls"] == []


def test_osascript_raising_is_not_a_match(monkeypatch):
    def boom(*a, **k):
        raise OSError("osascript not found")

    monkeypatch.setattr(terminal_jump.subprocess, "run", boom)
    assert terminal_jump._osascript(terminal_jump._ITERM_SCRIPT, "/dev/ttys003") is None


# ── take_there: the two steps together ─────────────────────────────────────


def test_take_there_in_tmux_raises_the_client_terminal_not_the_pane(claude_home, monkeypatch):
    """The live case: only the pane is recorded, and the terminal to raise
    is the one the client tmux switched is in."""
    _seed_alive("webapp", tmux_pane="%7")
    raised = []
    monkeypatch.setattr(terminal_jump, "_jump_tmux", lambda pane: (True, "/dev/ttys000"))
    monkeypatch.setattr(terminal_jump, "_raise_terminal",
                        lambda tty: raised.append(tty) or (True, "Ghostty.app raised"))
    assert terminal_jump.take_there("webapp") is True
    assert raised == ["/dev/ttys000"]
    assert ("[face] take me there for 'webapp': tmux client /dev/ttys000, terminal Ghostty.app raised"
            in _log(claude_home))


def test_take_there_in_tmux_with_no_client_raises_nothing(claude_home, monkeypatch):
    _seed_alive("webapp", tmux_pane="%7")
    raised = []
    monkeypatch.setattr(terminal_jump, "_jump_tmux", lambda pane: (True, None))
    monkeypatch.setattr(terminal_jump, "_raise_terminal", lambda tty: raised.append(tty))
    assert terminal_jump.take_there("webapp") is True
    assert raised == []
    assert "tmux no client attached, terminal none to raise" in _log(claude_home)


def test_take_there_with_the_pane_gone_fails(claude_home, monkeypatch):
    _seed_alive("webapp", tmux_pane="%7")
    monkeypatch.setattr(terminal_jump, "_jump_tmux", lambda pane: (False, None))
    assert terminal_jump.take_there("webapp") is False
    assert "tmux pane gone, terminal none to raise" in _log(claude_home)


def test_take_there_outside_tmux_raises_the_recorded_tty(claude_home, monkeypatch):
    _seed_alive("webapp", tty="/dev/ttys003")
    called = []
    monkeypatch.setattr(terminal_jump, "_jump_tmux", lambda *a: called.append(a) or (True, None))
    monkeypatch.setattr(terminal_jump, "_raise_terminal", lambda tty: (True, "iTerm.app tab selected"))
    assert terminal_jump.take_there("webapp") is True
    assert called == []
    assert "tmux not in tmux, terminal iTerm.app tab selected" in _log(claude_home)


def test_take_there_fails_when_neither_step_finds_anything(claude_home, monkeypatch):
    _seed_alive("webapp", tty="/dev/ttys003")
    monkeypatch.setattr(terminal_jump, "_raise_terminal", lambda tty: (False, "no app owns /dev/ttys003"))
    assert terminal_jump.take_there("webapp") is False


def test_take_there_end_to_end_with_ghostty(claude_home, no_subprocess):
    """Every command take_there runs for the live setup, in order: tmux
    first, then the app, and never an AppleScript for Ghostty."""
    _seed_alive("webapp", tmux_pane="%7")
    no_subprocess["results"][_SESSION] = _Result(stdout="$3\n")
    no_subprocess["results"][_CLIENTS] = _Result(stdout="900\t$1\t/dev/ttys000\n")
    _processes(no_subprocess, on_tty=[("3401", "3337")],
               parents={"3337": ("1", "/Applications/Ghostty.app/Contents/MacOS/ghostty")})
    assert terminal_jump.take_there("webapp") is True
    assert [c[:2] for c in no_subprocess["calls"]] == [
        ["tmux", "select-window"], ["tmux", "select-pane"], ["tmux", "display-message"],
        ["tmux", "list-clients"], ["tmux", "switch-client"], ["ps", "-o"], ["ps", "-o"],
        ["open", "-a"]]
