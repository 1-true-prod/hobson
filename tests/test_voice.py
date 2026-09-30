"""One voice at a time (voice.py): every clip waits its turn, and the player
holds the turn until it exits.

A script reading a spec aloud through the CLI stacked a new paragraph on the
one still playing every few seconds, with other sessions' announcements on
top: nothing made a clip wait for the last, since afplay was detached.
"""

import os
import subprocess
import sys
import time

import pytest

import voice
from engines.say import SayEngine

_REAL_POPEN = subprocess.Popen  # the autouse no_audio fixture replaces it

# A stand-in for afplay: logs when it starts and ends, and plays for 0.3s.
_PLAYER = ("import sys, time\n"
           "log = sys.argv[1]\n"
           "open(log, 'a').write(f'start {time.time()}\\n')\n"
           "time.sleep(0.3)\n"
           "open(log, 'a').write(f'end {time.time()}\\n')\n")


def _intervals(log):
    stamps = [line.split() for line in open(log).read().splitlines()]
    starts = [float(t) for kind, t in stamps if kind == "start"]
    ends = [float(t) for kind, t in stamps if kind == "end"]
    return sorted(zip(sorted(starts), sorted(ends)))


def test_a_turn_is_held_until_it_is_let_go(claude_home):
    turn = voice.take_turn()
    assert turn is not None and voice.busy()
    assert voice.take_turn(timeout=0) is None
    os.close(turn)
    assert not voice.busy()


def test_the_player_keeps_the_turn_after_the_speaker_lets_go(claude_home):
    """The hook that started a clip exits long before the clip ends: the
    lock must go with the player, not with the hook."""
    turn = voice.take_turn()
    player = _REAL_POPEN(["/bin/sleep", "0.2"], pass_fds=(turn,))
    os.close(turn)
    assert voice.busy()
    player.wait()
    assert not voice.busy()


def test_a_turn_not_had_in_time_is_passed_over(claude_home):
    held = voice.take_turn()
    began = time.monotonic()
    assert voice.take_turn(timeout=0.2) is None
    assert 0.2 <= time.monotonic() - began < 1
    os.close(held)


@pytest.mark.parametrize("cleanup", [False, True])
def test_two_clips_never_play_at_once(claude_home, tmp_path, monkeypatch, cleanup):
    """Through the one playback seam, _play: the second clip starts only once
    the first has finished, though _play returns as soon as each starts.
    With cleanup (every hook's clip) the player is `sh -c "afplay …; rm …"`,
    and the turn goes through sh to afplay and is let go when sh exits."""
    import engines.base as base
    log = tmp_path / "played"
    monkeypatch.setattr(base.subprocess, "Popen", _REAL_POPEN)
    monkeypatch.setattr(SayEngine, "_afplay_args",
                        lambda self, path: [sys.executable, "-c", _PLAYER, str(log)])
    clips = [tmp_path / "a.aiff", tmp_path / "b.aiff"]
    for clip in clips:
        clip.write_bytes(b"FORM")
    engine = SayEngine({})
    began = time.monotonic()
    first = engine._play(str(clips[0]), "I pushed the branch.", "done", cleanup=cleanup)
    assert time.monotonic() - began < 0.2  # it did not wait for its own clip
    second = engine._play(str(clips[1]), "I merged it.", "done", cleanup=cleanup)
    first.wait()
    second.wait()

    (s1, e1), (s2, e2) = _intervals(log)
    assert s2 >= e1
    assert not voice.busy()
    assert [clip.exists() for clip in clips] == [not cleanup] * 2


def test_a_reading_is_held_for_its_whole_length(claude_home):
    with voice.reading(0) as ours:
        assert ours and voice.reading_busy()
        with voice.reading(0.1) as second:
            assert not second
    assert not voice.reading_busy()


def test_commentary_is_dropped_while_anything_plays(claude_home, no_audio):
    """By the time its turn came it would be about work already done."""
    engine = SayEngine({})
    turn = voice.take_turn()
    try:
        assert engine.speak_dynamic("I'm running the tests.", allow_cold_start=False,
                                    kind="commentary") is True
        assert no_audio["say"] == []
    finally:
        os.close(turn)
    engine.speak_dynamic("I'm running the tests.", allow_cold_start=False, kind="commentary")
    assert no_audio["say"] == ["I'm running the tests."]
