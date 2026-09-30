"""`hobson daemon`: the CLI takes a daemon's port, files and start command from
its engine (DaemonEngine.cli_lines), so it serves every daemon engine.

It used to keep its own copy, for Kokoro alone: `hobson daemon stop` could not
stop Pocket TTS, and `hobson status` and `doctor` never reported its daemon.
"""

import contextlib
import json
import os
import subprocess
import sys

import pytest

import hobson
import tts_daemon
from engines.daemon import DaemonEngine
from engines.kokoro_realtime import KokoroRealtimeEngine
from engines.pocket_tts_realtime import PocketTTSRealtimeEngine

_REAL_POPEN = subprocess.Popen  # the autouse no_audio fixture replaces it
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
CLI = os.path.join(REPO, "hobson")

# The daemon runner with a backend that loads no model. Its file is named
# after the real daemon: the CLI knows a daemon by its script's name.
_STAND_IN = """
import sys
sys.path.insert(0, sys.argv[1])
import tts_daemon
backend = tts_daemon.Backend(synthesize=lambda *a: b"RIFF" + bytes(200), voices=frozenset())
tts = tts_daemon.Daemon(__file__, backend, "charles", idle_timeout=600)
print(tts.bind(0).server_address[1], flush=True)
tts.serve(poll_interval=0.05)
"""


def _read(lines):
    facts, argv = {}, []
    for line in lines:
        key, value = line.split("=", 1)
        if key == "ARG":
            argv.append(value)
        else:
            facts[key] = value
    return facts, argv


def test_only_the_daemon_engines_have_a_daemon():
    daemons = {name for name in hobson.ENGINES if issubclass(hobson.engine_class(name), DaemonEngine)}
    assert daemons == {"kokoro-realtime", "pocket-tts"}
    assert hobson.engine_class("nonesuch") is None


@pytest.mark.parametrize("cls", [KokoroRealtimeEngine, PocketTTSRealtimeEngine])
def test_the_cli_is_told_what_the_engine_and_its_daemon_use(claude_home, cls):
    engine = cls({cls.spec.section: {"daemon_port": 23456}})
    facts, argv = _read(engine.cli_lines())

    assert facts["PORT"] == "23456"
    assert argv == engine._daemon_argv()
    assert facts["POLLS"] == str(cls.spec.startup_polls)
    # The files are the ones the daemon itself writes, not names kept here.
    daemon = tts_daemon.Daemon(facts["SCRIPT"], None, None, 0)
    assert (facts["PID_FILE"], facts["TOKEN_FILE"], facts["LOG"]) == \
        (daemon.pid_file, daemon.token_file, daemon.log_file)


def _cli(*args):
    proc = _REAL_POPEN(["/bin/bash", CLI, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = proc.communicate(timeout=30)
    return proc.returncode, out


@contextlib.contextmanager
def _pocket_daemon(claude_home, script_dir, home, stand_in=_STAND_IN):
    """A stand-in Pocket TTS daemon run from script_dir with HOME=home (its
    pid and token files go there), on the port the config names."""
    script = script_dir / "pocket-tts-daemon.py"
    script.write_text(stand_in)
    daemon = _REAL_POPEN([sys.executable, str(script), SCRIPTS], stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "HOME": str(home)})
    try:
        port = int(daemon.stdout.readline())
        (claude_home / "hobson.json").write_text(
            json.dumps({"engine": "pocket-tts", "pocket_tts": {"daemon_port": port}}))
        yield daemon
    finally:
        if daemon.poll() is None:
            daemon.kill()
        daemon.stdout.close()


def test_daemon_stop_stops_pocket_tts(claude_home, tmp_path):
    with _pocket_daemon(claude_home, tmp_path, claude_home.parent) as daemon:
        rc, out = _cli("daemon", "status")
        assert rc == 0 and "running" in out and f"(pid {daemon.pid}," in out

        rc, out = _cli("daemon", "stop")
        assert rc == 0 and f"Daemon stopped (pid {daemon.pid})" in out
        # A clean exit: /shutdown was accepted, so the CLI sent this daemon's token.
        assert daemon.wait(5) == 0
        assert not (claude_home / "pocket-tts-daemon.pid").exists()

        rc, out = _cli("daemon", "stop")
        assert rc == 0 and "Daemon was not running" in out


def test_daemon_stop_leaves_another_checkouts_daemon_alone(claude_home, tmp_path):
    """Same script name, same port, another checkout and HOME: the sandbox's
    daemon, or your own seen from a scratch HOME. It was kill -9'd, which is
    how a timing run from a scratch HOME once killed the real one."""
    other = tmp_path / "other"
    (other / ".claude").mkdir(parents=True)
    with _pocket_daemon(claude_home, other, other) as daemon:
        rc, out = _cli("daemon", "stop")
        assert rc == 0 and "Daemon was not running" in out
        assert f"Left alone: pid {daemon.pid} on port" in out
        assert daemon.poll() is None


def test_an_engine_without_a_daemon_says_so(claude_home):
    (claude_home / "hobson.json").write_text(json.dumps({"engine": "say"}))
    rc, out = _cli("daemon", "stop")
    assert rc == 1 and "Current engine: say" in out


# ── hobson say ──────────────────────────────────────────────────────────────

# Its clips carry their text, so the stand-in afplay can say what it played.
_ECHO_STAND_IN = _STAND_IN.replace('lambda *a: b"RIFF" + bytes(200)',
                                   'lambda text, *a: b"RIFF" + text.encode().ljust(200)')
assert _ECHO_STAND_IN != _STAND_IN

# afplay -v VOLUME CLIP: logs the clip's text as it starts and ends.
_AFPLAY = """#!{python}
import os, sys, time
text = open(sys.argv[-1], "rb").read()[4:].decode().strip()
log = os.environ["FAKE_AFPLAY_LOG"]
open(log, "a").write(f"start {{time.time()}} {{text}}\\n")
time.sleep(0.15)
open(log, "a").write(f"end {{time.time()}} {{text}}\\n")
"""


def test_two_readings_at_once_take_turns_and_never_overlap(claude_home, tmp_path):
    """The real CLI, twice at once, against a stand-in daemon and afplay.
    The loop it replaced started a paragraph every few seconds on top of the
    last, because `hobson test` returned as soon as afplay started."""
    bin_dir, log = tmp_path / "bin", tmp_path / "played"
    bin_dir.mkdir()
    (bin_dir / "afplay").write_text(_AFPLAY.format(python=sys.executable))
    (bin_dir / "say").write_text("#!/bin/sh\necho say >> \"$FAKE_AFPLAY_LOG\"\nexit 1\n")
    for tool in ("afplay", "say"):
        (bin_dir / tool).chmod(0o755)
    texts = {"a": "Alpha one.\n\nAlpha two.\n", "b": "Beta one.\n\nBeta two.\n"}
    for name, text in texts.items():
        (tmp_path / f"{name}.txt").write_text(text)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_AFPLAY_LOG": str(log)}

    with _pocket_daemon(claude_home, tmp_path, claude_home.parent, _ECHO_STAND_IN):
        config = json.loads((claude_home / "hobson.json").read_text())
        config.update(face={"enabled": False}, presence={"enabled": False})
        (claude_home / "hobson.json").write_text(json.dumps(config))
        readers = [_REAL_POPEN(["/bin/bash", CLI, "say", "-f", str(tmp_path / f"{name}.txt")],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                   for name in texts]
        results = [(r.communicate(timeout=30)[0], r.returncode) for r in readers]

    assert [rc for _, rc in results] == [0, 0], results
    # Read straight after both exited: each waited for its last passage.
    events = [line.split(" ", 2) for line in log.read_text().splitlines()]
    assert "say" not in log.read_text()
    starts = [(float(t), text) for kind, t, text in events if kind == "start"]
    ends = [(float(t), text) for kind, t, text in events if kind == "end"]
    assert len(starts) == len(ends) == 4
    played = sorted(starts)
    for (_, text), (next_start, _) in zip(played, played[1:]):
        assert next_start >= dict((t, e) for e, t in ends)[text]  # never two at once
    order = [text for _, text in played]
    assert order in (["Alpha one.", "Alpha two.", "Beta one.", "Beta two."],
                     ["Beta one.", "Beta two.", "Alpha one.", "Alpha two."])
    assert not list(claude_home.glob("pocket-tts-playback-*.wav"))
