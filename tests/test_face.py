"""The face's Python side: what goes on it, the record, and the signal.

The window itself (presence/Face.swift, presence/face/) is checked by hand,
like the rest of the helper.
"""

import json
import os
import signal
import stat
import time

import pytest

import face
import presence

# Captured before the autouse no_face fixture swaps it out for every test.
_REAL_SHOW = face.show


@pytest.fixture
def helper(monkeypatch):
    """A presence record, os.kill and ensure_running, recorded."""
    box = {"record": {}, "kills": [], "started": [], "kill_error": None}

    def kill(pid, sig):
        if box["kill_error"]:
            raise box["kill_error"]
        box["kills"].append((pid, sig))

    monkeypatch.setattr(presence, "read_state", lambda: box["record"])
    monkeypatch.setattr(presence, "ensure_running", lambda config, now=None: box["started"].append(1))
    monkeypatch.setattr(face.os, "kill", kill)
    return box


def _record(claude_home):
    return json.loads((claude_home / "hobson-face.json").read_text())


def test_a_line_is_written_for_the_helper_and_it_is_signalled(claude_home, helper):
    now = time.time()
    helper["record"] = {"ts": now, "pid": 4242, "face": True}
    assert _REAL_SHOW({}, "The migration is finished.", "done", "/tmp/x.wav", project="webapp", now=now)
    rec = _record(claude_home)
    assert {k: rec[k] for k in ("ts", "phrase", "kind", "audio", "project", "width")} == {
        "ts": now, "phrase": "The migration is finished.", "kind": "done",
        "audio": "/tmp/x.wav", "project": "webapp", "width": 280}
    assert len(rec["id"]) == 32 and "activity" not in rec
    assert helper["kills"] == [(4242, signal.SIGUSR2)] and helper["started"] == []


def test_the_record_is_private_and_replaced_whole(claude_home, helper):
    """It holds what was said. No temp file is left beside it."""
    _REAL_SHOW({}, "One.", "done", project="p")
    first = _record(claude_home)["id"]
    _REAL_SHOW({}, "Two.", "done", project="p")
    assert stat.S_IMODE(os.stat(claude_home / "hobson-face.json").st_mode) == 0o600
    assert _record(claude_home)["phrase"] == "Two." and _record(claude_home)["id"] != first
    assert [p.name for p in claude_home.iterdir() if p.name.startswith("hobson-face")] == ["hobson-face.json"]


def test_a_helper_built_before_the_face_is_never_signalled(claude_home, helper):
    """SIGUSR2's default action is to terminate: it would kill the sensor.
    ensure_running restarts it instead."""
    now = time.time()
    helper["record"] = {"ts": now, "pid": 4242}
    assert not _REAL_SHOW({}, "Done.", "done", project="p", now=now)
    assert helper["kills"] == [] and helper["started"] == [1]


def test_a_stale_record_starts_the_helper_instead(claude_home, helper):
    now = time.time()
    helper["record"] = {"ts": now - 60, "pid": 4242, "face": True}
    _REAL_SHOW({}, "Done.", "done", project="p", now=now)
    assert helper["kills"] == [] and helper["started"] == [1]


def test_a_failed_signal_is_logged_once_and_never_raised(claude_home, helper):
    helper["record"] = {"ts": time.time(), "pid": 4242, "face": True}
    helper["kill_error"] = ProcessLookupError("gone")
    assert _REAL_SHOW({}, "Done.", "done", project="p") is False
    assert _REAL_SHOW({}, "Again.", "done", project="p") is False
    log = (claude_home / "hobson.log").read_text()
    assert log.count("[face] could not show a line") == 1


@pytest.mark.parametrize("config, phrase, kind", [
    ({"face": {"enabled": False}}, "Done.", "done"),
    ({}, "I'm reading the config.", "commentary"),
    ({}, "", "done"),
])
def test_what_stays_off_the_face(config, phrase, kind, claude_home, helper):
    assert _REAL_SHOW(config, phrase, kind, project="p") is False
    assert not (claude_home / "hobson-face.json").exists()
    assert helper["kills"] == [] and helper["started"] == []


def test_commentary_goes_on_the_face_when_asked_for(claude_home, helper):
    _REAL_SHOW({"face": {"commentary": True}}, "I'm reading the config.", "commentary", project="p")
    assert _record(claude_home)["kind"] == "commentary"


def test_the_company_line_names_no_project(claude_home, helper):
    """The voice left the project out; so does the window."""
    _REAL_SHOW({}, presence.COMPANY_LINE, "waiting", project="webapp")
    rec = _record(claude_home)
    assert rec["project"] is None and "activity" not in rec


def test_a_wait_carries_its_sessions_activity_token(claude_home, helper):
    """The helper keeps the window up until you type in that session."""
    import nudge
    for kind in ("waiting", "nudge"):
        _REAL_SHOW({}, "The migration needs you.", kind, project="webapp")
        assert _record(claude_home)["activity"] == nudge.activity_path("webapp")
    _REAL_SHOW({"face": {"hold_waiting": False}}, "The migration needs you.", "waiting", project="webapp")
    assert "activity" not in _record(claude_home)
    _REAL_SHOW({}, "Done.", "done", project="webapp")
    assert "activity" not in _record(claude_home)


# ── hobson face, hobson doctor ──────────────────────────────────────────────

def test_status_says_off_or_how_it_behaves(claude_home, monkeypatch):
    monkeypatch.setattr(presence, "read_state", lambda: {})
    assert face.status_lines({"face": {"enabled": False}}) == ["Face:        off (hobson face on)"]
    lines = face.status_lines({})
    assert lines[0] == "Face:        on" and "voice only" in lines[2] and "until you type" in lines[3]
    monkeypatch.setattr(presence, "read_state",
                        lambda: {"ts": time.time(), "face": True, "face_only": True})
    assert face.status_lines({})[0] == "Face:        on (face only: presence is off)"


def test_doctor_names_what_is_missing(claude_home, monkeypatch):
    assert face.doctor_lines({"face": {"enabled": False}})[0][0] == "info"
    monkeypatch.setattr(face, "page_ready", lambda: True)
    monkeypatch.setattr(presence, "helper_built", lambda: False)
    assert "not built" in face.doctor_lines({})[0][1]
    monkeypatch.setattr(presence, "helper_built", lambda: True)
    monkeypatch.setattr(face, "helper_current", lambda: False)
    assert face.doctor_lines({})[0] == ("warn", "Presence helper is older than its source (no face in it): scripts/build-presence.sh")
    monkeypatch.setattr(face, "helper_current", lambda: True)
    assert face.doctor_lines({})[0][0] == "ok"


def test_the_page_the_helper_loads_is_in_the_checkout():
    assert face.page_ready()


# ── Breadcrumbs for hobson monitor ──────────────────────────────────────────

def _log(claude_home):
    path = claude_home / "hobson.log"
    return path.read_text() if path.exists() else ""


def test_a_signalled_line_says_which_helper_and_what(claude_home, helper):
    helper["record"] = {"ts": time.time(), "pid": 4242, "face": True}
    _REAL_SHOW({}, "The migration is finished.", "done", "/tmp/x.wav", project="webapp")
    assert "[face] signalled pid 4242: 'The migration is finished.' (done, audio) for webapp" in _log(claude_home)


@pytest.mark.parametrize("record, started, why", [
    ({"pid": 1}, False, "helper built before the face or started without it, restarting it"),
    (None, True, "no helper running, starting one"),
    (None, False, "no helper running yet (starting)"),
])
def test_a_line_not_signalled_says_why(record, started, why, claude_home, monkeypatch, helper):
    helper["record"] = {**record, "ts": time.time()} if record else {}
    monkeypatch.setattr(presence, "ensure_running", lambda config, now=None: started)
    _REAL_SHOW({}, "Done.", "done", project="p")
    assert f"[face] not signalled, {why}: 'Done.' (done, no audio) for p" in _log(claude_home)


@pytest.mark.parametrize("event, detail, line", [
    ("shown", "4.6s, following audio 3.1s, gone: said",
     "[face] shown 'The build failed.' for webapp, 4.6s, following audio 3.1s, gone: said"),
    ("skipped", "nearly over (2.9s of 3.1s) after a cold start",
     "[face] skipped 'The build failed.' for webapp: nearly over (2.9s of 3.1s) after a cold start"),
    ("audio", "/tmp/gone.aiff",
     "[face] audio unreadable for 'The build failed.' for webapp (/tmp/gone.aiff), following the text"),
])
def test_the_helpers_events_are_logged(event, detail, line, claude_home):
    """What the helper reports, through presence.py --face-event, as it runs it."""
    presence.main(["--face-event", event, "The build failed.", "webapp", detail])
    assert line in _log(claude_home)


def test_page_trouble_is_logged_and_unknown_events_are_not(claude_home):
    presence.main(["--face-event", "page", "", "", "crashed (its WebContent process ended), reloading"])
    presence.main(["--face-event", "anything", "x", "y", "z"])
    log = _log(claude_home)
    assert "[face] page crashed (its WebContent process ended), reloading" in log
    assert "anything" not in log


def test_no_face_line_is_counted_as_something_spoken(claude_home, helper):
    """log_stats, log_analyse and recap read a trailing -> 'phrase' as speech."""
    import log_record
    import log_stats
    helper["record"] = {"ts": time.time(), "pid": 4242, "face": True}
    _REAL_SHOW({}, "The migration is finished.", "done", project="webapp")
    for event in ("shown", "skipped", "audio"):
        presence.main(["--face-event", event, "The migration is finished.", "webapp", "detail"])
    lines = _log(claude_home).splitlines()
    assert len(lines) == 4
    assert all(log_record.tail_phrase(r.body) is None for r in log_record.records(lines))
    assert log_stats.summarize(lines)["barked"] == 0
