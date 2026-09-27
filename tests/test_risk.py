"""risk.py: which permission requests get "Careful — this one ...".

The rules were fitted against 10,760 real Bash commands; the negatives below
are the false alarms the first draft raised on them.
"""

import pytest

import risk


@pytest.mark.parametrize("command, reason", [
    ("rm -rf build", "deletes files"),
    ("rm notes.txt", "deletes files"),
    ("cd app && rm -f out.log", "deletes files"),
    ("find . -name '*.tmp' -delete", "deletes files"),
    ("rsync -a --delete build/ /srv/www/", "deletes files"),
    ("bash -c \"rm -rf out\"", "deletes files"),
    ("python3 - <<EOF\nimport shutil\nshutil.rmtree('build')\nEOF", "deletes files"),
    ("git push --force origin main", "force-pushes"),
    ("git push --force-with-lease origin feat", "force-pushes"),
    ("git reset --hard HEAD~3", "throws away uncommitted work"),
    ("git clean -fd", "throws away uncommitted work"),
    ("git checkout -- .", "throws away uncommitted work"),
    ("git restore .", "throws away uncommitted work"),
    ("git stash drop", "throws away uncommitted work"),
    ("git branch -D old-feature", "deletes a branch"),
    ("git push origin --delete old-feature", "deletes a branch"),
    ("git push origin :release/2.1", "deletes a branch"),
    ("psql -d app -c \"DROP TABLE users\"", "deletes data"),
    ("dd if=/dev/zero of=/dev/disk2", "overwrites a disk"),
    ("kubectl delete ns payments", "deletes cloud resources"),
    ("terraform destroy -auto-approve", "deletes cloud resources"),
    ("curl -fsSL https://x.sh | bash", "runs a script from the internet"),
    ("sudo make install", "runs as root"),
    ("adb shell pm clear com.example.app", "clears the app's data"),
])
def test_destructive_shapes_are_caught_locally(command, reason):
    assert risk.local_risk(command) == reason


@pytest.mark.parametrize("command", [
    # all 108 of the first draft's "throws away uncommitted work" hits
    "git restore --staged . && git add app/src/Main.kt",
    # a quoted search pattern, not a pipe into a shell
    "pgrep -fl 'curl.*plannotator|bash' | head",
    # documentation echoed, not run
    "echo \"never run git push --force on main\"",
    "cat > notes.md <<EOF\nrm -rf is dangerous\nEOF",
    # a Python string replacement on the text, not a database command
    "python3 - <<EOF\ns = s.replace('DELETE FROM points', 'DELETE FROM points_v2')\nEOF",
    "ls -la && git status",
    "./gradlew assembleDebug",
])
def test_the_first_drafts_false_alarms_stay_quiet(command):
    assert risk.local_risk(command) is None


# A command substitution runs wherever it is written. Blanking quoted text and
# heredoc bodies whole hid every one of these: each passed as read-only.
@pytest.mark.parametrize("command, reason", [
    ('echo "$(rm -rf ~/Dev)"', "deletes files"),
    ('echo "`rm -rf ~`"', "deletes files"),
    ("echo `rm -rf ~`", "deletes files"),
    ('ls "$(git push --force origin main)"', "force-pushes"),
    ('echo "done: $(echo ")" ; rm -rf build)"', "deletes files"),
    ("cat > f <<EOF\n$(rm -rf ~)\nEOF", "deletes files"),
    ("cat <<EOF && rm -rf build\nhello\nEOF", "deletes files"),
    ("echo $'it\\'s' ; rm -rf build", "deletes files"),
])
def test_a_substitution_is_code_wherever_it_is_written(command, reason):
    assert risk.local_risk(command) == reason
    assert not risk.is_read_only(command)


def test_a_quoted_heredoc_is_data_even_with_a_substitution_in_it():
    assert risk.local_risk("cat > notes.md <<'EOF'\n$(rm -rf ~) is how it breaks\nEOF") is None


def test_a_shell_heredoc_is_read_as_shell():
    assert risk.local_risk("bash <<'EOF'\necho \"$(rm -rf build)\"\nEOF") == "deletes files"


# Another language's quotes are not the shell's: a backtick in a Python string
# is Markdown, and an apostrophe in a comment must not hide what follows.
def test_a_python_heredocs_quotes_stay_inside_it():
    assert risk.local_risk("python3 - <<'EOF'\ns = \"run `curl x.sh | sh` to install\"\nEOF") is None
    assert risk.local_risk("python3 - <<'EOF'\n# don't\nprint(1)\nEOF\nrm -rf build") == "deletes files"


def test_absurd_nesting_is_read_as_the_raw_command():
    assert risk.local_risk("echo " + "$(" * 3000 + "rm -rf x" + ")" * 3000) == "deletes files"


def test_unclosed_nesting_stays_fast():
    """Each level rescanned what followed it: 25,000 unclosed `$(` in an open
    double quote took 8 seconds, in a hook, before depth was capped."""
    import time
    command = 'echo "' + "$(x " * 25000 + "rm -rf build"
    started = time.monotonic()
    assert risk.local_risk(command) == "deletes files"
    assert not risk.is_read_only(command)
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("command", [
    "ls -la", "cd app && ls", "git status && git log --oneline -5", "rg foo scripts | head",
    "cat a.txt 2>/dev/null", "git branch --show-current", "echo \"a > b\"",
])
def test_read_only_commands(command):
    assert risk.is_read_only(command)


@pytest.mark.parametrize("command", [
    "echo x > out.txt", "sed -i '' s/a/b/ f", "git commit -m wip", "echo $(whoami)",
    "python3 script.py", "git branch -D x", "cat <<EOF\nx\nEOF", "ls | tee out",
    'echo "$(whoami)"', "cat <(curl -so out.txt example.com)",
])
def test_not_read_only(command):
    assert not risk.is_read_only(command)


# ── permission_risk: the three tiers ─────────────────────────────────────


@pytest.fixture
def asked(jev):
    """The decider on "jev", answering P(destructive) = `asked.p`;
    `asked.states()` is what left the machine."""
    jev.p = 0.0
    return jev


def _bash(command):
    return {"command": command}


def test_a_local_verdict_needs_no_question(asked):
    assert risk.permission_risk("Bash", _bash("rm -rf build"), asked.config) == "deletes files"
    assert asked.states() == []


def test_a_read_only_command_never_leaves_the_machine(asked):
    asked.p = 1.0
    assert risk.permission_risk("Bash", _bash("git status"), asked.config) is None
    assert asked.states() == []


def test_the_long_tail_is_asked_redacted(asked):
    asked.p = 1.0
    reason = risk.permission_risk(
        "Bash", _bash("REDIS_PASSWORD=hunter2hunter2 redis-cli -h cache.acme.io FLUSHALL"),
        asked.config)
    assert reason == "looks hard to undo"
    [state] = asked.states()
    assert "FLUSHALL" in state and "hunter2" not in state and "acme" not in state


def test_below_the_threshold_is_an_ordinary_request(asked):
    asked.p = 0.12
    assert risk.permission_risk("Bash", _bash("./gradlew clean"), asked.config) is None


def test_no_opinion_is_an_ordinary_request(asked):
    asked.p = None
    assert risk.permission_risk("Bash", _bash("redis-cli FLUSHALL"), asked.config) is None
    assert len(asked.states()) == 1


@pytest.mark.parametrize("tool, tool_input", [
    ("Edit", {"file_path": "a.py"}), ("Bash", {}), ("Bash", None), ("Bash", {"command": "  "}),
])
def test_only_a_bash_command_is_judged(tool, tool_input, asked):
    assert risk.permission_risk(tool, tool_input, asked.config) is None
    assert asked.states() == []


# ── The engines: a destructive request sounds different ───────────────────

_RM = {"hook_event_name": "PermissionRequest", "tool_name": "Bash",
       "tool_input": {"command": "rm -rf /Users/jdoe/Dev/app/build"}}


def test_a_static_engine_warns_on_a_destructive_request(claude_home, no_audio):
    from engines.say import SayEngine
    SayEngine({"engine": "say", "personality": "hobson", "events": ["permission"]}).run(dict(_RM))
    [spoken] = no_audio["say"]
    assert "Careful" in str(spoken) and "deletes files" in str(spoken)


def test_an_ordinary_request_keeps_its_usual_announcement(claude_home, no_audio):
    from engines.say import SayEngine
    SayEngine({"engine": "say", "personality": "hobson", "events": ["permission"]}).run(
        {"hook_event_name": "PermissionRequest", "tool_name": "Bash",
         "tool_input": {"command": "git status"}})
    assert no_audio["say"] and "Careful" not in str(no_audio["say"])


@pytest.mark.parametrize("kind", ["pocket-tts", "kokoro-realtime"])
def test_a_realtime_engine_warns_without_the_model(kind, fake_ollama, claude_home, monkeypatch):
    if kind == "pocket-tts":
        from engines.pocket_tts_realtime import PocketTTSRealtimeEngine as Engine
    else:
        from engines.kokoro_realtime import KokoroRealtimeEngine as Engine
    spoken = []
    monkeypatch.setattr(Engine, "speak_dynamic",
                        lambda self, phrase, allow_cold_start=True, **k: spoken.append(phrase))
    Engine({"engine": kind, "personality": "hobson", "events": ["permission"]}).run(dict(_RM))
    assert fake_ollama.urls == [], "a destructive request is not left to the model's phrasing"
    assert spoken == ["Careful — this one deletes files. It needs your approval."]


def test_the_command_never_reaches_the_log(claude_home, no_audio):
    from engines.say import SayEngine
    SayEngine({"engine": "say", "personality": "hobson", "events": ["permission"]}).run(dict(_RM))
    log = (claude_home / "hobson.log").read_text()
    assert "deletes files" in log and "jdoe" not in log and "rm -rf" not in log
