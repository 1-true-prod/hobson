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
def _pocket_daemon(claude_home, script_dir, home):
    """A stand-in Pocket TTS daemon run from script_dir with HOME=home (its
    pid and token files go there), on the port the config names."""
    script = script_dir / "pocket-tts-daemon.py"
    script.write_text(_STAND_IN)
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
