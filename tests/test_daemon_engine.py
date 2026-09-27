"""The daemon engine, at the seam where the two backends actually differ.

kokoro-realtime and pocket-tts are one engine with two specs. What varies is
how the daemon is started and what each /generate request carries; the rest
-- overlap lock, cold start, the say fallback -- is shared, and was untested
while it existed as two copies.
"""

import json
import os
import time

import pytest

import engines.daemon as daemon
from engines.kokoro_realtime import KokoroRealtimeEngine
from engines.pocket_tts_realtime import PocketTTSRealtimeEngine


def _engine(cls, **section):
    key = cls.spec.section
    return cls({"engine": "x", "personality": "hobson", "events": ["stop"], key: section})


class FakeDaemon:
    """Stands in for the daemon's HTTP endpoint."""

    def __init__(self, alive=True, wav=b"RIFF" + b"\0" * 200, fail=None):
        self.alive, self.wav, self.fail = alive, wav, fail
        self.requests = []

    def urlopen(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/health"):
            if not self.alive:
                raise OSError("connection refused")
            return _Resp(b"ok")
        self.requests.append({"url": url, "timeout": timeout, "headers": dict(req.header_items()),
                              "payload": json.loads(req.data.decode())})
        if self.fail:
            raise self.fail
        return _Resp(self.wav)


class _Resp:
    status = 200

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_daemon(monkeypatch):
    d = FakeDaemon()
    monkeypatch.setattr(daemon, "urlopen", d.urlopen)
    return d


# ── What each spec sends ────────────────────────────────────────────────────

def test_kokoro_starts_its_daemon_with_voice_and_speed(no_audio, monkeypatch):
    monkeypatch.setattr(daemon.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    eng = _engine(KokoroRealtimeEngine)
    monkeypatch.setattr(eng, "_daemon_alive", lambda: True)
    assert eng._start_daemon()
    argv = no_audio["popen"][-1]
    assert argv[0].endswith(os.path.join("venvs", "kokoro", "bin", "python3"))
    assert argv[1].endswith("kokoro-daemon.py")
    assert argv[2:] == ["--port", "19849", "--voice", "am_puck", "--speed", "1.1",
                        "--idle-timeout", "600"]


def test_pocket_starts_its_daemon_with_voice_language_and_temperature(no_audio, monkeypatch):
    monkeypatch.setattr(daemon.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    eng = _engine(PocketTTSRealtimeEngine)
    monkeypatch.setattr(eng, "_daemon_alive", lambda: True)
    assert eng._start_daemon()
    argv = no_audio["popen"][-1]
    assert argv[0].endswith(os.path.join("venvs", "pocket-tts", "bin", "python3"))
    assert argv[1].endswith("pocket-tts-daemon.py")
    assert argv[2:] == ["--port", "19850", "--voice", "charles", "--language", "english",
                        "--temp", "0.7", "--idle-timeout", "600"]


def test_kokoro_requests_name_voice_and_speed(fake_daemon, claude_home):
    eng = _engine(KokoroRealtimeEngine, voice="af_bella", speed=1.3)
    path = eng._daemon_generate("I pushed the branch.")
    req = fake_daemon.requests[-1]
    assert req["url"] == "http://127.0.0.1:19849/generate"
    assert req["payload"] == {"text": "I pushed the branch.", "voice": "af_bella", "speed": 1.3}
    assert req["timeout"] == 5
    assert path == str(claude_home / "kokoro-playback.wav")


def test_pocket_requests_name_only_the_voice(fake_daemon, claude_home):
    """Language and temperature are fixed when its daemon starts."""
    eng = _engine(PocketTTSRealtimeEngine, voice="alba", daemon_port=19999)
    path = eng._daemon_generate("I pushed the branch.")
    req = fake_daemon.requests[-1]
    assert req["url"] == "http://127.0.0.1:19999/generate"
    assert req["payload"] == {"text": "I pushed the branch.", "voice": "alba"}
    assert req["timeout"] == 10
    assert path == str(claude_home / "pocket-tts-playback.wav")


@pytest.mark.parametrize("cls, token_file", [(KokoroRealtimeEngine, "kokoro-daemon.token"),
                                              (PocketTTSRealtimeEngine, "pocket-tts-daemon.token")])
def test_generate_carries_the_token_its_daemon_wrote(cls, token_file, fake_daemon, claude_home):
    """The daemon refuses /generate without it (tts_daemon.py); it names the
    file after its script, and so does the engine."""
    eng = _engine(cls)
    eng._daemon_generate("I pushed the branch.")
    assert "X-hobson-token" not in fake_daemon.requests[-1]["headers"]
    (claude_home / token_file).write_text("t0k3n\n")
    eng._daemon_generate("I pushed the branch.")
    assert fake_daemon.requests[-1]["headers"]["X-hobson-token"] == "t0k3n"


def test_an_unset_option_takes_the_config_default(fake_daemon):
    from home import DEFAULT_CONFIG
    eng = _engine(KokoroRealtimeEngine, voice="af_bella")
    eng._daemon_generate("x" * 10)
    assert fake_daemon.requests[-1]["payload"]["speed"] == DEFAULT_CONFIG["kokoro"]["speed"]


# ── The shared playback path ────────────────────────────────────────────────

@pytest.mark.parametrize("cls", [KokoroRealtimeEngine, PocketTTSRealtimeEngine])
def test_a_live_daemon_plays_the_phrase(cls, fake_daemon, no_audio, claude_home):
    eng = _engine(cls)
    eng._speak_live("I pushed the branch.", allow_cold_start=True)
    assert no_audio["say"] == []
    assert no_audio["popen"][-1][0] == "afplay"
    assert no_audio["popen"][-1][-1] == str(claude_home / cls.spec.playback)


def test_a_down_daemon_falls_back_to_say_and_warms_up_for_next_time(
        fake_daemon, no_audio, monkeypatch):
    fake_daemon.alive = False
    eng = _engine(PocketTTSRealtimeEngine)
    started = []
    monkeypatch.setattr(eng, "_start_daemon_background", lambda: started.append(1))
    eng._speak_live("I pushed the branch.", allow_cold_start=False)
    assert no_audio["say"] == ["I pushed the branch."]
    assert started == [1]
    assert fake_daemon.requests == []


def test_a_failing_daemon_falls_back_to_say(fake_daemon, no_audio):
    fake_daemon.fail = OSError("boom")
    eng = _engine(KokoroRealtimeEngine)
    eng._speak_live("I pushed the branch.", allow_cold_start=True)
    assert no_audio["say"] == ["I pushed the branch."]


def test_a_phrase_right_after_another_is_dropped(fake_daemon, no_audio, claude_home, monkeypatch):
    """Async hooks: two utterances must not talk over each other."""
    (claude_home / "hobson.lock").write_text(str(time.time()))
    eng = _engine(PocketTTSRealtimeEngine)
    eng._speak_live("I pushed the branch.", allow_cold_start=False)
    assert no_audio["say"] == [] and fake_daemon.requests == []


def test_say_fallback_uses_the_personality_voice():
    """It used to re-read the config from disk, one engine with a
    claude-bark.json fallback the other lacked."""
    eng = _engine(KokoroRealtimeEngine)
    assert eng._say_voice == eng._load_say_voice(eng.config)
