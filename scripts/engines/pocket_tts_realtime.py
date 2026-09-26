"""Pocket TTS Realtime engine — all voice output goes through the live daemon.

Same live-only design as kokoro_realtime: every phrase renders through a
persistent Pocket TTS daemon, no pre-cached WAVs.
"""

import json
import os
import subprocess
import threading
import time
from urllib.request import urlopen, Request
from urllib.error import URLError

from .base import (BaseEngine, ROOT, derive_project_label, DEFAULT_DAEMON_IDLE_TIMEOUT,
                   request_for_prompt, stop_awaits_answer, stop_works_on_its_own)


DEFAULT_SAY_VOICE = "Daniel"


def _personality_say_voice():
    """Get the say voice from the active personality JSON."""
    personalities_dir = os.path.join(ROOT, "scripts", "personalities")
    config_file = os.path.expanduser("~/.claude/hobson.json")
    try:
        with open(config_file, encoding="utf-8") as f:
            name = json.load(f).get("personality", "hobson")
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_SAY_VOICE
    path = os.path.join(personalities_dir, name, "personality.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("say_voice", DEFAULT_SAY_VOICE)
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_SAY_VOICE


class PocketTTSRealtimeEngine(BaseEngine):
    """Pocket TTS with fully live voice generation via a persistent daemon.

    Falls back to macOS say with Daniel voice only when the daemon is down.
    """

    engine_name = "pocket-tts"
    templates_module = "bark_templates"
    cache_ext = "wav"

    def __init__(self, config):
        super().__init__(config)
        self._say_voice = _personality_say_voice()
        self.cache_dir = None  # no pre-generated cache — all audio is live

        pt_cfg = config.get("pocket_tts", {})
        port = pt_cfg.get("daemon_port", 19850)
        self._daemon_url = f"http://127.0.0.1:{port}"
        self._daemon_port = port
        self._voice = pt_cfg.get("voice", "charles")
        self._language = pt_cfg.get("language", "english")
        self._temp = pt_cfg.get("temp", 0.7)
        self._daemon_idle_timeout = pt_cfg.get(
            "daemon_idle_timeout", DEFAULT_DAEMON_IDLE_TIMEOUT
        )
        self._venv_python = os.path.join(ROOT, "venvs", "pocket-tts", "bin", "python3")
        self._daemon_script = os.path.join(ROOT, "scripts", "pocket-tts-daemon.py")

    # ── Daemon management ──────────────────────────────────────────────

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
                [
                    self._venv_python, self._daemon_script,
                    "--port", str(self._daemon_port),
                    "--voice", self._voice,
                    "--language", self._language,
                    "--temp", str(self._temp),
                    "--idle-timeout", str(self._daemon_idle_timeout),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._log(f"daemon: failed to start ({e})")
            return False

        # Model load is heavier than Kokoro's — allow up to 20s
        for _ in range(80):
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

    def _daemon_generate(self, text):
        """Send text to daemon, return path to a WAV file or None."""
        body = json.dumps({
            "text": text,
            "voice": self._voice,
        }).encode()

        try:
            req = Request(
                f"{self._daemon_url}/generate",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=10) as resp:
                wav_bytes = resp.read()
        except (URLError, OSError) as e:
            self._log(f"daemon generate failed: {e}")
            return None

        if not wav_bytes or len(wav_bytes) < 100:
            self._log("daemon returned empty/tiny response")
            return None

        playback_path = os.path.expanduser("~/.claude/pocket-tts-playback.wav")
        with open(playback_path, "wb") as f:
            f.write(wav_bytes)

        return playback_path

    # ── Live speak — all audio goes through here ───────────────────────

    def _update_bark_lock(self):
        from .base import BARK_LOCK_FILE
        try:
            with open(BARK_LOCK_FILE, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except OSError:
            pass

    def _speak_live(self, phrase, allow_cold_start=True):
        import fcntl
        from .base import BARK_LOCK_FILE

        if not allow_cold_start:
            fd = None
            try:
                fd = open(BARK_LOCK_FILE, "a+", encoding="utf-8")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd.seek(0)
                content = fd.read().strip()
                if content:
                    try:
                        if time.time() - float(content) < 2.0:
                            self._log(f"skipped (overlap) -> {phrase!r}")
                            if not self._daemon_alive():
                                self._start_daemon_background()
                            return
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
                return
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
                self._play_wav(wav_path)
                self._log(f"barked (daemon-live) -> {phrase!r}")
                return
            reason = "daemon error"
        else:
            if not allow_cold_start:
                self._start_daemon_background()
                reason = "daemon down"
            else:
                reason = "daemon start failed"

        self._say_with_volume(phrase)
        self._log(f"barked (say-fallback: {reason}) -> {phrase!r}")

    def _play_wav(self, path):
        try:
            self._afplay(path)
        except Exception as e:
            self._log(f"afplay failed: {e}")

    # ── Override backfill — not needed, we never use the template cache ─

    def backfill(self, text):
        pass

    # ── Override dynamic playback to use the daemon (BaseEngine seam) ──

    def speak_dynamic(self, phrase, allow_cold_start=True):
        self._speak_live(phrase, allow_cold_start=allow_cold_start)

    # ── Main dispatch — unified session-aware flow ─────────────────────

    def run(self, hook_input):
        if not self._is_event_enabled(hook_input):
            return

        event = hook_input.get("hook_event_name", "")

        if event == "PreToolUse":
            return self._handle_commentary(hook_input)

        if event == "Notification":
            self._maybe_nudge_from_notification(hook_input)

        from phrase_gen import generate_or_skip, last_raw
        from session_state import (load_session, save_session, record_voiced,
                                   record_skipped, build_session_context,
                                   seconds_since_last_voiced)
        project = derive_project_label()

        state = load_session(project)
        # Marks the session alive for the watchdog (Task 9) — no extra I/O,
        # this state was already being loaded and saved for this event.
        state["last_event_time"] = time.time()

        if event == "Notification" and self._speak_waiting(hook_input, state):
            return
        if event == "PermissionRequest" and (self._speak_risky_permission(hook_input, state)
                                             or self._speak_question(hook_input, state)):
            return

        if event == "Stop":
            state["last_stop_time"] = time.time()
            # Reset now, set below once classified: a stale "done" from the
            # previous turn must not stand in for this one if it fails.
            state["last_stop_category"] = None
            self._drop_pending_on_stop(state)

        event_detail = self._describe_event(hook_input)
        if not event_detail:
            self._log(f"[{event}] no event detail, skipping")
            record_skipped(state, event)
            save_session(state)
            return

        if event == "Stop" and stop_works_on_its_own(event_detail):
            return self._stop_still_working(state)

        session_context = build_session_context(
            state, task=request_for_prompt(hook_input, event_detail) if event == "Stop" else None)

        category, phrase = generate_or_skip(
            event, event_detail, session_context,
            project=project, model=self._ollama_model,
            ollama_url=self._ollama_url,
            # ponytail: 20s covers a cold Ollama load (~8s); see base.py note —
            # too-short timeouts cancel the load and never warm up.
            timeout=20,
            recent_voiced=state.get("recent_voiced", []),
            seconds_since_last_voiced=seconds_since_last_voiced(state),
            awaiting_answer=stop_awaits_answer(event_detail) if event == "Stop" else None,
        )

        if event == "Stop":
            state["last_stop_category"] = category

        if phrase:
            record_voiced(state, phrase)
            save_session(state)
            self._log(f"[{event}] ({self._ollama_model}) -> {category} -> {phrase!r}")
            self._speak_live(phrase, allow_cold_start=(event == "Stop"))
        else:
            record_skipped(state, event)
            save_session(state)
            from phrase_gen import last_raw as raw
            from .base import describe_generation_failure
            verdict, detail = describe_generation_failure(raw)
            self._log(f"[{event}] {verdict}: {detail}")
