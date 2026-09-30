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
from typing import NamedTuple, Optional

# scripts/ on the path, for bark_templates, phrase_gen and the rest.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import home  # noqa: E402
import log_record  # noqa: E402


# How long macOS `say` may take to render a phrase before it is given up.
SAY_RENDER_TIMEOUT = 15

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


def bark_hash(text):
    """Stable SHA-256 prefix for cache key. Must match bark_templates.bark_hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def format_tool_name(tool):
    from bark_templates import normalize_tool_name
    tool = normalize_tool_name(tool)
    if not tool:
        return "something important"
    return re.sub(r"\s+", " ", tool)[:80]


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


def describe_generation_failure(raw):
    """Turn phrase_gen's last_raw into a clear log verdict.

    Three outcomes get conflated if you just print raw: the model declining
    to speak (SKIP), the model erroring (timeout, connection refused), and
    the model returning something unusable. Only the first is normal. A real
    Ollama timeout logged as "skipped" reads like working-as-intended, which
    is how 366 of them sat unnoticed in the log.

    Returns (verdict, detail) where verdict is SKIP, ERROR, REJECTED or
    UNUSABLE.
    """
    text = "" if raw is None else str(raw)
    if text.startswith("(error:"):
        inner = text[len("(error:"):].rstrip(")").strip()
        if "timed out" in inner:
            return "ERROR", f"ollama timed out — {inner}"
        if "refused" in inner or "URLError" in inner:
            return "ERROR", f"ollama unreachable — {inner}"
        return "ERROR", f"ollama call failed — {inner}"
    if text.startswith("(rejected:"):
        inner = text[len("(rejected:"):].rstrip(")").strip()
        return "REJECTED", f"guard rejected — {inner}"
    stripped = text.strip().strip("'\"")
    if not stripped:
        return "ERROR", "ollama returned nothing"
    if stripped.upper() == "SKIP":
        return "SKIP", "model chose not to speak"
    return "UNUSABLE", f"could not parse a phrase from {stripped!r}"


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


def stop_kind(category):
    """What a Stop's phrase is about, for presence: a question waits on you,
    a failure is urgent, anything else (unknown included) is a finish."""
    return {"question": "waiting", "broken": "broken"}.get(category, "done")


def event_kind(hook_input):
    """What a permission request or notification is about, for presence. A
    permission request and a waiting notification wait on you; any other
    notification is news."""
    if hook_input.get("hook_event_name") == "PermissionRequest":
        return "waiting"
    if hook_input.get("notification_type") in NOTIFICATION_WAITING_TYPES:
        return "waiting"
    return "info"


# The notification types that mean the session is waiting on you; nudge.py's
# WAITING_TYPES plus a permission prompt, which does not start a nudge but is
# no less a wait.
NOTIFICATION_WAITING_TYPES = ("idle_prompt", "agent_needs_input", "permission_prompt")


class StopSession(NamedTuple):
    """What a Stop's phrase source knows of the session, read once."""
    context: str                    # build_session_context, with the Task line
    recent_voiced: list             # [phrase, ts] pairs, for the dedup guard
    since_voiced: Optional[float]   # seconds since the last phrase, None if none


class FixedAnnouncement(NamedTuple):
    """A template for a fixed-meaning event (BaseEngine._fixed_announcement):
    the phrase to speak, or None when the event is handled by saying
    nothing, and its log label."""
    phrase: Optional[str]
    label: str


class BaseEngine(ABC):
    """Abstract base for TTS engines.

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

    # Map hook event names to config event keys
    EVENT_MAP = {
        "Stop": "stop",
        "PermissionRequest": "permission",
        "Notification": "notification",
        "PreToolUse": "commentary",
    }

    def __init__(self, config):
        self.config = config
        self.cooldown = config.get("cooldown", 2.0)
        # Config scale 0-10 mapped to afplay's 0.0-1.0 (1.0 = unity gain)
        self.volume = max(0.0, min(10.0, float(config.get("volume", 3)))) / 10.0
        self._templates = None
        # Personality say voice for macOS say fallback
        self._say_voice = self._load_say_voice(config)
        # Event filtering
        self.enabled_events = set(config.get("events", ["stop", "permission", "notification"]))
        # Ollama config
        ollama_cfg = config.get("ollama", {})
        self._ollama_model = ollama_cfg.get("model", "llama3.2:3b")
        self._ollama_url = ollama_cfg.get("url", "http://localhost:11434")
        # Commentary config
        commentary_cfg = config.get("commentary", {})
        self.commentary_cooldown = commentary_cfg.get("cooldown", 8.0)
        self.commentary_tools = set(
            t.lower() for t in commentary_cfg.get(
                "tools", ["Agent", "Edit", "Write", "Bash"]
            )
        )
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
        _say_with_volume renders to AIFF and plays it through this same argv.
        Per-project identity belongs in voice selection (which the kokoro
        daemon supports per request), not in resampling the output.
        """
        args = ["afplay", "-v", str(self.volume)]
        identity = self.config.get("project_identity") or {}
        if identity.get("enabled", False):
            rate = project_rate(home.derive_project_label(),
                                spread=identity.get("spread", 0.06))
            if rate != 1.0:
                args += ["-r", str(rate), "-q", "1"]
        return args + [path]

    def _play(self, path, phrase, kind=None, cleanup=False):
        """Play one clip, detached (it must survive the hook process exiting),
        then show it on the face. Every spoken phrase leaves through here,
        after _for_audience, so a held or dropped phrase is never shown. The
        voice goes first, and the face cannot stop it. `cleanup` removes the
        clip once played (say's temp AIFF, a daemon's WAV).

        It plays after whatever is playing now, from any session: the player
        is handed the voice lock and holds it until it exits (voice.py), so
        two clips never play at once. Returns the player."""
        import voice
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
            face.show(self.config, phrase, kind or "info", path)
        except Exception as e:
            self._log(f"face failed ({e})")
        return player

    # ── Nudge (Task 7) ──────────────────────────────────────────────────

    def _maybe_start_nudge(self, subject):
        """Spawn a detached nudge for a session that is waiting on the user.

        Guarded end-to-end — nudge.enabled and any failure to spawn must
        never break the caller, which runs inline in an async hook. The
        spawned process is responsible for its own one-per-project lock.
        Its argv is round-tripped through nudge.py's parser in the tests:
        the child runs with stderr discarded, so an argument it does not
        accept would kill it without a trace.
        """
        try:
            cfg = self.config.get("nudge") or {}
            if not cfg.get("enabled", True):
                return
            project = home.derive_project_label()
            nudge_script = os.path.join(home.ROOT, "scripts", "nudge.py")
            subprocess.Popen(
                [sys.executable, nudge_script, "--project", project,
                 "--subject", subject or ""],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._log(f"nudge: spawned for {project!r}")
        except Exception as e:
            self._log(f"nudge: spawn failed ({e})")

    # ── Watchdog (Task 9) ───────────────────────────────────────────────

    def _maybe_start_watchdog(self, baseline=None):
        """Ensure a hang-detector is running for this project.

        Spawned lazily on a commentary flush rather than on every event, and
        guarded on both ends: the parent probes the watchdog's own lock file
        first (a quick non-blocking flock, released immediately) so a busy
        project does not fork a subprocess it already knows will exit; the
        child then takes the same lock for real before it starts polling.
        The probe is a courtesy, not the source of truth — the child's own
        lock is what actually enforces one-per-project. Guarded end-to-end:
        a failed spawn (or a failed probe) must never break the flush it
        rides on.

        `baseline` should be the caller's last_event_time. Passed straight
        through as `--baseline`, so the child never has to re-read it from
        disk -- it once raced the parent's own save and read the stale,
        pre-flush value, exiting on its first poll every time.
        """
        try:
            cfg = self.config.get("watchdog") or {}
            if not cfg.get("enabled", True):
                return
            project = home.derive_project_label()

            try:
                from nudge import _lock_path
                probe = open(_lock_path("watchdog", project), "a+", encoding="utf-8")
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe, fcntl.LOCK_UN)
                except BlockingIOError:
                    probe.close()
                    return  # a watchdog is already running for this project
                probe.close()
            except (ImportError, OSError):
                pass  # probe itself failed — fall through, the child's own
                      # lock is still authoritative either way

            nudge_script = os.path.join(home.ROOT, "scripts", "nudge.py")
            argv = [sys.executable, nudge_script, "--watchdog", "--project", project,
                    "--minutes", str(cfg.get("minutes", 10))]
            if baseline is not None:
                argv += ["--baseline", str(baseline)]
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._log(f"watchdog: spawn requested for {project!r}")
        except Exception as e:
            self._log(f"watchdog: spawn failed ({e})")

    @staticmethod
    def _waiting_type(hook_input):
        """This Notification's type if it means the session is blocked on the
        user (nudge.WAITING_TYPES), else None."""
        try:
            from nudge import WAITING_TYPES
        except ImportError:
            return None
        ntype = hook_input.get("notification_type") or ""
        return ntype if ntype in WAITING_TYPES else None

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
        ntype = self._waiting_type(hook_input)
        if not ntype:
            return
        if self._idle_after_finished_turn(hook_input):
            self._log("nudge: not started — the last turn finished, nothing is waiting on you")
            return
        self._maybe_start_nudge(NOTIFICATION_LABELS.get(ntype, ntype))

    def _idle_after_finished_turn(self, hook_input, state=None):
        """True for an idle_prompt that follows a turn classified "done", or
        one that ended waiting on the agent's own work ("working").

        It fires whenever the input sits idle, and after either of those
        nothing is waiting on you -- the Stop already said it finished, or
        the agent's subagents will wake it. The one gate for everything a
        waiting notification would otherwise start: the nudge, the realtime
        waiting line, the static bark. A question, a failure or an unknown
        ending does not match, and neither does agent_needs_input.
        """
        if self._waiting_type(hook_input) != "idle_prompt":
            return False
        try:
            from session_state import load_session, stop_category
            if state is None:
                state = load_session(home.derive_project_label())
            return stop_category(state) in ("done", "working")
        except Exception:
            return False

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

    @staticmethod
    def _is_question_dialog(hook_input):
        """AskUserQuestion or ExitPlanMode, which reach hobson as a
        PermissionRequest. 87 of them in the log were phrased by the model
        from "Tool: AskUserQuestion" alone: "I finished fixing hobson.", "I'm
        applying review fixes." The static engines said their permission
        template for a tool named AskUserQuestion."""
        if hook_input.get("hook_event_name") != "PermissionRequest":
            return False
        try:
            from nudge import USER_WAIT_TOOLS
        except ImportError:
            return False
        return hook_input.get("tool_name") in USER_WAIT_TOOLS

    @staticmethod
    def _risky_phrase(reason):
        return f"Careful — this one {reason}. It needs your approval."

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

        Chooses inside the caller's session transaction; the caller speaks
        (_announce) once it has committed.
        """
        from session_state import record_voiced
        event = hook_input.get("hook_event_name")
        if event == "PermissionRequest" and risk:
            phrase, label = self._risky_phrase(risk), f"[PermissionRequest] destructive ({risk})"
        elif self._is_question_dialog(hook_input):
            group = self.templates.CATEGORIES.get("question") or {}
            candidates = [f"{lead} {tail}" for lead in group.get("lead", [])
                          for tail in group.get("tail", [])]
            if not candidates:
                return None
            phrase, label = self._fresh_choice(candidates, state), "[PermissionRequest] question dialog"
        elif event == "Notification" and self._waiting_type(hook_input):
            if self._idle_after_finished_turn(hook_input, state):
                # Handled, by saying nothing -- falling through would hand
                # the event to the model, which is worse than either.
                return FixedAnnouncement(None, "[Notification] not announced — the last turn finished")
            templates = list(self.templates.NOTIFICATION_TEMPLATES)
            if not templates:
                return None
            phrase, label = self._fresh_choice(templates, state), "[Notification] waiting template"
        else:
            return None
        record_voiced(state, phrase)
        return FixedAnnouncement(phrase, label)

    def _announce(self, fixed):
        """Speak (or, for a silent one, just log) a fixed announcement."""
        if fixed.phrase is None:
            self._log(fixed.label)
            return
        self._log(f"{fixed.label} -> {fixed.phrase!r}")
        self.speak_dynamic(fixed.phrase, allow_cold_start=False, kind="waiting")

    @staticmethod
    def _fresh_choice(candidates, state):
        """A random candidate that is not a near-duplicate of anything said
        recently -- or any candidate, if every one was: the event is news
        even in the same words."""
        from phrase_gen import is_near_duplicate
        recent = state.get("recent_voiced", [])
        fresh = [c for c in candidates if not is_near_duplicate(c, recent)]
        return random.choice(fresh or candidates)

    def _say_with_volume(self, text, kind=None):
        """Speak via macOS `say` honoring self.volume.

        `say` has no volume flag, so render to a temp AIFF and play with
        `afplay -v`. The render is waited for (about half a second; the
        hooks are async) so the face has the audio to move its mouth to;
        the temp file is removed after playback. The text comes after `--`:
        a phrase is model output, and one starting "-o<path>" was taken as
        an option and wrote audio over that file.
        """
        path = self._render_say(text)
        if path:
            self._play(path, text, kind, cleanup=True)

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
        tool = format_tool_name(tool)
        if self.cache_dir:
            all_barks = [
                f"{l} {a.format(tool=tool)}"
                for l in t.PERMISSION_LEADS for a in t.PERMISSION_ACTIONS
            ]
            cached = [b for b in all_barks if self.cached_audio(b)]
            if cached:
                return random.choice(cached)
            return self._pick_prefer_cached(t.GENERIC_PERMISSION_PHRASES)
        else:
            # No cache (say engine) — skip MCP/internal tool names
            if "_" in tool:
                return random.choice(t.GENERIC_PERMISSION_PHRASES)
            return f"{random.choice(t.PERMISSION_LEADS)} {random.choice(t.PERMISSION_ACTIONS).format(tool=tool)}"

    def pick_notification(self):
        return self._pick_prefer_cached(self.templates.NOTIFICATION_TEMPLATES)

    def try_bark(self, bark, lock_file=None, cooldown=None, do_backfill=True, kind=None):
        """Speak bark via cached audio or fallback to macOS say.

        Priority: cached audio + afplay > macOS say > silent
        On cache miss: speak via say immediately, backfill cache in background.

        Optional params for commentary: separate lock_file, different cooldown,
        and do_backfill=False to skip cache backfill for dynamic phrases.
        `kind` is what the phrase is about, for presence (_for_audience).
        """
        bark = self._for_audience(bark, kind)
        if bark is None:
            return
        lock_file = lock_file or home.bark_lock()
        cooldown = cooldown if cooldown is not None else self.cooldown

        fd = None
        try:
            fd = open(lock_file, "a+", encoding="utf-8")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            fd.seek(0)
            content = fd.read().strip()
            now = time.time()

            if content:
                try:
                    if now - float(content) < cooldown:
                        self._log(f"skipped (cooldown) -> {bark!r}")
                        return
                except ValueError:
                    pass

            fd.seek(0)
            fd.truncate()
            fd.write(str(now))
            fd.flush()

            audio = self.cached_audio(bark)
            if audio:
                self._play(audio, bark, kind)
                self._log(log_record.playback("template-cache", bark, kind or "info"))
            else:
                self._say_with_volume(bark, kind)
                if self.cache_dir:
                    self._log(log_record.playback("say-fallback: cache miss", bark, kind or "info"))
                else:
                    self._log(log_record.playback("say", bark, kind or "info"))
                if do_backfill:
                    self.backfill(bark)

        except BlockingIOError:
            self._log(f"skipped (locked) -> {bark!r}")
        except Exception as e:
            self._log(f"bark failed ({e}) -> {bark!r}")
        finally:
            if fd is not None:
                try:
                    fd.close()
                except Exception:
                    pass

    def _is_event_enabled(self, hook_input):
        """Check if this hook event is enabled in config. Returns False + logs if disabled."""
        event = hook_input.get("hook_event_name")
        event_key = self.EVENT_MAP.get(event)
        if event_key and event_key not in self.enabled_events:
            self._log(f"skipped (event {event_key!r} disabled)")
            return False
        return True

    # ── Dynamic phrase playback (overridable seam) ─────────────────────

    def _for_audience(self, phrase, kind):
        """The phrase to speak now, or None when presence held it for your
        return or dropped it (presence.route). `kind` is what it is about:
        "waiting", "broken", "done", "info", "stalled", "commentary",
        "nudge" or "briefing"; None counts as "info". Every speak seam calls
        this once, so no engine or event can talk past an empty room. A
        failure here speaks: without presence, Hobson is as he was."""
        try:
            import presence
            return presence.route(self.config, kind or "info", phrase)
        except Exception as e:
            self._log(f"presence check failed ({e}), speaking")
            return phrase

    def _voice_busy_for(self, phrase, kind):
        """Commentary is dropped while anything is playing (voice.busy): by
        the time its turn came it would be about work already done. Every
        other kind waits for its turn in _play."""
        if kind != "commentary":
            return False
        import voice
        if not voice.busy():
            return False
        self._log(f"skipped (voice busy) -> {phrase!r}")
        return True

    def speak_dynamic(self, phrase, allow_cold_start=True, kind=None):
        """Speak a dynamic, non-cacheable phrase. False when presence held or
        dropped it instead (_for_audience).

        Default: macOS `say` with the personality voice, gated by the bark
        lock to avoid overlapping other engines' audio. Realtime engines
        override this to route through their daemon (with say as last resort).
        """
        phrase = self._for_audience(phrase, kind)
        if phrase is None:
            return False
        if self._voice_busy_for(phrase, kind):
            return True
        try:
            fd = open(home.bark_lock(), "a+", encoding="utf-8")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._log(f"skipped (locked) -> {phrase!r}")
                fd.close()
                return True
            fd.seek(0); fd.truncate()
            fd.write(str(time.time())); fd.flush(); fd.close()
        except OSError:
            pass
        self._say_with_volume(phrase, kind)
        self._log(log_record.playback("say-dynamic", phrase, kind or "info"))
        return True

    # ── Commentary (PreToolUse) — shared across all engines ────────────

    def _acquire_commentary_lock(self):
        """Acquire the commentary lock + check cooldown. Returns True on success."""
        try:
            fd = open(home.commentary_lock(), "a+", encoding="utf-8")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.seek(0)
            content = fd.read().strip()
            if content:
                try:
                    if time.time() - float(content) < self.commentary_cooldown:
                        fd.close()
                        return False
                except ValueError:
                    pass
            fd.seek(0); fd.truncate()
            fd.write(str(time.time())); fd.flush(); fd.close()
            return True
        except (BlockingIOError, OSError):
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

        if (self.config.get("commentary", {}).get("suppress_subagents", True)
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
            self.speak_dynamic(phrase, allow_cold_start=False, kind="commentary")
            return

        # terse / normal — append-to-queue must always succeed, so the lock
        # is acquired only once should_flush() opens the gate (inside
        # _handle_commentary_llm), never here.
        self._handle_commentary_llm(hook_input, verbosity)

    def _custom_phrase_prompt(self):
        """Optional per-engine custom persona prompt passed to phrase_gen.

        Override in subclasses to inject a per-engine persona prompt.
        """
        return None

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
                record_voiced, record_skipped, build_session_context,
                seconds_since_last_voiced,
                record_pending, should_flush, take_pending, pending_summary,
                record_flush_attempt,
                record_fingerprint, mark_repeat_announced, anomaly_reason,
                action_identity,
                PENDING_STALE_SECONDS,
            )
        except ImportError as e:
            self._log(f"[PreToolUse] LLM gating unavailable ({e})")
            return

        project = home.derive_project_label()
        tool = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input") or {}
        context = extract_context(tool, tool_input)

        cfg = self.config.get("commentary", {})
        min_calls = cfg.get("min_tool_calls", 5)
        min_seconds = cfg.get("min_seconds", 60.0)
        stale = cfg.get("pending_stale_seconds", PENDING_STALE_SECONDS)
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

            if not self._acquire_commentary_lock():
                # Batch has earned an utterance, but another flush spoke too
                # recently. Leave the queue intact — do not consume it — so
                # the next tool call gets another chance to flush it.
                self._log("[PreToolUse] flush ready but commentary locked; deferred")
                return

            # The flush is going ahead now — time the gate off this attempt,
            # not off whether it ends up producing speech.
            record_flush_attempt(state)
            baseline = state["last_event_time"]
            items = take_pending(state, stale_seconds=stale)
            event_detail = pending_summary(items)
            if event_detail:
                if anomaly and not anomaly.startswith("first time"):
                    # Mark before generating, so a failed generation cannot
                    # loop: a repeat must not re-fire on every further
                    # occurrence of the same action.
                    mark_repeat_announced(state, tool, identity)
                session_context = build_session_context(state)
                recent_voiced = list(state.get("recent_voiced", []))
                since_voiced = seconds_since_last_voiced(state)

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
        if not anomaly:
            # With the decider on "jev", whether a released batch is worth
            # hearing is its call, against commentary.chattiness, not
            # Ollama's; terse and normal then differ in nothing. No opinion
            # (backend local, any failure) leaves the verbosity's own bias.
            p_worth = gate.worth_probability(event_detail, self.config)
            if p_worth is not None:
                bar = gate.threshold(cfg.get("chattiness", gate.DEFAULT_CHATTINESS))
                if p_worth < bar:
                    with transaction(project) as state:
                        record_skipped(state, "PreToolUse")
                    self._log(f"[PreToolUse] batch of {len(items)} held back "
                              f"(decider worth={p_worth:.2f} < {bar:.2f})")
                    return
                gen_verbosity = "decided"

        # The slow part -- the model -- holds no lock; what it produced is
        # applied to the state as it is now, below.
        category, phrase = generate_or_skip(
            "PreToolUse", event_detail, session_context,
            project=project, model=self._ollama_model,
            ollama_url=self._ollama_url,
            # Must exceed a cold model load (~8s for llama3.2:3b) — a
            # shorter timeout aborts the load, Ollama cancels it, and every
            # later call is cold again, so commentary never recovers. Hooks
            # are async so nothing user-facing blocks on this.
            timeout=20,
            verbosity=gen_verbosity,
            custom_prompt=self._custom_phrase_prompt(),
            recent_voiced=recent_voiced,
            seconds_since_last_voiced=since_voiced,
        )

        with transaction(project) as state:
            if phrase:
                record_voiced(state, phrase)
            else:
                record_skipped(state, "PreToolUse")
        if phrase:
            decided = "" if p_worth is None else f", worth={p_worth:.2f}"
            self._log(log_record.outcome("PreToolUse", f"{self._ollama_model}, {verbosity}{decided}",
                                         category, phrase, batch=len(items)))
            self.speak_dynamic(phrase, allow_cold_start=False, kind="commentary")
        else:
            from phrase_gen import last_raw as raw
            verdict, detail = describe_generation_failure(raw)
            self._log(f"[PreToolUse] batch of {len(items)} {verdict}: {detail}")

    # ── Stop — one pipeline for every engine ───────────────────────────

    def _handle_stop(self, hook_input):
        """A Stop, for every engine: read how the turn ended (stop_outcome),
        drop the queued commentary it supersedes, and -- unless the agent is
        only waiting on its own work -- have the engine's phrase source judge
        and phrase it, record the category for the nudge, and speak.

        The phrase source is the one part that differs between engines
        (_stop_phrase / _speak_stop): templates for the static engines, the
        model for the daemon engines. The reading, the rules and the
        bookkeeping are the same for both.
        """
        import stop_outcome
        from session_state import (transaction, begin_stop, record_stop_category,
                                   record_voiced, record_skipped, build_session_context,
                                   seconds_since_last_voiced)
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
                session = StopSession(
                    context=build_session_context(state, task=reading.task if reading else None),
                    recent_voiced=list(state.get("recent_voiced", [])),
                    since_voiced=seconds_since_last_voiced(state),
                )
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
            self._speak_stop(phrase, kind=stop_kind(category))

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

    def _speak_stop(self, phrase, kind=None):
        """Speak a Stop's phrase: a template, cache first."""
        self.try_bark(phrase, kind=kind)

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

    def run(self, hook_input):
        """Main dispatch — called by hobson.py entrypoint."""
        if not self._is_event_enabled(hook_input):
            return

        event = hook_input.get("hook_event_name")

        if event == "PreToolUse":
            return self._handle_commentary(hook_input)

        if event == "PermissionRequest":
            risk = self._permission_risk(hook_input)
            if risk:
                self._announce(FixedAnnouncement(self._risky_phrase(risk),
                                                 f"[PermissionRequest] destructive ({risk})"))
                return
            if self._is_question_dialog(hook_input):
                bark = self.pick("question")
                self._log(f"[PermissionRequest] question dialog -> {bark!r}")
                self.try_bark(bark, kind="waiting")
                return
            tool = hook_input.get("tool_name") or "something important"
            bark = self.pick_permission(tool)
            self._log(f"permission ({tool!r}) template -> {bark!r}")
            self.try_bark(bark, kind="waiting")
            return

        if event == "Notification":
            self._maybe_nudge_from_notification(hook_input)
            if self._idle_after_finished_turn(hook_input):
                self._log("notification: not announced — the last turn finished")
                return
            bark = self.pick_notification()
            self._log(f"notification template -> {bark!r}")
            self.try_bark(bark, kind=event_kind(hook_input))
            return

        return self._handle_stop(hook_input)
