"""hobson coach (coach.py): a script of spoken steps run on Hobson's own
clock, with every other session's announcement held until it ends.

It replaced chained `hobson say` calls, whose only pauses were how long each
line took to say: "Ten seconds. Get into position." went straight on to the
countdown.
"""

import json

import pytest

import coach
import presence
import voice


# ── The script ──────────────────────────────────────────────────────────────

def test_a_script_is_parsed_line_by_line():
    steps = coach.parse("# framing\nsay Stand back.\n\nwait 10\ncountdown 3\nready\nready 5\n")
    assert [(s.line, s.do, s.seconds, s.parts) for s in steps] == [
        (2, "say", None, ("Stand back.",)),
        (4, "wait", 10.0, ()),
        (5, "countdown", None, ("Three.", "Two.", "One.")),
        (6, "ready", coach.READY_SECONDS, ()),
        (7, "ready", 5.0, ()),
    ]


def test_a_long_say_is_cut_into_passages_as_hobson_say_cuts_it():
    sentence = "Keep both feet flat and your shoulders level with the floor. "
    (step,) = coach.parse("say " + sentence * 8)
    assert len(step.parts) > 1
    assert " ".join(step.parts) == (sentence * 8).strip()


@pytest.mark.parametrize("script,complaint", [
    ("say Hello.\nsing", "line 2: no step called 'sing'"),
    ("say", "line 1: say what?"),
    ("wait", "line 1: wait takes a number of seconds"),
    ("wait soon", "line 1: wait takes a number of seconds, not 'soon'"),
    ("wait 0", "more than 0"),
    ("wait 601", "at most 600"),
    ("ready -1", "more than 0"),
    ("countdown 0", "from 1 to 10"),
    ("countdown 11", "from 1 to 10"),
    ("countdown 2.5", "from 1 to 10"),
])
def test_a_bad_line_is_named_before_anything_is_said(script, complaint):
    with pytest.raises(coach.ScriptError) as error:
        coach.parse(script)
    assert complaint in str(error.value)


# ── Clips ───────────────────────────────────────────────────────────────────

def _wav(path, pieces, rate=24000, float_samples=True):
    """A mono WAV of (seconds, amplitude) pieces: a tone, or near-silence."""
    import array
    import math
    import struct
    samples = []
    for seconds, amplitude in pieces:
        samples += [amplitude * math.sin(2 * math.pi * 220 * i / rate) for i in range(int(seconds * rate))]
    if float_samples:
        audio, fmt = array.array("f", samples).tobytes(), struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    else:
        audio = array.array("h", [int(x * 32767) for x in samples]).tobytes()
        fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(audio)) + audio
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def _seconds_of(path):
    import struct
    data = path.read_bytes()
    chunks = dict(coach._chunks(data))
    _, _, rate, per_second, _, _ = struct.unpack("<HHIIHH", chunks[b"fmt "][:16])
    return len(chunks[b"data"]) / per_second


@pytest.mark.parametrize("float_samples", [True, False])
def test_a_clip_is_cut_down_to_its_speech(tmp_path, float_samples):
    """As Pocket TTS renders one word: a click, 0.8 s at -66 dB, the word, a long tail."""
    clip = _wav(tmp_path / "three.wav", [(0.01, 0.01), (0.8, 0.0007), (0.35, 0.3), (0.6, 0.0007)],
                float_samples=float_samples)
    coach._trim(str(clip))
    assert _seconds_of(clip) == pytest.approx(0.02 + 0.35 + coach.TRIM_TAIL, abs=0.02)


def test_silence_and_other_formats_are_left_alone(tmp_path):
    quiet = _wav(tmp_path / "quiet.wav", [(1.0, 0.0005)])
    before = quiet.read_bytes()
    coach._trim(str(quiet))
    assert quiet.read_bytes() == before
    aiff = tmp_path / "say.aiff"
    aiff.write_bytes(b"FORM\0\0\0\x04AIFF")
    coach._trim(str(aiff))
    assert aiff.read_bytes() == b"FORM\0\0\0\x04AIFF"
    coach._trim(str(tmp_path / "missing.wav"))


def test_the_speech_span_keeps_a_breath_either_side_and_skips_a_click():
    """Every Pocket TTS clip opens with a 10 ms click at about -42 dB: taken
    for speech, it kept the whole silence after it, in half the renders."""
    levels = [-42.0] + [-66.0] * 79 + [-30.0, -10.0, -20.0, -40.0] + [-66.0] * 50
    start, end = coach.speech_span(levels)
    assert start == pytest.approx(0.80 - coach.TRIM_LEAD)
    assert end == pytest.approx(0.84 + coach.TRIM_TAIL)
    # Nothing sustained at all: the short sound is all there is, so it is kept.
    assert coach.speech_span([-66.0] * 5 + [-10.0] + [-66.0] * 20) == pytest.approx(
        (0.05 - coach.TRIM_LEAD, 0.06 + coach.TRIM_TAIL))
    assert coach.speech_span([-80.0] * 10) is None and coach.speech_span([]) is None


# ── Running ─────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds


class FakePlayer:
    def __init__(self, events, name, finishes):
        self.events, self.name, self.finishes, self.done = events, name, finishes, False

    def poll(self):
        return 0 if self.done else None

    def wait(self, timeout=None):
        if not self.finishes:
            import subprocess
            raise subprocess.TimeoutExpired("afplay", timeout)
        self.done = True
        return 0

    def terminate(self):
        self.events.append(f"terminate {self.name}")
        self.done = True


class FakeEngine:
    def __init__(self, tmp_path, clock):
        self.tmp_path, self.clock = tmp_path, clock
        self.events, self.clips, self.spoken = [], [], []
        self.finishes, self.unrenderable, self.on_play = True, (), None

    def render(self, text, timeout=None):
        self.events.append(f"render {text}")
        if text in self.unrenderable:
            return None
        path = self.tmp_path / f"clip{len(self.clips)}.wav"
        path.write_bytes(b"RIFF")
        self.clips.append(path)
        return str(path)

    def play(self, path, text, kind, cleanup=False):
        assert kind == "answer" and not cleanup
        self.events.append(f"{self.clock.now - 1000:g} play {text}")
        if self.on_play:
            self.on_play(text)
        return FakePlayer(self.events, text, self.finishes)

    def speak(self, text, kind, cold_start=False):
        self.spoken.append((kind, text))

    def _log(self, msg):
        pass


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def engine(monkeypatch, tmp_path, clock):
    import hobson
    fake = FakeEngine(tmp_path, clock)
    monkeypatch.setattr(hobson, "load_engine", lambda config: fake)
    return fake


@pytest.fixture
def go(clock):
    """Run a script with the fake clock; (how it ended, what was emitted)."""
    def go(script, idle=lambda: None, config=None):
        emitted = []
        ended = coach.run(coach.parse(script), config={} if config is None else config,
                          emit=emitted.append, clock=clock, sleep=clock.sleep, idle=idle)
        return ended, emitted
    return go


def _steps(emitted):
    return [{k: v for k, v in e.items() if k != "event"} for e in emitted if e["event"] == "step"]


def test_the_steps_keep_time(engine, go):
    ended, emitted = go("say Get into position.\nwait 10\ncountdown 3\nsay Relax.")
    assert ended == "done"
    assert _steps(emitted) == [
        {"line": 1, "do": "say", "start": 0, "end": 0},
        {"line": 2, "do": "wait", "start": 0, "end": 10},
        {"line": 3, "do": "countdown", "start": 10, "end": 13},
        {"line": 4, "do": "say", "start": 13, "end": 13},
    ]
    plays = [e for e in engine.events if " play " in e]
    assert plays == ["0 play Get into position.", "10 play Three.", "11 play Two.",
                     "12 play One.", "13 play Relax."]
    assert emitted[0]["event"] == "start" and emitted[0]["steps"] == 4
    assert emitted[-1] == {"event": "end", "ended": "done", "seconds": 13, "held": 0}


def test_a_number_still_sounding_at_the_next_beat_is_cut(engine, go):
    """A clip that outlasts its beat would hold the voice, and every number
    after it would slip."""
    engine.on_play = lambda text: setattr(engine, "finishes", text == "One.")
    go("countdown 3")
    events = [e for e in engine.events if not e.startswith("render")]
    assert events[:5] == ["0 play Three.", "terminate Three.", "1 play Two.",
                          "terminate Two.", "2 play One."]


def test_every_clip_is_rendered_once_before_the_clock_starts_and_removed_after(engine, go):
    go("countdown 2\nsay Two.\ncountdown 2")
    renders = [e for e in engine.events if e.startswith("render")]
    assert renders == ["render Two.", "render One."]
    assert engine.events.index(renders[-1]) < next(i for i, e in enumerate(engine.events) if " play " in e)
    assert not any(clip.exists() for clip in engine.clips)


def test_a_clip_that_cannot_be_made_says_nothing(engine, go):
    engine.unrenderable = ("One.",)
    ended, emitted = go("say Ready.\ncountdown 3")
    assert ended == "unrendered"
    assert not any(" play " in e for e in engine.events)
    assert emitted == [{"event": "end", "ended": "unrendered", "line": 2, "held": 0}]
    assert not any(clip.exists() for clip in engine.clips)


def test_ready_ends_at_the_first_input_after_it_begins(engine, go, clock):
    began = []

    def idle():
        # Someone clicked 2.5 seconds into the wait, and had touched
        # nothing for a minute before it.
        if not began:
            began.append(clock.now)
        since_click = clock.now - (began[0] + 2.5)
        return since_click if since_click >= 0 else 60 + clock.now - began[0]
    ended, emitted = go("ready 30", idle=idle)
    (step,) = _steps(emitted)
    assert step["by"] == "input" and step["after"] == pytest.approx(2.5, abs=0.11)
    assert step["end"] == pytest.approx(2.5, abs=0.11)


def test_ready_without_input_times_out(engine, go):
    ended, emitted = go("ready 5", idle=lambda: 600.0)
    assert _steps(emitted) == [{"line": 1, "do": "ready", "start": 0, "by": "timeout", "end": 5}]


def test_ready_says_when_input_could_not_be_read(engine, go):
    ended, emitted = go("ready 2")
    assert _steps(emitted)[0]["input"] == "unreadable"


def test_hobson_off_stops_a_run_where_it_is(engine, go, claude_home):
    def mute(text):
        if text == "Two.":
            (claude_home / "hobson.json").write_text(json.dumps({"muted": True}))
    engine.on_play = mute
    ended, emitted = go("say Begin.\ncountdown 3\nsay Never said.")
    assert ended == "stopped (muted)"
    assert emitted[-1]["ended"] == "stopped (muted)" and emitted[-1]["line"] == 2
    assert [e["line"] for e in emitted if e["event"] == "step"] == [1]
    assert not any("Never said." in e for e in engine.events if " play " in e)
    assert "terminate Two." in engine.events
    assert not any(clip.exists() for clip in engine.clips)


def test_ctrl_c_stops_the_run_and_cleans_up(engine, go):
    def interrupt(text):
        if text == "Hold.":
            raise KeyboardInterrupt
    engine.on_play = interrupt
    ended, emitted = go("say Hold.\nwait 5")
    assert ended == "interrupted" and emitted[-1]["line"] == 1
    assert not any(clip.exists() for clip in engine.clips)


def test_ctrl_c_while_rendering_says_nothing_and_cleans_up(engine, go):
    def render(text, timeout=None):
        if engine.clips:
            raise KeyboardInterrupt
        return FakeEngine.render(engine, text, timeout)
    engine.render = render
    ended, emitted = go("say Begin.\nsay Hold.")
    assert ended == "interrupted" and emitted == [{"event": "end", "ended": "interrupted", "held": 0}]
    assert len(engine.clips) == 1 and not engine.clips[0].exists()
    assert not any(" play " in e for e in engine.events)


def test_muted_runs_nothing(engine, go):
    ended, emitted = go("say Hello.", config={"muted": True})
    assert ended == "silent (muted)" and engine.events == []


def test_a_run_waits_for_a_reading_and_gives_up(engine, go, monkeypatch):
    monkeypatch.setattr(coach, "READING_WAIT", 0.1)
    with voice.reading(0):
        ended, emitted = go("say Hello.")
    assert ended == "busy" and engine.events == []


# ── Everything else waits for the run ──────────────────────────────────────

def test_while_a_run_goes_other_sessions_are_held_and_told_at_the_end(engine, go):
    """Presence is paused (no sensor at all here), so the room is
    "unknown", which speaks: the run must hold regardless."""
    routed = {}

    def meanwhile(text):
        if text == "Two.":
            config = {"presence": {"enabled": False}}
            routed["done"] = presence.route(config, "done", "I shipped the release.", project="webapp")
            routed["commentary"] = presence.route(config, "commentary", "I'm running the tests.")
            routed["answer"] = presence.route(config, "answer", "All quiet.")
            routed["state"] = presence.audience(config)[0]
            routed["nudge paused"] = presence.pauses_nudge(routed["state"])
    engine.on_play = meanwhile
    ended, emitted = go("countdown 3")
    assert routed == {"done": None, "commentary": None, "answer": "All quiet.",
                      "state": "coaching", "nudge paused": True}
    assert engine.spoken == [("briefing", "In the meantime: On webapp: I shipped the release.")]
    assert emitted[-1]["held"] == 1
    assert presence.held() == []
    assert presence.audience({"presence": {"enabled": False}})[0] == "unknown"


def test_silenced_at_the_end_keeps_what_was_held(engine, go, claude_home):
    def meanwhile(text):
        presence.route({}, "done", "I shipped the release.", project="webapp")
        (claude_home / "hobson.json").write_text(json.dumps({"muted": True}))
    engine.on_play = meanwhile
    ended, emitted = go("say Begin.")
    assert ended == "stopped (muted)" and engine.spoken == []
    assert [i["phrase"] for i in presence.held()] == ["I shipped the release."]


def test_presence_says_nothing_at_a_transition_or_a_wave_during_a_run(claude_home, monkeypatch):
    said = []
    monkeypatch.setattr(presence, "_speak", lambda config, text: said.append(text))
    presence.hold("webapp", "done", "I shipped the release.")
    config = {"presence": {"enabled": True}}
    with voice.reading(0), voice.coaching():
        assert presence.on_transition("present", "away", "camera", config=config) is None
        assert presence.on_transition("away", "present", "input", away_for=600, config=config) is None
        assert presence.on_wave(config=config) is None
    assert said == [] and len(presence.held()) == 1


# ── The command ─────────────────────────────────────────────────────────────

def test_the_cli_reads_a_script_and_rejects_a_bad_one(engine, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(coach, "POLL_SECONDS", 0.001)
    script = tmp_path / "steps.txt"
    script.write_text("say Hello.\nwait 0.01\n")
    assert coach.main(["-f", str(script)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["start", "step", "step", "end"]

    script.write_text("say Hello.\njump 3\n")
    assert coach.main(["-f", str(script)]) == 2
    assert "line 2: no step called 'jump'" in capsys.readouterr().err
    assert coach.main(["-f", str(tmp_path / "missing.txt")]) == 2


def test_input_is_read_without_the_presence_helper():
    idle = coach.idle_seconds()
    assert idle is None or idle >= 0


def test_a_run_is_hobsons_own_talk_for_the_recap():
    import recap
    assert recap._own_talk("[coach] running 4 step(s)")
