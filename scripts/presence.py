#!/usr/bin/env python3
"""presence.py — whether anyone is listening, and what Hobson does about it.

Hobson used to speak into the void: every finish, nudge and watchdog line went
out whether or not anyone was at the desk, and 6 of 29 nudge sequences in the
log ran to their cap unanswered. The sensor is the Swift helper in presence/
(build/HobsonPresence.app, started here by ensure_running). It writes
state_path() about once a second and runs this script with --transition on
every change of state. Everything that decides what to say is here.

States: present, away, call (a call app has the microphone), company (two or
more people in frame), and unknown -- no helper, a stale file, presence off.
Unknown behaves exactly as Hobson did before presence existed: that is the
fallback for every failure in this module.

What each state does to a phrase (route, at the engines' speak seams):

    present, unknown   spoken, as always
    away, call         held on the salver (held_path()); commentary is dropped,
                       since a mid-work remark is stale by the time you are back
    company            a wait goes out as one detail-free sentence (COMPANY_LINE)
                       -- the project and what it wants stay private -- and the
                       rest is held

and at the transitions the helper reports:

    back (away -> present)                one briefing, most urgent first;
                                          nothing held, a greeting
                                          ("Welcome back. All quiet.")
    back from a call or company           the briefing, or nothing
    leaving (a lock; the camera in        "Before you go -- X is waiting on
    continuous mode)                      you", else a farewell

Greetings and farewells (presence.greetings) have guards, because a voice
that remarks on every movement gets switched off: no greeting after less
than MIN_AWAY_FOR_GREETING, at most one farewell per FAREWELL_EVERY, never
the same line twice running.

A nudge pauses while you are away without spending a step (nudge._run), and a
watchdog line is held like anything else.

The phone switch (presence.phone): an Android phone face down turns the camera
off, face up turns it on, and Hobson says so. The helper reads it over adb and
counts a phone it cannot read as face down: a privacy switch fails closed.

Hard lines: presence never approves anything, never lets the agent proceed on
its own and never unmutes: silence_reason() still wins over everything here.
Frames never leave the helper. It writes a state, its source and a count of
people; this module keeps held phrases for at most HELD_MAX_AGE.
"""

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home  # noqa: E402
import log_record  # noqa: E402

STATES = ("present", "away", "call", "company")

# The helper writes every second; a file older than this is a helper that
# died without removing it.
STALE_SECONDS = 5.0

# In auto mode a look is taken only when Hobson is about to speak and you
# have been idle; one this recent is reused rather than taken again.
LOOK_FRESH_SECONDS = 20.0
# A look is about two seconds of camera plus its start-up. The hooks are
# async, so waiting blocks nobody.
LOOK_WAIT_SECONDS = 4.0

MAX_HELD = 20
HELD_MAX_AGE = 12 * 3600

# Most urgent first: what the briefing leads with.
URGENCY = {"waiting": 0, "broken": 1, "stalled": 2, "done": 3, "info": 4}

# Never held: the briefing itself, and a nudge, which checks for itself and
# pauses rather than piling up (nudge._run).
NEVER_HELD = ("briefing", "nudge")

COMPANY_LINE = "Something needs your attention when you have a moment."

GREETINGS = {
    "away": "Welcome back.",
    "call": "Now that your call's over:",
    "company": "Now that it's just you:",
    "wave": "Hello.",
}

# A return after less than this says nothing about sessions that are merely
# still waiting -- you stood up and sat down, and you already know -- though
# anything held while you were gone is always told.
MIN_AWAY_FOR_WAITS = 60.0

# How old a permission request or a question can be and still count as
# "waiting on you" for the lines at the door. An abandoned session from this
# morning is not what "before you go" means.
PERMISSION_WAIT_SECONDS = 600
QUESTION_WAIT_SECONDS = 2 * 3600

# Spawns of the helper at most this often: every hook may ask, and one is
# plenty.
SPAWN_THROTTLE_SECONDS = 15

NEED = {"approval": "your approval", "answer": "an answer from you", "you": "you"}

# Said when the phone is turned over (the helper runs --switch).
SWITCH_LINES = {"down": "Camera off.", "up": "Camera on."}

# Back from under this long -- a lean out of frame, a stretch -- is not an
# arrival, and gets no greeting (anything held is still told). The absence
# runs from when the camera last saw you (presence/main.swift, settle).
MIN_AWAY_FOR_GREETING = 20.0
# At most one farewell this often: a camera that loses you twice in a
# minute is not two departures.
FAREWELL_EVERY = 300.0
# From this long away, the greeting says how long it was quiet.
LONG_AWAY = 300.0

GREETINGS_NOTHING = [
    "Welcome back. All quiet.",
    "Welcome back. Nothing needed you.",
    "Ah, there you are. Nothing to report.",
    "Welcome back. Everything's in order.",
]
# A wave is "hey Hobson": he answers with what there is -- what was held,
# who is waiting on you -- or, with nothing, one of these.
WAVE_HELLOS = [
    "Hello to you too.",
    "At your service.",
    "Hello. All quiet.",
    "I see you. Nothing needs you.",
]
# One answer per wave this often, however much waving.
WAVE_EVERY = 8.0

FAREWELLS = [
    "I'll hold anything that comes in.",
    "Off you go. I'll mind things here.",
    "I'll keep an eye on things.",
    "Take your time. I'll hold the fort.",
]


# ── Paths and settings ─────────────────────────────────────────────────────

def state_path():
    return home.path("hobson-presence.json")


def held_path():
    return home.path("hobson-held.json")


def _held_lock_path():
    return home.path("hobson-held.lock")


def _spawn_marker():
    return home.path("hobson-presence.spawn")


def app_path():
    return os.path.join(home.ROOT, "build", "HobsonPresence.app")


def settings(config):
    """The presence section, over its defaults."""
    return {**home.DEFAULT_CONFIG["presence"], **((config or {}).get("presence") or {})}


# ── Reading the sensor ─────────────────────────────────────────────────────

def read_state():
    """The helper's last record, or {} if there is none or it cannot be read."""
    try:
        with open(state_path(), encoding="utf-8") as f:
            record = json.load(f)
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def audience_of(record, now):
    """Who is listening, from one record: a state, or "unknown" when the
    record is missing, stale or malformed. Pure."""
    if not record:
        return "unknown"
    try:
        age = now - float(record.get("ts") or 0)
    except (TypeError, ValueError):
        return "unknown"
    if age > STALE_SECONDS or age < -STALE_SECONDS:
        return "unknown"
    state = record.get("state")
    return state if state in STATES else "unknown"


def needs_look(record, cfg, now):
    """True when a look would change what is known: auto mode, a camera the
    helper may use, and a "present" that rests on nothing -- you have been
    idle and no look has been taken since -- or on a look that is no longer
    fresh. A keystroke, a lock or a call is already an answer. Pure."""
    if cfg.get("mode") != "auto" or record.get("mode") != "auto":
        return False
    if record.get("camera") != "authorized" or record.get("state") != "present":
        return False
    if record.get("camera_allowed") is False:  # the phone switch is face down
        return False
    source = record.get("source")
    if source == "idle":
        return True
    if source == "camera":
        return now - float(record.get("glance_ts") or 0) > LOOK_FRESH_SECONDS
    return False


def _look(record, timeout=LOOK_WAIT_SECONDS):
    """Ask the helper to look (SIGUSR1) and wait for the answer. Returns the
    newer record, or the old one if none came in time."""
    try:
        pid = int(record.get("pid") or 0)
        if pid <= 0:
            return record
        asked = time.time()
        os.kill(pid, signal.SIGUSR1)
    except (OSError, ValueError, TypeError):
        return record
    deadline = asked + timeout
    while time.time() < deadline:
        time.sleep(0.15)
        fresh = read_state()
        if float(fresh.get("glance_ts") or 0) >= asked:
            return fresh
    return record


def audience(config, look=True, now=None):
    """(state, record): who is listening right now. Takes a look first when
    that is allowed and would settle it (needs_look). Never raises."""
    cfg = settings(config)
    if not cfg.get("enabled"):
        return "unknown", {}
    record = read_state()
    now = time.time() if now is None else now
    state = audience_of(record, now)
    if look and state == "present" and needs_look(record, cfg, now):
        record = _look(record)
        state = audience_of(record, time.time())
    return state, record


# ── What to do with a phrase ───────────────────────────────────────────────

def decision(state, kind):
    """What happens to a phrase of this kind, said to this audience:
    "speak", "hold", "drop" or "company". Pure; see the module docstring."""
    if kind in NEVER_HELD or state not in ("away", "call", "company"):
        return "speak"
    if kind == "commentary":
        return "drop"
    if state == "company":
        return "company" if kind == "waiting" else "hold"
    return "hold"


def route(config, kind, phrase, project=None):
    """The phrase to speak now -- itself, or the company line -- or None when
    it was held for your return or dropped. Called at the speak seams, so
    every engine and every event goes through it once. Commentary never
    triggers a look: it is dropped while away, and not worth the camera."""
    if kind in NEVER_HELD or not phrase:
        return phrase
    state, _ = audience(config, look=kind != "commentary")
    what = decision(state, kind)
    if what == "speak":
        return phrase
    if what == "drop":
        log_record.write(f"[presence] dropped ({state}, {kind}) -> {phrase!r}")
        return None
    hold(project if project is not None else home.derive_project_label(), kind, phrase)
    log_record.write(f"[presence] held ({state}, {kind}) -> {phrase!r}")
    return COMPANY_LINE if what == "company" else None


def pauses_nudge(state):
    """A nudge waits for you to come back rather than talking to an empty
    room or over a call. Company hears it: its phrases name nothing."""
    return state in ("away", "call")


# ── The salver: what is held for your return ───────────────────────────────

@contextlib.contextmanager
def _held_items():
    """The held list under its lock, saved once on a clean exit. Items older
    than HELD_MAX_AGE fall away; the newest MAX_HELD are kept."""
    os.makedirs(home.state_dir(), exist_ok=True)
    with open(_held_lock_path(), "a+", encoding="utf-8") as lock:
        deadline = time.monotonic() + 3.0
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break  # go ahead without it rather than hang a hook
                time.sleep(0.01)
        try:
            with open(held_path(), encoding="utf-8") as f:
                items = json.load(f).get("items") or []
        except (OSError, ValueError, AttributeError):
            items = []
        now = time.time()
        items = [i for i in items if isinstance(i, dict) and now - float(i.get("ts") or 0) <= HELD_MAX_AGE]
        yield items
        items[:] = items[-MAX_HELD:]
        tmp = f"{held_path()}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, indent=2)
        os.replace(tmp, held_path())


def hold(project, kind, phrase, now=None):
    with _held_items() as items:
        items.append({"ts": time.time() if now is None else now, "project": project or "",
                      "kind": kind, "phrase": phrase})


def take_held():
    """Everything held, oldest first, and an empty salver."""
    with _held_items() as items:
        taken = list(items)
        items.clear()
    return taken


def held():
    """Everything held, left where it is."""
    with _held_items() as items:
        return list(items)


# ── Who is waiting on you ──────────────────────────────────────────────────

def _lock_busy(path):
    """True while some process holds the flock on `path` (a running nudge)."""
    try:
        with open(path, "a+", encoding="utf-8") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def _read_float(path):
    try:
        with open(path, encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def waiting_on_you(now=None):
    """[(project, need)] for every session blocked on you, need being a key
    of NEED. Read from what already exists -- a running nudge, the last
    Stop's category, the liveness record -- and answered by anything that
    happened since: a prompt you typed, a later turn, a later tool call.

    Known false positive: an approved Bash call that runs long (a build)
    leaves the liveness record at PermissionRequest until the next tool
    call, so for up to PERMISSION_WAIT_SECONDS it reads as waiting on your
    approval. The window is what bounds it; nothing records the approval."""
    import nudge
    now = time.time() if now is None else now
    found = []
    try:
        names = sorted(os.listdir(home.sessions_dir()))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(home.sessions_dir(), name), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            continue
        project = state.get("project") or ""
        activity = _read_float(nudge.activity_path(project))
        alive = nudge.read_alive(project)
        alive_ts = float(alive.get("ts") or 0)
        last_stop = float(state.get("last_stop_time") or 0)
        need = None
        if _lock_busy(home.project_file("nudge", project, ".lock")):
            need = "you"
        elif (state.get("last_stop_category") == "question"
              and last_stop > max(activity, alive_ts)
              and now - last_stop <= QUESTION_WAIT_SECONDS):
            need = "answer"
        elif (alive_ts > max(activity, last_stop) and now - alive_ts <= PERMISSION_WAIT_SECONDS
              and (alive.get("event") == "PermissionRequest"
                   or alive.get("tool") in nudge.USER_WAIT_TOOLS)):
            need = "answer" if alive.get("tool") in nudge.USER_WAIT_TOOLS else "approval"
        if need:
            found.append((project, need))
    return found


# ── What is said at the door ───────────────────────────────────────────────

def _sentence(text):
    text = (text or "").strip()
    return text if not text or text[-1] in ".!?" else text + "."


def compose_briefing(items, waits, came_from):
    """One briefing for your return, or None when there is nothing to tell.

    Most urgent first (URGENCY), two told in full and the rest counted. A
    session's repeated waiting lines collapse to its latest, and a session
    that is still waiting but held nothing is named once. Pure.
    """
    latest_wait = {}
    others = []
    for item in sorted(items, key=lambda i: float(i.get("ts") or 0)):
        if item.get("kind") == "waiting":
            latest_wait[item.get("project") or ""] = item
        else:
            others.append(item)
    # (urgency, ts, project or None for a line that names its own, text)
    entries = [(URGENCY["waiting"], float(i.get("ts") or 0), i.get("project") or "",
                _sentence(i.get("phrase"))) for i in latest_wait.values()]
    for project, need in waits:
        if project not in latest_wait:
            # After the held waits of the same urgency: those carry the words.
            entries.append((URGENCY["waiting"], float("inf"), None,
                            f"{project or 'A session'} is still waiting on {NEED.get(need, 'you')}."))
    entries += [(URGENCY.get(i.get("kind"), URGENCY["info"]), float(i.get("ts") or 0),
                 i.get("project") or "", _sentence(i.get("phrase"))) for i in others]
    if not entries:
        return None
    entries.sort(key=lambda e: (e[0], e[1]))
    told, named = [], None
    for _, _, project, text in entries[:2]:
        # Name the project once for a run of its lines, not before each.
        if project and project != named:
            text = f"On {project}: {text}"
        named = project
        told.append(text)
    rest = len(entries) - len(told)
    if rest:
        told.append(f"Plus {rest} more {'update' if rest == 1 else 'updates'}.")
    return " ".join([GREETINGS.get(came_from, GREETINGS["away"])] + told)


def compose_departure(waits):
    """"Before you go" -- only if something is waiting on you. Pure."""
    if not waits:
        return None
    if len(waits) == 1:
        project, need = waits[0]
        return f"Before you go — {project or 'a session'} is waiting on {NEED.get(need, 'you')}."
    return f"Before you go — {len(waits)} sessions are waiting on you."


def _speak(config, text):
    import hobson
    engine = hobson.load_engine(config)
    engine.speak_dynamic(text, allow_cold_start=True, kind="briefing")


def on_transition(came_from, to, source, away_for=0.0, config=None, now=None):
    """What the helper's change of state calls for. Returns what was said."""
    config = home.load_config() if config is None else config
    cfg = settings(config)
    now = time.time() if now is None else now
    log_record.write(f"[presence] {came_from} -> {to} ({source}, after {away_for:.0f}s)")
    if not cfg.get("enabled"):
        return None
    reason = home.silence_reason(config)

    if to == "present" and came_from in ("away", "call", "company"):
        if reason:
            log_record.write(f"[presence] briefing skipped ({reason}), held items kept")
            return None
        items = take_held()
        waits = waiting_on_you(now) if away_for >= MIN_AWAY_FOR_WAITS else []
        text = compose_briefing(items, waits, came_from)
        if text:
            log_record.write(f"[presence] briefing ({len(items)} held) -> {text!r}")
        elif came_from == "away" and cfg.get("greetings") and away_for >= MIN_AWAY_FOR_GREETING:
            text = _pick_line("greeting", compose_greeting(away_for))
            log_record.write(f"[presence] greeting -> {text!r}")
        else:
            log_record.write("[presence] back, nothing to report")
            return None
        _speak(config, text)
        return text

    leaving = to == "away" and came_from in ("present", "company") and (
        source == "lock" or (source == "camera" and cfg.get("mode") == "continuous"))
    if leaving and not reason:
        text = compose_departure(waiting_on_you(now))
        if not text and cfg.get("greetings") and _farewell_due(now):
            text = _pick_line("farewell", FAREWELLS, now=now)
        if text:
            log_record.write(f"[presence] leaving -> {text!r}")
            _speak(config, text)
            return text
    return None


def _duration(seconds):
    """'20 minutes', 'an hour', '3 hours': how a person says it."""
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} minutes"
    hours = int(round(seconds / 3600))
    return "an hour" if hours == 1 else f"{hours} hours"


def compose_greeting(away_for):
    """The greetings to choose from for a return with nothing to tell. Pure."""
    if away_for >= LONG_AWAY:
        return [f"Welcome back. All quiet for the last {_duration(away_for)}."]
    return GREETINGS_NOTHING


# ── Lines already said (greetings and farewells) ───────────────────────────

def _lines_path():
    return home.path("hobson-presence-lines.json")


def _read_lines():
    try:
        with open(_lines_path(), encoding="utf-8") as f:
            record = json.load(f)
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def _farewell_due(now):
    return now - float(_read_lines().get("farewell_ts") or 0) >= FAREWELL_EVERY


def _pick_line(kind, candidates, now=None):
    """One of `candidates`, never the one said last time for this kind, and
    remember it. Only the helper's transitions call this, one at a time."""
    import random
    record = _read_lines()
    last = record.get(kind)
    fresh = [c for c in candidates if c != last] or list(candidates)
    line = random.choice(fresh)
    record[kind] = line
    if kind in ("farewell", "wave"):
        record[f"{kind}_ts"] = time.time() if now is None else now
    _save_lines(record)
    return line


def _note_line(key, value):
    record = _read_lines()
    record[key] = value
    _save_lines(record)


def _save_lines(record):
    tmp = f"{_lines_path()}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    os.replace(tmp, _lines_path())


def on_wave(config=None, now=None):
    """You waved at the camera. Returns what was said."""
    config = home.load_config() if config is None else config
    now = time.time() if now is None else now
    log_record.write("[presence] wave")
    if not settings(config).get("enabled") or home.silence_reason(config):
        return None
    if now - float(_read_lines().get("wave_ts") or 0) < WAVE_EVERY:
        return None
    text = compose_briefing(take_held(), waiting_on_you(now), "wave")
    if text:
        _note_line("wave_ts", now)
    else:
        text = _pick_line("wave", WAVE_HELLOS, now=now)
    log_record.write(f"[presence] wave -> {text!r}")
    _speak(config, text)
    return text


def on_switch(position, config=None):
    """The phone was turned over: confirm it out loud, so nobody has to
    wonder whether the camera is really off. Returns what was said."""
    config = home.load_config() if config is None else config
    text = SWITCH_LINES.get(position)
    log_record.write(f"[presence] phone {position}, camera {'on' if position == 'up' else 'off'}")
    if not text or not settings(config).get("enabled") or home.silence_reason(config):
        return None
    _speak(config, text)
    return text


# ── The helper's life ──────────────────────────────────────────────────────

def helper_built():
    return os.path.isfile(os.path.join(app_path(), "Contents", "MacOS", "HobsonPresence"))


def _helper_argv(cfg):
    argv = ["--home", home.state_dir(), "--mode", str(cfg.get("mode") or "auto"),
            "--idle-seconds", str(cfg.get("idle_seconds")),
            "--away-after", str(cfg.get("away_after")),
            "--exit-after", str(cfg.get("exit_after")),
            "--python", sys.executable, "--script", os.path.abspath(__file__)]
    for app in cfg.get("call_apps") or []:
        argv += ["--call-app", str(app)]
    if cfg.get("phone"):
        argv += ["--phone", str(cfg["phone"]), "--adb", find_adb(cfg) or "adb"]
    if cfg.get("preview"):
        argv.append("--preview")
    return argv


def find_adb(cfg):
    """adb's path. Hooks run with Claude Code's PATH, which rarely has the
    Android SDK on it, so the usual install places are tried too. None if
    there is no adb, and then the phone switch reads as off."""
    sdk = "platform-tools/adb"
    for candidate in (cfg.get("adb"), shutil.which("adb"),
                      os.path.join(os.environ.get("ANDROID_HOME") or "/nonexistent", sdk),
                      os.path.join(os.environ.get("ANDROID_SDK_ROOT") or "/nonexistent", sdk),
                      os.path.expanduser(f"~/Library/Android/sdk/{sdk}"),
                      "/opt/homebrew/bin/adb"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def stop_helper(record=None):
    record = read_state() if record is None else record
    try:
        pid = int(record.get("pid") or 0)
        if pid > 0:
            os.kill(pid, signal.SIGTERM)
            return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def ensure_running(config, now=None):
    """Start the helper if presence is on, it is built, and none is running
    in the configured mode. Cheap when one is: a stat and a read. Launched
    through `open`, so the camera permission is the bundle's own, not the
    terminal's that ran the hook."""
    cfg = settings(config)
    if not cfg.get("enabled") or not helper_built():
        return False
    now = time.time() if now is None else now
    record = read_state()
    if audience_of(record, now) != "unknown":
        if (record.get("mode") == cfg.get("mode")
                and (record.get("phone_setting") or None) == (cfg.get("phone") or None)
                and bool(record.get("preview")) == bool(cfg.get("preview"))):
            return False
        stop_helper(record)  # restarted with the new settings by the next hook
        return False
    try:
        if now - os.path.getmtime(_spawn_marker()) < SPAWN_THROTTLE_SECONDS:
            return False
    except OSError:
        pass
    with open(_spawn_marker(), "w", encoding="utf-8") as f:
        f.write(str(now))
    # -g: never take focus. -j (start hidden) only without the preview
    # window, which a hidden app cannot show.
    flags = ["-g"] if cfg.get("preview") else ["-g", "-j"]
    subprocess.Popen(["open"] + flags + [app_path(), "--args"] + _helper_argv(cfg),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_record.write(f"[presence] sensor started ({cfg.get('mode')})")
    return True


def look_now(request_permission=False, timeout=30):
    """One look through the camera, as its own process (`open -W`), for the
    CLI and doctor. Returns the helper's answer, or {} if it gave none."""
    if not helper_built():
        return {}
    out = home.path(f"hobson-presence-look.{os.getpid()}.json")
    # -n: a second instance. Without it `open` hands the request to a sensor
    # already running, which ignores it, and -W then waits on it forever.
    argv = ["open", "-n", "-W", "-g", "-j", app_path(), "--args", "--look", "--out", out]
    if request_permission:
        argv.append("--request-permission")
    try:
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        with open(out, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    finally:
        with contextlib.suppress(OSError):
            os.remove(out)


def camera_status(timeout=15):
    """The camera permission as the helper's own bundle sees it, without a
    prompt and without turning the camera on. "" if it cannot be asked."""
    if not helper_built():
        return ""
    out = home.path(f"hobson-presence-signals.{os.getpid()}.json")
    try:
        subprocess.run(["open", "-n", "-W", "-g", "-j", app_path(), "--args", "--signals", "--out", out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        with open(out, encoding="utf-8") as f:
            return json.load(f).get("camera") or ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    finally:
        with contextlib.suppress(OSError):
            os.remove(out)


def status(config=None, now=None):
    """Everything `hobson presence` shows, as one dict."""
    config = home.load_config() if config is None else config
    cfg = settings(config)
    now = time.time() if now is None else now
    record = read_state()
    return {
        "enabled": bool(cfg.get("enabled")), "mode": cfg.get("mode"),
        "built": helper_built(), "state": audience_of(record, now) if cfg.get("enabled") else "off",
        "source": record.get("source"), "since": record.get("since"),
        "camera": record.get("camera"), "call_app": record.get("call_app"),
        "idle": record.get("idle"), "people": record.get("people"),
        "phone": record.get("phone"), "camera_allowed": record.get("camera_allowed"),
        "held": len(held()), "waiting": waiting_on_you(now),
    }


def read_phone(config=None):
    """The phone switch, read once through the helper's own code (no camera,
    so no permission involved). {} when there is no switch or no helper."""
    config = home.load_config() if config is None else config
    cfg = settings(config)
    binary = os.path.join(app_path(), "Contents", "MacOS", "HobsonPresence")
    if not cfg.get("phone") or not helper_built():
        return {}
    try:
        out = subprocess.run([binary, "--read-phone", "--phone", str(cfg["phone"]),
                              "--adb", find_adb(cfg) or "adb"],
                             capture_output=True, text=True, timeout=15).stdout
        return json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def status_lines(config=None, now=None):
    """`hobson presence`, as lines of text."""
    config = home.load_config() if config is None else config
    s = status(config, now)
    now = time.time() if now is None else now
    if not s["enabled"]:
        return ["Presence:    off (hobson presence on)"]
    lines = [f"Presence:    {s['state']}" + (f" ({s['source']})" if s["source"] and s["state"] != "unknown" else ""),
             f"Mode:        {s['mode']}"]
    if s["state"] == "unknown":
        lines.append("Sensor:      " + ("not running (starts with the next hook)" if s["built"]
                                        else "not built (hobson presence setup)"))
    if s.get("since") and s["state"] != "unknown":
        lines.append(f"Since:       {int(now - float(s['since']))}s ago")
    if s.get("call_app"):
        lines.append(f"Call:        {s['call_app']}")
    camera = s.get("camera") or "unknown"
    if s.get("phone") is not None:
        camera += f", phone {s['phone']}" + ("" if s.get("camera_allowed") else " (camera off)")
    lines.append(f"Camera:      {camera}")
    lines.append(f"Held:        {s['held']} for your return")
    for project, need in s["waiting"]:
        lines.append(f"Waiting:     {project or 'a session'} needs {NEED.get(need, 'you')}")
    return lines


def doctor_lines(config=None):
    """[(level, text)] for `hobson doctor`: level is ok, warn or info."""
    config = home.load_config() if config is None else config
    cfg = settings(config)
    if not cfg.get("enabled"):
        return [("info", "Presence off (hobson presence on to enable)")]
    out = []
    if not helper_built():
        out.append(("warn", "Presence sensor not built: hobson presence setup (needs Xcode Command Line Tools)"))
        return out
    out.append(("ok", f"Presence sensor built ({cfg.get('mode')} mode)"))
    record = read_state()
    state = audience_of(record, time.time())
    if state == "unknown":
        out.append(("info", "Presence sensor not running (starts with the next hook)"))
    else:
        out.append(("ok", f"Presence sensor running: {state} ({record.get('source')})"))
    camera = record.get("camera")
    if cfg.get("mode") != "signals":
        if camera == "authorized":
            out.append(("ok", "Camera permission granted"))
        elif camera in ("denied", "restricted"):
            out.append(("warn", "Camera permission denied: System Settings → Privacy & Security → Camera → Hobson Presence"))
        elif camera == "not-determined":
            out.append(("warn", "Camera permission not asked yet: hobson presence setup"))
        elif camera == "unavailable":
            out.append(("warn", "No usable camera (lid closed, or only virtual cameras)"))
    if cfg.get("phone"):
        if not find_adb(cfg):
            out.append(("warn", "Phone switch set but adb not found: set presence.adb (camera stays off)"))
        else:
            phone = read_phone(config)
            where = phone.get("phone", "unreachable")
            level = "ok" if where in ("up", "down") else "warn"
            out.append((level, f"Phone switch: {where}" + ("" if phone.get("camera_allowed") else " (camera off)")))
    return out


def set_setting(key, value):
    """Write presence.<key> to the config file. The running helper is
    restarted with it by the next hook (ensure_running)."""
    path = home.config_file()
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        config = {}
    section = config.get("presence") if isinstance(config.get("presence"), dict) else {}
    section[key] = value
    config["presence"] = section
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--transition", nargs=2, metavar=("FROM", "TO"))
    parser.add_argument("--source", default="")
    parser.add_argument("--away-for", type=float, default=0.0)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--look", action="store_true")
    parser.add_argument("--request-permission", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--switch", choices=("up", "down"))
    parser.add_argument("--wave", action="store_true")
    parser.add_argument("--status-text", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--phone-check", action="store_true")
    parser.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"))
    parser.add_argument("--camera-status", action="store_true")
    args = parser.parse_args(argv)

    if args.wave:
        on_wave()
    elif args.switch:
        on_switch(args.switch)
    elif args.status_text:
        print("\n".join(status_lines()))
    elif args.doctor:
        for level, text in doctor_lines():
            print(f"{level}\t{text}")
    elif args.camera_status:
        print(camera_status())
    elif args.phone_check:
        print(json.dumps(read_phone(), indent=2))
    elif args.set:
        key, value = args.set
        parsed = {"true": True, "false": False, "none": None, "off": None}.get(value.lower(), value)
        set_setting(key, parsed)
        if key == "enabled" and parsed is False:
            stop_helper()  # nothing else would: a disabled presence starts nothing
        elif key in ("mode", "phone", "preview"):
            # Now, not at the next hook: whoever changed it is watching.
            if stop_helper():
                time.sleep(1.0)
            with contextlib.suppress(OSError):
                os.remove(_spawn_marker())
            ensure_running(home.load_config())
    elif args.transition:
        home.migrate_legacy_state()
        on_transition(args.transition[0], args.transition[1], args.source, args.away_for)
    elif args.status:
        print(json.dumps(status(), indent=2))
    elif args.look:
        print(json.dumps(look_now(request_permission=args.request_permission), indent=2))
    elif args.stop:
        print("stopped" if stop_helper() else "not running")
    elif args.start:
        started = ensure_running(home.load_config())
        print("started" if started else "running, off, or not built")


if __name__ == "__main__":
    main()
