"""home.py — where Hobson keeps its state, and how it is configured.

Every file Hobson writes lives under state_dir(), ~/.claude, resolved from
$HOME each time it is asked for, never at import. A test, or a hook run under
another HOME, moves all of it by setting HOME alone. The shell scripts and
the two TTS daemon scripts keep their own copies of these paths.

Also here, because everything that reads config needs them and none of it
should have to load an engine for them: the defaults and load_config(), the
project label, and the quiet controls (silence_reason). Nothing in this
module imports the rest of Hobson, so the UserPromptSubmit hook, which runs
on every prompt, imports only this.
"""

import copy
import hashlib
import json
import os
import re
import time

# The repo: CLAUDE_PLUGIN_ROOT in plugin mode, else this checkout.
ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)


# ── Paths ──────────────────────────────────────────────────────────────────

def state_dir():
    """~/.claude, from $HOME as it is now."""
    return os.path.expanduser("~/.claude")


def path(name):
    """A file or directory under state_dir()."""
    return os.path.join(state_dir(), name)


def config_file():
    return path("hobson.json")


def log_file():
    return path("hobson.log")


def env_file():
    """OPENROUTER_API_KEY lives here, never in config_file(), which
    `hobson config show` prints."""
    return path("hobson.env")


def sessions_dir():
    return path("hobson-sessions")


def bark_lock():
    """Cross-process lock and last-spoke timestamp, shared by every engine."""
    return path("hobson.lock")


def commentary_lock():
    return path("hobson-commentary.lock")


def project_key(project):
    """The short stable key every per-project file is named by."""
    return hashlib.sha256((project or "").encode("utf-8")).hexdigest()[:12]


def project_file(kind, project, suffix=""):
    """hobson-<kind>-<key><suffix>: a nudge or watchdog lock, the activity
    token, the liveness record."""
    return path(f"hobson-{kind}-{project_key(project)}{suffix}")


# ── Migration from claude-bark ─────────────────────────────────────────────

# Hobson was claude-bark once. A config kept under that name is moved to the
# new one the first time it is needed. Only a missing target is ever
# written: nothing is overwritten.
_LEGACY_NAMES = (
    # (new name, old names to take it from, newest first)
    ("hobson.json", ("claude-bark.json",)),
)

# The default personality was "alfred" until it became Hobson himself.
_PERSONALITY_ALIASES = {"alfred": "hobson"}


def migrate_legacy_state():
    """Move claude-bark state to Hobson's names. Returns what moved."""
    moved = []
    for new, olds in _LEGACY_NAMES:
        target = path(new)
        if os.path.lexists(target):
            continue
        for old in olds:
            source = path(old)
            if not os.path.lexists(source):
                continue
            try:
                os.rename(source, target)  # atomic, and keeps the file's mode
            except OSError:
                break  # another hook got there first, or the disk refused
            moved.append(f"{old} -> {new}")
            break
    if any(m.endswith("-> hobson.json") for m in moved):
        _rename_legacy_personality()
    return moved


def _rename_legacy_personality():
    """A migrated config naming "alfred" now names "hobson"."""
    config_path = config_file()
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(config, dict):
        return
    new = _PERSONALITY_ALIASES.get(config.get("personality"))
    if new:
        config["personality"] = new
        tmp = f"{config_path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(tmp, config_path)


# ── Config ─────────────────────────────────────────────────────────────────

# 600s, not 180s: measured over 11,173 real gaps between spoken phrases, a
# 180s timeout expires during 4.1% of them and each expiry costs the next
# utterance a fallback to macOS `say`, because commentary deliberately
# passes allow_cold_start=False rather than blocking a hook for a 20s model
# load. 600s drops that to 1.1% and still frees the model after a real
# break. One constant for both daemon sections of DEFAULT_CONFIG below,
# which the daemon engine reads its defaults from.
DEFAULT_DAEMON_IDLE_TIMEOUT = 600

DEFAULT_CONFIG = {
    "engine": "say",
    "personality": "hobson",
    "events": ["stop", "permission", "notification"],
    "cooldown": 2.0,
    "volume": 3,
    "muted": False,
    "mute_until": 0,
    "quiet_hours": None,
    "ollama": {
        "model": "llama3.2:3b",
        "url": "http://localhost:11434",
    },
    "kokoro": {
        "voice": "am_puck",
        "speed": 1.1,
        "daemon_port": 19849,
        # See DEFAULT_DAEMON_IDLE_TIMEOUT above for why this is 600, not 180.
        "daemon_idle_timeout": DEFAULT_DAEMON_IDLE_TIMEOUT,
    },
    "chatterbox": {
        "device": "mps",
        "exaggeration": 1.0,
    },
    "pocket_tts": {
        "voice": "charles",
        "language": "english",
        "temp": 0.7,
        "daemon_port": 19850,
        "daemon_idle_timeout": DEFAULT_DAEMON_IDLE_TIMEOUT,  # see the kokoro note above
    },
    # Off by default: afplay -r resamples, which makes speech sound robotic
    # (and degraded the macOS `say` fallback too, since that also plays via
    # afplay). See BaseEngine._afplay_args.
    "project_identity": {
        "enabled": False,
        "spread": 0.06,
    },
    "commentary": {
        "cooldown": 8.0,
        "tools": ["Agent", "Edit", "Write", "Bash"],
        # terse  — LLM-gated, only voice notable events (rare voicings)
        # normal — LLM-gated, bias toward voicing meaningful actions (default)
        # chatty — no LLM, voice every allowed tool with a templated phrase
        # With the decider on "jev", terse and normal both defer to
        # "chattiness" below instead.
        "verbosity": "normal",
        # Subagents (background workers) stamp agent_id/agent_type on their
        # events; main-session events carry neither. Measured: ~51 PreToolUse
        # events/minute from background workers the user isn't watching.
        # Commentary only — a subagent's Stop still speaks (see run()).
        "suppress_subagents": True,
        # Substance gate for batched commentary (terse/normal only — chatty
        # speaks every call). Chosen by simulating the real 16,875-event
        # PreToolUse log: 5/60s gives a 2.9x reduction in utterances vs
        # today; 3/15s (an earlier draft) only gave 1.5x because the short
        # timer fires constantly before enough calls accumulate.
        "min_tool_calls": 5,
        "min_seconds": 60.0,
        "pending_stale_seconds": 90.0,
        # Decider backend "jev" only: how much of what the gate above
        # releases gets spoken, 0 (next to nothing) to 1 (all of it). A batch
        # is spoken when P(worth) >= 1 - chattiness (gate.py), and terse and
        # normal stop differing. 0.6 caught all 7 worth-hearing batches in
        # 140 real ones while speaking a third as often as Ollama did.
        "chattiness": 0.6,
        # Anomaly mode only: how far back "back on the same thing again"
        # looks for the same action (session_state.anomaly_reason).
        "repeat_window": 180,
    },
    "nudge": {
        "enabled": True,
        "delays": [45, 120, 300],
    },
    "watchdog": {
        "enabled": True,
        "minutes": 10,
    },
    # Whether anyone is listening (scripts/presence.py; the sensor is the
    # Swift helper in presence/). Away or on a call, what Hobson would have
    # said is held and told on your return; with company, a wait is spoken
    # without its details. No helper, or a stale one, changes nothing.
    "presence": {
        "enabled": True,
        # "signals"    keyboard/mouse idle, screen lock, display sleep, calls
        # "auto"       signals, plus one short look through the camera only
        #              when Hobson is about to speak and you have been idle
        # "continuous" signals, plus a frame a second: departures within
        #              seconds, and company at all
        "mode": "auto",
        # Untouched this long before the camera may be asked (auto).
        "idle_seconds": 60,
        # No face seen (and no keystroke) this long means away (continuous).
        # Faces are all that count, and someone looking down shows none: at
        # a desk they went unseen for 15-20s at a time, so this sits above it.
        "away_after": 30,
        # A farewell when you leave and a greeting when you are back, even
        # with nothing to tell (presence.py: guarded against flapping).
        "greetings": True,
        # The helper exits once no hook has run for this long and nothing is
        # held for your return.
        "exit_after": 1800,
        # Bundle-id prefixes whose microphone use also means a call, beyond
        # the built-in list (presence/main.swift, Signals.callApps).
        "call_apps": [],
        # The camera switch: an Android phone over adb (USB or Wireless
        # debugging), face up = camera on, face down = off. "auto" is the one
        # real device adb sees; else an adb serial prefix. None: no switch.
        # A phone that cannot be read counts as face down.
        "phone": None,
        # adb's path, if not found on PATH or in the Android SDK's usual place.
        "adb": None,
        # A floating window with the camera feed and what the sensor makes of
        # it (hobson presence preview on). On screen only; nothing is saved.
        "preview": False,
    },
    # Optional remote decision model (Jev, via OpenRouter's Decisions API).
    # See scripts/decider.py. At "local" (the default) it makes zero network
    # calls. Three questions go to it:
    #   - phrase_gen: does this phrase restate one just spoken? Sends the
    #     phrase plus up to six recently spoken ones.
    #   - gate: is this commentary batch worth hearing? Sends the batch
    #     summary (at most 220 characters), redacted as below.
    #   - risk: is this shell command destructive? Asked only for a Bash
    #     permission request the local rules can neither flag nor clear, and
    #     sends the command REDACTED (risk.redact: no heredoc bodies or long
    #     quoted text; URLs, hosts, IPs, emails, secrets, tokens and all but
    #     the last component of every path replaced).
    # Never code, file contents, or conversation.
    "decider": {
        "backend": "local",          # "local" | "jev"
        "model": "typesafe/jev-1.13",
        # Headroom, not a tuned figure. Every hook is a fresh process, so
        # each call pays a new TCP+TLS handshake to OpenRouter with no
        # connection pooling -- real latency is well above Jev's quoted
        # 70-500ms of inference whatever the link. The observations behind
        # this number (p50 0.6s, p90 3.2s) were taken on a degraded
        # connection and are an upper bound, not a measurement; 2000ms was
        # visibly timing out into local under those conditions. Re-measure
        # on a good link before treating this as tuned.
        "timeout_ms": 4000,
        "endpoint": "https://openrouter.ai/api/alpha/decisions",
        # Speak a rejected phrase when P(restates) falls below this
        # (phrase_gen._decider_rescues_duplicate).
        "dedup_restates_max": 0.5,
        # Silence commentary the local guard passed when P(restates) is at or
        # above this (phrase_gen._decider_blocks_restatement). Commentary
        # only: a Stop, permission or notification is never blocked by it.
        "dedup_block_min": 0.7,
        # Warn "Careful — this one looks hard to undo" when P(destructive)
        # for a command the local rules cannot place is at or above this
        # (risk.permission_risk).
        "permission_risk_min": 0.5,
    },
}


def load_config():
    """config_file() merged over DEFAULT_CONFIG (deep for sub-dicts)."""
    # A deep copy: a shallow one hands out DEFAULT_CONFIG's own sub-dicts for
    # every section the user did not set, so any caller that mutates one
    # changes the defaults for the rest of the process.
    config = copy.deepcopy(DEFAULT_CONFIG)
    user_config = {}
    config_path = config_file()
    if not os.path.isfile(config_path):
        migrate_legacy_state()
    try:
        with open(config_path, encoding="utf-8") as f:
            user_config = json.load(f)
        if not isinstance(user_config, dict):
            user_config = {}
        # Shallow merge top-level, deep merge engine sub-dicts
        for key, val in user_config.items():
            if isinstance(val, dict) and key in config and isinstance(config[key], dict):
                config[key] = {**config[key], **val}
            else:
                config[key] = val
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Migrate kokoro.realtime_events -> top-level events
    if "events" not in user_config:
        rt_events = config.get("kokoro", {}).get("realtime_events")
        if rt_events:
            config["events"] = rt_events

    # Clean up deprecated key from merged config
    config.get("kokoro", {}).pop("realtime_events", None)

    config["personality"] = _PERSONALITY_ALIASES.get(config.get("personality"),
                                                     config.get("personality"))
    return config


# ── Quiet controls ─────────────────────────────────────────────────────────

def parse_duration(text):
    """Seconds from a human duration. Bare numbers mean minutes.

    Accepts "45", "90s", "30m", "2h", "1.5h". Returns None if unparseable —
    callers must treat None as "the user typed something wrong", never as 0.
    """
    if not text:
        return None
    text = str(text).strip().lower()
    mult = 60.0  # a bare number means minutes
    if text and text[-1] in "smh":
        mult = {"s": 1.0, "m": 60.0, "h": 3600.0}[text[-1]]
        text = text[:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return value * mult if value > 0 else None


def _in_quiet_hours(window, now):
    """True when `now` falls inside a [start_hour, end_hour] window.

    Handles windows that wrap midnight (22 -> 8). Returns False for anything
    malformed: this runs inside a hook and a hand-edited config must never
    take voice down with a traceback.
    """
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        return False  # [1, 2, 3] used to read as 1-2, silencing 01:00-02:00
    try:
        start, end = int(window[0]), int(window[1])
    except (TypeError, ValueError):
        return False
    if not (0 <= start <= 24 and 0 <= end <= 24) or start == end:
        return False
    hour = time.localtime(now).tm_hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def silence_reason(config, now=None):
    """Why voice is suppressed right now, or None if it may speak.

    Checked cheapest-first. The returned string is logged, so the user can
    always find out why hobson went quiet — a detector that silences without
    saying so is the worse bug.
    """
    now = time.time() if now is None else now
    if config.get("muted"):
        return "muted"
    until = config.get("mute_until") or 0
    try:
        remaining = float(until) - now
    except (TypeError, ValueError):
        remaining = 0
    if remaining > 0:
        return f"muted for another {int(remaining // 60)}m{int(remaining % 60):02d}s"
    window = config.get("quiet_hours")
    if window and _in_quiet_hours(window, now):
        return f"quiet hours {window[0]}-{window[1]}"
    return None


# ── Project identification ─────────────────────────────────────────────────

_PROJECT_LABEL_CACHE = None


def derive_project_label():
    """Derive a short, speakable project label from the current working directory.

    Handles workmux __worktrees pattern:
      /Dev/mobile-app                         → "mobile app"
      /Dev/mobile-app__worktrees/app-4005-...  → "mobile app, A P P 4005"

    Returns empty string if label can't be determined.
    Caches result per-process (cwd doesn't change within a hook invocation).
    """
    global _PROJECT_LABEL_CACHE
    if _PROJECT_LABEL_CACHE is not None:
        return _PROJECT_LABEL_CACHE

    try:
        cwd = os.getcwd()
    except OSError:
        _PROJECT_LABEL_CACHE = ""
        return ""

    if "__worktrees" in cwd:
        idx = cwd.index("__worktrees")
        repo_dir = cwd[:idx].rstrip("/")
        repo_name = os.path.basename(repo_dir).replace("-", " ").replace("_", " ")
        remainder = cwd[idx + len("__worktrees"):].lstrip("/")
        branch = remainder.split("/")[0] if remainder else ""

        m = re.match(r'^[a-zA-Z]{2,6}-\d+-(.+)', branch)
        if m:
            # Strip ticket prefix (e.g. "app-4005-") and use the task description
            task = m.group(1).replace("-", " ")
            # Keep first 4 words max for TTS brevity
            task = " ".join(task.split()[:4])
            label = f"{repo_name}, {task}"
        elif branch:
            label = f"{repo_name}, {branch.replace('-', ' ')[:25]}"
        else:
            label = repo_name
    else:
        name = os.path.basename(cwd.rstrip("/"))
        label = name.replace("-", " ").replace("_", " ")

    _PROJECT_LABEL_CACHE = label
    return label
