#!/usr/bin/env python3
"""kokoro-daemon.py — Kokoro ONNX, served by the TTS daemon runner.

Keeps the Kokoro model loaded in memory and turns text into WAV for the
kokoro-realtime engine. tts_daemon.py does the serving: the routes, the
loopback guard, the idle timeout, the pid and token files.

Usage (run in the kokoro venv):
    venvs/kokoro/bin/python3 scripts/kokoro-daemon.py [--port 19849]

A request: {"text": "...", "voice": "am_puck", "speed": 1.1}, the voice one
of the voices file's, the speed 0.5 to 2.0.
"""

import io
import os
import sys

import home
import tts_daemon

MODEL_DIRS = [os.path.join(home.ROOT, "models"), home.path("models")]


def find_model(filename):
    for d in MODEL_DIRS:
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            return p
    return os.path.join(MODEL_DIRS[0], filename)


def add_arguments(parser):
    parser.add_argument("--speed", type=float, default=1.1,
                        help="Default speech speed (default: 1.1)")


def load(args, log):
    model_path = find_model("kokoro-v1.0.int8.onnx")
    voices_path = find_model("voices-v1.0.bin")
    for path in (model_path, voices_path):
        if not os.path.isfile(path):
            print(f"ERROR: Model file not found: {path}", file=sys.stderr)
            print("Run: hobson setup kokoro", file=sys.stderr)
            sys.exit(1)

    import soundfile as sf
    from kokoro_onnx import Kokoro
    kokoro = Kokoro(model_path, voices_path)

    def synthesize(text, voice, params):
        speed = params.get("speed", args.speed)
        if not isinstance(speed, (int, float)) or not 0.5 <= speed <= 2.0:
            raise ValueError(f"speed {speed!r} is outside 0.5 to 2.0")
        samples, sr = kokoro.create(text, voice=voice, speed=float(speed), lang="en-us")
        buf = io.BytesIO()
        sf.write(buf, samples, sr, format="WAV")
        return buf.getvalue()

    return tts_daemon.Backend(synthesize=synthesize, voices=frozenset(kokoro.get_voices()))


if __name__ == "__main__":
    tts_daemon.run(__file__, "Kokoro", 19849, "am_puck", add_arguments, load)
