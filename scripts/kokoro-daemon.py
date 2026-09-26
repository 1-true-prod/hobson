#!/usr/bin/env python3
"""kokoro-daemon.py — Persistent HTTP server for real-time Kokoro ONNX TTS.

Keeps the Kokoro model loaded in memory and serves TTS requests via HTTP.
Auto-shuts down after an idle timeout to free resources.

Usage (run in the kokoro venv):
    venvs/kokoro/bin/python3 scripts/kokoro-daemon.py [--port 19849]

Endpoints:
    POST /generate  — {"text": "...", "voice": "am_puck", "speed": 1.1} → WAV bytes
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.expanduser("~/.claude/kokoro-daemon.pid")
LOG_FILE = os.path.expanduser("~/.claude/kokoro-daemon.log")
MODEL_DIRS = [os.path.join(ROOT, "models"), os.path.expanduser("~/.claude/models")]

# Globals set after model load
_kokoro = None
_start_time = None
_idle_timer = None
_server = None
_default_voice = "am_puck"
_default_speed = 1.1


def log(msg):
    try:
        from datetime import datetime
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def find_model(filename):
    for d in MODEL_DIRS:
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            return p
    return os.path.join(MODEL_DIRS[0], filename)


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
        speed = params.get("speed", _default_speed)

        try:
            import soundfile as sf
            t0 = time.monotonic()
            samples, sr = _kokoro.create(text, voice=voice, speed=speed, lang="en-us")
            buf = io.BytesIO()
            sf.write(buf, samples, sr, format="WAV")
            wav_bytes = buf.getvalue()
            elapsed = time.monotonic() - t0
            log(f"generated ({elapsed:.1f}s, {len(wav_bytes)} bytes) -> {text!r}")

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
    global _kokoro, _start_time, _server, _default_voice, _default_speed

    parser = argparse.ArgumentParser(description="Kokoro ONNX TTS daemon")
    parser.add_argument("--port", type=int, default=19849)
    parser.add_argument("--voice", default="am_puck",
                        help="Default Kokoro voice (default: am_puck)")
    parser.add_argument("--speed", type=float, default=1.1,
                        help="Default speech speed (default: 1.1)")
    parser.add_argument("--idle-timeout", type=int, default=180,
                        help="Seconds idle before auto-shutdown (default: 180)")
    args = parser.parse_args()

    model_path = find_model("kokoro-v1.0.int8.onnx")
    voices_path = find_model("voices-v1.0.bin")

    for path in (model_path, voices_path):
        if not os.path.isfile(path):
            print(f"ERROR: Model file not found: {path}", file=sys.stderr)
            print("Run: hobson setup kokoro", file=sys.stderr)
            sys.exit(1)

    log(f"loading model...")
    print("Loading Kokoro model...", file=sys.stderr)
    from kokoro_onnx import Kokoro
    _kokoro = Kokoro(model_path, voices_path)
    _default_voice = args.voice
    _default_speed = args.speed
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
    print(f"Kokoro daemon listening on http://127.0.0.1:{args.port}", file=sys.stderr)

    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup_and_exit()
        log("stopped")


if __name__ == "__main__":
    main()
