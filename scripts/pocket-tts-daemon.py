#!/usr/bin/env python3
"""pocket-tts-daemon.py — Persistent HTTP server for real-time Pocket TTS.

Keeps the Pocket TTS model loaded in memory and serves TTS requests via HTTP.
Auto-shuts down after an idle timeout to free resources.

Usage (run in the pocket-tts venv):
    venvs/pocket-tts/bin/python3 scripts/pocket-tts-daemon.py [--port 19850]

Endpoints:
    POST /generate  — {"text": "...", "voice": "charles"} → WAV bytes
    GET  /health    — {"status": "ok", "uptime": N, "pid": N}
    POST /shutdown  — graceful stop
"""

import argparse
import io
import json
import os
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PID_FILE = os.path.expanduser("~/.claude/pocket-tts-daemon.pid")
LOG_FILE = os.path.expanduser("~/.claude/pocket-tts-daemon.log")

# Globals set after model load
_tts_model = None
_voice_states = {}  # voice name -> cached model_state
_start_time = None
_idle_timer = None
_server = None
_default_voice = "charles"


def log(msg):
    try:
        from datetime import datetime
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _reset_idle_timer(timeout):
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    if timeout <= 0:
        return  # no idle timeout — daemon stays alive indefinitely
    _idle_timer = threading.Timer(timeout, _idle_shutdown)
    _idle_timer.daemon = True
    _idle_timer.start()


def _idle_shutdown():
    log("idle timeout reached, shutting down")
    _cleanup_and_exit()


def _cleanup_and_exit():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    if _server:
        threading.Thread(target=_server.shutdown, daemon=True).start()


def _get_voice_state(voice):
    if voice not in _voice_states:
        t0 = time.monotonic()
        _voice_states[voice] = _tts_model.get_state_for_audio_prompt(voice)
        log(f"loaded voice state for {voice!r} ({time.monotonic()-t0:.2f}s)")
    return _voice_states[voice]


class DaemonHandler(BaseHTTPRequestHandler):
    idle_timeout = 180  # overridden from args

    def log_message(self, format, *args):
        pass  # suppress default stderr logging

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({
                "status": "ok",
                "uptime": int(time.time() - _start_time),
                "pid": os.getpid(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/generate":
            self._handle_generate()
        elif self.path == "/shutdown":
            body = b'{"status":"shutting_down"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log("shutdown requested via HTTP")
            _cleanup_and_exit()
        else:
            self.send_error(404)

    def _handle_generate(self):
        _reset_idle_timer(self.idle_timeout)
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            params = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self.send_error(400, "Invalid JSON")
            return

        text = params.get("text", "").strip()
        if not text:
            self.send_error(400, "Missing 'text' field")
            return

        voice = params.get("voice", _default_voice)

        try:
            import numpy as np
            import scipy.io.wavfile as wav

            t0 = time.monotonic()
            model_state = _get_voice_state(voice)
            audio = _tts_model.generate_audio(model_state, text).numpy()

            # Pocket TTS outputs float audio well below full scale — normalize
            # so it isn't inaudible at normal system volume.
            peak = np.abs(audio).max()
            if peak > 1e-6:
                audio = audio * (0.95 / peak)

            buf = io.BytesIO()
            wav.write(buf, _tts_model.sample_rate, audio)
            wav_bytes = buf.getvalue()
            elapsed = time.monotonic() - t0
            log(f"generated ({elapsed:.2f}s, {len(wav_bytes)} bytes, voice={voice}) -> {text!r}")

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav_bytes)))
            self.end_headers()
            self.wfile.write(wav_bytes)

        except BrokenPipeError:
            log(f"client disconnected during generate -> {text!r}")
        except Exception as e:
            log(f"generate failed: {e}")
            try:
                self.send_error(500, f"TTS generation failed: {e}")
            except BrokenPipeError:
                pass


def main():
    global _tts_model, _start_time, _server, _default_voice

    parser = argparse.ArgumentParser(description="Pocket TTS daemon")
    parser.add_argument("--port", type=int, default=19850)
    parser.add_argument("--voice", default="charles",
                        help="Default Pocket TTS voice (default: charles)")
    parser.add_argument("--language", default="english")
    parser.add_argument("--temp", type=float, default=0.7,
                        help="Sampling temperature (default: 0.7)")
    parser.add_argument("--idle-timeout", type=int, default=180,
                        help="Seconds idle before auto-shutdown (default: 180)")
    args = parser.parse_args()

    log(f"loading model (temp={args.temp})...")
    print("Loading Pocket TTS model...", file=sys.stderr)
    from pocket_tts import TTSModel
    _tts_model = TTSModel.load_model(language=args.language, temp=args.temp)
    _default_voice = args.voice
    _get_voice_state(_default_voice)  # warm the default voice at startup
    print("Model loaded.", file=sys.stderr)
    log("model loaded")

    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    DaemonHandler.idle_timeout = args.idle_timeout
    _start_time = time.time()
    _reset_idle_timer(args.idle_timeout)

    signal.signal(signal.SIGTERM, lambda *_: _cleanup_and_exit())

    ThreadingHTTPServer.allow_reuse_address = True
    _server = ThreadingHTTPServer(("127.0.0.1", args.port), DaemonHandler)
    log(f"listening on 127.0.0.1:{args.port} (voice={args.voice}, idle_timeout={args.idle_timeout}s)")
    print(f"Pocket TTS daemon listening on http://127.0.0.1:{args.port}", file=sys.stderr)

    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup_and_exit()
        log("stopped")


if __name__ == "__main__":
    main()
