"""home.py: every Hobson path under one directory, resolved from $HOME."""

import glob
import json
import os
import re

import pytest

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
    settings and transcripts. The TTS daemons were exceptions too, until
    their runner (tts_daemon.py) took its paths from home.py like the rest."""
    allowed = {"home.py", "settings-merge.py", "stop_outcome.py"}
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


# ── Writing the config ─────────────────────────────────────────────────────

def _written(claude_home):
    import json
    return json.loads((claude_home / "hobson.json").read_text())


def test_update_config_sets_a_value_and_leaves_a_default_out(claude_home):
    assert home.update_config({"volume": 7, "kokoro.voice": "bm_lewis"}) == [
        {"key": "volume", "value": 7, "op": "set"},
        {"key": "kokoro.voice", "value": "bm_lewis", "op": "set"}]
    home.update_config({"volume": home.DEFAULT_CONFIG["volume"]})
    assert _written(claude_home) == {"kokoro": {"voice": "bm_lewis"}}
    assert home.load_config()["kokoro"]["voice"] == "bm_lewis"


def test_update_config_writes_through_a_symlink_and_keeps_the_mode(claude_home, tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text("{}")
    real.chmod(0o600)
    (claude_home / "hobson.json").symlink_to(real)
    home.update_config({"muted": True})
    assert (claude_home / "hobson.json").is_symlink()
    assert oct(real.stat().st_mode & 0o777) == oct(0o600)
    assert '"muted": true' in real.read_text()


def test_an_unreadable_config_is_kept_aside_not_written_over(claude_home):
    (claude_home / "hobson.json").write_text("{half a fi")
    home.update_config({"volume": 5})
    assert _written(claude_home) == {"volume": 5}
    [kept] = glob.glob(str(claude_home / "hobson.json.unreadable-*"))
    assert open(kept).read() == "{half a fi"


def test_write_config_keeps_only_what_it_is_given(claude_home):
    home.update_config({"volume": 9, "engine": "kokoro-realtime"})
    home.write_config({"engine": "kokoro-realtime", "personality": "hobson"})
    assert _written(claude_home) == {"engine": "kokoro-realtime"}


def test_the_cli_takes_json_or_a_word_and_never_runs_it(claude_home):
    assert home.main(["--set", "volume", "4.5", "events", '["stop"]',
                      "kokoro.voice", "x'); import os #"]) == 0
    assert _written(claude_home) == {"volume": 4.5, "events": ["stop"],
                                     "kokoro": {"voice": "x'); import os #"}}
    assert home.main(["--set", "volume"]) == 2


def test_the_cli_reads_every_default_from_default_config(claude_home, capsys):
    """The CLI keeps no default of its own: a value left at its default is
    not in the file, so --get must find it in DEFAULT_CONFIG."""
    home.update_config({"volume": 7})
    assert home.main(["--get", "volume", "events", "commentary.chattiness",
                      "kokoro.voice", "muted", "quiet_hours", "nope.nothing"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "7", " ".join(home.DEFAULT_CONFIG["events"]),
        str(home.DEFAULT_CONFIG["commentary"]["chattiness"]),
        home.DEFAULT_CONFIG["kokoro"]["voice"], "false", "", ""]


def test_the_cli_reads_through_load_config_aliases(claude_home, capsys):
    (claude_home / "hobson.json").write_text('{"personality": "alfred"}')
    home.main(["--get", "personality"])
    assert capsys.readouterr().out == "hobson\n"


def test_an_atomic_write_given_a_mode_is_never_wider(claude_home, monkeypatch):
    """The face record and hobson.env hold what was said and a key: the temp
    file is 600 from creation, not chmodded after it was written."""
    target = str(claude_home / "secret")
    modes = []
    real_replace = os.replace

    def replace(src, dst):
        modes.append(os.stat(src).st_mode & 0o777)
        real_replace(src, dst)

    monkeypatch.setattr(home.os, "replace", replace)
    home.write_atomic(target, "key=1\n", mode=0o600)
    assert modes == [0o600] and os.stat(target).st_mode & 0o777 == 0o600
    assert open(target).read() == "key=1\n"


def test_an_atomic_write_that_fails_leaves_the_old_file_and_no_temp(claude_home, monkeypatch):
    target = claude_home / "record.json"
    home.write_json_atomic(str(target), {"n": 1})

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(home.os, "replace", fail)
    with pytest.raises(OSError):
        home.write_json_atomic(str(target), {"n": 2})
    assert json.loads(target.read_text()) == {"n": 1}
    assert [f for f in os.listdir(claude_home) if ".tmp" in f] == []


# ── hobson.env and the endpoint rule, shared by the decider and push ───────

def test_read_env_reads_simple_lines(claude_home):
    (claude_home / "hobson.env").write_text(
        "# a comment\n\nOTHER=1\nA_KEY = \"quoted\"\nSINGLE='s'\nEMPTY=\nNO_EQUALS\nPLAIN=tk_abc=def\n")
    assert home.read_env("A_KEY") == "quoted"
    assert home.read_env("SINGLE") == "s"
    assert home.read_env("PLAIN") == "tk_abc=def"
    assert home.read_env("EMPTY") is None
    assert home.read_env("NO_EQUALS") is None
    assert home.read_env("MISSING") is None
    assert home.read_env("comment") is None


def test_read_env_without_a_file_is_none(claude_home):
    assert home.read_env("A_KEY") is None


def test_read_env_ignores_the_environment_and_export_lines(claude_home, monkeypatch):
    monkeypatch.setenv("A_KEY", "from-env")
    (claude_home / "hobson.env").write_text("export A_KEY=exported\n")
    assert home.read_env("A_KEY") is None


@pytest.mark.parametrize("url, ok", [
    ("https://ntfy.example.com/t", True),
    ("https://user:pw@ntfy.example.com/t", True),
    ("http://127.0.0.1:8080/t", True),
    ("http://localhost/t", True),
    ("http://[::1]:9/t", True),
    ("http://ntfy.example.com/t", False),
    ("http://localhost.evil.com/t", False),
    ("http://127.0.0.1@evil.com/t", False),
    ("http://evil.com/127.0.0.1", False),
    ("ftp://localhost/t", False),
    ("http://[::1", False),
    ("", False),
    (None, False),
])
def test_https_or_loopback(url, ok):
    assert home.https_or_loopback(url) is ok
