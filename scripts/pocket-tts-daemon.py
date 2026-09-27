#!/usr/bin/env python3
"""pocket-tts-daemon.py — Pocket TTS, served by the TTS daemon runner.

Keeps the Pocket TTS model loaded in memory and turns text into WAV for the
pocket-tts engine. tts_daemon.py does the serving: the routes, the loopback
guard, the idle timeout, the pid and token files.

Usage (run in the pocket-tts venv):
    venvs/pocket-tts/bin/python3 scripts/pocket-tts-daemon.py [--port 19850]

A request: {"text": "...", "voice": "charles"}, the voice one of the
library's catalogue or the one this daemon was started with. Pocket TTS
fetches a voice named by URL and loads one named by path, so nothing else
reaches it.
"""

import io
import re
import time

import tts_daemon


def add_arguments(parser):
    parser.add_argument("--language", default="english")
    parser.add_argument("--temp", type=float, default=0.7,
                        help="Sampling temperature (default: 0.7)")


def catalogue():
    """The library's own voice names. The table is private to pocket_tts: if a
    release moves it, only the startup voice is allowed, never any string."""
    try:
        from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES
    except ImportError:
        return frozenset()
    return frozenset(name for name in _ORIGINS_OF_PREDEFINED_VOICES if re.fullmatch(r"[a-z_]+", name))


def load(args, log):
    import numpy as np
    import scipy.io.wavfile as wav
    from pocket_tts import TTSModel

    log(f"language={args.language}, temp={args.temp}")
    model = TTSModel.load_model(language=args.language, temp=args.temp)
    states = {}  # voice -> model state: only allowed voices reach it, so it stays small

    def state_for(voice):
        if voice not in states:
            t0 = time.monotonic()
            states[voice] = model.get_state_for_audio_prompt(voice)
            log(f"loaded voice state for {voice!r} ({time.monotonic() - t0:.2f}s)")
        return states[voice]

    state_for(args.voice)  # warm the default voice at startup

    def synthesize(text, voice, params):
        audio = model.generate_audio(state_for(voice), text).numpy()
        # Pocket TTS outputs float audio well below full scale — normalize
        # so it isn't inaudible at normal system volume.
        peak = np.abs(audio).max()
        if peak > 1e-6:
            audio = audio * (0.95 / peak)
        buf = io.BytesIO()
        wav.write(buf, model.sample_rate, audio)
        return buf.getvalue()

    return tts_daemon.Backend(synthesize=synthesize, voices=catalogue())


if __name__ == "__main__":
    tts_daemon.run(__file__, "Pocket TTS", 19850, "charles", add_arguments, load)
