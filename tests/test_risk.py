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


@pytest.mark.parametrize("command", [
    "ls -la", "cd app && ls", "git status && git log --oneline -5", "rg foo scripts | head",
    "cat a.txt 2>/dev/null", "git branch --show-current", "echo \"a > b\"",
])
def test_read_only_commands(command):
    assert risk.is_read_only(command)


@pytest.mark.parametrize("command", [
    "echo x > out.txt", "sed -i '' s/a/b/ f", "git commit -m wip", "echo $(whoami)",
    "python3 script.py", "git branch -D x", "cat <<EOF\nx\nEOF", "ls | tee out",
])
def test_not_read_only(command):
    assert not risk.is_read_only(command)


# ── Redaction ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("command, secret", [
    ("curl -H 'Authorization: Bearer abc123secretvalue' https://api.example.com/v1", "abc123secretvalue"),
    ("API_KEY=sk-live-0123456789abcdefghij npm run deploy", "0123456789abcdefghij"),
    ("GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123 gh pr create", "ghp_abcdef"),
    ("deploy --token=s3cr3t-value-here --env prod", "s3cr3t-value-here"),
    ("mysql --password hunter2hunter2 -u root", "hunter2hunter2"),
    ("aws s3 ls --profile x AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ("curl -H 'x: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_part' localhost", "eyJhbGci"),
    ("git clone https://user:pass@github.com/org/repo.git", "user:pass"),
    ("ssh deploy@10.0.0.12 uptime", "10.0.0.12"),
    ("scp build.zip jane.doe@corp.example.com:/srv", "jane.doe"),
    ("ping db.internal.acme.io", "acme"),
    ("cat /Users/jdoe/Projects/secret-client/notes.txt", "jdoe"),
    ("cat ~/.ssh/id_rsa", ".ssh"),
])
def test_redaction_removes_what_identifies_or_authenticates(command, secret):
    assert secret not in risk.redact(command)


def test_redaction_keeps_what_the_command_does():
    out = risk.redact("rm -rf /Users/jdoe/Dev/app/build && git push --force origin main")
    assert "rm -rf" in out and "build" in out and "git push --force" in out


def test_redaction_keeps_the_dangerous_roots():
    assert risk.redact("rm -rf /") == "rm -rf /"
    assert risk.redact("rm -rf ~") == "rm -rf ~"


def test_heredoc_bodies_and_long_quoted_text_never_leave():
    out = risk.redact("python3 - <<EOF\nprint('customer 42 owes 900')\nEOF\n"
                      "git commit -m \"Fix the refund bug reported by the Acme account team last week\"")
    assert "customer" not in out and "Acme" not in out


def test_redaction_is_bounded():
    assert len(risk.redact("echo " + "a b " * 1000)) <= 602


# ── permission_risk: the three tiers ─────────────────────────────────────


@pytest.fixture
def asked(monkeypatch):
    """Record every decider question; answer with `asked.p`."""
    import decider

    class Box:
        p = 0.0
        states = []

    def fake_choice(state, instructions, criteria, config=None):
        Box.states.append(state)
        return decider.ChoiceResult(choice="x", probs={"destructive": Box.p}, confidence=1.0)

    Box.states = []
    monkeypatch.setattr(decider, "choice", fake_choice)
    return Box


def _bash(command):
    return {"command": command}


def test_a_local_verdict_needs_no_question(asked):
    assert risk.permission_risk("Bash", _bash("rm -rf build"), {}) == "deletes files"
    assert asked.states == []


def test_a_read_only_command_never_leaves_the_machine(asked):
    asked.p = 1.0
    assert risk.permission_risk("Bash", _bash("git status"), {}) is None
    assert asked.states == []


def test_the_long_tail_is_asked_redacted(asked):
    asked.p = 1.0
    reason = risk.permission_risk(
        "Bash", _bash("REDIS_PASSWORD=hunter2hunter2 redis-cli -h cache.acme.io FLUSHALL"), {})
    assert reason == "looks hard to undo"
    [state] = asked.states
    assert "FLUSHALL" in state and "hunter2" not in state and "acme" not in state


def test_below_the_threshold_is_an_ordinary_request(asked):
    asked.p = 0.12
    assert risk.permission_risk("Bash", _bash("./gradlew clean"), {}) is None


def test_no_opinion_is_an_ordinary_request(monkeypatch):
    import decider
    monkeypatch.setattr(decider, "choice", lambda *a, **k: None)
    assert risk.permission_risk("Bash", _bash("redis-cli FLUSHALL"), {}) is None


@pytest.mark.parametrize("tool, tool_input", [
    ("Edit", {"file_path": "a.py"}), ("Bash", {}), ("Bash", None), ("Bash", {"command": "  "}),
])
def test_only_a_bash_command_is_judged(tool, tool_input, asked):
    assert risk.permission_risk(tool, tool_input, {}) is None
    assert asked.states == []


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
