"""hobson say (read_aloud.py): text read aloud a passage at a time, each one
waited for, and stopped by hobson off.

It replaced a loop over `hobson test "<paragraph>"`, which returned as soon as
its player started and so stacked a whole spec's paragraphs on top of each
other.
"""

import json
import os

import pytest

import read_aloud
import voice


# ── Passages ────────────────────────────────────────────────────────────────

def test_paragraphs_are_passages_and_wrapped_lines_are_joined():
    text = "Overview.\nThis spec defines\ntwo item types.\n\n\nRollout guard.\n"
    assert read_aloud.passages(text) == ["Overview. This spec defines two item types.", "Rollout guard."]


def test_markdown_is_read_as_prose():
    text = ("# Contract\n\n"
            "- **Store** catalogue\n- the `item_type` field\n\n"
            "> See [the Slack thread](https://example.com/x).\n\n"
            "```kotlin\n```")
    assert read_aloud.passages(text) == [
        "Contract.",
        "Store catalogue. the item_type field.",
        "See the Slack thread.",
    ]


def test_a_long_paragraph_is_cut_at_sentence_ends():
    sentence = "The backend returns item type thumbnail with the download URL. "
    parts = read_aloud.passages(sentence * 12)
    assert len(parts) > 1
    assert all(len(p) <= read_aloud.MAX_CHARS for p in parts)
    assert all(p.endswith("URL.") for p in parts)
    assert " ".join(parts) == (sentence * 12).strip()


def test_a_sentence_longer_than_a_passage_is_cut_at_a_space():
    words = " ".join(["thumbnail"] * 80)
    parts = read_aloud.passages(words)
    assert all(len(p) <= read_aloud.MAX_CHARS for p in parts)
    assert " ".join(parts) == words


def test_nothing_to_read():
    assert read_aloud.passages("\n  \n```\n```\n") == []
    assert read_aloud.read("  \n", config={}) == "empty"


# ── Reading ─────────────────────────────────────────────────────────────────

class FakePlayer:
    def __init__(self, events, name, finishes=True):
        self.events, self.name, self.finishes = events, name, finishes
        self.done = False

    def poll(self):
        return 0 if self.done else None

    def wait(self, timeout=None):
        if self.finishes:
            if not self.done:
                self.events.append(f"end {self.name}")
            self.done = True
            return 0
        import subprocess
        raise subprocess.TimeoutExpired("afplay", timeout)

    def terminate(self):
        self.events.append(f"terminate {self.name}")
        self.done = True


class FakeEngine:
    def __init__(self, tmp_path, finishes=True, on_play=None):
        self.tmp_path, self.finishes, self.on_play = tmp_path, finishes, on_play
        self.events, self.clips = [], []

    def render(self, text, timeout=None):
        assert timeout == read_aloud.RENDER_TIMEOUT
        path = self.tmp_path / f"clip{len(self.clips)}.wav"
        path.write_bytes(b"RIFF")
        self.clips.append(path)
        self.events.append(f"render {text}")
        return str(path)

    def _play(self, path, text, kind=None, cleanup=False):
        assert kind == "answer" and not cleanup
        self.events.append(f"play {text}")
        if self.on_play:
            self.on_play()
        return FakePlayer(self.events, text, self.finishes)

    def _log(self, msg):
        pass


@pytest.fixture
def engine(monkeypatch, tmp_path, claude_home):
    import hobson
    fake = FakeEngine(tmp_path)
    monkeypatch.setattr(hobson, "load_engine", lambda config: fake)
    monkeypatch.setattr(read_aloud, "GAP_SECONDS", 0)
    return fake


def test_each_passage_is_rendered_while_the_last_plays_and_waited_for(engine):
    assert read_aloud.read("One.\n\nTwo.\n\nThree.", config={}, echo=lambda *_: None) == "read"
    assert engine.events == [
        "render One.", "play One.",
        "render Two.", "end One.", "play Two.",
        "render Three.", "end Two.", "play Three.",
        "end Three.",
    ]
    assert not any(clip.exists() for clip in engine.clips)


def test_muted_reads_nothing(engine, claude_home):
    assert read_aloud.read("One.", config={"muted": True}) == "silent (muted)"
    assert engine.events == []


def test_hobson_off_stops_the_passage_playing(engine, claude_home, monkeypatch):
    """The emergency brake: `hobson test` bypassed it, so muting did not
    stop the spec."""
    monkeypatch.setattr(read_aloud, "POLL_SECONDS", 0)
    engine.finishes = False

    def mute():
        (claude_home / "hobson.json").write_text(json.dumps({"muted": True}))
    engine.on_play = mute
    assert read_aloud.read("One.\n\nTwo.", config={}, echo=lambda *_: None) == "stopped (muted)"
    assert engine.events == ["render One.", "play One.", "render Two.", "terminate One."]
    assert not any(clip.exists() for clip in engine.clips)


def test_ctrl_c_stops_the_passage_and_cleans_up(engine, monkeypatch):
    engine.finishes = False

    def interrupt():
        raise KeyboardInterrupt
    monkeypatch.setattr(read_aloud, "_wait", lambda player: interrupt() if player else None)
    assert read_aloud.read("One.\n\nTwo.", config={}, echo=lambda *_: None) == "interrupted"
    assert engine.events[-1] == "terminate One."
    assert not any(clip.exists() for clip in engine.clips)


def test_a_second_reading_waits_for_the_first(engine, monkeypatch):
    monkeypatch.setattr(read_aloud, "READING_WAIT", 0.1)
    with voice.reading(0):
        assert read_aloud.read("One.", config={}, echo=lambda *_: None) == "busy"
    assert engine.events == []


def test_the_cli_reads_a_file_or_standard_input(engine, tmp_path, monkeypatch, capsys):
    spec = tmp_path / "spec.md"
    spec.write_text("# Overview\n\nTwo item types.\n")
    assert read_aloud.main(["-f", str(spec)]) == 0
    assert [e for e in engine.events if e.startswith("play")] == ["play Overview.", "play Two item types."]

    engine.events.clear()
    assert read_aloud.main(["Hello", "there."]) == 0
    assert engine.events[1] == "play Hello there."

    assert read_aloud.main(["-f", str(tmp_path / "missing.md")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_a_reading_is_hobsons_own_talk_for_the_recap():
    import log_record
    import recap
    assert recap._own_talk("[read] reading 3 passage(s), 120 characters")
    assert recap._own_talk(log_record.playback("read-aloud", "Overview.", "answer"))
