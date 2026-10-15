"""Base engine — shared logic for all hobson TTS engines.

Handles: logging, event filtering, lock/cooldown, template selection (with
cache-preference), commentary, and playback. Config, paths and the project
label live in home.py; reading a Stop in stop_outcome.py. Engine subclasses
only need to implement cached_audio() and backfill().
"""

import contextlib
import json
import sys
import os
import re
import subprocess
import random
import fcntl
import time
import hashlib
from abc import ABC, abstractmethod
from typing import NamedTuple

# scripts/ on the path, for bark_templates, phrase_gen and the rest.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import home  # noqa: E402
import kinds  # noqa: E402
import session_status  # noqa: E402
import log_record  # noqa: E402
from bark_templates import bark_hash  # noqa: E402,F401  (the cache key, defined once)
from announcement import fixed_line, format_tool_name, permission_line  # noqa: E402


# How long macOS `say` may take to render a phrase before it is given up.
SAY_RENDER_TIMEOUT = 15

# How long one model call may take. A cold Ollama load takes ~8s, and a
# shorter timeout cancels the load itself, so the next call is cold again.
# The hooks are async, so waiting blocks nobody.
OLLAMA_TIMEOUT = 20

# Chatty commentary templates — used when commentary.verbosity == "chatty".
# Keyed by lowercased tool name. {ctx} is filled from extract_context().
COMMENTARY_TEMPLATES = {
    "edit":      ["I'm editing {ctx}.", "Updating {ctx}.", "Tweaking {ctx}."],
    "write":     ["I'm writing {ctx}.", "Creating {ctx}.", "Drafting {ctx}."],
    "multiedit": ["I'm patching {ctx}.", "Editing {ctx}."],
    "bash":      ["I'm running {ctx}.", "Executing {ctx}.", "Kicking off {ctx}."],
    "agent":     ["I'm dispatching {ctx}.", "Spawning agent for {ctx}."],
    "task":      ["I'm dispatching {ctx}."],
    "read":      ["I'm reading {ctx}.", "Checking {ctx}."],
    "grep":      ["I'm searching for {ctx}."],
    "glob":      ["I'm looking for {ctx}."],
}

COMMENTARY_GENERIC = [
    "I'm using {tool}.",
    "I'll use {tool}.",
]

# Documented Claude Code notification_type values, mapped to a plain-English
# clause. Free-text `message` alone caused a live misclassification: a bare
# "Claude needs your permission" was handed to the model with no type and it
# inferred an error where none existed. Values not listed here (including
# future ones) fall back to the raw message.
NOTIFICATION_LABELS = {
    "permission_prompt": "waiting for your approval",
    "idle_prompt": "waiting for you — nothing to do until you reply",
    "agent_needs_input": "a background agent is waiting for you",
    "agent_completed": "a background agent finished",
    "auth_success": "signed in",
}


# A declaration: annotations, any number of modifiers, a declaring keyword,
# then the name -- past a Go receiver `(r *Repo)`, type parameters `<T>`, and
# an extension receiver `Foo.Companion.`, so the function is named rather
# than the type it extends. Not `val`: in Kotlin that is mostly a local
# inside the body being edited, which is not what the edit is about.
_DECL_MODIFIERS = (
    r"(?:(?:export|default|public|private|protected|internal|open|abstract|"
    r"final|static|override|suspend|inline|async|pub(?:\([a-z]+\))?|data|"
    r"sealed|enum|annotation|inner|lateinit|operator|infix|tailrec|external|"
    r"fileprivate|mutating|unsafe|value)\s+)*"
)
_SYMBOL_RE = re.compile(
    r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*" + _DECL_MODIFIERS
    + r"(?:def|class|fun|func|fn|function|const|let|var|struct|enum|interface|"
    r"object|trait|impl|type|protocol)\s+"
    r"(?:\([^)]*\)\s*)?(?:<[^>]*>\s*)?(?:[A-Za-z_]\w*(?:<[^>]*>)?\??\.)*"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)
_SHELL_FN_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{")

# Prose has sentences, not declarations: "let me explain" is not a `let`.
_PROSE_EXTS = {".md", ".markdown", ".txt", ".rst", ".adoc"}


def _code_gist(text, path=""):
    """Name the first symbol defined in a code blob, or None.

    Returns an identifier, never a source line: raw code read aloud is
    unlistenable, and every comparable tool that shipped it backed it out.

    Measured on 1,137 real Kotlin edits, the earlier one-modifier regex
    found a name for 23% (now 36%), and of those, 18 were the word "class"
    (every `enum class X`) and 46 the receiver type instead of the function.
    """
    if os.path.splitext(path or "")[1].lower() in _PROSE_EXTS:
        return None
    for line in (text or "").splitlines()[:40]:
        m = _SYMBOL_RE.match(line) or _SHELL_FN_RE.match(line)
        if m:
            return m.group(1)
    return None


def _line_delta(old, new):
    """'+4/-1 lines' when the counts differ, else None."""
    added = len((new or "").splitlines())
    removed = len((old or "").splitlines())
    if added == removed:
        return None
    return f"+{added}/-{removed} lines"


def extract_context(tool_name, tool_input):
    """Pull a short context string from tool_input for commentary."""
    if not tool_input or not isinstance(tool_input, dict):
        return None
    tool = tool_name.lower()
    if tool in ("edit", "write", "multiedit"):
        fp = tool_input.get("file_path", "")
        name = os.path.basename(fp) if fp else None
        body = tool_input.get("new_string") or tool_input.get("content") or ""
        detail = _code_gist(body, fp)
        if not detail and tool != "write":
            detail = _line_delta(tool_input.get("old_string"), body)
        if name and detail:
            return f"{name} ({detail})"
        return name or detail or None
    if tool == "bash":
        return (tool_input.get("description")
                or (tool_input.get("command", "")[:40])) or None
    if tool == "agent":
        return tool_input.get("description") or None
    if tool == "read":
        fp = tool_input.get("file_path", "")
        return os.path.basename(fp) if fp else None
    if tool in ("grep", "glob"):
        return tool_input.get("pattern") or None
    return None


# ── Project identification ─────────────────────────────────────────────────

def project_rate(label, spread=0.06, steps=5):
    """A stable playback-rate offset for a project label.

    Gives each project a recognisable sound without changing the configured
    voice — the live voice is a custom clone and must survive. Rate shifts
    pitch and tempo together, so keep the spread small. `steps` is odd so
    exactly one bucket lands on 1.0 and some project sounds untouched.
    """
    if not label:
        return 1.0
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % steps
    middle = steps // 2
    return round(1.0 + (bucket - middle) * (spread * 2 / (steps - 1)), 4)


# ── TTS text normalization ─────────────────────────────────────────────────

_TTS_EXT_MAP = {
    "kt": "K T", "py": "P Y", "js": "J S", "ts": "T S",
    "tsx": "T S X", "jsx": "J S X", "json": "JSON", "yaml": "YAML",
    "yml": "YAML", "xml": "X M L", "html": "H T M L", "css": "C S S",
    "scss": "S C S S", "md": "M D", "sh": "S H", "rb": "R B",
    "go": "Go", "rs": "R S", "cpp": "C plus plus", "swift": "Swift",
    "gradle": "Gradle", "toml": "TOML", "txt": "text", "csv": "C S V",
    "sql": "S Q L", "env": "E N V",
}

# Initialisms that TTS mispronounces when title-cased ("Kyc" -> "kick").
# Applied after de-camelling, so the standalone token is what gets matched.
_TTS_INITIALISMS = {
    "Kyc": "KYC", "Api": "API", "Url": "URL", "Uri": "URI", "Pr": "PR",
    "Id": "ID", "Ui": "UI", "Ux": "UX", "Tts": "TTS", "Json": "JSON",
    "Http": "HTTP", "Https": "HTTPS", "Sql": "SQL", "Css": "CSS",
    "Html": "HTML", "Cli": "CLI", "Sdk": "SDK", "Jwt": "JWT",
}

# Product names that must survive de-camelling intact. Protected by
# placeholder substitution before the CamelCase pass, restored after.
_TTS_PROPER_NOUNS = (
    "GitHub", "GitLab", "JavaScript", "TypeScript", "PostgreSQL", "GraphQL",
    "MacBook", "iOS", "macOS", "iPadOS", "OpenAI", "YouTube", "WebSocket",
    "OAuth", "JavaDoc", "PyPI", "npm", "ESLint",
)


def tts_normalize(text):
    """Normalize code-related terms in text for TTS pronunciation.

    Handles: URLs (keep hostname only), file paths (reduce to basename),
    file extensions (.kt → dot K T), version numbers (v2.1 → version 2 1),
    snake_case (underscores → spaces), CamelCase (insert spaces).
    Call before sending text to any TTS engine.
    """
    if not text:
        return text

    # Protect product names from the CamelCase pass below.
    protected = {}
    for i, noun in enumerate(_TTS_PROPER_NOUNS):
        token = f"\x00{i}\x00"
        if noun in text:
            protected[token] = noun
            text = text.replace(noun, token)

    # URLs: keep only the hostname
    text = re.sub(
        r'https?://([a-zA-Z0-9._-]+)(?:[:/][^\s]*)?',
        lambda m: m.group(1),
        text,
    )

    # Absolute/home file paths: reduce to basename.
    # Requires 3+ absolute components or ~ prefix to avoid false-positives.
    text = re.sub(
        r'(?:~(?:/[a-zA-Z0-9._-]+)+|(?:/[a-zA-Z0-9._-]+){3,})/([a-zA-Z0-9._-]+)',
        r'\1', text
    )

    # File extensions: word.ext → word dot EXT
    def _ext_sub(m):
        base, ext = m.group(1), m.group(2).lower()
        spoken = _TTS_EXT_MAP.get(ext, " ".join(ext.upper()))
        return f"{base} dot {spoken}"

    text = re.sub(r'\b([A-Za-z0-9_]+)\.([a-zA-Z]{1,6})\b', _ext_sub, text)

    # Version numbers: v2.1.0 → version 2 1 0
    text = re.sub(
        r'\bv(\d[\d.]*)\b',
        lambda m: "version " + m.group(1).replace(".", " "),
        text,
    )

    # Ticket numbers: APP-4005 -> stripped. Keep any trailing slug, which
    # carries the human task name ("APP-4005-dark-mode" -> "dark mode").
    text = re.sub(
        r'\b[A-Z]{2,6}-\d+(?:-([-\w]+))?\b',
        lambda m: (m.group(1) or "").replace("-", " "),
        text,
    )

    # Hex blobs (task/agent ids) are unspeakable — drop them.
    text = re.sub(r'\b[0-9a-f]{8,}\b', '', text)

    # snake_case: underscores between word chars → spaces
    text = re.sub(r'(?<=\w)_(?=\w)', ' ', text)

    # CamelCase/PascalCase: insert spaces before uppercase transitions
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)

    # Recase initialisms now that de-camelling has left them standalone.
    text = re.sub(
        r'\b([A-Za-z]{2,5})\b',
        lambda m: _TTS_INITIALISMS.get(m.group(1).capitalize(), m.group(1)),
        text,
    )

    for token, noun in protected.items():
        text = text.replace(token, noun)

    # Collapse whitespace left behind by stripped tickets and hex ids, and
    # tidy punctuation that lost its word.
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s+([.,!?])', r'\1', text)
    return text.strip()


class Snapshot(NamedTuple):
    """What a phrase source knows of the session, read once inside a
    transaction and used after it, while the model runs with no lock held."""
    context: str                    # build_session_context (a Stop's has the Task line)
    recent_voiced: list             # [phrase, ts] pairs, for the dedup guard


def _snapshot(state, task=None):
    from session_state import build_session_context
    return Snapshot(context=build_session_context(state, task=task),
                    recent_voiced=list(state.get("recent_voiced", [])))


class Clip(NamedTuple):
    """A phrase rendered to audio (BaseEngine.clip), ready to play."""
    path: str
    source: str     # how it was made, for the playback log line ("template-cache", "daemon-live", ...)
    cleanup: bool   # remove the file once played: say's temp AIFF, a daemon's WAV; never a cached clip


class BaseEngine(ABC):
    """Abstract base for TTS engines.

    Every phrase leaves through speak(), which holds the whole path -- the
    audience, the busy voice, the bark lock and cooldown, playback and the
    face. What an engine adds is how a phrase becomes audio: clip() (and
    render(), for a caller that plays its own), and warm() for one that
    needs warming.

    Subclasses must set:
        templates_module: "bark_templates"
        cache_dir: path to cache directory (or None)
        cache_ext: "wav" or None
        engine_name: short name for log lines (e.g. "kokoro-rt")

    Subclasses must implement:
        backfill(text): spawn background process to generate audio for cache miss
    """

    templates_module = "bark_templates"
    cache_dir = None
    cache_ext = None
    engine_name = "base"

    def __init__(self, config):
        # Every default comes from DEFAULT_CONFIG, merged here once, so no
        # read below carries a fallback of its own to drift from it.
        config = self.config = home.with_defaults(config)
        self.cooldown = config["cooldown"]
        # Config scale 0-10 mapped to afplay's 0.0-1.0 (1.0 = unity gain)
        self.volume = max(0.0, min(10.0, float(config["volume"]))) / 10.0
        self._templates = None
        # Personality say voice for macOS say fallback
        self._say_voice = self._load_say_voice(config)
        # Ollama config
        self._ollama_model = config["ollama"]["model"]
        self._ollama_url = config["ollama"]["url"]
        # Commentary config
        commentary_cfg = config["commentary"]
        self.commentary_cooldown = commentary_cfg["cooldown"]
        self.commentary_tools = set(t.lower() for t in commentary_cfg["tools"])
        verbosity = (commentary_cfg.get("verbosity") or "normal").lower()
        if verbosity not in ("terse", "normal", "chatty", "anomaly"):
            verbosity = "normal"
        self.commentary_verbosity = verbosity

    @staticmethod
    def _load_say_voice(config):
        """Get the macOS say voice from the active personality JSON."""
        personalities_dir = os.path.join(home.ROOT, "scripts", "personalities")
        name = config.get("personality", "hobson")
        path = os.path.join(personalities_dir, name, "personality.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("say_voice", "Daniel")
        except (FileNotFoundError, json.JSONDecodeError):
            return "Daniel"

    def voice_label(self):
        """The voice this engine speaks in, as `hobson status` shows it."""
        return self._say_voice

    def _log(self, msg):
        """Log with [engine_name] prefix for structured monitor output."""
        log_record.write(f"[{self.engine_name}] {msg}")

    def _afplay_args(self, path):
        """Build the afplay argv for one clip. Single source of truth.

        Four call sites used to assemble this independently, so anything that
        alters playback has to be added here or it reaches only some of them.

        Playback-rate identity is OFF by default and should stay off. `afplay
        -r` resamples, shifting pitch and tempo together like tape speed, and
        on speech even a 3% shift is audibly robotic — it moves the formants.
        It also silently degraded the macOS `say` fallback, because
        _render_say renders to AIFF and it plays through this same argv.
        Per-project identity belongs in voice selection (which the kokoro
        daemon supports per request), not in resampling the output.
        """
        args = ["afplay", "-v", str(self.volume)]
        identity = self.config["project_identity"]
        if identity["enabled"]:
            rate = project_rate(home.derive_project_label(),
                                spread=identity["spread"])
            if rate != 1.0:
                args += ["-r", str(rate), "-q", "1"]
        return args + [path]

    def play(self, path, phrase, kind, cleanup=False, project=None):
        """Play one clip, detached (it must survive the hook process exiting),
        then show it on the face. Every spoken phrase leaves through here:
        speak()'s, after its audience check, so a held or dropped phrase is
        never shown, and the clips `hobson say` and `hobson coach` render
        for themselves (render()). The
        voice goes first, and the face cannot stop it. `cleanup` removes the
        clip once played (say's temp AIFF, a daemon's WAV). `kind` is
        required (kinds.require). `project`: the session the phrase is
        about, for the face's corner, when it is not this one.

        It plays after whatever is playing now, from any session: the player
        is handed the voice lock and holds it until it exits (voice.py), so
        two clips never play at once. Returns the player."""
        import voice
        kinds.require(kind)
        turn = voice.take_turn()
        if turn is None:
            self._log("no turn on the voice, playing anyway")
        keep = () if turn is None else (turn,)
        args = self._afplay_args(path)
        try:
            if cleanup:
                import shlex
                played = " ".join(shlex.quote(a) for a in args)
                player = subprocess.Popen(["sh", "-c", f"{played}; rm -f {shlex.quote(path)}"],
                                          pass_fds=keep)
            else:
                player = subprocess.Popen(args, pass_fds=keep)
        finally:
            if turn is not None:
                os.close(turn)
        try:
            import face
            face.show(self.config, phrase, kind, path, project=project)
        except Exception as e:
            self._log(f"face failed ({e})")
        return player

    # ── Nudge (Task 7) ──────────────────────────────────────────────────

    def _maybe_start_nudge(self, subject):
        """Start a nudge for a session that is waiting on you (nudge.start),
        if nudges are on. Never breaks the caller, which runs inline in an
        async hook."""
        try:
            if not self.config["nudge"]["enabled"]:
                return
            import nudge
            project = home.derive_project_label()
            nudge.start(project, subject)
            self._log(f"nudge: spawned for {project!r}")
        except Exception as e:
            self._log(f"nudge: spawn failed ({e})")

    # ── Watchdog (Task 9) ───────────────────────────────────────────────

    def _maybe_start_watchdog(self, baseline=None):
        """Ensure a hang-detector is running for this project
        (nudge.start_watchdog), if the watchdog is on. Started on a
        commentary flush rather than on every event; never breaks the flush
        it rides on."""
        try:
            cfg = self.config["watchdog"]
            if not cfg["enabled"]:
                return
            import nudge
            project = home.derive_project_label()
            if nudge.start_watchdog(project, cfg["minutes"], baseline=baseline):
                self._log(f"watchdog: spawn requested for {project!r}")
        except Exception as e:
            self._log(f"watchdog: spawn failed ({e})")

    def _maybe_nudge_from_notification(self, hook_input):
        """Start a 'waiting' nudge when this Notification means the session
        is blocked on the user — idle_prompt / agent_needs_input are that
        signal already, free, with no timer and no inference required.

        Except idle_prompt after a turn that simply finished. It fires
        whenever the input sits idle, and on the first day nudges worked all
        16 followed a Stop classified "done" -- none a question -- so every
        finished task you walked away from started "nothing moves until you
        reply", which was not true. A question or a failure nudges; so does
        a turn whose category is unknown, since a failed classification must
        not quietly switch nudges off. agent_needs_input always nudges: a
        background agent that asks is blocked by definition.
        """
        ntype = session_status.nudge_type(hook_input)
        if not ntype:
            return
        if session_status.idle_after_settled_turn(hook_input):
            self._log("nudge: not started — the last turn finished, nothing is waiting on you")
            return
        self._maybe_start_nudge(NOTIFICATION_LABELS.get(ntype, ntype))

    def _permission_risk(self, hook_input):
        """Why this permission request is destructive (risk.permission_risk),
        or None. `rm -rf` and `ls` used to sound identical: the phrase was
        built from the tool name alone. May ask the decider, so never call
        it inside a session transaction. The log carries the reason, never
        the command."""
        if hook_input.get("hook_event_name") != "PermissionRequest":
            return None
        try:
            from risk import permission_risk
            return permission_risk(hook_input.get("tool_name"),
                                   hook_input.get("tool_input"), self.config)
        except Exception as e:
            self._log(f"[PermissionRequest] risk check failed ({e})")
            return None

    def _fixed_announcement(self, hook_input, state, risk=None):
        """For an event whose meaning is fixed -- a destructive permission
        request (`risk` from _permission_risk), a question dialog, a
        notification that the session is waiting on you -- the template to
        speak, chosen and recorded in `state`, or None if this is not one.

        Never the model: a fact the user must hear is not left to phrasing
        that can lose it. In ten days of real idle_prompt notifications the
        model mostly lost the meaning -- restated the last completion ("I
        finished the order history view.") or named the wrong wait -- and
        when its phrase repeated something recent the dedup guard silenced
        the announcement outright. Rotating past templates said recently
        keeps it from sounding canned; when all were said recently, one is
        spoken anyway -- being blocked again is news even in the same words.

        Chooses inside the caller's session transaction (announcement.fixed_line,
        which the silenced hook shares); the caller speaks (_announce) once
        it has committed.
        """
        from session_state import record_voiced, record_wait_said
        chosen = fixed_line(hook_input, state, self.templates, self._fresh_choice, risk)
        if chosen is None or chosen.phrase is None:
            return chosen
        record_voiced(state, chosen.phrase)
        if hook_input.get("hook_event_name") == "PermissionRequest":
            record_wait_said(state, hook_input.get("session_id"))
        return chosen

    def _announce(self, fixed):
        """Speak (or, for a silent one, just log) a fixed announcement."""
        if fixed.phrase is None:
            self._log(fixed.label)
            return
        self._log(f"{fixed.label} -> {fixed.phrase!r}")
        # A wait is never dropped by the cooldown: one recorded as said
        # (record_wait_said) must have been heard, or held for your return.
        self.speak(fixed.phrase, "waiting", cold_start=True)

    def _fresh_choice(self, candidates, state):
        """A candidate that is not a near-duplicate of anything said recently
        -- or any candidate, if every one was: the event is news even in the
        same words. One with cached audio first (_pick_prefer_cached)."""
        from phrase_gen import is_near_duplicate
        recent = state.get("recent_voiced", [])
        fresh = [c for c in candidates if not is_near_duplicate(c, recent)]
        return self._pick_prefer_cached(fresh or candidates)

    def _render_say(self, text):
        """`text` rendered by macOS `say` to a temp AIFF, or None."""
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            prefix="hobson-say-", suffix=".aiff", delete=False
        )
        tmp.close()
        say = subprocess.Popen(["say", "-v", self._say_voice, "-o", tmp.name, "--", text])
        try:
            rendered = say.wait(timeout=SAY_RENDER_TIMEOUT) == 0
        except subprocess.TimeoutExpired:
            say.kill()
            rendered = False
        if not rendered:
            self._log(f"say could not render -> {text!r}")
            with contextlib.suppress(OSError):
                os.remove(tmp.name)
            return None
        return tmp.name

    def render(self, text, timeout=None):
        """A clip of `text` to play, or None: the static engines render
        anything outside their templates with `say`. `timeout` is for the
        daemon engines' request."""
        return self._render_say(text)

    @property
    def templates(self):
        """Lazy-load the templates module."""
        if self._templates is None:
            import importlib
            self._templates = importlib.import_module(self.templates_module)
        return self._templates

    def cached_audio(self, text):
        """Return path to cached audio file if it exists, else None."""
        if not self.cache_dir or not self.cache_ext:
            return None
        path = os.path.join(self.cache_dir, f"{bark_hash(text)}.{self.cache_ext}")
        return path if os.path.isfile(path) else None

    @abstractmethod
    def backfill(self, text):
        """Spawn background process to generate audio for a cache miss."""
        pass

    def _pick_prefer_cached(self, candidates):
        """Pick randomly from cached candidates if any exist, else random from all."""
        if self.cache_dir:
            cached = [t for t in candidates if self.cached_audio(t)]
            if cached:
                return random.choice(cached)
        return random.choice(candidates)

    def pick(self, category):
        t = self.templates
        group = t.CATEGORIES.get(category, t.CATEGORIES["done"])
        if self.cache_dir:
            all_barks = [f"{l} {t_}" for l in group["lead"] for t_ in group["tail"]]
            return self._pick_prefer_cached(all_barks)
        return f"{random.choice(group['lead'])} {random.choice(group['tail'])}"

    def pick_permission(self, tool):
        t = self.templates
        if not self.cache_dir:
            return permission_line(t, tool)
        tool = format_tool_name(tool)
        all_barks = [
            f"{l} {a.format(tool=tool)}"
            for l in t.PERMISSION_LEADS for a in t.PERMISSION_ACTIONS
        ]
        cached = [b for b in all_barks if self.cached_audio(b)]
        if cached:
            return random.choice(cached)
        return self._pick_prefer_cached(t.GENERIC_PERMISSION_PHRASES)

    def pick_notification(self):
        return self._pick_prefer_cached(self.templates.NOTIFICATION_TEMPLATES)

    def _is_event_enabled(self, hook_input):
        """Check if this hook event is enabled in config. Returns False + logs if disabled."""
        event = hook_input.get("hook_event_name")
        if home.event_enabled(self.config, event):
            return True
        self._log(f"skipped (event {home.EVENT_KEYS[event]!r} disabled)")
        return False

    # ── Speaking: one path for every phrase ────────────────────────────

    def speak(self, phrase, kind, cold_start=False, project=None):
        """Say `phrase`, of kind `kind` (kinds.KINDS, required). Every phrase
        Hobson says leaves through here, from a hook or not, on every engine.
        False only when presence held it for your return or dropped it;
        True when it played, and when it was skipped (the voice busy for
        commentary, the bark lock or its cooldown) or could not be rendered,
        each of which is logged.

        `cold_start`: the phrase may wait for a cold daemon, and the bark
        lock's cooldown never drops it -- a Stop, and anything said because
        you asked or because something waits (a nudge, a briefing, an
        answer). Without it a phrase is dropped while another hook holds the
        bark lock or spoke within `cooldown` seconds, and a daemon that is
        down is started in the background for next time.

        It plays after whatever is playing now (play), so a phrase that is
        not dropped is heard in turn, not over another.

        `project`: the session the phrase is about, when it is not this one
        (another session's note): held under it and shown in the face's
        corner. A private kind's words never reach the log (kinds.guarded)."""
        phrase = self._for_audience(kinds.guarded(phrase, kind), kind, project)
        if phrase is None:
            return False
        if self._voice_busy_for(phrase, kind):
            return True
        if not self._claim_bark_lock(phrase, cold_start):
            self.warm()
            return True
        clip = None
        try:
            clip = self.clip(phrase, kind, cold_start)
            if clip is None:
                return True
            self.play(clip.path, phrase, kind, cleanup=clip.cleanup, project=project)
        except Exception as e:
            self._log(f"bark failed ({e}) -> {phrase!r}")
            if clip is not None and clip.cleanup:
                with contextlib.suppress(OSError):
                    os.remove(clip.path)
            return True
        self._log(log_record.playback(clip.source, phrase, kind))
        return True

    def _claim_bark_lock(self, phrase, cold_start):
        """Stamp the bark lock: when a hook last spoke. A `cold_start` phrase
        stamps it and goes on. Any other is skipped (False) while another
        hook holds the lock or one spoke within `cooldown` seconds. A lock
        that cannot be opened lets the phrase through."""
        try:
            with open(home.bark_lock(), "a+", encoding="utf-8") as fd:
                if not cold_start:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self._log(f"skipped (locked) -> {phrase!r}")
                        return False
                    fd.seek(0)
                    try:
                        if time.time() - float(fd.read().strip()) < self.cooldown:
                            self._log(f"skipped (cooldown) -> {phrase!r}")
                            return False
                    except ValueError:
                        pass
                fd.seek(0)
                fd.truncate()
                fd.write(str(time.time()))
        except OSError:
            pass
        return True

    def clip(self, phrase, kind, cold_start=False):
        """`phrase` as audio to play (a Clip), or None when it could not be
        rendered. The static engines' clip: the template cache first, else
        macOS `say` -- and on a cache miss a template (a kind neither said
        in passing nor Hobson's own talk) is backfilled for next time."""
        audio = self.cached_audio(phrase)
        if audio:
            return Clip(audio, "template-cache", False)
        path = self._render_say(phrase)
        if path is None:
            return None
        if not self.cache_dir:
            return Clip(path, "say", True)
        props = kinds.of(kind)
        if not (props.passing or props.own_talk or props.private):
            self.backfill(phrase)
        return Clip(path, "say-fallback: cache miss", True)

    def warm(self):
        """Get ready for the next phrase after one was skipped. Nothing to do
        for the static engines."""

    def _for_audience(self, phrase, kind, project=None):
        """The phrase to speak now, or None when presence held it for your
        return or dropped it (presence.route). `kind` is what it is about,
        one of kinds.KINDS, and required: a phrase with none used to count as
        news. speak() calls this once, so no engine or event can talk past
        an empty room. A failure here speaks: without presence,
        Hobson is as he was."""
        kinds.require(kind)
        try:
            import presence
            return presence.route(self.config, kind, phrase, project)
        except Exception as e:
            self._log(f"presence check failed ({e}), speaking")
            return phrase

    def _voice_busy_for(self, phrase, kind):
        """A phrase said in passing (commentary) is dropped while anything is
        playing (voice.busy): by the time its turn came it would be about
        work already done. Every other kind waits for its turn in play()."""
        if not kinds.of(kind).passing:
            return False
        import voice
        if not voice.busy():
            return False
        self._log(f"skipped (voice busy) -> {phrase!r}")
        return True

    # ── Commentary (PreToolUse) — shared across all engines ────────────

    def _acquire_commentary_lock(self, watched_cooldown=None, last_flush=0):
        """Acquire the commentary lock + check cooldown. Returns True on success.

        A watched session (focus) passes its own cooldown and its project's
        last flush time, and is paced on a clock of its own: the focus lock,
        which only watched flushes read and write, and its own last flush.
        Sharing the commentary timestamp, it flushed every 9s beside a busy
        unwatched session instead of every 4s; writing it, it kept every
        other session short of the 8s window until their batches went stale.
        """
        watched = watched_cooldown is not None
        cooldown = watched_cooldown if watched else self.commentary_cooldown
        try:
            with open(home.focus_lock() if watched else home.commentary_lock(),
                      "a+", encoding="utf-8") as fd:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd.seek(0)
                content = fd.read().strip()
                now = time.time()
                try:
                    if content and now - float(content) < cooldown:
                        return False
                except ValueError:
                    pass
                if watched and now - (last_flush or 0) < cooldown:
                    return False
                fd.seek(0); fd.truncate()
                fd.write(str(now))
                return True
        except OSError:
            return False

    def _pick_chatty_commentary(self, tool, tool_input):
        """Build a chatty commentary phrase from a template + extracted context."""
        tool_lower = (tool or "").lower()
        ctx = extract_context(tool, tool_input or {})
        templates = COMMENTARY_TEMPLATES.get(tool_lower)
        if templates and ctx:
            return random.choice(templates).format(ctx=tts_normalize(ctx))
        return random.choice(COMMENTARY_GENERIC).format(tool=format_tool_name(tool))

    def _is_subagent_event(self, hook_input):
        """True when this event came from a subagent rather than the main session.

        Claude Code stamps `agent_id`/`agent_type` on events originating in a
        subagent; main-session events carry neither. Measured: ~51 PreToolUse
        events per minute during background worker activity.
        """
        return bool(hook_input.get("agent_id"))

    def _handle_commentary(self, hook_input):
        """PreToolUse commentary — verbosity-aware dispatch shared by all engines.

        - terse  : LLM-gated with SKIP-heavy bias (current behavior)
        - normal : LLM-gated with relaxed bias (voice meaningful actions)
        - chatty : no LLM call; pick a template phrase and speak it
        """
        tool = hook_input.get("tool_name", "")
        if (tool or "").lower() not in self.commentary_tools:
            return

        if (self.config["commentary"]["suppress_subagents"]
                and self._is_subagent_event(hook_input)):
            self._log("[PreToolUse] skipped (subagent "
                      f"{hook_input.get('agent_type') or 'unknown'})")
            return

        verbosity = self.commentary_verbosity

        if verbosity == "chatty":
            # chatty speaks immediately on every call — no batching, so the
            # lock keeps its original job of gating speech directly.
            if not self._acquire_commentary_lock():
                self._log("[PreToolUse] skipped (commentary cooldown)")
                return
            tool_input = hook_input.get("tool_input", {})
            phrase = self._pick_chatty_commentary(tool, tool_input)
            self._log(f"[PreToolUse] chatty template -> {phrase!r}")
            self.speak(phrase, "commentary")
            return

        # terse / normal — append-to-queue must always succeed, so the lock
        # is acquired only once should_flush() opens the gate (inside
        # _handle_commentary_llm), never here.
        self._handle_commentary_llm(hook_input, verbosity)

    def _handle_commentary_llm(self, hook_input, verbosity):
        """Append this tool call to the pending batch; flush (LLM + speak)
        once the substance gate opens.

        Queueing must always succeed — the commentary lock is acquired only
        once should_flush() says the batch has earned an utterance, so it
        gates the flush/speak path, never the append path. Otherwise the
        lock would silently drop tool calls out of the batch instead of
        merely delaying the announcement of them.
        """
        try:
            import gate
            from phrase_gen import generate_or_skip
            from session_state import (
                transaction, mark_event,
                record_voiced, record_skipped,
                record_pending, should_flush, take_pending, pending_summary,
                record_flush_attempt,
                record_fingerprint, mark_repeat_announced, anomaly_reason,
                action_identity,
            )
        except ImportError as e:
            self._log(f"[PreToolUse] LLM gating unavailable ({e})")
            return

        project = home.derive_project_label()
        tool = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input") or {}
        context = extract_context(tool, tool_input)

        cfg = self.config["commentary"]
        watched = False
        if verbosity != "anomaly":
            try:
                import presence
                watched = presence.watched(self.config, project)
            except Exception as e:
                self._log(f"[PreToolUse] watched check failed ({e})")
        focus = self.config["focus"]
        pace = focus if watched else cfg
        min_calls = pace["min_tool_calls"]
        min_seconds = pace["min_seconds"]
        stale = cfg["pending_stale_seconds"]
        # Repetition is judged on the action itself (action_identity), not
        # on `context`, the short display string: two commands sharing a
        # description are two actions, not one repeated.
        identity = action_identity(tool, tool_input) if verbosity == "anomaly" else None

        # Short: queue the call, decide whether the batch has earned an
        # utterance, and take it. Returning inside commits too.
        with transaction(project) as state:
            # Every handled event marks the session alive — the watchdog
            # reads this to notice when nothing has moved for a while.
            mark_event(state)
            record_pending(state, tool, context)
            anomaly = None
            if verbosity == "anomaly":
                # Count/timer gates are ignored entirely in anomaly mode —
                # the only reasons to speak are: something is repeated, or a
                # context is being touched for the first time.
                record_fingerprint(state, tool, identity)
                anomaly = anomaly_reason(state, tool, context, cfg, identity=identity)
                if not anomaly:
                    self._log(f"[PreToolUse] queued (anomaly, nothing notable) {tool}: {context}")
                    return
            elif not should_flush(state, min_calls=min_calls, min_seconds=min_seconds):
                self._log(f"[PreToolUse] queued ({len(state['pending'])} pending) {tool}: {context}")
                return

            if not self._acquire_commentary_lock(
                    focus["cooldown"] if watched else None, state.get("last_flush_time")):
                # Batch has earned an utterance, but another flush spoke too
                # recently. Leave the queue intact — do not consume it — so
                # the next tool call gets another chance to flush it.
                self._log("[PreToolUse] flush ready but commentary locked; deferred")
                return

            # The flush is going ahead now — time the gate off this attempt,
            # not off whether it ends up producing speech.
            record_flush_attempt(state)
            if watched:
                self._log("[PreToolUse] flush (watched)")
            baseline = state["last_event_time"]
            items = take_pending(state, stale_seconds=stale)
            event_detail = pending_summary(items)
            if event_detail:
                if anomaly and not anomaly.startswith("first time"):
                    # Mark before generating, so a failed generation cannot
                    # loop: a repeat must not re-fire on every further
                    # occurrence of the same action.
                    mark_repeat_announced(state, tool, identity)
                session = _snapshot(state)

        self._maybe_start_watchdog(baseline=baseline)
        if not event_detail:
            self._log("[PreToolUse] flush found only stale items, dropped")
            return
        if anomaly:
            event_detail = f"{anomaly[0].upper()}{anomaly[1:]}. {event_detail}".strip()

        # phrase_gen has no "anomaly" prompt bias (it would fall back to the
        # SKIP-heavy terse rules) — by the time we've forced a flush here,
        # the gating decision is already made, so use the relaxed "normal"
        # bias for the phrasing call itself rather than risk the model
        # SKIPping something we deliberately decided is worth voicing.
        gen_verbosity = "normal" if verbosity == "anomaly" else verbosity

        p_worth = None
        if watched:
            # You are looking at it: worth hearing by definition, and the
            # decider's question is about a user not watching the screen.
            gen_verbosity = "decided"
        elif not anomaly:
            # With the decider on "jev", whether a released batch is worth
            # hearing is its call, against commentary.chattiness, not
            # Ollama's; terse and normal then differ in nothing. No opinion
            # (backend local, any failure) leaves the verbosity's own bias.
            p_worth = gate.worth_probability(event_detail, self.config)
            if p_worth is not None:
                bar = gate.threshold(cfg["chattiness"])
                if p_worth < bar:
                    with transaction(project) as state:
                        record_skipped(state, "PreToolUse")
                    self._log(f"[PreToolUse] batch of {len(items)} held back "
                              f"(decider worth={p_worth:.2f} < {bar:.2f})")
                    return
                gen_verbosity = "decided"

        # The slow part -- the model -- holds no lock; what it produced is
        # applied to the state as it is now, below.
        gen = generate_or_skip(
            "PreToolUse", event_detail, session.context,
            project=project, model=self._ollama_model,
            ollama_url=self._ollama_url,
            # Must exceed a cold model load (~8s for llama3.2:3b) — a
            # shorter timeout aborts the load, Ollama cancels it, and every
            # later call is cold again, so commentary never recovers. Hooks
            # are async so nothing user-facing blocks on this.
            timeout=OLLAMA_TIMEOUT,
            verbosity=gen_verbosity,
            recent_voiced=session.recent_voiced,
            **({"words": (focus["min_words"], focus["max_words"])} if watched else {}),
        )
        phrase = gen.phrase

        with transaction(project) as state:
            if phrase:
                record_voiced(state, phrase)
            else:
                record_skipped(state, "PreToolUse")
        notes = "" if p_worth is None else f", worth={p_worth:.2f}"
        notes += ", watched" if watched else ""
        self._log(gen.log_line("PreToolUse", f"{self._ollama_model}, {verbosity}{notes}",
                               batch=len(items)))
        if phrase:
            self.speak(phrase, "commentary")

    # ── Stop — one pipeline for every engine ───────────────────────────

    def _handle_stop(self, hook_input):
        """A Stop, for every engine: read how the turn ended (stop_outcome),
        drop the queued commentary it supersedes, and -- unless the agent is
        only waiting on its own work -- have the engine's phrase source judge
        and phrase it, record the category for the nudge, and speak.

        The phrase source is the one part that differs between engines
        (_stop_phrase): templates for the static engines, the model for the
        daemon engines. The reading, the rules and the bookkeeping are the
        same for both, and the phrase is spoken with cold_start: the turn
        has ended, and the hook is async.
        """
        import stop_outcome
        from session_state import (transaction, begin_stop, record_stop_category,
                                   record_voiced, record_skipped)
        reading = stop_outcome.read(hook_input)
        project = home.derive_project_label()
        working = reading is not None and reading.still_working

        with transaction(project) as state:
            dropped = begin_stop(state)
            if working:
                # Not a finish, and saying one buried the real finish among
                # them; "working" also keeps the idle prompt after it from
                # nudging.
                record_stop_category(state, "working")
                record_skipped(state, "Stop")
            else:
                session = _snapshot(state, task=reading.task if reading else None)
        if dropped:
            self._log(f"[Stop] dropped {dropped} pending commentary items")
        if working:
            self._log(stop_outcome.STILL_WORKING_LOG)
            return

        # The slow part -- a model call -- holds no lock.
        category, phrase = self._stop_phrase(reading, session)

        with transaction(project) as state:
            record_stop_category(state, category)
            if phrase:
                record_voiced(state, phrase)
            else:
                record_skipped(state, "Stop")
        if phrase:
            self.speak(phrase, kinds.stop_kind(category), cold_start=True)

    def _handle_plugin_notes(self, hook_input):
        """After a Stop's own announcement: say the notes the Claude Code
        plugin cc-plugin-you-should-know has left, once each (plugin_notes).
        Spoken with cold_start and as a "note": held for your return like
        news, shown on the face, and its words kept out of the log. Never raises: a note is a courtesy, and
        its source is undocumented."""
        try:
            import plugin_notes
            notes = plugin_notes.claim()
            if notes:
                phrase, project = plugin_notes.phrase(notes, self.config, hook_input.get("session_id"))
                self.speak(phrase, "note", cold_start=True, project=project)
        except Exception as e:
            self._log(f"[plugin-notes] failed ({e})")

    def _stop_phrase(self, reading, session):
        """The static engines' phrase source: the model's verdict, settled by
        the Stop rules, then a template for the category. Returns (category,
        phrase). The category is the classification itself, None when there
        was none; the phrase for an unknown turn is a "done" one."""
        import stop_outcome
        category = None
        if reading is not None:
            if reading.awaiting_answer:
                category = "question"  # the rules would say so whatever the model did
            else:
                verdict = stop_outcome.classify(reading.last_message or reading.context,
                                                engine_tag=self.engine_name,
                                                model=self._ollama_model,
                                                ollama_url=self._ollama_url)
                category, _ = reading.settle(verdict, failed=verdict is None)
        phrased_as = category or "done"
        bark = self.pick(phrased_as)
        self._log(f"template ({phrased_as}) -> {bark!r}")
        return category, bark

    def _describe_event(self, hook_input):
        """Build a description string for a PermissionRequest, PreToolUse or
        Notification."""
        event = hook_input.get("hook_event_name", "")

        # A Stop is read by stop_outcome.read(), not described here.
        if event == "PermissionRequest":
            tool = format_tool_name(hook_input.get("tool_name") or "something")
            return f"Tool: {tool}"

        if event == "PreToolUse":
            tool = hook_input.get("tool_name", "")
            tool_input = hook_input.get("tool_input", {})
            context = extract_context(tool, tool_input)
            return f"{tool}: {context}" if context else f"Using {tool}"

        if event == "Notification":
            ntype = hook_input.get("notification_type") or ""
            label = NOTIFICATION_LABELS.get(ntype)
            message = hook_input.get("message") or ""
            if label:
                return f"Status: {label}"
            return f"Message: {message or 'attention_required'}"

        return None

    # ── A permission request or a notification ─────────────────────────

    def _handle_attention(self, hook_input):
        """A PermissionRequest or Notification, for every engine. A
        fixed-meaning one (a destructive request, a question dialog, a
        waiting notification) is a template, never the model
        (_fixed_announcement); anything else goes to the engine's phrase
        source (_event_phrase). Either way the session is marked alive and
        what was said recorded, and a waiting notification may start a
        nudge."""
        from session_state import (transaction, mark_event, record_voiced, record_skipped,
                                   record_wait_said)
        event = hook_input.get("hook_event_name", "")
        if event == "Notification":
            self._maybe_nudge_from_notification(hook_input)
        project = home.derive_project_label()
        risk = self._permission_risk(hook_input)  # may ask the decider: not under the lock
        event_detail = self._describe_event(hook_input)
        session = None

        with transaction(project) as state:
            # Marks the session alive for the watchdog.
            mark_event(state)
            fixed = self._fixed_announcement(hook_input, state, risk)
            if fixed is None:
                if event_detail:
                    session = _snapshot(state)
                else:
                    record_skipped(state, event)

        if fixed is not None:
            return self._announce(fixed)
        if session is None:
            self._log(f"[{event}] no event detail, skipping")
            return

        # The slow part -- a model call, on the daemon engines -- holds no lock.
        category, phrase = self._event_phrase(hook_input, event_detail, session)

        kind = kinds.event_kind(hook_input)
        with transaction(project) as state:
            if phrase:
                record_voiced(state, phrase)
                if event == "PermissionRequest":
                    record_wait_said(state, hook_input.get("session_id"))
            else:
                record_skipped(state, event)
        if phrase:
            self.speak(phrase, kind, cold_start=kind == "waiting")

    def _event_phrase(self, hook_input, event_detail, session):
        """The static engines' phrase source for an event that is not a
        fixed announcement: a permission or notification template. Returns
        (category, phrase); the category is None, there being no model."""
        if hook_input.get("hook_event_name") == "PermissionRequest":
            tool = hook_input.get("tool_name") or "something important"
            bark = self.pick_permission(tool)
            self._log(f"permission ({tool!r}) template -> {bark!r}")
        else:
            bark = self.pick_notification()
            self._log(f"notification template -> {bark!r}")
        return None, bark

    def run(self, hook_input):
        """Main dispatch — called by hobson.py entrypoint. The same for every
        engine: only the phrase sources (_stop_phrase, _event_phrase) and how
        a phrase becomes audio (clip) differ."""
        if not self._is_event_enabled(hook_input):
            return
        event = hook_input.get("hook_event_name")
        if event == "PreToolUse":
            return self._handle_commentary(hook_input)
        if event == "Stop":
            self._handle_stop(hook_input)
            return self._handle_plugin_notes(hook_input)
        if event in ("PermissionRequest", "Notification"):
            return self._handle_attention(hook_input)
        self._log(f"[{event}] not an event Hobson speaks for")
