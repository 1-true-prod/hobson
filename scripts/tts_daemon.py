"""tts_daemon.py — the one HTTP runner behind both TTS daemons.

kokoro-daemon.py and pocket-tts-daemon.py are backends: each loads its model
and turns text into WAV bytes. Everything else is here once: the routes, the
loopback guard, the idle timeout, and the pid, token and log files. It was
two near-copies, and neither had any guard.

Endpoints:
    GET  /health    {"status": "ok", "uptime": N, "pid": N}; no token
    POST /generate  {"text": "...", "voice": "...", ...} -> WAV bytes; token
    POST /shutdown  graceful stop; token

A request may name a voice only from its backend's catalogue, or the one the
daemon was started with (which may be a path the user configured). Pocket
TTS fetches a voice named by URL, and loads one named by path, before any
other check, so a request's voice was a way to make this Mac fetch anything.

A daemon's files are named after its script (kokoro-daemon.py ->
~/.claude/kokoro-daemon.{pid,token,log}); the engine finds the token the
same way.
"""

import argparse
import os
import signal
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime

import home
import loopback

# synthesize(text, voice, params) -> WAV bytes, where params is the request's
# JSON object; voices: the names a request may pick besides the startup voice.
Backend = namedtuple("Backend", ["synthesize", "voices"])

MAX_BODY = 16 * 1024  # a phrase is a dozen words


class Daemon:
    """One backend, served on 127.0.0.1 until it idles out or is stopped."""

    def __init__(self, script, backend, default_voice, idle_timeout):
        stem = os.path.splitext(os.path.basename(script))[0]
        self.pid_file = home.path(f"{stem}.pid")
        self.token_file = home.path(f"{stem}.token")
        self.log_file = home.path(f"{stem}.log")
        self.backend = backend
        self.default_voice = default_voice
        self.idle_timeout = idle_timeout
        self.started = time.time()
        self.server = None
        self._timer = None
        self._timer_lock = threading.Lock()

    def log(self, msg):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def bind(self, port):
        """Take the port, then write the token and pid files. In that order:
        of two daemons started at once, the one that loses the port never
        overwrites the other's token, which would lock every client out."""
        self.server = loopback.Server(port, _Handler)
        self.server.tts = self
        self.server.token = loopback.issue_token(self.token_file)
        with open(self.pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self.touch()
        return self.server

    def serve(self, poll_interval=0.5):
        try:
            self.server.serve_forever(poll_interval=poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self._remove_files()
            self.log("stopped")

    def stop(self):
        """Stop serving; safe from a request thread, the idle timer or a
        signal handler (shutdown() waits for serve_forever, so not inline)."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
        self._remove_files()
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def touch(self):
        """Restart the idle countdown (a timeout of 0 or less: stay up).
        Locked: two requests at once used to leave an orphan timer that shut
        a busy daemon down."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            if self.idle_timeout > 0:
                self._timer = threading.Timer(self.idle_timeout, self._idle)
                self._timer.daemon = True
                self._timer.start()

    def _idle(self):
        self.log("idle timeout reached, shutting down")
        self.stop()

    def voice_allowed(self, voice):
        return isinstance(voice, str) and (voice == self.default_voice or voice in self.backend.voices)

    def _remove_files(self):
        """Remove the pid and token files, only while they are still this daemon's."""
        for path, mine in ((self.pid_file, str(os.getpid())), (self.token_file, self.server.token)):
            try:
                with open(path, encoding="utf-8") as f:
                    if f.read().strip() != mine:
                        continue
                os.remove(path)
            except OSError:
                pass


class _Handler(loopback.GuardedHandler):
    max_body = MAX_BODY

    def do_GET(self):
        if not self.guard(token=False):
            return
        if self.path != "/health":
            return self.send_json(404, {"error": "not found"})
        tts = self.server.tts
        self.send_json(200, {"status": "ok", "uptime": int(time.time() - tts.started),
                             "pid": os.getpid()})

    def do_POST(self):
        if not self.guard(token=True):
            return
        tts = self.server.tts
        if self.path == "/shutdown":
            self.send_json(200, {"status": "shutting_down"})
            tts.log("shutdown requested via HTTP")
            return tts.stop()
        if self.path != "/generate":
            return self.send_json(404, {"error": "not found"})
        tts.touch()
        try:
            params = self.read_json()
        except ValueError:
            return self.send_json(400, {"error": "invalid JSON"})
        text = params.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            return self.send_json(400, {"error": "missing 'text'"})
        voice = params.get("voice", tts.default_voice)
        if not tts.voice_allowed(voice):
            tts.log(f"refused a voice outside the catalogue: {str(voice)[:80]!r}")
            return self.send_json(400, {"error": "unknown voice"})
        try:
            t0 = time.monotonic()
            wav = tts.backend.synthesize(text, voice, params)
            tts.log(f"generated ({time.monotonic() - t0:.2f}s, {len(wav)} bytes, "
                    f"voice={voice}) -> {text!r}")
            self.send_body(200, wav, "audio/wav")
        except BrokenPipeError:
            tts.log(f"client disconnected during generate -> {text!r}")
        except Exception as e:
            tts.log(f"generate failed: {e}")
            try:
                self.send_json(500, {"error": f"TTS generation failed: {e}"})
            except BrokenPipeError:
                pass


def run(script, title, default_port, default_voice, add_arguments, load):
    """A backend's main(): parse its command line, load it, and serve.
    add_arguments(parser) declares the backend's own options; load(args,
    log) loads the model and returns a Backend."""
    parser = argparse.ArgumentParser(description=f"{title} TTS daemon")
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--voice", default=default_voice,
                        help=f"Default voice (default: {default_voice})")
    parser.add_argument("--idle-timeout", type=int, default=180,
                        help="Seconds idle before auto-shutdown (default: 180)")
    add_arguments(parser)
    args = parser.parse_args()

    os.makedirs(home.state_dir(), exist_ok=True)
    tts = Daemon(script, None, args.voice, args.idle_timeout)
    tts.log("loading model...")
    print(f"Loading {title} model...", file=sys.stderr)
    tts.backend = load(args, tts.log)
    print("Model loaded.", file=sys.stderr)
    tts.log("model loaded")

    tts.bind(args.port)
    signal.signal(signal.SIGTERM, lambda *_: tts.stop())
    tts.log(f"listening on 127.0.0.1:{args.port} (voice={args.voice}, "
            f"idle_timeout={args.idle_timeout}s)")
    print(f"{title} daemon listening on http://127.0.0.1:{args.port}", file=sys.stderr)
    tts.serve()
