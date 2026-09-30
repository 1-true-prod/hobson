"""Kokoro realtime engine — Kokoro ONNX via a persistent daemon, live only.

See daemon.py for the engine; this is the Kokoro daemon's spec.
"""

from .daemon import DaemonEngine, DaemonOption, DaemonSpec


class KokoroRealtimeEngine(DaemonEngine):
    engine_name = "kokoro-rt"
    spec = DaemonSpec(
        section="kokoro",
        venv="kokoro",
        script="kokoro-daemon.py",
        playback="kokoro-playback",
        startup_polls=40,       # Kokoro loads in 1-3s; up to 10s
        generate_timeout=5,
        options=(DaemonOption("voice", "--voice", True),
                 DaemonOption("speed", "--speed", True)),
    )
