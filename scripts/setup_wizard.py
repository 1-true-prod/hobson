"""setup_wizard.py — the rules and the server behind Hobson's setup wizard.

The wizard is a page (setup/ui/) in a window of its own (setup/main.swift,
built into build/HobsonSetup.app). This module is everything else, and the
page decides nothing:

  probe()      what this Mac has: memory, disk, uv, Ollama and its models,
               an OpenRouter key, the camera permission, adb and its phones
  recommend()  what to suggest for it (Express), and why, in a sentence each
  plan()       what the answers would change: only settings that differ from
               DEFAULT_CONFIG are written, and one set back to its default is
               removed, so a default improved later still reaches this install
  apply()      doing it, one task at a time, streamed to the page

    python3 scripts/setup_wizard.py [--python PATH] [--browser]

The server listens on 127.0.0.1 only, on a random port, and every /api call
must carry the random token that is in the window's URL, from a page served
by this same server (the Host header is checked too). It writes config,
saves an API key and runs installers, and any web page open in a browser can
send requests to localhost.

Decider rule: backend "jev" is written only after a key has passed a real
call in this session (decider.check_key). Anything else stays local.

Exit status: 0 applied, 1 a task failed, 3 no desktop to show a window on,
10 closed before COMMIT (nothing written).
"""

import argparse
import hmac
import json
import mimetypes
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import bark_templates
import decider
import home
import log_record
import presence

# Every section the wizard can show. A new one added here is offered to
# existing installs on their next `hobson setup` (see unseen()).
SECTIONS = ("voice", "events", "brain", "jev", "presence", "phone")
# Installs that predate the wizard were asked these by the old installer.
LEGACY_SEEN = ("voice", "events")
SETUP_VERSION = 1

EVENTS = ("stop", "permission", "notification", "commentary")
VERBOSITIES = ("terse", "normal", "chatty", "anomaly")
ENGINES = ("say", "kokoro-realtime", "pocket-tts", "chatterbox")
PRESENCE_MODES = ("signals", "auto", "continuous", "off")

# The models the wizard names: the ones with measurements behind them
# (scripts/ab_models.py; the numbers are in CLAUDE.md), plus 1b, marked
# untested. Each note is those numbers as a pro and a con: gemma 60/60 on
# classifying Stops, llama3.2:3b 58/60 and 95% first-person phrasing, qwen
# 18/20 on questions and 35% first-person. Any other Ollama model can be
# typed in (MODEL_NAME). Disk sizes are what `ollama list` reports.
MODELS = (
    {"id": "llama3.2:3b", "disk_gb": 2.0, "default": True,
     "note": "What Hobson is tuned on: reads a turn well and speaks naturally as himself. Light enough for any recent Mac."},
    {"id": "gemma4:e4b", "disk_gb": 9.6,
     "note": "The best at telling done from broken from a question, but a 9.6 GB download and heavier to run."},
    {"id": "qwen3.5:4b", "disk_gb": 3.4,
     "note": "Apache-2.0 licensed, but more likely to miss a question for you, and often narrates instead of speaking as Hobson."},
    {"id": "llama3.2:1b", "disk_gb": 1.3, "untested": True,
     "note": "The smallest and quickest, for a Mac short on memory. Not measured with Hobson: expect more misreads."},
)
DEFAULT_MODEL = home.DEFAULT_CONFIG["ollama"]["model"]

# The voices the wizard offers each neural engine, male and female, every one
# heard from a shipped clip before anything is downloaded
# (scripts/cache-gen/setup_voice_gen.py renders them). Kokoro's are
# Apache-2.0; the Pocket TTS ones are VCTK speakers, CC BY 4.0 (credited in
# setup/ui/voice/LICENSES.md). The first of each is the engine's default.
VOICES = {
    "kokoro-realtime": (
        {"id": "am_puck", "name": "Puck", "sex": "male", "accent": "American"},
        {"id": "bm_george", "name": "George", "sex": "male", "accent": "British"},
        {"id": "bm_daniel", "name": "Daniel", "sex": "male", "accent": "British"},
        {"id": "bm_fable", "name": "Fable", "sex": "male", "accent": "British"},
        {"id": "bf_emma", "name": "Emma", "sex": "female", "accent": "British"},
        {"id": "bf_isabella", "name": "Isabella", "sex": "female", "accent": "British"},
        {"id": "af_heart", "name": "Heart", "sex": "female", "accent": "American"},
    ),
    "pocket-tts": (
        {"id": "charles", "name": "Charles", "sex": "male", "accent": "British"},
        {"id": "paul", "name": "Paul", "sex": "male", "accent": "British"},
        {"id": "anna", "name": "Anna", "sex": "female", "accent": "British"},
        {"id": "vera", "name": "Vera", "sex": "female", "accent": "British"},
        {"id": "fantine", "name": "Fantine", "sex": "female", "accent": "British"},
        {"id": "eponine", "name": "Eponine", "sex": "female", "accent": "British"},
    ),
}
# Where each engine keeps its voice in the config.
VOICE_KEYS = {"kokoro-realtime": "kokoro.voice", "pocket-tts": "pocket_tts.voice"}
# Headroom left on disk after a pull. A judgment, not a measurement.
DISK_MARGIN_GB = 4.0
# llama3.2:3b holds about 4 GB while loaded; below 8 GB of memory that is
# half the machine, so the smaller model is suggested instead. Also a judgment.
DEFAULT_MODEL_MIN_RAM_GB = 8

HELLO = "Good day. Hobson, at your service."
IDLE_EXIT_SECONDS = 1800
MAX_BODY = 64 * 1024
STATIC = {"index.html", "wizard.css", "wizard.js", "city.js", "lines.json", "voice/manifest.json"}
CLIP = re.compile(r"^voice/[0-9a-f]{16}\.m4a$")
# Any name Ollama accepts: library (mistral:7b), namespaced (user/model) or
# Hugging Face (hf.co/user/repo:Q4_K_M). It only ever goes to Ollama's API,
# as JSON, but the page is outside input all the same.
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")

EXIT_OK, EXIT_FAILED, EXIT_NO_GUI, EXIT_CLOSED = 0, 1, 3, 10


# ── Pure rules ─────────────────────────────────────────────────────────────

def installed(model, names):
    """Whether Ollama has `model`, whose tag list may spell it name:latest."""
    return model in names or (":" not in model and f"{model}:latest" in names)


def recommend_model(ram_gb, disk_free_gb, names):
    """The model to suggest: one already here first, then what fits. None
    means no brain: nothing is pulled."""
    if installed(DEFAULT_MODEL, names):
        return DEFAULT_MODEL
    for m in MODELS:
        if installed(m["id"], names):
            return m["id"]
    if ram_gb >= DEFAULT_MODEL_MIN_RAM_GB and disk_free_gb >= 2.0 + DISK_MARGIN_GB:
        return DEFAULT_MODEL
    if disk_free_gb >= 1.3 + DISK_MARGIN_GB:
        return "llama3.2:1b"
    return None


def recommend(facts):
    """Express mode's answers for this Mac, and a reason for each.

    Nothing heavy that the Mac cannot do unaided: Kokoro only with uv already
    here (installing uv means running astral.sh's script, which is Custom's
    call to make), a model only with Ollama here or Homebrew to install it.
    The decider is left unanswered: it is always asked.
    """
    ollama = facts["ollama"]
    ram, disk = facts["ram_gb"], facts["disk_free_gb"]
    why = {}

    model = None
    if ollama["installed"] or facts["brew"]:
        model = recommend_model(ram, disk, ollama["models"])
    if model is None:
        why["model"] = ("no Ollama, and no Homebrew to install it" if not (ollama["installed"] or facts["brew"])
                        else f"{disk:g} GB free: too little for a model")
    elif installed(model, ollama["models"]):
        why["model"] = "already on this Mac"
    elif not ollama["installed"]:
        why["model"] = f"installs Ollama with Homebrew, then pulls {model}"
    elif model == DEFAULT_MODEL:
        why["model"] = f"the tested default; fits {ram:g} GB with room to spare"
    else:
        why["model"] = f"{ram:g} GB of memory: the smallest model"

    # Never pocket-tts: it is the heavier, experimental engine (about 1 GB
    # with PyTorch, a slow first load), a choice for Custom to make.
    if facts["uv"] and model and disk >= 1.0:
        engine = "kokoro-realtime"
        why["engine"] = "uv is here, so about 340 MB buys a natural voice"
    else:
        engine = "say"
        why["engine"] = ("the built-in voice: Kokoro needs uv, which Custom can install" if not facts["uv"]
                         else "the built-in voice: Kokoro's phrases need a brain")

    camera = facts["presence"]["camera"]
    sensor = facts["presence"]["built"] or facts["presence"]["swiftc"]
    if not sensor:
        mode, why["presence"] = "signals", "no sensor without the Command Line Tools"
    elif camera in ("denied", "restricted"):
        mode, why["presence"] = "signals", "the camera is denied: keyboard, lock and calls"
    elif camera == "authorized":
        mode, why["presence"] = "auto", "the camera is already allowed; one glance, only before speaking"
    else:
        mode, why["presence"] = "auto", "one glance, only before speaking; macOS asks for the camera at the last step"

    devices = facts["phone"]["devices"]
    why["phone"] = ("a phone is connected: set it up in Custom (it needs a flip test)" if devices
                    else "no Android phone seen over adb")
    why["personality"] = "courteous, precise, faintly synthetic"
    why["events"] = "finishes, approvals and waits; no running commentary"

    return {
        "engine": engine,
        "voice": None,
        "personality": "hobson",
        "events": ["stop", "permission", "notification"],
        "verbosity": "normal",
        "model": model or "none",
        "decider": None,
        "presence": mode,
        "greetings": True,
        "phone": None,
        "why": why,
    }


def answers_from_config(config):
    """The answers a config (as load_config() returns it) already amounts to."""
    p = config.get("presence") or {}
    engine = config.get("engine")
    voice = _get(config, VOICE_KEYS[engine]) if engine in VOICE_KEYS else None
    return {
        "engine": engine,
        # A voice not in the catalogue (a clone of your own) is shown as
        # yours and left alone: None writes nothing.
        "voice": voice if voice_known(engine, voice) else None,
        "voice_custom": os.path.basename(str(voice)) if voice and not voice_known(engine, voice) else None,
        "personality": config.get("personality"),
        "events": list(config.get("events") or []),
        "verbosity": (config.get("commentary") or {}).get("verbosity", "normal"),
        "model": (config.get("ollama") or {}).get("model", DEFAULT_MODEL),
        "decider": (config.get("decider") or {}).get("backend", "local"),
        "presence": p.get("mode", "auto") if p.get("enabled", True) else "off",
        "greetings": bool(p.get("greetings", True)),
        "phone": p.get("phone"),
    }


def voice_known(engine, voice):
    return any(v["id"] == voice for v in VOICES.get(engine, ()))


def clean_answers(raw):
    """The page's answers, with anything unknown dropped. The page is ours,
    but it is also the only input this process takes from outside."""
    raw = raw if isinstance(raw, dict) else {}
    a = {}
    if raw.get("engine") in ENGINES:
        a["engine"] = raw["engine"]
        # Only a catalogue id, never a path: the page cannot point the
        # config at a file.
        if voice_known(a["engine"], raw.get("voice")):
            a["voice"] = raw["voice"]
    personalities = set(personality_ids())
    if raw.get("personality") in personalities:
        a["personality"] = raw["personality"]
    if isinstance(raw.get("events"), list):
        events = [e for e in EVENTS if e in raw["events"]]
        if events:
            a["events"] = events
    if raw.get("verbosity") in VERBOSITIES:
        a["verbosity"] = raw["verbosity"]
    model = raw.get("model")
    if isinstance(model, str) and (model == "none" or MODEL_NAME.match(model)):
        a["model"] = model
    if raw.get("decider") in ("local", "jev"):
        a["decider"] = raw["decider"]
    if raw.get("presence") in PRESENCE_MODES:
        a["presence"] = raw["presence"]
    if isinstance(raw.get("greetings"), bool):
        a["greetings"] = raw["greetings"]
    if isinstance(raw.get("save_env_key"), bool):
        a["save_env_key"] = raw["save_env_key"]
    # Only an answer that is there: a missing one must not read as "no
    # phone", which would take away a switch already set up.
    phone = raw.get("phone", "")
    if phone is None or (isinstance(phone, str) and phone.strip() and len(phone) < 200):
        a["phone"] = phone.strip() if isinstance(phone, str) else None
    return a


def desired_settings(answers, jev_verified, device_count):
    """Dotted config key -> the value the answers ask for. Only keys the
    answers speak to: a section left unanswered is left alone."""
    want = {}
    for key in ("engine", "personality", "events"):
        if key in answers:
            want[key] = answers[key]
    if answers.get("voice") and answers.get("engine") in VOICE_KEYS:
        want[VOICE_KEYS[answers["engine"]]] = answers["voice"]
    if "verbosity" in answers:
        want["commentary.verbosity"] = answers["verbosity"]
    if answers.get("model") not in (None, "none"):
        want["ollama.model"] = answers["model"]
    if answers.get("decider") is not None:
        want["decider.backend"] = "jev" if answers["decider"] == "jev" and jev_verified else "local"
    if "presence" in answers:
        if answers["presence"] == "off":
            want["presence.enabled"] = False
        else:
            want["presence.enabled"] = True
            want["presence.mode"] = answers["presence"]
    if "greetings" in answers:
        want["presence.greetings"] = answers["greetings"]
    if "phone" in answers:
        phone = answers["phone"]
        # One phone: "auto" follows it even if its wireless serial changes.
        want["presence.phone"] = None if not phone else ("auto" if device_count == 1 else phone)
    return want


_MISSING = object()


def _get(tree, dotted, default=_MISSING):
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def apply_settings(user_config, desired, defaults=None):
    """The user config with `desired` applied, and what changed.

    A value equal to its default is removed rather than written, so the
    default keeps flowing from DEFAULT_CONFIG; a different one is set.
    Returns (new_config, changes); each change is {"key", "value", "op"}
    with op "set" or "unset". `user_config` is not modified.
    """
    defaults = home.DEFAULT_CONFIG if defaults is None else defaults
    config = json.loads(json.dumps(user_config if isinstance(user_config, dict) else {}))
    changes = []
    for dotted, value in desired.items():
        default = _get(defaults, dotted)
        current = _get(config, dotted)
        parts = dotted.split(".")
        if default is not _MISSING and value == default:
            if current is _MISSING:
                continue
            parent = config
            for part in parts[:-1]:
                parent = parent[part]
            del parent[parts[-1]]
            if len(parts) == 2 and not config[parts[0]]:
                del config[parts[0]]
            changes.append({"key": dotted, "value": value, "op": "unset"})
        elif current != value:
            parent = config
            for part in parts[:-1]:
                if not isinstance(parent.get(part), dict):
                    parent[part] = {}
                parent = parent[part]
            parent[parts[-1]] = value
            changes.append({"key": dotted, "value": value, "op": "set"})
    return config, changes


def defaults_kept(config, defaults=None):
    """How many settings `config` leaves to DEFAULT_CONFIG."""
    defaults = home.DEFAULT_CONFIG if defaults is None else defaults
    kept = 0
    for key, value in defaults.items():
        if isinstance(value, dict):
            section = config.get(key) if isinstance(config.get(key), dict) else {}
            kept += sum(1 for sub in value if sub not in section)
        elif key not in config:
            kept += 1
    return kept


def tasks_for(answers, facts, key_pending):
    """What COMMIT will do, in order. Each is {"id", "label", "detail"}."""
    tasks = []
    engine = answers.get("engine")
    ready = {e["id"]: e["ready"] for e in facts["engines"]}
    if engine == "kokoro-realtime" and not ready.get(engine):
        if not facts["uv"]:
            tasks.append({"id": "uv", "label": "Install uv", "detail": "astral.sh's installer, into ~/.local/bin"})
        tasks.append({"id": "kokoro", "label": "Install Kokoro", "detail": "a venv with kokoro-onnx, then 120 MB of model and voices: about 340 MB"})
    if engine == "pocket-tts" and not ready.get(engine):
        if not facts["uv"]:
            tasks.append({"id": "uv", "label": "Install uv", "detail": "astral.sh's installer, into ~/.local/bin"})
        tasks.append({"id": "pocket", "label": "Install Pocket TTS",
                      "detail": "a venv with PyTorch, then 240 MB of weights: about 1 GB"})
    if engine == "chatterbox" and not ready.get(engine):
        tasks.append({"id": "chatterbox", "label": "Chatterbox: finish by hand",
                      "detail": "hobson setup chatterbox: a voice sample, then about two hours of phrases"})
    model = answers.get("model")
    if model and model != "none":
        if not facts["ollama"]["installed"]:
            tasks.append({"id": "ollama", "label": "Install Ollama",
                          "detail": "brew install ollama, then start it" if facts["brew"]
                          else "no Homebrew: get it from ollama.com"})
        if not installed(model, facts["ollama"]["models"]):
            size = next((m["disk_gb"] for m in MODELS if m["id"] == model), None)
            source = "Hugging Face" if model.startswith("hf.co/") else "ollama.com"
            tasks.append({"id": "pull", "label": f"Pull {model}",
                          "detail": f"{size:g} GB from {source}" if size else f"from {source}; its size shows once it starts"})
    mode = answers.get("presence")
    if mode and mode != "off" and not facts["presence"]["built"] and facts["presence"]["swiftc"]:
        tasks.append({"id": "sensor", "label": "Build the presence sensor", "detail": "swiftc, a few seconds"})
    if mode in ("auto", "continuous") and facts["presence"]["camera"] in ("not-determined", ""):
        tasks.append({"id": "camera", "label": "Ask macOS for the camera", "detail": "a dialog from Hobson Presence"})
    if key_pending:
        tasks.append({"id": "key", "label": "Save the OpenRouter key", "detail": "~/.claude/hobson.env, mode 600"})
    tasks.append({"id": "config", "label": "Write settings", "detail": "~/.claude/hobson.json"})
    tasks.append({"id": "hooks", "label": "Refresh Claude Code hooks", "detail": "~/.claude/settings.json"})
    tasks.append({"id": "hello", "label": "Say hello", "detail": "through the voice you chose"})
    return tasks


def unseen(state, config_exists):
    """Sections this install has not been shown. An install older than the
    wizard (config, no setup record) was asked LEGACY_SEEN by the installer."""
    seen = set(state.get("seen") or [])
    if not state and config_exists:
        seen = set(LEGACY_SEEN)
    return [s for s in SECTIONS if s not in seen]


def parse_adb_devices(text):
    """`adb devices -l` -> [{"serial", "model", "transport"}]: phones that are
    ready, as the sensor's PhoneSwitch sees them. No emulators (there is
    nothing to turn over), and a wireless phone listed twice, by address and
    by mDNS name, is kept once, by the name, which survives a new port."""
    devices = []
    for line in (text or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2 or fields[1] != "device" or fields[0].startswith("emulator-"):
            continue
        serial = fields[0]
        info = dict(f.split(":", 1) for f in fields[2:] if ":" in f)
        wireless = "_adb-tls-connect" in serial or ":" in serial
        devices.append({"serial": serial, "model": info.get("model", "").replace("_", " ") or "Android",
                        "transport": "wireless" if wireless else "usb"})
    named = {d["model"] for d in devices if "_adb-tls-connect" in d["serial"]}
    return [d for d in devices
            if not (d["model"] in named and ":" in d["serial"] and "_adb-tls-connect" not in d["serial"])]


def set_env_value(text, key, value):
    """KEY=value set in the text of an env file, other lines untouched."""
    lines = [ln for ln in (text or "").splitlines()
             if ln.partition("=")[0].strip() != key]
    lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


# ── State files ────────────────────────────────────────────────────────────

def _write_json_atomic(path, data, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_user_config():
    """hobson.json as written, not merged over the defaults. {} if absent."""
    try:
        with open(home.config_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_setup_state():
    try:
        with open(home.setup_state_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_seen(sections):
    state = load_setup_state()
    seen = sorted(set(state.get("seen") or []) | set(sections))
    _write_json_atomic(home.setup_state_file(),
                       {"version": SETUP_VERSION, "seen": seen, "updated": int(time.time())})


def save_key(key):
    """OPENROUTER_API_KEY into hobson.env, mode 600 from the first byte."""
    path = home.env_file()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(set_env_value(text, "OPENROUTER_API_KEY", key))
    os.replace(tmp, path)


# ── Probe ──────────────────────────────────────────────────────────────────

def _run(argv, timeout=10):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _which(name, *extra):
    for candidate in (shutil.which(name),) + extra:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def _uv():
    return _which("uv", os.path.expanduser("~/.local/bin/uv"), os.path.expanduser("~/.cargo/bin/uv"))


def _brew():
    return _which("brew", "/opt/homebrew/bin/brew", "/usr/local/bin/brew")


def _ollama_bin():
    return _which("ollama", "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
                  "/Applications/Ollama.app/Contents/Resources/ollama")


def _ollama_get(config, path, timeout=2.0):
    url = (config.get("ollama") or {}).get("url") or home.DEFAULT_CONFIG["ollama"]["url"]
    try:
        with urlopen(url.rstrip("/") + path, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (URLError, OSError, ValueError):
        return None


def ollama_facts(config):
    version = _ollama_get(config, "/api/version")
    tags = _ollama_get(config, "/api/tags") if version else None
    names = [m.get("name") for m in (tags or {}).get("models") or [] if m.get("name")]
    return {"installed": bool(_ollama_bin() or version), "running": bool(version),
            "version": (version or {}).get("version"), "models": names}


def engine_ready(engine, root=None):
    root = root or home.ROOT
    if engine == "say":
        return True
    if engine == "kokoro-realtime":
        venv = os.path.join(root, "venvs", "kokoro", "bin", "python3")
        onnx = "kokoro-v1.0.int8.onnx"
        return os.path.exists(venv) and any(os.path.exists(os.path.join(d, onnx))
                                            for d in (os.path.join(root, "models"), home.path("models")))
    if engine == "pocket-tts":
        venv = os.path.join(root, "venvs", "pocket-tts", "bin", "python3")
        return os.path.exists(venv) and os.path.isdir(pocket_weights_dir())
    if engine == "chatterbox":
        return os.path.exists(os.path.join(root, "venvs", "chatterbox", "bin", "python3"))
    return False


def pocket_weights_dir():
    """Where Hugging Face keeps Pocket TTS once it has been downloaded."""
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"), "hub")
    return os.path.join(hub, "models--kyutai--pocket-tts-without-voice-cloning")


def personality_ids():
    base = os.path.join(home.ROOT, "scripts", "personalities")
    try:
        return sorted(d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "personality.json")))
    except OSError:
        return []


def personalities():
    out = []
    for pid in personality_ids():
        try:
            with open(os.path.join(home.ROOT, "scripts", "personalities", pid, "personality.json"),
                      encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        done = ((data.get("templates") or {}).get("categories") or {}).get("done") or {}
        sample = " ".join(x[0] for x in (done.get("lead"), done.get("tail")) if x) or HELLO
        out.append({"id": pid, "name": data.get("display_name") or pid, "desc": data.get("description", ""),
                    "sample": sample, "voice": data.get("say_voice") or "Daniel"})
    # Hobson first: he is the default, and the page shows them in this order.
    return sorted(out, key=lambda p: (p["id"] != "hobson", p["id"]))


def key_facts():
    """Where an OpenRouter key was found, and its last four characters."""
    source = "env" if os.environ.get("OPENROUTER_API_KEY") else None
    key = decider._find_key()
    if key and not source:
        source = "file"
    return {"source": source, "tail": key[-4:]} if key else None


def adb_devices(adb):
    if not adb:
        return []
    r = _run([adb, "devices", "-l"], timeout=6)
    return parse_adb_devices(r.stdout) if r and r.returncode == 0 else []


def phone_facts(config):
    adb = presence.find_adb(presence.settings(config))
    return {"adb": adb, "devices": adb_devices(adb)}


def hooks_state():
    r = _run([sys.executable, os.path.join(home.ROOT, "scripts", "settings-merge.py"), "--check"], timeout=10)
    return "current" if r and r.returncode == 0 else "missing"


def _ram_gb():
    r = _run(["sysctl", "-n", "hw.memsize"], timeout=3)
    try:
        return round(int(r.stdout.strip()) / 2 ** 30)
    except (AttributeError, ValueError):
        return 0


def probe():
    config = home.load_config()
    config_exists = os.path.isfile(home.config_file())
    built = presence.helper_built()
    facts = {
        "host": socket.gethostname().split(".")[0],
        "arch": platform.machine(),
        "macos": platform.mac_ver()[0] or "?",
        "ram_gb": _ram_gb(),
        "disk_free_gb": round(shutil.disk_usage(os.path.expanduser("~")).free / 2 ** 30),
        "python": platform.python_version(),
        "uv": bool(_uv()),
        "brew": bool(_brew()),
        "hooks": hooks_state(),
        "ollama": ollama_facts(config),
        "engines": [{"id": e, "ready": engine_ready(e)} for e in ENGINES],
        "personalities": personalities(),
        "models": [dict(m) for m in MODELS],
        "voices": {engine: [dict(v) for v in voices] for engine, voices in VOICES.items()},
        "jev": {"key": key_facts(), "model": (config.get("decider") or {}).get("model", decider.DEFAULT_MODEL),
                "cost_per_call": 0.000017},
        "presence": {"built": built, "swiftc": bool(shutil.which("swiftc")),
                     "camera": presence.camera_status() if built else ""},
        "phone": phone_facts(config),
    }
    rec = recommend(facts)
    facts["recommend"] = rec
    facts["existing"] = answers_from_config(config) if config_exists else None
    facts["seen"] = [s for s in SECTIONS if s not in unseen(load_setup_state(), config_exists)]
    return facts


# ── Actions ────────────────────────────────────────────────────────────────

def speak(text, personality=None):
    """Through macOS `say`, in the personality's voice: an audition, not
    Hobson speaking, so it is never held for presence or muted."""
    text = str(text or "")[:240]
    if not text:
        return
    voice = next((p["voice"] for p in personalities() if p["id"] == personality), "Daniel")
    try:
        subprocess.Popen(["say", "-v", voice, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def read_phone(serial, config=None):
    """Face up or down, for a phone that may not be configured yet."""
    config = home.load_config() if config is None else config
    binary = os.path.join(presence.app_path(), "Contents", "MacOS", "HobsonPresence")
    adb = presence.find_adb(presence.settings(config))
    if not serial or not adb or not os.path.exists(binary):
        return {"phone": "unreachable"}
    r = _run([binary, "--read-phone", "--phone", serial, "--adb", adb], timeout=15)
    try:
        data = json.loads(r.stdout)
    except (AttributeError, ValueError):
        return {"phone": "unreachable"}
    return {"phone": data.get("phone") or "unreachable", "serial": data.get("serial"),
            "camera_allowed": data.get("camera_allowed")}


def hook_python():
    """The interpreter the hooks run with now, else this one."""
    try:
        with open(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or home.state_dir(), "settings.json"),
                  encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return sys.executable
    for groups in (settings.get("hooks") or {}).values():
        for group in groups or []:
            for hook in group.get("hooks") or []:
                command = hook.get("command") or ""
                if "scripts/hobson.py" in command:
                    try:
                        return shlex.split(command)[0]
                    except ValueError:
                        pass
    return sys.executable


class Wizard:
    """What one run of the wizard holds between requests."""

    def __init__(self, python=None):
        self.token = secrets.token_urlsafe(24)
        self.python = python or hook_python()
        self.facts = None
        self.verified_key = None     # the key that passed, typed or found; never sent back to the page
        self.verified_source = None  # "typed", "env" or "file"
        self.jev_verified = False
        self.applied = None        # None: not yet; True/False: how it went
        self.done = threading.Event()
        self.busy = threading.Lock()
        self.last_request = time.time()

    def get_facts(self, fresh=False):
        if fresh or self.facts is None:
            self.facts = probe()
        return self.facts

    # -- the page's calls --

    def jev_test(self, body):
        typed = str(body.get("key") or "").strip()
        key = typed or decider._find_key()
        result = decider.check_key(key)
        if result.get("ok"):
            self.jev_verified = True
            self.verified_key = key
            self.verified_source = "typed" if typed else (key_facts() or {}).get("source")
        return result

    def plan(self, answers):
        facts = self.get_facts()
        devices = len(facts["phone"]["devices"])
        desired = desired_settings(answers, self.jev_verified, devices)
        new_config, changes = apply_settings(read_user_config(), desired)
        writes = [{"key": c["key"], "value": c["value"]} if c["op"] == "set"
                  else {"key": c["key"], "value": "back to its default", "note": True} for c in changes]
        if answers.get("decider") == "jev" and not self.jev_verified:
            writes.append({"key": "decider.backend", "value": "stays local: no key passed", "note": True})
        return {"writes": writes, "defaults_kept": defaults_kept(new_config),
                "secrets": ["OPENROUTER_API_KEY → ~/.claude/hobson.env"] if self.key_pending(answers) else [],
                "tasks": tasks_for(answers, facts, self.key_pending(answers))}

    def key_pending(self, answers):
        """Whether COMMIT saves the key to hobson.env: one typed into the
        page, or one found only in this shell's environment, which hooks
        started from the desktop app or an IDE do not inherit."""
        if answers.get("decider") != "jev" or not self.verified_key:
            return False
        if self.verified_source == "typed":
            return True
        return self.verified_source == "env" and answers.get("save_env_key", True)

    def apply(self, answers, emit):
        """Run the plan's tasks, emitting {"task", "state", ...} per step and
        {"done": True, "ok": ...} at the end. A failed task is reported and
        the rest still run: settings are worth writing even if a pull failed."""
        facts = self.get_facts()
        ok = True
        for task in tasks_for(answers, facts, self.key_pending(answers)):
            emit({"task": task["id"], "state": "run"})
            try:
                state, detail = getattr(self, "_task_" + task["id"])(answers, facts, emit)
            except Exception as exc:  # a task must never take the server down with it
                state, detail = "fail", f"{type(exc).__name__}: {exc}"
            if state == "fail":
                ok = False
            log_record.write(f"[setup] {task['id']} -> {state}" + (f" ({detail})" if detail and state != "ok" else ""))
            emit({"task": task["id"], "state": state, "detail": detail})
        self.applied = ok
        emit({"done": True, "ok": ok})

    # -- tasks: each returns (state, detail) --

    def _stream(self, argv, task, emit, env=None, timeout=1800):
        """Run argv, sending its latest output line to the page as the detail."""
        return run_streaming(argv, lambda line: emit({"task": task, "state": "run", "detail": line}),
                             env=env, timeout=timeout)

    def _env_with_uv(self):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.cargo/bin"),
                                       env.get("PATH", "")])
        return env

    def _task_uv(self, answers, facts, emit):
        return self._stream(["/bin/bash", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], "uv", emit,
                            timeout=600)

    def _task_kokoro(self, answers, facts, emit):
        state, detail = self._stream([os.path.join(home.ROOT, "hobson"), "setup", "kokoro"], "kokoro", emit,
                                     env=self._env_with_uv())
        if state == "ok" and not engine_ready("kokoro-realtime"):
            return "fail", "installed, but the models did not download: hobson setup kokoro"
        return state, detail

    def _task_pocket(self, answers, facts, emit):
        """The venv CLAUDE.md used to leave to you, then one model load, so
        the weights are here before the first hook needs them."""
        return install_pocket(lambda line: emit({"task": "pocket", "state": "run", "detail": line}),
                              env=self._env_with_uv(), voice=answers.get("voice") or VOICES["pocket-tts"][0]["id"])

    def _task_chatterbox(self, answers, facts, emit):
        return "skip", "run hobson setup chatterbox when you have a voice sample"

    def _task_ollama(self, answers, facts, emit):
        brew = _brew()
        if not brew:
            return "fail", "no Homebrew: install Ollama from ollama.com"
        state, detail = self._stream([brew, "install", "ollama"], "ollama", emit, timeout=1200)
        if state != "ok":
            return state, detail
        self._stream([brew, "services", "start", "ollama"], "ollama", emit, timeout=60)
        for _ in range(40):
            if ollama_facts(home.load_config())["running"]:
                return "ok", ""
            time.sleep(0.5)
        return "fail", "installed, but it did not start: open the Ollama app"

    def _task_pull(self, answers, facts, emit):
        """Ollama's /api/pull streams {status, digest, total, completed}; the
        progress bar is the sum of every layer seen so far."""
        config = home.load_config()
        url = ((config.get("ollama") or {}).get("url") or home.DEFAULT_CONFIG["ollama"]["url"]).rstrip("/")
        body = json.dumps({"model": answers["model"], "name": answers["model"], "stream": True}).encode()
        req = Request(url + "/api/pull", data=body, headers={"Content-Type": "application/json"})
        layers = {}
        last_emit = 0.0
        try:
            with urlopen(req, timeout=60) as resp:
                for raw in resp:
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    if ev.get("error"):
                        return "fail", str(ev["error"])[:160]
                    if ev.get("digest") and ev.get("total"):
                        layers[ev["digest"]] = (ev.get("completed") or 0, ev["total"])
                    done = sum(c for c, _ in layers.values())
                    total = sum(t for _, t in layers.values())
                    if time.time() - last_emit > 0.25 and total:
                        last_emit = time.time()
                        emit({"task": "pull", "state": "run", "progress": done / total,
                              "detail": f"{done / 1e9:.2f} / {total / 1e9:.2f} GB"})
                    if ev.get("status") == "success":
                        return "ok", ""
        except (URLError, OSError) as exc:
            return "fail", f"Ollama did not answer ({type(exc).__name__})"
        return "fail", "the pull ended without finishing"

    def _task_sensor(self, answers, facts, emit):
        return self._stream([os.path.join(home.ROOT, "scripts", "build-presence.sh")], "sensor", emit, timeout=300)

    def _task_camera(self, answers, facts, emit):
        result = presence.look_now(request_permission=True)
        if result.get("camera") == "authorized":
            facts["presence"]["camera"] = "authorized"
            return "ok", ""
        # No camera: presence falls back to what it can sense without one.
        answers["presence"] = "signals"
        return "skip", "no camera access: presence uses signals only"

    def _task_key(self, answers, facts, emit):
        save_key(self.verified_key)
        return "ok", ""

    def _task_config(self, answers, facts, emit):
        desired = desired_settings(answers, self.jev_verified, len(facts["phone"]["devices"]))
        new_config, changes = apply_settings(read_user_config(), desired)
        _write_json_atomic(home.config_file(), new_config)
        mark_seen(SECTIONS)
        return "ok", f"{len(changes)} change{'s' if len(changes) != 1 else ''}"

    def _task_hooks(self, answers, facts, emit):
        return self._stream([self.python, os.path.join(home.ROOT, "scripts", "settings-merge.py"),
                             "--install-dir", home.ROOT, "--python", self.python], "hooks", emit, timeout=60)

    def _task_hello(self, answers, facts, emit):
        # `hobson test "…"` speaks through the engine just configured: it
        # proves the voice works, where `say` would only prove macOS does.
        # Two minutes: a neural engine's daemon loads its model first.
        return self._stream([os.path.join(home.ROOT, "hobson"), "test", HELLO], "hello", emit, timeout=120)


def run_streaming(argv, on_line, env=None, timeout=1800):
    """Run argv, handing each output line to on_line. (state, detail)."""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, env=env)
    except OSError as exc:
        return "fail", str(exc)
    deadline = time.time() + timeout
    last = ""
    for line in proc.stdout:
        line = line.strip()
        if line:
            last = line[:160]
            on_line(last)
        if time.time() > deadline:
            proc.kill()
            return "fail", "timed out"
    proc.wait()
    return ("ok", "") if proc.returncode == 0 else ("fail", last or f"exit {proc.returncode}")


def install_pocket(on_line, env=None, voice="charles"):
    """venvs/pocket-tts with requirements-pocket-tts.txt, then the weights."""
    uv = shutil.which("uv", path=(env or os.environ).get("PATH")) or _uv()
    if not uv:
        return "fail", "uv is needed: curl -LsSf https://astral.sh/uv/install.sh | sh"
    venv = os.path.join(home.ROOT, "venvs", "pocket-tts")
    python = os.path.join(venv, "bin", "python3")
    if not os.path.exists(python):
        state, detail = run_streaming([uv, "venv", "--python", "3.13", venv], on_line, env=env, timeout=600)
        if state != "ok":
            return state, detail
    state, detail = run_streaming([uv, "pip", "install", "--python", python, "-r",
                                   os.path.join(home.ROOT, "requirements-pocket-tts.txt")], on_line, env=env)
    if state != "ok":
        return state, detail
    on_line("downloading the weights (about 240 MB)")
    warm = ("import sys\nfrom pocket_tts import TTSModel\nm = TTSModel.load_model(language='english')\n"
            "m.get_state_for_audio_prompt(sys.argv[1])\nprint('weights ready')\n")
    state, detail = run_streaming([python, "-c", warm, voice], on_line, env=env, timeout=1800)
    if state == "ok" and not engine_ready("pocket-tts"):
        return "fail", "installed, but the weights are not where Hobson looks for them"
    return state, detail


# ── Server ─────────────────────────────────────────────────────────────────

def make_handler(wizard):
    ui_dir = os.path.join(home.ROOT, "setup", "ui")

    class Handler(BaseHTTPRequestHandler):
        server_version = "HobsonSetup"

        def log_message(self, *args):
            pass  # the URL carries the token: nothing about requests is logged

        def _allowed_host(self):
            port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, status, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            token = self.headers.get("X-Hobson-Token") or ""
            return hmac.compare_digest(token.encode(), wizard.token.encode())

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}

        def do_GET(self):
            wizard.last_request = time.time()
            if not self._allowed_host():
                return self._send(403, {"error": "forbidden"})
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                if not self._authorized():
                    return self._send(403, {"error": "forbidden"})
                return self._api_get(url)
            name = url.path.lstrip("/") or "index.html"
            if name not in STATIC and not CLIP.match(name):
                return self._send(404, {"error": "not found"})
            try:
                with open(os.path.join(ui_dir, name), "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, {"error": "not found"})
            ctype = "audio/mp4" if name.endswith(".m4a") else mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, data, ctype)

        def _api_get(self, url):
            route = url.path[len("/api/"):]
            if route == "probe":
                return self._send(200, wizard.get_facts(fresh=True))
            if route == "phone":
                serial = (parse_qs(url.query).get("serial") or [""])[0]
                return self._send(200, read_phone(serial))
            if route == "phone/scan":
                facts = wizard.get_facts()
                facts["phone"] = phone_facts(home.load_config())
                return self._send(200, facts["phone"])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            wizard.last_request = time.time()
            if not self._allowed_host() or not self._authorized():
                return self._send(403, {"error": "forbidden"})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "bad request"})
            route = urlparse(self.path).path[len("/api/"):]
            if route == "speak":
                speak(body.get("text"), body.get("personality"))
                return self._send(200, {"ok": True})
            if route == "jev/test":
                return self._send(200, wizard.jev_test(body))
            if route == "presence/look":
                return self._send(200, presence.look_now(request_permission=bool(body.get("request_permission"))))
            if route == "phone/test":
                return self._send(200, {"ok": True})
            if route == "plan":
                return self._send(200, wizard.plan(clean_answers(body.get("answers"))))
            if route == "apply":
                return self._stream_apply(clean_answers(body.get("answers")))
            if route == "done":
                self._send(200, {"ok": True})
                wizard.done.set()
                return None
            self._send(404, {"error": "not found"})

        def _stream_apply(self, answers):
            if not wizard.busy.acquire(blocking=False):
                return self._send(409, {"error": "already running"})
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                def emit(event):
                    try:
                        self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
                        self.wfile.flush()
                    except OSError:
                        pass  # the window went away; the tasks still finish

                wizard.apply(answers, emit)
            finally:
                wizard.busy.release()

    return Handler


def gui_session():
    """A desktop to put a window on: not over SSH, and a GUI login session."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    r = _run(["launchctl", "managername"], timeout=3)
    return bool(r and r.stdout.strip() == "Aqua")


def serve(wizard, port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(wizard))
    server.daemon_threads = True
    # shutdown() waits out one poll: at the default 0.5s, closing the window
    # (and every server test) stalled half a second for nothing.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    def idle():
        while not wizard.done.wait(30):
            if time.time() - wizard.last_request > IDLE_EXIT_SECONDS and not wizard.busy.locked():
                wizard.done.set()
    threading.Thread(target=idle, daemon=True).start()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hobson's setup wizard")
    parser.add_argument("--python", help="the interpreter the hooks should run with")
    parser.add_argument("--browser", action="store_true", help="open in the default browser, not a window")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--install", choices=["pocket-tts"], help="install an engine, no window (hobson setup pocket-tts)")
    args = parser.parse_args(argv)

    if args.install == "pocket-tts":
        wizard = Wizard(python=args.python)
        state, detail = install_pocket(print, env=wizard._env_with_uv())
        print("Pocket TTS ready." if state == "ok" else f"Pocket TTS: {detail}")
        return EXIT_OK if state == "ok" else EXIT_FAILED

    if not gui_session():
        print("Hobson's setup is a window, and this session has no desktop to show it on "
              "(SSH, or no one logged in at the Mac). Run `hobson setup` at the Mac itself.")
        return EXIT_NO_GUI

    home.migrate_legacy_state()
    wizard = Wizard(python=args.python)
    server = serve(wizard, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={wizard.token}"

    app = os.path.join(home.ROOT, "build", "HobsonSetup.app")
    built = not args.browser and _run([os.path.join(home.ROOT, "scripts", "build-setup.sh")], timeout=300)
    if built and built.returncode == 0:
        # -W: this returns when the window closes. -n: a fresh instance, even
        # if a previous wizard window is somehow still open.
        _run(["open", "-n", "-W", app, "--args", "--url", url], timeout=24 * 3600)
        wizard.done.set()
    else:
        if not args.browser:
            print("Could not build the setup window (xcode-select --install); opening it in your browser.")
        webbrowser.open(url)
        print("Setup is open in your browser. It closes itself when you're done (Ctrl-C to stop).")
        try:
            wizard.done.wait()
        except KeyboardInterrupt:
            pass
    # A COMMIT still running when the window closed is let finish.
    with wizard.busy:
        pass
    server.shutdown()
    if wizard.applied is None:
        return EXIT_CLOSED
    return EXIT_OK if wizard.applied else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
