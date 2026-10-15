#!/usr/bin/env python3
"""face.py — Hobson's face: the line he is speaking, shown in a small window.

Presence lets Hobson see you; this is the other direction. Every phrase that
is played (BaseEngine.play, after presence has let it through) is handed to
show(), which writes it to record_path() and signals the presence helper
(SIGUSR2). The helper (presence/Face.swift) reads the record and the audio it
names, and draws him saying it (presence/face/, a page in a web view): the
mouth follows the audio's loudness, and he looks towards where the camera
last saw you, in process -- nothing about your face is written anywhere.

The face is a viewer. afplay has already started when show() is called, and
show() never raises: if anything here fails, the voice is exactly as before.

A helper built before the face would be killed by SIGUSR2 (its default
action), so the signal goes only to a helper whose record says "face": true;
anything else is left to presence.ensure_running, which restarts it.
"""

import argparse
import os
import signal
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home  # noqa: E402
import log_record  # noqa: E402

import kinds  # noqa: E402

# A kind that waits on you (kinds.Kind.waits_on_you) stays, with
# face.hold_waiting, until you type in that session (the activity token is
# newer than the record). One that names no project (an answer is about
# every session, "One moment." about none) leaves the corner blank.

_failed = False  # one log line per process, however many failures


def record_path():
    return home.path("hobson-face.json")


def settings(config):
    """The face section, over its defaults."""
    return {**home.DEFAULT_CONFIG["face"], **((config or {}).get("face") or {})}


def wanted(config, phrase, kind):
    """Whether this phrase goes on the face. Pure."""
    cfg = settings(config)
    if not cfg.get("enabled") or not phrase:
        return False
    return not kinds.of(kind).passing or bool(cfg.get("commentary"))


def compose(config, phrase, kind, audio, project, now, asked=False, silent=False):
    """The record the helper reads. Pure. In company the phrase is the
    company line, and the project stays off the window as it stays out of
    the voice. `asked`: you asked for it (Ask Hobson), so the helper shows it
    even with the face off. `silent`: shown and not said (Hobson is muted or
    in quiet hours), so there is no "once said" for it to go after: it stays
    until you type in that session, whatever its kind, the company line
    included."""
    import presence
    cfg = settings(config)
    company = phrase == presence.COMPANY_LINE
    unnamed = company or not kinds.of(kind).names_project
    record = {
        "id": uuid.uuid4().hex, "ts": now, "phrase": phrase, "kind": kind,
        "audio": audio or None, "project": None if unnamed else (project or None),
        "width": cfg.get("width"),
    }
    if asked:
        record["asked"] = True
    if silent:
        record["silent"] = True
    if isinstance(phrase, log_record.Unlogged):
        record["logged"] = phrase.logged  # what the helper reports back for the log
    held = silent or (kinds.of(kind).waits_on_you and cfg.get("hold_waiting"))
    if held and (silent or not company):
        import nudge
        record["activity"] = nudge.activity_path(project)
    return record


def _write(record):
    """Atomically, at mode 600: the record holds what was said."""
    home.write_json_atomic(record_path(), record, mode=0o600, indent=None)


def _for(project):
    return f" for {project}" if project else ""


def _signal(config, line, now):
    """Tell a running face-capable helper there is a line; otherwise have
    presence start (or restart) one, which reads the record on launch.
    SIGNALLED when a helper was, STARTED when one was launched (it reads the
    record on launch, so the line will show), else None. Says which in the
    log: the one place to see why a line did or did not reach the window."""
    import presence
    record = presence.read_state()
    sound = "silent" if line.get("silent") else "audio" if line["audio"] else "no audio"
    what = f"{line['phrase']!r} ({line['kind']}, {sound}){_for(line['project'])}"
    alive = presence.helper_alive(record, now)
    # Silenced, a helper with the camera is not signalled: ensure_running
    # turns it into a face only one, which shows the line when it starts.
    turning = alive and home.silence_reason(config) and not record.get("face_only")
    if alive and record.get("face") and not turning:
        pid = int(record.get("pid") or 0)
        if pid > 0:
            os.kill(pid, signal.SIGUSR2)
            log_record.write(f"[face] signalled pid {pid}: {what}")
            return SIGNALLED
    started = presence.ensure_running(config)
    if turning:
        why = "silenced, turning the helper with the camera into a face only one"
    elif alive:
        why = "helper built before the face or started without it, restarting it"
    else:
        why = "no helper running, starting one" if started else "no helper running yet (starting)"
    log_record.write(f"[face] not signalled, {why}: {what}")
    return STARTED if started else None


SIGNALLED = "signalled"
STARTED = "started"


def show(config, phrase, kind, audio=None, project=None, now=None, asked=False, silent=False):
    """Put this phrase on the face. Never raises; False when it was not
    shown (off, commentary, or a failure, logged once per process). An
    `asked` line is shown with the face off too; a `silent` one is not.
    True means a running helper was signalled; a line that started the
    helper shows too, but is False here (see deliver)."""
    return _deliver(config, phrase, kind, audio, project, now, asked, silent) == SIGNALLED


def deliver(*args, **kwargs):
    """show(), saying more: SIGNALLED, STARTED (the record is written and
    the helper was launched, which shows it at launch) or None (nothing will
    show it: off, commentary, a failed write, a spawn throttled with no
    helper running)."""
    return _deliver(*args, **kwargs)


def _deliver(config, phrase, kind, audio=None, project=None, now=None, asked=False, silent=False):
    global _failed
    try:
        if not phrase or not (asked or wanted(config, phrase, kind)):
            return None
        now = time.time() if now is None else now
        project = home.derive_project_label() if project is None else project
        line = compose(config, phrase, kind, audio, project, now, asked, silent)
        _write(line)
        return _signal(config, line, now)
    except Exception as e:
        if not _failed:
            _failed = True
            log_record.write(f"[face] could not show a line ({e})")
        return None


# What the helper reports about a line, by event (presence.py --face-event).
# The phrase never follows "->": log_stats, log_analyse and recap read a
# trailing "-> 'phrase'" as something spoken.
EVENTS = {
    "shown": "shown {phrase}{project}, {detail}",
    "skipped": "skipped {phrase}{project}: {detail}",
    "audio": "audio unreadable for {phrase}{project} ({detail}), following the text",
    "page": "page {detail}",
}


def log_event(event, phrase="", project="", detail=""):
    """One line from the helper about the window, written as every other
    line is (log_record). Unknown events are ignored: the helper's argv is
    not a way to write arbitrary text to the log."""
    template = EVENTS.get(event)
    if template is None:
        return False
    detail = " ".join(str(detail).split())[:200]
    log_record.write("[face] " + template.format(
        phrase=repr(phrase), project=_for(project), detail=detail))
    return True


def page_ready():
    """The page the helper loads is all there."""
    import presence
    return all(os.path.isfile(os.path.join(presence.face_page(), name))
               for name in ("index.html", "face.js", "face.css"))


def helper_current():
    """The helper is built from this checkout's source, the face included."""
    import subprocess
    script = os.path.join(home.ROOT, "scripts", "build-presence.sh")
    try:
        return subprocess.run([script, "--check"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def status_lines(config=None):
    """`hobson face`, as lines of text."""
    import presence
    config = home.load_config() if config is None else config
    cfg = settings(config)
    if not cfg.get("enabled"):
        return ["Face:        off (hobson face on)"]
    record = presence.read_state()
    running = presence.helper_alive(record, time.time()) and record.get("face")
    lines = ["Face:        on" + (" (face only: presence is off)" if record.get("face_only") and running else "")]
    lines.append("Window:      " + ("ready" if running else "starts with the next line Hobson speaks"))
    lines.append("Commentary:  " + ("shown" if cfg.get("commentary") else "voice only"))
    lines.append("Waits:       " + ("stay until you type" if cfg.get("hold_waiting") else "go once said"))
    return lines


def doctor_lines(config=None):
    """[(level, text)] for `hobson doctor`: level is ok, warn or info."""
    import presence
    config = home.load_config() if config is None else config
    if not settings(config).get("enabled"):
        return [("info", "Face off (hobson face on to enable)")]
    if not page_ready():
        return [("warn", f"Face page missing from {presence.face_page()}: the window shows nothing")]
    if not presence.helper_built():
        return [("warn", "Face on but the helper is not built: hobson presence setup (needs Xcode Command Line Tools)")]
    if not helper_current():
        return [("warn", "Presence helper is older than its source (no face in it): scripts/build-presence.sh")]
    return [("ok", "Face on: the helper and its page are in place")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--test", metavar="LINE", help="show this line, with no engine and no audio")
    parser.add_argument("--kind", default="done")
    parser.add_argument("--audio", default=None, help="a clip to move the mouth to (not played)")
    parser.add_argument("--project", default=None)
    parser.add_argument("--status-text", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args(argv)
    if args.status_text:
        print("\n".join(status_lines()))
    elif args.doctor:
        for level, text in doctor_lines():
            print(f"{level}\t{text}")
    elif args.test:
        config = home.load_config()
        if not wanted(config, args.test, args.kind):
            print("not shown: the face is off, or this kind is voice only")
        elif show(config, args.test, args.kind, args.audio, project=args.project):
            print("signalled")
        else:
            print("written; the helper shows it when it starts (see hobson.log)")


if __name__ == "__main__":
    main()
