"""Chatterbox TTS engine — local voice-cloned TTS with WAV caching."""

import os
import subprocess

from .base import BaseEngine
import home


def _find_reference_audio(personality="hobson"):
    """Find reference audio: try <personality>-reference, then hobson-reference (alfred-reference before 0.3.0), then reference."""
    search_names = []
    if personality and personality != "hobson":
        search_names.append(f"{personality}-reference")
    search_names.append("hobson-reference")
    search_names.append("alfred-reference")  # its name before 0.3.0
    search_names.append("reference")

    for name in search_names:
        for ext in ("wav", "mp3", "flac", "ogg", "m4a"):
            p = os.path.join(home.ROOT, "models", f"{name}.{ext}")
            if os.path.isfile(p):
                return p
            p2 = os.path.join(home.path("models"), f"{name}.{ext}")
            if os.path.isfile(p2):
                return p2
    return os.path.join(home.ROOT, "models", "hobson-reference.wav")


class ChatterboxEngine(BaseEngine):
    """Chatterbox TTS with voice cloning via reference audio.

    Uses pre-generated WAV cache. Falls back to macOS say on cache miss,
    then backfills the cache in background via the chatterbox venv.
    """

    engine_name = "chatterbox"
    templates_module = "bark_templates"
    cache_ext = "wav"

    def __init__(self, config):
        super().__init__(config)
        self.cache_dir = home.path("voice-cache-chatterbox")
        self._venv_python = os.path.join(home.ROOT, "venvs", "chatterbox", "bin", "python3")
        self._cache_gen = os.path.join(home.ROOT, "scripts", "cache-gen", "chatterbox_gen.py")
        self._reference_audio = _find_reference_audio(config.get("personality", "hobson"))

    def backfill(self, text):
        """Spawn background process to generate WAV via Chatterbox for a cache miss."""
        if not os.path.isfile(self._cache_gen):
            return
        if not os.path.isfile(self._venv_python):
            return
        if not os.path.isfile(self._reference_audio):
            return
        try:
            subprocess.Popen(
                [self._venv_python, self._cache_gen, "--single", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
