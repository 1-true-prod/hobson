"""hobson coach: a short script of spoken steps, run on Hobson's own clock.

    hobson coach -f steps.txt         hobson coach <<'EOF'
                                      say Stand where the camera can see you.
                                      wait 10
                                      countdown 3
                                      EOF

A guided session -- an exercise, a photo, a demonstration -- needs its pauses
kept to the second, and an agent cannot keep them: its harness will not let a
command sleep, and a turn through the agent takes far longer than "hold for
two seconds". Chained `hobson say` calls made pauses out of how long each line
took to say. So the agent writes the steps and Hobson runs them in one call:

    say TEXT          say it; the next step starts when it has been said
    wait SECONDS      a pause
    countdown N       N down to one, a second apart (N at most MAX_COUNTDOWN);
                      the next step starts a second after "one"
    ready [SECONDS]   wait for a key press, a click or the mouse moving, or
                      SECONDS (default READY_SECONDS), whichever comes first.
                      Any input counts, so a presentation clicker works from
                      across the room; a key goes to whichever window has focus

Blank lines and lines starting with # are skipped. The whole script is
checked before anything is said, and every clip is rendered before the clock
starts: Pocket TTS takes seconds a line, which would stretch a countdown. Each
WAV is then cut down to its speech (_trim): a Pocket TTS clip of one word ran
1-2 s with the word up to 0.9 s in, a different amount each render, so the
numbers landed off the beat and a clip still sounding at the next beat held
the voice and pushed that number back. A number still sounding at the next
beat is stopped for the same reason.

Standard output is one JSON object per line, written as it happens, so a run
cut short still leaves what it did: "start", one "step" as each step ends
(its line, start and end in seconds from the start, and for `ready` whether
input came and after how long), and one "end" with how the run ended. Hobson
decides nothing with any of it; the agent does.

A run holds the reading lock (voice.reading), so `hobson say` elsewhere waits
for it, and voice.coaching(), so every other session's announcement is held on
presence's salver and a nudge pauses. When it ends, one briefing tells what
was held. `hobson off`, quiet hours, Ctrl-C, SIGTERM and SIGHUP stop a run
within a quarter of a second, and the clip playing with it.
"""

import argparse
import array
import collections
import ctypes
import ctypes.util
import json
import math
import os
import signal
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home  # noqa: E402
import log_record  # noqa: E402
import presence  # noqa: E402
import read_aloud  # noqa: E402
import voice  # noqa: E402

MAX_COUNTDOWN = 10
READY_SECONDS = 60.0
MAX_SECONDS = 600.0      # one wait or ready; longer is a typo, not a plan
POLL_SECONDS = 0.1       # how often a pause checks for hobson off, or input
READING_WAIT = 60        # someone may be standing there: no 15 minutes' wait

NUMBERS = ("One.", "Two.", "Three.", "Four.", "Five.",
           "Six.", "Seven.", "Eight.", "Nine.", "Ten.")

# Cutting a clip to its speech. Measured on Pocket TTS: a click in the first
# 10 ms (-42 dB), then -66 dB until the word, which peaks near -10 dB and
# decays through -45 dB in about 0.4 s.
TRIM_WINDOW = 0.01       # seconds per loudness reading
TRIM_BELOW_PEAK = 35.0   # dB under the loudest reading that still counts as speech
TRIM_SUSTAIN = 3         # readings in a row: a click is shorter than speech
TRIM_SILENT = -60.0      # a clip whose loudest reading is under this is left alone
TRIM_LEAD = 0.02         # seconds kept before the speech
TRIM_TAIL = 0.12         # and after it, for the decay

# `parts`: the passages of a `say` (read_aloud.passages), the numbers of a
# countdown in the order said, nothing for the rest.
Step = collections.namedtuple("Step", "line do seconds parts")


class ScriptError(ValueError):
    pass


def _seconds(line, word, arg, default=None):
    if arg is None and default is not None:
        return default
    try:
        value = float(arg)
    except (TypeError, ValueError):
        raise ScriptError(f"line {line}: {word} takes a number of seconds, not {arg!r}") from None
    if not 0 < value <= MAX_SECONDS:
        raise ScriptError(f"line {line}: {word} takes more than 0 and at most {MAX_SECONDS:g} seconds")
    return value


def parse(text):
    """The steps of a script, or ScriptError naming the first bad line."""
    steps = []
    for line, raw in enumerate(text.splitlines(), 1):
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        word, _, arg = body.partition(" ")
        word, arg = word.lower(), arg.strip() or None
        if word == "say":
            parts = read_aloud.passages(arg or "")
            if not parts:
                raise ScriptError(f"line {line}: say what?")
            steps.append(Step(line, "say", None, tuple(parts)))
        elif word == "wait":
            steps.append(Step(line, "wait", _seconds(line, "wait", arg), ()))
        elif word == "countdown":
            if arg is None or not arg.isdigit() or not 1 <= int(arg) <= MAX_COUNTDOWN:
                raise ScriptError(f"line {line}: countdown takes a whole number from 1 to {MAX_COUNTDOWN}")
            steps.append(Step(line, "countdown", None, NUMBERS[int(arg) - 1::-1]))
        elif word == "ready":
            steps.append(Step(line, "ready", _seconds(line, "ready", arg, READY_SECONDS), ()))
        else:
            raise ScriptError(f"line {line}: no step called {word!r} (say, wait, countdown, ready)")
    return steps


# ── Clips ──────────────────────────────────────────────────────────────────

def speech_span(levels, window=TRIM_WINDOW):
    """(start, end) in seconds of the speech in a clip, from its loudness in
    dB per `window` seconds, or None when there is none to find: from the
    first run of TRIM_SUSTAIN loud readings to the end of the last. Pure."""
    if not levels or max(levels) < TRIM_SILENT:
        return None
    floor = max(levels) - TRIM_BELOW_PEAK
    runs = []  # [first, last] of each run of loud readings
    for i, level in enumerate(levels):
        if level < floor:
            continue
        if runs and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    speech = [run for run in runs if run[1] - run[0] + 1 >= TRIM_SUSTAIN] or runs
    return (max(0.0, speech[0][0] * window - TRIM_LEAD),
            min(len(levels) * window, (speech[-1][1] + 1) * window + TRIM_TAIL))


def _chunks(data):
    pos = 12
    while pos + 8 <= len(data):
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        yield data[pos:pos + 4], data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)


def _trim(path):
    """Cut a WAV clip down to its speech (speech_span), in place. Anything
    else -- the AIFF `say` renders, which has no long silences, or a WAV
    format this does not read -- is left as it was. Never raises."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE" or sys.byteorder != "little":
            return
        chunks = dict(_chunks(data))
        fmt, audio = chunks.get(b"fmt "), chunks.get(b"data")
        if not fmt or audio is None or len(fmt) < 16:
            return
        code, channels, rate, _, align, bits = struct.unpack("<HHIIHH", fmt[:16])
        if code == 0xFFFE and len(fmt) >= 26:  # WAVE_FORMAT_EXTENSIBLE: the real code is in the subformat
            code = struct.unpack("<H", fmt[24:26])[0]
        if (code, bits) == (3, 32):
            samples, scale = array.array("f", audio[:len(audio) // 4 * 4]), 1.0
        elif (code, bits) == (1, 16):
            samples, scale = array.array("h", audio[:len(audio) // 2 * 2]), 32768.0
        else:
            return
        per = max(1, int(rate * TRIM_WINDOW)) * channels
        levels = []
        for i in range(0, len(samples), per):
            window = samples[i:i + per]
            rms = math.sqrt(sum(x * x for x in window) / len(window)) / scale
            levels.append(20 * math.log10(rms + 1e-9))
        span = speech_span(levels, per / channels / rate)
        if span is None:
            return
        kept = audio[int(span[0] * rate) * align:int(span[1] * rate) * align]
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + (b"\0" if len(fmt) & 1 else b"")
        body += b"data" + struct.pack("<I", len(kept)) + kept
        tmp = f"{path}.trim"
        with open(tmp, "wb") as f:
            f.write(b"RIFF" + struct.pack("<I", len(body)) + body)
        os.replace(tmp, path)
    except (OSError, ValueError, struct.error, ZeroDivisionError):
        return


# ── Input ──────────────────────────────────────────────────────────────────

_input_clock = None


def idle_seconds():
    """Seconds since the last key press, click or mouse movement, from any
    device, or None when it cannot be read. The presence helper reads the
    same counter; it needs no permission."""
    global _input_clock
    try:
        if _input_clock is None:
            cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
            _input_clock = cg.CGEventSourceSecondsSinceLastEventType
            _input_clock.restype = ctypes.c_double
            _input_clock.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        # kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType
        return float(_input_clock(0, 0xFFFFFFFF))
    except (OSError, AttributeError, TypeError):
        return None


# ── Running ────────────────────────────────────────────────────────────────

class Run:
    """One run of parsed steps through `engine`. The clock, the pause and
    the input counter are passed in, so a test runs one in no time."""

    def __init__(self, steps, engine, emit, clock=time.monotonic, sleep=time.sleep, idle=idle_seconds):
        self.steps, self.engine, self.emit = steps, engine, emit
        self.clock, self.sleep, self.idle = clock, sleep, idle
        self.clips, self.players = {}, []
        self.t0 = None
        self.at_line = None     # the step going, until the last has ended

    def _at(self):
        return round(self.clock() - self.t0, 2)

    def _pause_until(self, until):
        """Wait until `until` on the clock. Why it was cut short, or None."""
        while True:
            stopped = read_aloud.silenced()
            if stopped:
                return stopped
            left = until - self.clock()
            if left <= 0:
                return None
            self.sleep(min(POLL_SECONDS, left))

    def _play(self, text):
        player = read_aloud.play(self.engine, self.clips[text], text, "coach")
        self.players.append(player)
        return player

    def render(self):
        """Every clip the steps will play, before the clock starts. The step
        whose clip could not be made, or None."""
        for step in self.steps:
            for text in step.parts:
                if text not in self.clips:
                    clip = self.engine.render(text, timeout=read_aloud.RENDER_TIMEOUT)
                    if clip is None:
                        return step
                    _trim(clip)
                    self.clips[text] = clip
        return None

    def _say(self, step):
        for text in step.parts:
            stopped = read_aloud.wait(self._play(text))
            if stopped:
                return stopped
        return None

    def _countdown(self, step):
        beat, last = self.clock(), None
        for n, text in enumerate(step.parts):
            stopped = self._pause_until(beat + n)
            if stopped:
                return stopped
            read_aloud.stop(last)  # still sounding: it would hold the voice past this beat
            last = self._play(text)
        return self._pause_until(beat + len(step.parts))

    def _ready(self, step, report):
        began, readable = self.clock(), False
        while True:
            stopped = read_aloud.silenced()
            if stopped:
                return stopped
            elapsed = self.clock() - began
            since = self.idle()
            readable = readable or since is not None
            if since is not None and since < elapsed:
                report.update(by="input", after=round(elapsed - since, 2))
                return None
            if elapsed >= step.seconds:
                report.update(by="timeout")
                if not readable:
                    report["input"] = "unreadable"
                return None
            self.sleep(min(POLL_SECONDS, step.seconds - elapsed))

    def go(self):
        """Run the steps in order: "done", "stopped (…)" or "interrupted".
        Every clip playing is stopped and every clip removed however it ends."""
        self.t0 = self.clock()
        self.emit({"event": "start", "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": len(self.steps)})
        try:
            for step in self.steps:
                self.at_line = step.line
                report = {"event": "step", "line": step.line, "do": step.do, "start": self._at()}
                if step.do == "say":
                    stopped = self._say(step)
                elif step.do == "wait":
                    stopped = self._pause_until(self.clock() + step.seconds)
                elif step.do == "countdown":
                    stopped = self._countdown(step)
                else:
                    stopped = self._ready(step, report)
                if stopped:
                    return stopped
                report["end"] = self._at()
                self.emit(report)
            self.at_line = None
            return read_aloud.wait(self.players[-1] if self.players else None) or "done"
        except KeyboardInterrupt:
            return "interrupted"
        finally:
            for player in self.players:
                read_aloud.stop(player)
            self.remove_clips()

    def remove_clips(self):
        for clip in self.clips.values():
            read_aloud.remove(clip)
        self.clips.clear()


def brief(engine, config):
    """Tell what was held while the run went, in one briefing, unless Hobson
    has been silenced since: then it stays on the salver. How many items were
    told."""
    reason = home.silence_reason(home.load_config())
    if reason:
        if presence.held():
            log_record.write(f"[coach] briefing skipped ({reason}), held items kept")
        return 0
    items = presence.take_held()
    text = presence.compose_briefing(items, [], presence.COACHING)
    if not text:
        return 0
    log_record.write(f"[coach] briefing ({len(items)} held) -> {text!r}")
    engine.speak(text, "briefing", cold_start=True)
    return len(items)


def run(steps, config=None, emit=None, **clock):
    """Run parsed steps. How it ended: "done", "empty", "silent (…)",
    "stopped (…)", "interrupted", "busy" (a reading or run kept going), or
    "unrendered" (a clip could not be made, so nothing was said)."""
    config = home.load_config() if config is None else config
    emit = emit or _print
    ending = {"event": "end"}

    def end(ended, **more):
        ending.update(ended=ended, **more)
        emit(ending)
        return ended

    if not steps:
        return end("empty")
    reason = home.silence_reason(config)
    if reason:
        return end(f"silent ({reason})")
    if voice.reading_busy():
        print("hobson coach: waiting for the reading already going to finish…", file=sys.stderr)
    with voice.reading(READING_WAIT) as ours:
        if not ours:
            log_record.write("[coach] a reading or another run was still going; gave up")
            return end("busy")
        with voice.coaching():
            import hobson
            engine = hobson.load_engine(config)
            ended, more = _run_held(Run(steps, engine, emit, **clock))
    # Anything held from the moment the run took the lock is told, however
    # it ended. Outside the locks: the briefing takes a turn like any other.
    try:
        more["held"] = brief(engine, config)
    except KeyboardInterrupt:
        ended = "interrupted"
    return end(ended, **more)


def _run_held(go):
    """Render, then run, with the locks held. (how it ended, details)."""
    log_record.write(f"[coach] running {len(go.steps)} step(s)")
    try:
        unmade = go.render()
    except KeyboardInterrupt:
        go.remove_clips()
        log_record.write("[coach] interrupted while rendering; nothing said")
        return "interrupted", {}
    if unmade is not None:
        go.remove_clips()
        log_record.write(f"[coach] could not render line {unmade.line}; nothing said")
        return "unrendered", {"line": unmade.line}
    ended = go.go()
    more = {"seconds": go._at()}
    if go.at_line is not None:
        more["line"] = go.at_line
    log_record.write(f"[coach] {ended} after {more['seconds']:.0f}s")
    return ended, more


def _print(record):
    print(json.dumps(record), flush=True)


_EXIT = {"done": 0, "interrupted": 130}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hobson coach",
        description="Run a short script of spoken steps on Hobson's clock: "
                    "say TEXT, wait SECONDS, countdown N, ready [SECONDS].")
    parser.add_argument("-f", "--file", help="the script (default: standard input)")
    args = parser.parse_args(argv)
    home.migrate_legacy_state()
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            print(f"hobson coach: cannot read {args.file}: {e}", file=sys.stderr)
            return 2
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.error("no script: give --file, or pipe it in")
    try:
        steps = parse(text)
    except ScriptError as e:
        print(f"hobson coach: {e}", file=sys.stderr)
        return 2
    # Killed by its caller (an agent's background task, a closed terminal),
    # a run stops its clip too rather than leave it playing.
    signal.signal(signal.SIGTERM, read_aloud.interrupt)
    signal.signal(signal.SIGHUP, read_aloud.interrupt)
    try:
        ended = run(steps)
    except KeyboardInterrupt:  # still waiting for a reading to end
        ended = "interrupted"
    return _EXIT.get(ended, 1)


if __name__ == "__main__":
    sys.exit(main())
