"""plugin_notes.py -- the notes Claude Code's cc-plugin-you-should-know leaves,
read aloud.

The plugin keeps a store file, ~/.claude/plugins/store/
cc-plugin-you-should-know_builtin-<hash>.json (undocumented, so every read
here is defensive), with one entry per session it has looked at. An entry
that has a `view` is a note: {kind: "offer", line, explanation, shownAt}.

At a Stop, `claim` returns the notes not yet said, for this session and any
other, and records them as said before anything is spoken: two Stops at once
must not both read one. The first run records every note already in the
store as said, so an install, or a reinstall that removed the state file,
never reads out old ones; that waits until a store has been read, so a
store that appears later is its own first run. A note older than MAX_AGE,
how long the salver keeps a held line, is recorded and not said: a note
written after its turn's Stop waits for the next Stop, which can be hours.

A note is session text and stays on this Mac: it goes only to the local
model, and the log gets its id and length, never its words, on every path
(kinds "note" is private: log_record.Unlogged).
"""

import fcntl
import glob
import json
import math
import os
import re
import time
from typing import NamedTuple

import home
import log_record
import presence

STORE_GLOB = "plugins/store/cc-plugin-you-should-know_builtin-*.json"
MAX_AGE = presence.HELD_MAX_AGE
MIN_SHOWN_AT = 1e11  # epoch milliseconds from 1973 on; seconds would be far below
FOLLOW_WORDS = 14
SUMMARY_TIMEOUT = 15
_SESSION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_logged = set()


class Note(NamedTuple):
    session: str
    shown_at: int   # epoch milliseconds, as the plugin writes it
    line: str
    explanation: str

    @property
    def key(self):
        return (self.session, self.shown_at)

    @property
    def length(self):
        return len(self.line) + len(self.explanation)

    @property
    def tag(self):
        return self.session[:8]


def _log_once(msg):
    if msg not in _logged:
        _logged.add(msg)
        log_record.write(f"[plugin-notes] {msg}")


def _note(entry):
    """The Note an entry holds, or None. An entry with no view is no note; a
    view that is not what this expects is logged once per process, by its
    keys (never their values): the plugin is undocumented and may change."""
    view = entry.get("view") if isinstance(entry, dict) else None
    if view is None:
        return None
    session = entry.get("id")
    fields = view if isinstance(view, dict) else {}
    line, shown = fields.get("line"), fields.get("shownAt")
    if (isinstance(session, str) and _SESSION_ID.match(session)
            and fields.get("kind") == "offer" and isinstance(line, str) and line.strip()
            and isinstance(shown, (int, float)) and not isinstance(shown, bool)
            and math.isfinite(shown) and shown >= MIN_SHOWN_AT):
        explanation = view.get("explanation")
        return Note(session, int(shown), line.strip(), explanation if isinstance(explanation, str) else "")
    keys = sorted(view) if isinstance(view, dict) else type(view).__name__
    _log_once(f"an entry's view did not have the expected shape (keys {keys}); skipped")
    return None


def read_store():
    """Every note in the store, each once (an old file a plugin update left
    behind can repeat one), or None when there is no store, or a file of it
    could not be read or no longer has the shape this expects (logged once
    per process)."""
    paths = sorted(glob.glob(home.path(STORE_GLOB)))
    if not paths:
        return None
    notes = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fd:
                sessions = json.load(fd)["sessions"]
            if not isinstance(sessions, list):
                raise TypeError("sessions is not a list")
        except (OSError, ValueError, KeyError, TypeError) as e:
            _log_once(f"could not read {os.path.basename(path)} ({type(e).__name__}); skipped")
            return None
        notes.update((n.key, n) for n in map(_note, sessions) if n)
    return list(notes.values())


def _state_path():
    return home.path("hobson-plugin-notes.json")


def _said():
    try:
        with open(_state_path(), encoding="utf-8") as fd:
            said = json.load(fd)["said"]
        return {(s, at) for s, at in said}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        _log_once("state file unreadable; treated as a first run")
        return None


def claim(now=None):
    """The notes to say now, oldest first, recorded as said. Under a lock, so
    that two hooks at once never claim the same note."""
    now = time.time() if now is None else now
    notes = read_store()
    if notes is None:
        return []
    os.makedirs(home.state_dir(), exist_ok=True)
    with open(home.path("hobson-plugin-notes.lock"), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        said = _said()
        first_run = said is None
        fresh = [n for n in notes if first_run or n.key not in said]
        # Every note still in the store, and nothing that has left it.
        keep = {n.key for n in notes}
        if first_run or keep != said:
            home.write_json_atomic(_state_path(), {"said": [list(k) for k in sorted(keep)]})
    if first_run:
        _log_once(f"first run: {len(fresh)} existing notes recorded as said")
        return []
    fresh.sort(key=lambda n: n.shown_at)
    current = [n for n in fresh if now - n.shown_at / 1000.0 <= MAX_AGE]
    if len(current) < len(fresh):
        log_record.write(f"[plugin-notes] {len(fresh) - len(current)} notes over "
                         f"{MAX_AGE / 3600:g} hours old recorded, not said")
    return current


def project_of(session):
    """The speakable project a session works in, from the first cwd in the
    first 200 lines of its transcript, or "" when there is none."""
    for path in glob.glob(home.path(f"projects/*/{session}.jsonl")):
        try:
            with open(path, encoding="utf-8") as fd:
                lines = [raw for _, raw in zip(range(200), fd)]
        except (OSError, ValueError):
            continue
        for raw in lines:
            try:
                record = json.loads(raw) if '"cwd"' in raw else None
            except ValueError:
                continue
            cwd = record.get("cwd") if isinstance(record, dict) else None
            if isinstance(cwd, str) and cwd:
                return home.project_label(cwd)
    return ""


def _speakable(markdown):
    import read_aloud
    return " ".join(read_aloud.passages(markdown))


def build_prompt(note):
    return (
        "Below is a note a coding tool left for a developer: its first line, then the detail.\n"
        f"Write ONE short sentence, under {FOLLOW_WORDS} words, saying what to do about it or what "
        "follows. Use the note's own words. Add no file, command, name or number that is not in it. "
        "Do not repeat the first line.\n"
        "No preamble, no quotes, nothing else.\n\n"
        f"Notes:\n{note.line}\n{_speakable(note.explanation)}"
    )


def summary(note, config):
    """What to say of a note: (text, how). The note's own first line, which
    is the plugin's one-sentence summary of it, and the model's one sentence
    on what to do about it when that reply is about the note.

    The model is not trusted to summarise the whole note. Asked to, in a
    butler's voice or plainly, llama3.2:3b did not pass the check below in
    any of 25 tries, and some reversed a cause ("pushes to production after
    the review check"). Asked only for the follow-up, with the line kept as
    written, 7 of 20 passed (the ones that did read right), and the rest
    are the line alone."""
    import engines.base as base
    import recap
    prompt = build_prompt(note)
    items = [note.line, _speakable(note.explanation)]

    def check(reply):
        if recap._echoes_the_prompt(reply, prompt):
            return "repeated the instructions"
        stray = recap._not_in_the_notes(reply, items)
        return f"{len(stray)} words not in the note" if stray else None

    follow, err = recap.summarise(prompt, config, SUMMARY_TIMEOUT, limit=FOLLOW_WORDS + 6, check=check)
    line = base.tts_normalize(note.line)
    if follow:
        return f"{line} {follow}", "the note's line and the model's follow-up"
    return line, f"the note's own line, the model's reply dropped ({err})"


_COUNTS = {2: "two", 3: "three", 4: "four", 5: "five"}


def _more(rest):
    if len(rest) == 1:
        project = project_of(rest[0].session)
        return f" And one more{', for ' + project if project else ''}."
    return f" And {_COUNTS.get(len(rest), len(rest))} more." if rest else ""


def phrase(notes, config, current_session):
    """The one phrase for `notes` (oldest first), and the project it is
    about when that is not this session's (None for this one, which the face
    already names). The first is summarised, opening with its project when
    it is another session's, and the rest counted. The phrase logs as the
    note's id and length (log_record.Unlogged)."""
    import recap
    first, rest = notes[0], notes[1:]
    text, how = summary(first, config)
    log_record.write(f"[plugin-notes] note {first.tag} ({first.length} chars) said from {how}"
                     + (f", {len(rest)} more counted" if rest else ""))
    project = None
    if first.session != current_session:
        project = project_of(first.session) or None
        text = f"On {project}: {recap._after_colon(text)}" if project else text
    return log_record.Unlogged(text + _more(rest), f"note {first.tag}, {first.length} chars"), project
