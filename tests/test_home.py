"""home.py: every Hobson path under one directory, resolved from $HOME."""

import glob
import os
import re

import home

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")

NAMED_PATHS = (home.config_file, home.log_file, home.env_file, home.sessions_dir,
               home.bark_lock, home.commentary_lock)


def test_state_dir_follows_home_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "a"))
    first = home.state_dir()
    monkeypatch.setenv("HOME", str(tmp_path / "b"))
    assert first == str(tmp_path / "a" / ".claude")
    assert home.state_dir() == str(tmp_path / "b" / ".claude")


def test_every_named_path_is_under_state_dir(claude_home):
    for named in NAMED_PATHS:
        assert os.path.dirname(named()) == str(claude_home), named.__name__
    assert os.path.dirname(home.project_file("nudge", "demo", ".lock")) == str(claude_home)


def test_project_files_share_one_key(claude_home):
    key = home.project_key("demo")
    assert re.fullmatch(r"[0-9a-f]{12}", key)
    assert os.path.basename(home.project_file("alive", "demo")) == f"hobson-alive-{key}"
    assert os.path.basename(home.project_file("nudge", "demo", ".lock")) == f"hobson-nudge-{key}.lock"
    assert home.project_key(None) == home.project_key("")


def test_no_other_module_builds_a_state_path():
    """A path built from "~/.claude" outside home.py is one that setting HOME
    in a test does not move -- which is how a test once wrote to the real
    ~/.claude. The exceptions are not Hobson's state: Claude Code's own
    settings and transcripts, and the TTS daemons, which run in their own
    venvs and keep their own pid and log files."""
    allowed = {"home.py", "settings-merge.py", "stop_outcome.py",
               "kokoro-daemon.py", "pocket-tts-daemon.py"}
    built = re.compile(r"""(expanduser|join)\(\s*f?["']~/\.claude""")
    offenders = []
    for path in glob.glob(os.path.join(SCRIPTS, "**", "*.py"), recursive=True):
        if os.path.basename(path) in allowed:
            continue
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if built.search(line):
                    offenders.append(f"{os.path.relpath(path, SCRIPTS)}:{n}")
    assert offenders == []
