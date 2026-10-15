"""While Hobson is silenced, what waits on you is shown on the face, silently.

The user mutes for meetings. A permission request, a waiting notification or
a Stop the rules read as a question is shown, with no sound, no engine, no
model, and nothing sent off the Mac.
"""

import io
import json
import os
import signal
import subprocess
import sys
import time

import pytest

import announcement
import bark_templates
import face
import nudge
import presence

_REAL_DELIVER = face.deliver
_REAL_SHOW = face.show  # taken before the autouse no_face fixture swaps it out
_REAL_POPEN = subprocess.Popen  # the autouse no_audio fixture replaces it


def _run(claude_home, hobson_entry, monkeypatch, config, hook, ensure_presence=False):
    if not ensure_presence:
        monkeypatch.setattr(hobson_entry, "_ensure_presence", lambda cfg: None)
    (claude_home / "hobson.json").write_text(json.dumps(config))
    monkeypatch.setattr(hobson_entry, "load_engine", lambda cfg: (_ for _ in ()).throw(
        AssertionError("no engine while silenced")))
    # Liveness is not under test, and outside tmux it reads the terminal through ps.
    monkeypatch.setattr(hobson_entry, "_record_alive", lambda hook_input: None)
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO(json.dumps(hook)))
    hobson_entry.main()
    return (claude_home / "hobson.log").read_text()


def _bash(command, session="s1"):
    return {"hook_event_name": "PermissionRequest", "tool_name": "Bash",
            "tool_input": {"command": command}, "session_id": session}


def _permission_lines(tool):
    return {f"{lead} {action.format(tool=tool)}"
            for lead in bark_templates.PERMISSION_LEADS for action in bark_templates.PERMISSION_ACTIONS}


_FACE_ONLY_RECORD = {"pid": 4242, "mode": "auto", "face": True, "face_only": True, "waves": True}


@pytest.fixture
def helper(monkeypatch):
    """The real face.show, against a running face helper whose signal is recorded."""
    kills = []
    monkeypatch.setattr(face, "show", _REAL_SHOW)
    monkeypatch.setattr(face, "deliver", _REAL_DELIVER)
    monkeypatch.setattr(presence, "read_state",
                        lambda: {"ts": time.time(), **_FACE_ONLY_RECORD})
    monkeypatch.setattr(face.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    return kills


def test_a_muted_permission_request_is_shown_silently(claude_home, hobson_entry, monkeypatch, helper):
    from home import derive_project_label
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"))
    rec = json.loads((claude_home / "hobson-face.json").read_text())
    assert rec["phrase"] in _permission_lines("Bash")
    assert (rec["kind"], rec["silent"], rec["audio"]) == ("waiting", True, None)
    assert rec["activity"] == nudge.activity_path(derive_project_label())
    assert "silenced (muted): PermissionRequest, shown\n" in log
    assert helper == [(4242, signal.SIGUSR2)]


def test_quiet_hours_show_it_the_same_way(claude_home, hobson_entry, no_face, monkeypatch):
    hour = time.localtime().tm_hour
    window = [hour, (hour + 1) % 24]
    log = _run(claude_home, hobson_entry, monkeypatch, {"quiet_hours": window}, _bash("make deploy"))
    assert f"silenced (quiet hours {window[0]}-{window[1]}): PermissionRequest, shown" in log
    [(phrase, kind, audio, flag)] = no_face
    assert phrase in _permission_lines("Bash") and (kind, audio, flag) == ("waiting", None, "silent")


def test_a_destructive_command_gets_the_warning_and_asks_no_one(claude_home, hobson_entry, no_face,
                                                                monkeypatch, jev):
    """The local rules only: nothing leaves the Mac while muted, so a command
    they cannot place is an ordinary request, whatever the decider would say."""
    jev.p = 0.99
    config = {"muted": True, **jev.config}
    _run(claude_home, hobson_entry, monkeypatch, config, _bash("rm -rf build"))
    _run(claude_home, hobson_entry, monkeypatch, config, _bash("helm uninstall web", session="s2"))
    assert no_face[0] == ("Careful — this one deletes files. It needs your approval.", "waiting", None, "silent")
    assert no_face[1][0] in _permission_lines("Bash")
    assert jev.states() == []


def test_a_question_dialog_shows_the_question_line(claude_home, hobson_entry, no_face, monkeypatch):
    _run(claude_home, hobson_entry, monkeypatch, {"muted": True},
         {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion"})
    assert no_face[0][0] in announcement.question_lines(bark_templates)


def test_a_muted_question_stop_is_shown_and_still_recorded(claude_home, hobson_entry, no_face, monkeypatch,
                                                           fake_ollama):
    import session_state
    from home import derive_project_label
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True},
               {"hook_event_name": "Stop",
                "last_assistant_message": "I drafted both versions. Which one should I ship, A or B?"})
    assert "silenced (muted): Stop, read as question, shown" in log
    assert no_face[0][0] in announcement.question_lines(bark_templates)
    assert no_face[0][1:] == ("waiting", None, "silent")
    assert session_state.load_session(derive_project_label())["last_stop_category"] == "question"
    assert fake_ollama.urls == []


def test_a_muted_finish_shows_nothing(claude_home, hobson_entry, no_face, monkeypatch):
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True},
               {"hook_event_name": "Stop", "last_assistant_message": "I pushed the branch."})
    assert "silenced (muted): Stop, read as unknown\n" in log
    assert no_face == []


def test_an_idle_prompt_is_shown_only_after_a_question(claude_home, hobson_entry, no_face, monkeypatch):
    import session_state
    from home import derive_project_label
    idle = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}
    for category in (None, "done", "working", "broken"):
        with session_state.transaction(derive_project_label()) as state:
            state["last_stop_category"] = category
        log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, idle)
        assert log.endswith("silenced (muted): Notification (idle_prompt)\n"), category
    assert no_face == []
    with session_state.transaction(derive_project_label()) as state:
        state["last_stop_category"] = "question"
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, idle)
    assert log.endswith("silenced (muted): Notification (idle_prompt), shown\n")
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True},
               {"hook_event_name": "Notification", "notification_type": "agent_needs_input"})
    assert log.endswith("silenced (muted): Notification (agent_needs_input), shown\n")
    assert [line[0] in bark_templates.NOTIFICATION_TEMPLATES for line in no_face] == [True, True]


def test_claude_codes_reminder_does_not_replace_the_request(claude_home, hobson_entry, no_face, monkeypatch):
    """The permission_prompt Notification arrives seconds after the request it
    reminds of: shown, the vaguer line would replace the specific one."""
    _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"))
    reminder = {"hook_event_name": "Notification", "notification_type": "permission_prompt",
                "session_id": "s1"}
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, reminder)
    assert log.endswith("silenced (muted): Notification (permission_prompt)\n")
    assert len(no_face) == 1
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, {**reminder, "session_id": "s2"})
    assert log.endswith("silenced (muted): Notification (permission_prompt), shown\n")


def test_nothing_is_shown_with_the_face_off_or_the_event_off(claude_home, hobson_entry, no_face, monkeypatch):
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True, "face": {"enabled": False}},
               _bash("make deploy"))
    assert log.endswith("silenced (muted): PermissionRequest, not shown (face off)\n")
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True, "events": ["stop"]},
               _bash("make deploy"))
    assert log.endswith("silenced (muted): PermissionRequest\n")
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True},
               {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert log.endswith("silenced (muted): PreToolUse\n")
    assert no_face == []


def test_the_silent_path_imports_no_engine_and_no_model(claude_home, tmp_path):
    """As cheap as the muted Stop: no engine, no phrase generation, no decider.
    A fresh spawn marker keeps the face from launching a helper."""
    claude = claude_home
    (claude / "hobson.json").write_text(json.dumps({"muted": True}))
    (claude / "hobson-presence.spawn").write_text(str(time.time()))
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "scripts", "hobson.py")
    proc = _REAL_POPEN([sys.executable, "-X", "importtime", entry], cwd=str(tmp_path),
                       env={**os.environ, "HOME": str(tmp_path)},
                       stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    _, err = proc.communicate(json.dumps(_bash("make deploy")).encode())
    imported = {line.rsplit("|", 1)[-1].strip() for line in err.decode().splitlines()
                if line.startswith("import time:")}
    assert proc.returncode == 0
    assert {"announcement", "face"} <= imported
    assert not {"engines", "engines.base", "phrase_gen", "decider", "ollama_client"} & imported
    assert json.loads((claude / "hobson-face.json").read_text())["silent"] is True


@pytest.mark.parametrize("events", [None, 3, True])
def test_a_malformed_events_list_does_not_break_the_muted_hook(claude_home, hobson_entry, no_face, monkeypatch,
                                                               events):
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True, "events": events}, _bash("make deploy"))
    assert log.endswith("silenced (muted): PermissionRequest\n")
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True, "events": events},
               {"hook_event_name": "Stop", "last_assistant_message": "Which one should I ship, A or B?"})
    assert log.endswith("silenced (muted): Stop\n")
    assert no_face == []


def test_if_the_risk_rules_fail_the_ordinary_line_is_shown(claude_home, hobson_entry, no_face, monkeypatch):
    import risk
    monkeypatch.setattr(risk, "local_risk", lambda command: 1 / 0)
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("rm -rf build"))
    assert "silenced (muted): PermissionRequest, shown\n" in log
    assert no_face[0][0] in _permission_lines("Bash")


def _unmuted(claude_home, hobson_entry, monkeypatch, hook):
    (claude_home / "hobson.json").write_text(json.dumps(
        {"engine": "say", "events": ["permission", "notification"], "nudge": {"enabled": False}}))
    monkeypatch.setattr(hobson_entry, "_ensure_presence", lambda config: None)
    monkeypatch.setattr(hobson_entry, "load_engine", lambda cfg: hobson_entry.engine_class("say")(cfg))
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO(json.dumps(hook)))
    hobson_entry.main()
    return (claude_home / "hobson.log").read_text()


_REMINDER = {"hook_event_name": "Notification", "notification_type": "permission_prompt", "session_id": "s1"}


@pytest.mark.parametrize("signalled", [True, False])
def test_a_reminder_that_arrives_unmuted_is_spoken(claude_home, hobson_entry, no_face, no_audio, monkeypatch,
                                                   signalled):
    if not signalled:
        monkeypatch.setattr(face, "deliver", lambda *a, **k: None)
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"))
    assert "silenced (muted): PermissionRequest, " in log
    log = _unmuted(claude_home, hobson_entry, monkeypatch, _REMINDER)
    assert "[Notification] permission template -> " in log
    assert "just said" not in log
    assert any(p in " ".join(map(str, no_audio["say"])) for p in bark_templates.NOTIFICATION_TEMPLATES)


def test_a_request_not_shown_leaves_the_muted_reminder_to_be_shown(claude_home, hobson_entry, no_face,
                                                                   monkeypatch):
    real = face.deliver
    monkeypatch.setattr(face, "deliver", lambda *a, **k: None)
    _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"))
    monkeypatch.setattr(face, "deliver", real)
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _REMINDER)
    assert log.endswith("silenced (muted): Notification (permission_prompt), shown\n")
    assert no_face[0][0] in bark_templates.NOTIFICATION_TEMPLATES


@pytest.mark.parametrize("spawned, reminder_ending", [(True, ""), (False, ", not signalled (see [face])")])
def test_a_request_that_starts_the_helper_is_shown_so_its_reminder_is_not_a_second_line(
        claude_home, hobson_entry, no_face, monkeypatch, spawned, reminder_ending):
    """The helper reads the record at launch and shows the line, so the
    reminder must not replace it. A spawn that did not happen shows nothing."""
    monkeypatch.setattr(face, "show", _REAL_SHOW)
    monkeypatch.setattr(face, "deliver", _REAL_DELIVER)
    monkeypatch.setattr(presence, "read_state", lambda: {})
    monkeypatch.setattr(presence, "ensure_running", lambda config, now=None: spawned)
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"))
    assert ("silenced (muted): PermissionRequest, shown\n" in log) is spawned
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _REMINDER)
    assert log.endswith("silenced (muted): Notification (permission_prompt)" + reminder_ending + "\n")


def test_company_is_not_known_while_silenced_so_the_line_is_shown_whole(claude_home, hobson_entry,
                                                                        monkeypatch, helper):
    """The helper is face only then and senses nothing; whatever a stale
    record says, the line is the permission line, not the company line."""
    monkeypatch.setattr(presence, "read_state", lambda: {"ts": time.time(), "state": "company",
                                                         **_FACE_ONLY_RECORD})
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"))
    rec = json.loads((claude_home / "hobson-face.json").read_text())
    assert rec["phrase"] in _permission_lines("Bash") and rec["project"]
    assert "silenced (muted): PermissionRequest, shown\n" in log


def test_on_a_call_the_silent_line_is_shown_as_it_is(claude_home, hobson_entry, monkeypatch, helper):
    monkeypatch.setattr(presence, "read_state", lambda: {"ts": time.time(), "state": "call",
                                                         **_FACE_ONLY_RECORD})
    _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"))
    rec = json.loads((claude_home / "hobson-face.json").read_text())
    assert rec["phrase"] in _permission_lines("Bash")


@pytest.mark.parametrize("started", [True, False])
def test_a_line_is_not_signalled_to_a_helper_that_is_being_turned_face_only(
        claude_home, hobson_entry, monkeypatch, helper, started):
    """The old helper is stopping: signalling it would show the line on a
    camera helper that is about to go. The line is written for the new one to
    read at launch, and a wait is recorded as shown only if one will show it."""
    monkeypatch.setattr(presence, "read_state", lambda: {"ts": time.time(), "pid": 4242, "mode": "auto",
                                                         "face": True, "face_only": False, "waves": True})
    ensured = []
    monkeypatch.setattr(presence, "ensure_running", lambda config, now=None: ensured.append(1) or started)
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"))
    assert helper == [] and ensured
    assert ("silenced (muted): PermissionRequest, shown\n" in log) is started
    import session_state
    from home import derive_project_label
    shown = session_state.load_session(derive_project_label()).get("last_shown_wait_session")
    assert (shown == "s1") is started


@pytest.mark.parametrize("raises", [False, True])
def test_a_silenced_hook_reconciles_the_helper_and_never_raises(
        claude_home, hobson_entry, monkeypatch, raises):
    calls = []

    def ensure(config, now=None):
        calls.append(config.get("muted"))
        if raises:
            raise RuntimeError("no helper")

    monkeypatch.setattr(presence, "ensure_running", ensure)
    log = _run(claude_home, hobson_entry, monkeypatch, {"muted": True},
               {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1"},
               ensure_presence=True)
    assert calls == [True]
    assert ("could not start the sensor (no helper)" in log) is raises


@pytest.mark.parametrize("command", ["off", "off 30m", "on"])
def test_muting_and_unmuting_reconcile_the_helper_at_once(claude_home, tmp_path, command):
    """The CLI runs presence.py --start after changing the config, so the
    camera goes off with the mute, not at the next hook."""
    stub = tmp_path / "bin"
    stub.mkdir()
    seen = tmp_path / "seen"
    (stub / "python3").write_text(
        '#!/bin/bash\ncase "$*" in *--start*) echo "$*" >> "%s";; *) exec /usr/bin/python3 "$@";; esac\n' % seen)
    (stub / "python3").chmod(0o755)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{stub}:{os.environ['PATH']}"}
    cli = _REAL_POPEN([os.path.join(repo, "hobson"), *command.split()], env=env,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert cli.wait() == 0
    deadline = time.time() + 5
    while not seen.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert seen.read_text().strip().endswith("presence.py --start")


def test_a_silenced_hook_shows_its_wait_before_it_reconciles_the_helper(
        claude_home, hobson_entry, monkeypatch, helper):
    """Reconciling first turns a camera helper over: the old one unlinks its
    state, no live helper is left to signal, and the wait is not shown."""
    order = []
    monkeypatch.setattr(face, "deliver", lambda *a, **k: order.append("show") or _REAL_DELIVER(*a, **k))
    monkeypatch.setattr(presence, "ensure_running", lambda config, now=None: order.append("reconcile"))
    _run(claude_home, hobson_entry, monkeypatch, {"muted": True}, _bash("make deploy"),
         ensure_presence=True)
    assert order == ["show", "reconcile"]
