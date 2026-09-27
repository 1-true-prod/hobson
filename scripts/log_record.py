#!/usr/bin/env python3
"""log_record.py — the one writer and the one reader of hobson.log.

The log is the only interface between the hooks and everything that measures
them: `hobson stats`, `hobson monitor`, `hobson recap` and log_analyse.py. A
record is one line:

    [YYYY-MM-DD HH:MM:SS] [project] [engine] message    an engine wrote it
    [YYYY-MM-DD HH:MM:SS] [project] message             anything else

Two older forms are read, never written: "[HH:MM:SS]" with no date (before the
log was dated), and no project tag at all, where the first tag is the engine
(before lines were tagged by project). A message holding a line break used to
spill onto lines with no envelope; write() now folds line breaks, and
records() folds those old lines back into the record they belong to.

Three message shapes are read by more than one module, so each has its
formatter and its parser side by side here: playback ("barked (source) ->
'phrase'"), a generation outcome ("[Event] (model) -> category -> 'phrase'")
and phrase_gen's trace ("gen[Event] ... raw='...' -> ..."). A marker written in
one place and read in one place stays a plain string check on Record.body.

`python3 log_record.py --fields` reads log lines on stdin and prints each as
stamp, engine, body and the line itself, separated by \\x1f, for the monitor.
"""

import re
import sys
import time
from typing import NamedTuple, Optional

import home

# Every tag an engine has written, current and retired. A tag after the
# project that is not one of these (e.g. "[nudge]") is part of the message.
ENGINE_TAGS = frozenset({"say", "chatterbox", "kokoro-rt", "pocket-tts", "qwen-tts"})

_LINE_BREAK = re.compile(r"\r\n|[\r\n]")


def write(msg):
    """Append one record, stamped with the date, time and project. Never
    raises: a hook must not fail because its log line could not be written."""
    try:
        project = home.derive_project_label() or "unknown"
    except Exception:
        project = "unknown"
    try:
        with open(home.log_file(), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{project}] "
                    f"{_LINE_BREAK.sub(' ', str(msg))}\n")
    except Exception:
        pass


# ── Reading records ────────────────────────────────────────────────────────

_STAMP = re.compile(r"\[(?:(\d{4}-\d{2}-\d{2}) )?(\d{2}:\d{2}:\d{2})\] ")
_TAG = re.compile(r"\[([^\]]*)\] ")


class Record(NamedTuple):
    date: Optional[str]      # "YYYY-MM-DD"; None before the log was dated
    time: str                # "HH:MM:SS"
    project: Optional[str]   # None before lines were tagged by project
    engine: Optional[str]    # the engine's tag, None if no engine wrote it
    body: str                # the message

    @property
    def stamp(self):
        """The timestamp as the line wrote it, brackets included."""
        return f"[{self.date} {self.time}]" if self.date else f"[{self.time}]"

    def timestamp(self, now=None):
        """Seconds since the epoch, None if unreadable. An undated record
        takes its date from `now`, the day before if that would put it in
        the future (a run spanning midnight)."""
        now = time.time() if now is None else now
        try:
            if self.date:
                return time.mktime(time.strptime(f"{self.date} {self.time}",
                                                 "%Y-%m-%d %H:%M:%S"))
            hour, minute, second = (int(part) for part in self.time.split(":"))
            today = time.localtime(now)
            candidate = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                     hour, minute, second, 0, 0, -1))
        except (ValueError, OverflowError):
            return None
        return candidate - 86400 if candidate > now else candidate


def parse(line):
    """The Record in one log line, or None for a line with no envelope."""
    line = line.rstrip("\r\n")
    m = _STAMP.match(line)
    if not m:
        return None
    date, clock = m.groups()
    rest = line[m.end():]
    project = engine = None
    tag = _TAG.match(rest)
    if tag and tag.group(1) not in ENGINE_TAGS:
        project, rest = tag.group(1), rest[tag.end():]
        tag = _TAG.match(rest)
    if tag and tag.group(1) in ENGINE_TAGS:
        engine, rest = tag.group(1), rest[tag.end():]
    return Record(date, clock, project, engine, rest)


def records(lines):
    """The Records in an iterable of log lines, oldest first. A line with no
    envelope is the rest of the message before it, and is folded back into
    that record as write() would have folded it."""
    current = None
    for line in lines:
        record = parse(line)
        if record is None:
            if current is not None:
                rest = line.rstrip("\r\n")
                current = current._replace(body=f"{current.body} {rest}")
            continue
        if current is not None:
            yield current
        current = record
    if current is not None:
        yield current


def read(path=None):
    """The Records of a log file, hobson.log by default. Raises OSError if
    it cannot be opened."""
    with open(path or home.log_file(), encoding="utf-8", errors="replace") as f:
        yield from records(f)


# ── Shapes read by more than one module ────────────────────────────────────

# A phrase is always written as its repr(), so it can hold any quote.
_REPR = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""


def _unrepr(text):
    import ast  # readers only: keeps write(), on every hook, light
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text[1:-1]
    return value if isinstance(value, str) else text[1:-1]


class Playback(NamedTuple):
    source: str   # e.g. "daemon-live", "say-fallback: daemon down"
    phrase: str


def playback(source, phrase):
    """A phrase was played: written by every speaking path, once per utterance."""
    return f"barked ({source}) -> {phrase!r}"


_PLAYBACK = re.compile(rf"barked \(([^)]*)\) -> ({_REPR})$")


def parse_playback(body):
    m = _PLAYBACK.match(body)
    return Playback(m.group(1), _unrepr(m.group(2))) if m else None


class Outcome(NamedTuple):
    event: str
    batch: Optional[int]   # commentary: how many tool calls the phrase covers
    notes: str             # in the parentheses: the model, then commentary's verbosity
    category: str
    phrase: str


def outcome(event, notes, category, phrase, batch=None):
    """What a generated event came to: its category and the phrase for it."""
    size = f"batch of {batch} " if batch is not None else ""
    return f"[{event}] {size}({notes}) -> {category} -> {phrase!r}"


_OUTCOME = re.compile(rf"\[(\w+)\] (?:batch of (\d+) )?\(([^)]*)\) -> (\w+) -> ({_REPR})$")


def parse_outcome(body):
    m = _OUTCOME.match(body)
    if not m:
        return None
    event, batch, notes, category, phrase = m.groups()
    return Outcome(event, int(batch) if batch else None, notes, category, _unrepr(phrase))


class Trace(NamedTuple):
    event: str
    model: str
    detail: Optional[str]    # what the model was told happened; absent on old lines
    raw: str                 # the model's reply, before any repair or guard
    result: str              # what became of it: "-> SKIP", "FAILED ...", verdict()
    spoken: Optional[str]    # the phrase, when it was spoken
    rejected: Optional[str]  # the phrase, when a guard rejected it


def trace(event, elapsed, attempt, attempts, model, detail, raw):
    """The start of phrase_gen's line for one model call. The caller adds
    what became of the reply: "FAILED <error>", "-> SKIP", "-> ... retrying",
    or verdict()."""
    return (f"gen[{event}] {elapsed:.2f}s attempt={attempt}/{attempts} "
            f"model={model} detail={detail!r} raw={raw!r}")


def verdict(category, steps, phrase, reason=None):
    """How a trace ends when the reply held a phrase: spoken, or REJECTED
    with the guard's reason."""
    notes = " ".join(steps)
    if reason is None:
        return f"-> {category} {notes} spoken={phrase!r}"
    return f"-> {category} {notes} REJECTED={phrase!r} ({reason})"


_TRACE = re.compile(rf"gen\[(\w+)\] \S+ attempt=\S+ model=(\S*) "
                    rf"(?:detail=({_REPR}) )?raw=({_REPR})(?: (.*))?$")
_SPOKEN = re.compile(rf" spoken=({_REPR})$")
_REJECTED = re.compile(rf" REJECTED=({_REPR}) \(")


def parse_trace(body):
    m = _TRACE.match(body)
    if not m:
        return None
    event, model, detail, raw, result = m.groups()
    result = result or ""
    spoken, rejected = _SPOKEN.search(result), _REJECTED.search(result)
    return Trace(event, model, _unrepr(detail) if detail else None, _unrepr(raw), result,
                 _unrepr(spoken.group(1)) if spoken else None,
                 _unrepr(rejected.group(1)) if rejected else None)


_TAIL = re.compile(rf" -> ({_REPR})$")


def tail_phrase(body):
    """The phrase ending a line written as "... -> 'phrase'": playback,
    outcome, and the template, skip and announcement lines. None otherwise."""
    m = _TAIL.search(body)
    return _unrepr(m.group(1)) if m else None


# ── For the monitor ────────────────────────────────────────────────────────

_SEP = "\x1f"  # not a tab: bash's read collapses runs of tabs, losing empty fields


def fields(line):
    """stamp, engine, body and the line itself, for `hobson monitor`. A line
    with no envelope has only the last."""
    line = line.rstrip("\r\n")
    record = parse(line)
    if record is None:
        return _SEP.join(("", "", "", line))
    return _SEP.join((record.stamp, record.engine or "", record.body, line))


def main(argv):
    if argv[1:] != ["--fields"]:
        sys.exit("usage: log_record.py --fields  < hobson.log")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        for line in iter(sys.stdin.readline, ""):
            sys.stdout.write(fields(line) + "\n")
            sys.stdout.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main(sys.argv)
