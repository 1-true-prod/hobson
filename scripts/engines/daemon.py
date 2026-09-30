"""Daemon engine — every phrase rendered live by a persistent TTS daemon.

The realtime engines (kokoro-realtime, pocket-tts) are the same engine
pointed at different daemons: a DaemonSpec says which venv and script start
it, which config section and options it reads, and how long it may take.
Everything else — the event flow, the overlap lock, the say fallback — is
here once. The two used to be separate copies, and a fix to one was a latent
bug in the other.

Ollama generates contextual phrases for Stop/Permission/Notification;
fixed-meaning events use templates but still render live. No pre-cached
audio is ever used.
"""

import contextlib
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

from .base import BaseEngine, describe_generation_failure, event_kind
import home
import log_record
import loopback


# A cold Ollama load takes ~8s, and a shorter timeout cancels the load
# itself, so the next call is cold again. The hook is async, so waiting
# blocks nobody.
_OLLAMA_TIMEOUT = 20


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
        # The section's defaults come from DEFAULT_CONFIG, so none of them is
        # written twice; an engine built from a partial config (the tests do)
        # still gets them.
        cfg = {**home.DEFAULT_CONFIG.get(spec.section, {}), **(config.get(spec.section) or {})}
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

        A new file for every phrase, played with cleanup (_play): one fixed
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

    # ── Live speak — all audio goes through here ───────────────────────

    def _update_bark_lock(self):
        """Update the main bark lock timestamp so other hooks know we just spoke."""
        try:
            with open(home.bark_lock(), "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except OSError:
            pass

    def _speak_live(self, phrase, allow_cold_start=True, kind=None):
        """Speak a phrase live via daemon. This is the ONLY playback path.
        False when presence held or dropped it instead (_for_audience).

        1. Generate via daemon (no cache)
        2. Last resort: macOS say with the personality's voice
        """
        import fcntl

        phrase = self._for_audience(phrase, kind)
        if phrase is None:
            return False
        if self._voice_busy_for(phrase, kind):
            return True

        # For async hooks, acquire the cross-process bark lock and check
        # for recent barks — prevents overlapping audio across all engines.
        if not allow_cold_start:
            fd = None
            try:
                fd = open(home.bark_lock(), "a+", encoding="utf-8")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd.seek(0)
                content = fd.read().strip()
                if content:
                    try:
                        if time.time() - float(content) < 2.0:
                            self._log(f"skipped (overlap) -> {phrase!r}")
                            if not self._daemon_alive():
                                self._start_daemon_background()
                            return True
                    except ValueError:
                        pass
                fd.seek(0)
                fd.truncate()
                fd.write(str(time.time()))
                fd.flush()
            except BlockingIOError:
                self._log(f"skipped (locked) -> {phrase!r}")
                if not self._daemon_alive():
                    self._start_daemon_background()
                return True
            except Exception:
                pass
            finally:
                if fd is not None:
                    try:
                        fd.close()
                    except Exception:
                        pass
        else:
            self._update_bark_lock()

        if allow_cold_start:
            daemon_ok = self._ensure_daemon()
        else:
            daemon_ok = self._daemon_alive()

        if daemon_ok:
            wav_path = self._daemon_generate(phrase)
            if wav_path:
                self._play_wav(wav_path, phrase, kind)
                self._log(log_record.playback("daemon-live", phrase, kind or "info"))
                return True
            reason = "daemon error"
        else:
            if not allow_cold_start:
                # Daemon is down — start it in the background so it's
                # warm by the next hook, without blocking this one.
                self._start_daemon_background()
                reason = "daemon down"
            else:
                reason = "daemon start failed"

        self._say_with_volume(phrase, kind)
        self._log(log_record.playback(f"say-fallback: {reason}", phrase, kind or "info"))
        return True

    def _play_wav(self, path, phrase, kind=None):
        try:
            self._play(path, phrase, kind, cleanup=True)
        except Exception as e:
            self._log(f"afplay failed: {e}")
            with contextlib.suppress(OSError):
                os.remove(path)

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

    # ── Dynamic playback goes through the daemon (BaseEngine seam) ─────

    def speak_dynamic(self, phrase, allow_cold_start=True, kind=None):
        """All dynamic phrases route through the live daemon."""
        return self._speak_live(phrase, allow_cold_start=allow_cold_start, kind=kind)

    # ── Stop: the live phrase source (BaseEngine._handle_stop) ─────────

    def _stop_phrase(self, reading, session):
        """One model call judges the turn and phrases it, under the Stop
        rules (generate_or_skip(stop=reading))."""
        if reading is None:
            self._log("[Stop] no event detail, skipping")
            return None, None
        from phrase_gen import generate_or_skip
        category, phrase = generate_or_skip(
            "Stop", reading.context, session.context,
            project=home.derive_project_label(), model=self._ollama_model,
            ollama_url=self._ollama_url,
            timeout=_OLLAMA_TIMEOUT,
            recent_voiced=session.recent_voiced,
            seconds_since_last_voiced=session.since_voiced,
            stop=reading,
        )
        self._log_generation("Stop", category, phrase)
        return category, phrase

    def _speak_stop(self, phrase, kind=None):
        """A Stop may wait for a cold daemon: the turn has ended, and the
        hook is async."""
        self._speak_live(phrase, allow_cold_start=True, kind=kind)

    def _log_generation(self, event, category, phrase):
        if phrase:
            self._log(log_record.outcome(event, self._ollama_model, category, phrase))
        else:
            from phrase_gen import last_raw
            verdict, detail = describe_generation_failure(last_raw)
            self._log(f"[{event}] {verdict}: {detail}")

    # ── Main dispatch ──────────────────────────────────────────────────

    def run(self, hook_input):
        if not self._is_event_enabled(hook_input):
            return

        event = hook_input.get("hook_event_name", "")

        # PreToolUse commentary is handled by BaseEngine — it picks between
        # terse/normal LLM gating and chatty templates and routes through
        # speak_dynamic (overridden above to use the daemon).
        if event == "PreToolUse":
            return self._handle_commentary(hook_input)
        if event == "Stop":
            return self._handle_stop(hook_input)

        if event == "Notification":
            self._maybe_nudge_from_notification(hook_input)

        from phrase_gen import generate_or_skip
        from session_state import (transaction, mark_event, record_voiced,
                                   record_skipped, build_session_context,
                                   seconds_since_last_voiced)
        project = home.derive_project_label()
        risk = self._permission_risk(hook_input)  # may ask the decider: not under the lock
        event_detail = self._describe_event(hook_input)
        snapshot = None

        with transaction(project) as state:
            # Marks the session alive for the watchdog.
            mark_event(state)
            fixed = self._fixed_announcement(hook_input, state, risk)
            if fixed is None:
                if event_detail:
                    snapshot = (build_session_context(state),
                                list(state.get("recent_voiced", [])),
                                seconds_since_last_voiced(state))
                else:
                    record_skipped(state, event)

        if fixed is not None:
            return self._announce(fixed)
        if snapshot is None:
            self._log(f"[{event}] no event detail, skipping")
            return

        # Single Ollama call: decide + generate. It holds no lock.
        session_context, recent_voiced, since_voiced = snapshot
        category, phrase = generate_or_skip(
            event, event_detail, session_context,
            project=project, model=self._ollama_model,
            ollama_url=self._ollama_url,
            timeout=_OLLAMA_TIMEOUT,
            recent_voiced=recent_voiced,
            seconds_since_last_voiced=since_voiced,
        )

        with transaction(project) as state:
            if phrase:
                record_voiced(state, phrase)
            else:
                record_skipped(state, event)
        self._log_generation(event, category, phrase)
        if phrase:
            self._speak_live(phrase, allow_cold_start=False, kind=event_kind(hook_input))
