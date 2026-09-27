"""Pocket TTS realtime engine — Pocket TTS via a persistent daemon, live only.

Experimental, and config-only: select it with "engine": "pocket-tts". See
daemon.py for the engine; this is the Pocket TTS daemon's spec.
"""

from .daemon import DaemonEngine, DaemonOption, DaemonSpec


class PocketTTSRealtimeEngine(DaemonEngine):
    engine_name = "pocket-tts"
    spec = DaemonSpec(
        section="pocket_tts",
        venv="pocket-tts",
        script="pocket-tts-daemon.py",
        playback="pocket-tts-playback.wav",
        startup_polls=80,       # model load is heavier than Kokoro's; up to 20s
        generate_timeout=10,
        # Language and temperature are fixed when the daemon starts; each
        # request names only the voice.
        options=(DaemonOption("voice", "--voice", True),
                 DaemonOption("language", "--language", False),
                 DaemonOption("temp", "--temp", False)),
    )
