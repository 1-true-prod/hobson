"""Daemon engine — every phrase rendered live by a persistent TTS daemon.

The realtime engines (kokoro-realtime, pocket-tts) are the same engine
pointed at different daemons: a DaemonSpec says which venv and script start
it, which config section and options it reads, and how long it may take.
Everything else — how a phrase is rendered, the say fallback — is here
once, and the speaking path itself is BaseEngine.speak, every engine's.
The two used to be separate copies, and a fix to one was a latent bug in
the other.

Ollama generates contextual phrases for Stop/Permission/Notification;
fixed-meaning events use templates but still render live. No pre-cached
audio is ever used.
"""

import glob
import json
import os
import subprocess
import tempfile
import threading
import time
from typing import NamedTuple, Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError

from .base import OLLAMA_TIMEOUT, BaseEngine, Clip
import home
import loopback


class DaemonOption(NamedTuple):
    """One setting from the engine's config section: its key, the daemon's
    CLI flag for it (None if not passed at startup), and whether it goes in
    every /generate request too."""
    key: str
    flag: Optional[str]
    in_payload: bool


class DaemonSpec(NamedTuple):
    """What differs between one daemon backend and another."""
    section: str            # config section, e.g. "kokoro"
    venv: str               # venvs/<venv>/bin/python3 runs the daemon
    script: str             # scripts/<script>
    playback: str           # scratch WAVs' prefix under ~/.claude — see _daemon_generate
    startup_polls: int      # readiness polls at 0.25s after a cold start
    generate_timeout: int   # seconds for one /generate request
    options: Tuple[DaemonOption, ...]


class DaemonEngine(BaseEngine):
    """A live-only engine backed by the TTS daemon its `spec` names.

    Falls back to macOS say (the personality's voice) only when the daemon
    is down or fails.
    """

    spec: DaemonSpec = None  # set by each backend

    def __init__(self, config):
        super().__init__(config)
        spec = self.spec
        cfg = self.config[spec.section]  # merged over DEFAULT_CONFIG by BaseEngine
        self._daemon_port = cfg["daemon_port"]
        self._daemon_url = f"http://127.0.0.1:{self._daemon_port}"
        self._daemon_idle_timeout = cfg["daemon_idle_timeout"]
        self._options = {o.key: cfg[o.key] for o in spec.options}
        self._venv_python = os.path.join(home.ROOT, "venvs", spec.venv, "bin", "python3")
        self._daemon_script = os.path.join(home.ROOT, "scripts", spec.script)
        # Written by the daemon once it holds the port, named after its
        # script (tts_daemon.py); /generate is refused without it.
        self._token_file = home.path(os.path.splitext(spec.script)[0] + ".token")

    # ── Daemon management ──────────────────────────────────────────────

    def _daemon_argv(self):
        argv = [self._venv_python, self._daemon_script, "--port", str(self._daemon_port)]
        for o in self.spec.options:
            if o.flag:
                argv += [o.flag, str(self._options[o.key])]
        return argv + ["--idle-timeout", str(self._daemon_idle_timeout)]

    def cli_lines(self):
        """What `hobson daemon` needs, as this engine sees it: KEY=value
        lines, then one ARG= line per word of the command that starts the
        daemon. The CLI used to keep its own copy, for Kokoro alone, and could
        neither start, stop nor report Pocket TTS."""
        stem = os.path.splitext(self.spec.script)[0]
        lines = [f"PORT={self._daemon_port}",
                 f"PID_FILE={home.path(stem + '.pid')}",
                 f"TOKEN_FILE={self._token_file}",
                 f"LOG={home.path(stem + '.log')}",
                 f"VENV={self._venv_python}",
                 f"SCRIPT={self._daemon_script}",
                 f"POLLS={self.spec.startup_polls}"]
        return lines + [f"ARG={word}" for word in self._daemon_argv()]

    def voice_label(self):
        return f"{self._options.get('voice')}  (say fallback: {self._say_voice})"

    def _daemon_alive(self):
        try:
            req = Request(f"{self._daemon_url}/health")
            with urlopen(req, timeout=1) as resp:
                return resp.status == 200
        except (URLError, OSError):
            return False

    def _start_daemon(self):
        if not os.path.isfile(self._venv_python):
            self._log("daemon: venv python not found, cannot start")
            return False
        if not os.path.isfile(self._daemon_script):
            self._log("daemon: script not found, cannot start")
            return False

        self._log("daemon: starting...")
        try:
            subprocess.Popen(
                self._daemon_argv(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._log(f"daemon: failed to start ({e})")
            return False

        for _ in range(self.spec.startup_polls):
            time.sleep(0.25)
            if self._daemon_alive():
                self._log("daemon: ready")
                return True

        self._log("daemon: startup timed out")
        return False

    def _ensure_daemon(self):
        if self._daemon_alive():
            return True
        return self._start_daemon()

    def _start_daemon_background(self):
        """Fire-and-forget daemon start so it's warm for the next hook."""
        threading.Thread(target=self._start_daemon, daemon=True).start()

    # ── Daemon TTS ─────────────────────────────────────────────────────

    def _daemon_generate(self, text, timeout=None):
        """Send text to daemon, return path to a WAV file or None.

        A new file for every phrase, played with cleanup (play): one fixed
        path was rewritten by the next phrase while the last still played,
        and a phrase waiting for its turn on the voice would have played the
        one after it. mkstemp, not a NamedTemporaryFile, which is deleted when
        the hook exits, before afplay has opened it. `timeout` overrides the
        spec's, which is sized for one line.
        """
        payload = {"text": text}
        payload.update({o.key: self._options[o.key] for o in self.spec.options if o.in_payload})
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        token = loopback.read_token(self._token_file)
        if token:
            headers[loopback.TOKEN_HEADER] = token

        try:
            req = Request(f"{self._daemon_url}/generate", data=body, headers=headers)
            with urlopen(req, timeout=timeout or self.spec.generate_timeout) as resp:
                wav_bytes = resp.read()
        except (URLError, OSError) as e:
            self._log(f"daemon generate failed: {e}")
            return None

        if not wav_bytes or len(wav_bytes) < 100:
            self._log("daemon returned empty/tiny response")
            return None

        self._prune_playback()
        fd, playback_path = tempfile.mkstemp(
            prefix=f"{self.spec.playback}-", suffix=".wav", dir=home.state_dir())
        with os.fdopen(fd, "wb") as f:
            f.write(wav_bytes)

        return playback_path

    def _prune_playback(self, older_than=3600):
        """Remove scratch WAVs a player never cleaned up (killed mid-phrase)."""
        cutoff = time.time() - older_than
        for stale in glob.glob(home.path(f"{self.spec.playback}-*.wav")):
            try:
                if os.path.getmtime(stale) < cutoff:
                    os.remove(stale)
            except OSError:
                pass

    # ── How a phrase becomes audio (BaseEngine.speak) ──────────────────

    def clip(self, phrase, kind, cold_start=False):
        """`phrase` rendered live by the daemon -- never a cache -- or, when
        it is down or fails, by macOS `say` in the personality's voice.
        `cold_start` waits for a daemon that is not running; otherwise one
        that is down is started in the background, warm by the next hook,
        and this phrase goes to `say`."""
        if cold_start:
            daemon_ok = self._ensure_daemon()
        else:
            daemon_ok = self._daemon_alive()
            if not daemon_ok:
                self._start_daemon_background()
        if daemon_ok:
            wav_path = self._daemon_generate(phrase)
            if wav_path:
                return Clip(wav_path, "daemon-live", True)
            reason = "daemon error"
        else:
            reason = "daemon start failed" if cold_start else "daemon down"
        path = self._render_say(phrase)
        return Clip(path, f"say-fallback: {reason}", True) if path else None

    def warm(self):
        """A phrase was skipped: have the daemon warm for the next one."""
        if not self._daemon_alive():
            self._start_daemon_background()

    def render(self, text, timeout=None):
        """A clip of `text` from the daemon, starting it if need be, else
        from `say`; None when neither could."""
        if self._ensure_daemon():
            path = self._daemon_generate(text, timeout=timeout)
            if path:
                return path
        return self._render_say(text)

    def backfill(self, text):
        pass  # all audio is live, no cache to backfill

    # ── Stop: the live phrase source (BaseEngine._handle_stop) ─────────

    def _stop_phrase(self, reading, session):
        """One model call judges the turn and phrases it, under the Stop
        rules (generate_or_skip(stop=reading))."""
        if reading is None:
            self._log("[Stop] no event detail, skipping")
            return None, None
        from phrase_gen import generate_or_skip
        gen = generate_or_skip(
            "Stop", reading.context, session.context,
            project=home.derive_project_label(), model=self._ollama_model,
            ollama_url=self._ollama_url,
            timeout=OLLAMA_TIMEOUT,
            recent_voiced=session.recent_voiced,
            stop=reading,
        )
        self._log(gen.log_line("Stop", self._ollama_model))
        return gen.category, gen.phrase

    # ── Permission requests and notifications: the model ───────────────

    def _event_phrase(self, hook_input, event_detail, session):
        """One model call decides and phrases (BaseEngine._handle_attention,
        for an event that is not a fixed announcement)."""
        from phrase_gen import generate_or_skip
        event = hook_input.get("hook_event_name", "")
        gen = generate_or_skip(
            event, event_detail, session.context,
            project=home.derive_project_label(), model=self._ollama_model,
            ollama_url=self._ollama_url,
            timeout=OLLAMA_TIMEOUT,
            recent_voiced=session.recent_voiced,
        )
        self._log(gen.log_line(event, self._ollama_model))
        return gen.category, gen.phrase
