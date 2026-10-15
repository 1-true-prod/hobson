"""hobson say: read text aloud, on demand, one voice at a time.

    hobson say "The build is green."     hobson say -f spec.md     pbpaste | hobson say

The text is cut into passages -- paragraphs, and a long one at sentence ends,
at most MAX_CHARS each -- and read in order through the configured engine.
Each passage is rendered while the one before it plays, and the command
returns only when the last has finished, so a shell loop of `hobson say`
calls reads one after another. The same loop over `hobson test "..."`, which
returned as soon as its player started, stacked every paragraph of a spec on
the one still playing.

Every passage takes its turn on the voice (voice.py): an announcement from
another session waits for the passage playing and is heard before the next,
and a second reading waits for this one to end. `hobson off` or quiet hours
stop a reading within a quarter of a second, as does Ctrl-C. Passages are
kind "answer": you asked, so presence never holds them.
"""

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home  # noqa: E402
import log_record  # noqa: E402
import voice  # noqa: E402

# A passage is about 20 seconds of speech: short enough for one /generate
# request well inside RENDER_TIMEOUT, long enough to read as prose.
MAX_CHARS = 300
RENDER_TIMEOUT = 30      # one passage (the hooks' 5-10s is sized for one line)
GAP_SECONDS = 0.35       # between passages: a breath, and a turn for anything waiting
POLL_SECONDS = 0.25      # how often a passage playing checks for hobson off
READING_WAIT = 900       # the longest a reading waits for another to end

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET = re.compile(r"^\s*[-*+]\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKUP = re.compile(r"\*\*|__|`")


def _line(raw):
    """One line as it should be heard: Markdown's markers gone, and a heading
    or list item ended, so it does not run into the next line."""
    marked = bool(_HEADING.match(raw) or _BULLET.match(raw))
    text = _QUOTE.sub("", _BULLET.sub("", _HEADING.sub("", raw)))
    text = _MARKUP.sub("", _LINK.sub(r"\1", text)).strip()
    if marked and text and text[-1] not in ".!?:;,":
        text += "."
    return text


def _cut(paragraph):
    """A paragraph as pieces of at most MAX_CHARS, cut at sentence ends, and
    a sentence longer than that at a space."""
    pieces, current = [], ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > MAX_CHARS:
            at = sentence.rfind(" ", 0, MAX_CHARS)
            at = at if at > 0 else MAX_CHARS
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:at])
            sentence = sentence[at:].lstrip()
        if current and len(current) + 1 + len(sentence) > MAX_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def passages(text):
    """The text as passages to read in turn: paragraphs split at blank lines,
    hard-wrapped lines joined, code fences dropped, each cut by _cut."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [_line(raw) for raw in block.splitlines() if not raw.strip().startswith("```")]
        paragraph = re.sub(r"\s+", " ", " ".join(line for line in lines if line)).strip()
        if paragraph:
            out.extend(_cut(paragraph))
    return out


# ── Playing a clip someone asked for (hobson say, hobson coach) ────────────

def silenced():
    """Why a reading must stop now (hobson off, quiet hours), as how it
    ended, or None. Read fresh: it changes while a reading goes."""
    reason = home.silence_reason(home.load_config())
    return f"stopped ({reason})" if reason else None


def play(engine, clip, text, source):
    """Play a clip as kind "answer" (you asked: presence never holds it) and
    log it under `source`, which recap reads as Hobson's own talk."""
    player = engine.play(clip, text, "answer")
    engine._log(log_record.playback(source, text, "answer"))
    return player


def wait(player):
    """Wait for a clip to finish. Why it was cut short (silenced()), or None
    when it played to the end."""
    while player is not None and player.poll() is None:
        stopped = silenced()
        if stopped:
            return stopped
        with contextlib.suppress(subprocess.TimeoutExpired):
            player.wait(timeout=POLL_SECONDS)
    return None


def stop(player):
    if player is not None and player.poll() is None:
        player.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            player.wait(timeout=2)


def remove(path):
    if path:
        with contextlib.suppress(OSError):
            os.remove(path)


def interrupt(signum, frame):
    """A SIGTERM/SIGHUP handler: a reader killed by its caller (an agent's
    background task, a closed terminal) stops its clip rather than leave it
    playing."""
    raise KeyboardInterrupt


def _read(parts, engine, echo):
    """Read the passages; how it ended: "read", "stopped (…)" or "interrupted".
    However it ends, the passage playing is stopped and every clip removed."""
    player = clip = rendered = None
    try:
        for n, part in enumerate(parts, 1):
            rendered = engine.render(part, timeout=RENDER_TIMEOUT)  # while the last one plays
            stopped = wait(player)
            if stopped:
                return stopped
            remove(clip)
            player = clip = None
            if rendered is None:
                engine._log(f"read-aloud: could not render passage {n}, skipped -> {part!r}")
                continue
            if n > 1:
                time.sleep(GAP_SECONDS)
            echo(f"[{n}/{len(parts)}] {part[:70]}{'…' if len(part) > 70 else ''}")
            clip, rendered = rendered, None
            player = play(engine, clip, part, "read-aloud")
        return wait(player) or "read"
    except KeyboardInterrupt:
        return "interrupted"
    finally:
        stop(player)
        remove(clip)
        remove(rendered)


def read(text, config=None, echo=print):
    """Read `text` aloud. How it ended: "read", "empty", "silent (…)",
    "stopped (…)", "interrupted" or "busy" (another reading kept going)."""
    config = home.load_config() if config is None else config
    parts = passages(text)
    if not parts:
        return "empty"
    reason = home.silence_reason(config)
    if reason:
        return f"silent ({reason})"
    if voice.reading_busy():
        echo("Waiting for the reading already going to finish…")
    with voice.reading(READING_WAIT) as ours:
        if not ours:
            log_record.write("[read] another reading was still going; gave up")
            return "busy"
        reason = home.silence_reason(home.load_config())  # it may have been a long wait
        if reason:
            return f"silent ({reason})"
        import hobson
        engine = hobson.load_engine(config)
        log_record.write(f"[read] reading {len(parts)} passage(s), {sum(map(len, parts))} characters")
        ended = _read(parts, engine, echo)
        log_record.write(f"[read] {ended}")
        return ended


_EXIT = {"read": 0, "empty": 1, "busy": 1, "interrupted": 130}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hobson say", description="Read text aloud, a passage at a time, one voice at a time.")
    parser.add_argument("text", nargs="*", help="what to say (default: standard input)")
    parser.add_argument("-f", "--file", help="read this file aloud")
    args = parser.parse_args(argv)
    if args.file and args.text:
        parser.error("give the text or --file, not both")
    home.migrate_legacy_state()
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            print(f"hobson say: cannot read {args.file}: {e}", file=sys.stderr)
            return 2
    elif args.text:
        text = " ".join(args.text)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.error("nothing to say: give the text, --file, or pipe it in")
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    ended = read(text)
    if ended != "read":
        print(f"hobson say: {'nothing to read' if ended == 'empty' else ended}", file=sys.stderr)
    return _EXIT.get(ended, 1)


if __name__ == "__main__":
    sys.exit(main())
