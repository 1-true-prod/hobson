"""The notes cc-plugin-you-should-know leaves, read aloud after a Stop.

The store's text here is invented; only its shape is the plugin's.
"""

import io
import json
import os
import subprocess
import sys
import time
import urllib.error

import pytest

import log_record
import plugin_notes
from engines.say import SayEngine

_POPEN = subprocess.Popen  # the real one: no_audio patches it during each test

NOW = 1_800_000_000.0
SESSION = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER = "22222222-aaaa-bbbb-cccc-000000000002"
LINE = "The migration script drops the staging table before the copy finishes."
EXPLANATION = "**Heads up.** The `migrate` script runs the drop first.\n\n- Staging data is lost\n- Run the copy first"


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    monkeypatch.setattr(plugin_notes, "_logged", set())


def _entry(session=SESSION, shown=NOW * 1000, line=LINE, explanation=EXPLANATION, **view):
    return {"id": session, "askedTurn": "a", "offeredTurn": "b", "tag": "Heads up",
            "view": {"kind": "offer", "line": line, "explanation": explanation,
                     "shownAt": shown, **view}}


def _store(claude_home, *entries, name="cc-plugin-you-should-know_builtin-abc123.json"):
    d = claude_home / "plugins" / "store"
    d.mkdir(parents=True, exist_ok=True)
    plain = {"id": "33333333-aaaa-bbbb-cccc-000000000003", "askedTurn": "a"}
    (d / name).write_text(json.dumps({"sessions": [plain, *entries], "seen": [], "known": [],
                                      "typedPastInARow": 0, "looksToSkip": 0}))


def _transcript(claude_home, session, cwd):
    d = claude_home / "projects" / "-some-project"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text(
        json.dumps({"type": "mode", "sessionId": session}) + "\n"
        + json.dumps({"type": "attachment", "cwd": cwd, "sessionId": session}) + "\n")


def _said_log():
    return [r.body for r in log_record.records(log_record.lines()) if "[plugin-notes]" in r.body]


# ── Which notes are new ─────────────────────────────────────────────────────

def test_the_first_run_records_every_existing_note_and_says_none(claude_home):
    _store(claude_home, _entry(shown=NOW * 1000 - 5000))
    assert plugin_notes.claim(NOW) == []
    assert json.loads((claude_home / "hobson-plugin-notes.json").read_text()) == \
        {"said": [[SESSION, NOW * 1000 - 5000]]}


def test_a_note_after_the_first_run_is_new_and_said_once(claude_home):
    _store(claude_home)
    assert plugin_notes.claim(NOW) == []
    _store(claude_home, _entry(shown=NOW * 1000 - 5000))
    assert [(n.session, n.line) for n in plugin_notes.claim(NOW)] == [(SESSION, LINE)]
    assert plugin_notes.claim(NOW) == []


def test_no_store_records_nothing_so_a_store_that_appears_later_is_the_baseline(claude_home):
    assert plugin_notes.claim(NOW) == []
    assert not (claude_home / "hobson-plugin-notes.json").exists()
    _store(claude_home, _entry(shown=NOW * 1000 - 5000))
    assert plugin_notes.claim(NOW) == []
    _store(claude_home, _entry(shown=NOW * 1000 - 5000), _entry(OTHER, shown=NOW * 1000 - 1000))
    assert [n.session for n in plugin_notes.claim(NOW)] == [OTHER]


def test_a_second_note_in_the_same_session_is_new(claude_home):
    _store(claude_home, _entry(shown=NOW * 1000 - 90_000))
    plugin_notes.claim(NOW)
    _store(claude_home, _entry(shown=NOW * 1000 - 90_000), _entry(shown=NOW * 1000 - 5000, line="Another."))
    assert [n.line for n in plugin_notes.claim(NOW)] == ["Another."]


def test_notes_come_oldest_first_from_any_session(claude_home):
    _store(claude_home)
    plugin_notes.claim(NOW)
    _store(claude_home, _entry(OTHER, shown=NOW * 1000 - 1000), _entry(SESSION, shown=NOW * 1000 - 9000))
    assert [n.session for n in plugin_notes.claim(NOW)] == [SESSION, OTHER]


def test_a_note_hours_old_is_still_said(claude_home):
    """A note written after its turn's Stop waits for the next one, which can
    be hours later: as long as the salver keeps a held line."""
    _store(claude_home)
    plugin_notes.claim(NOW)
    _store(claude_home, _entry(shown=(NOW - 2 * 3600) * 1000))
    assert len(plugin_notes.claim(NOW)) == 1


def test_a_note_over_twelve_hours_old_is_recorded_and_not_said(claude_home):
    _store(claude_home)
    plugin_notes.claim(NOW)
    _store(claude_home, _entry(shown=(NOW - 13 * 3600) * 1000))
    assert plugin_notes.claim(NOW) == []
    assert plugin_notes.claim(NOW + 1) == []
    assert any("1 notes over 12 hours old" in line for line in _said_log())


def _said_keys(claude_home):
    return sorted(map(tuple, json.loads((claude_home / "hobson-plugin-notes.json").read_text())["said"]))


def test_every_note_still_in_the_store_stays_recorded_and_a_gone_one_is_dropped(claude_home):
    _store(claude_home)
    plugin_notes.claim(NOW)
    many = [_entry(shown=NOW * 1000 - i) for i in range(1, 251)]
    _store(claude_home, *many)
    assert len(plugin_notes.claim(NOW)) == 250
    assert _said_keys(claude_home) == sorted((SESSION, NOW * 1000 - i) for i in range(1, 251))
    _store(claude_home, *many[1:])
    assert plugin_notes.claim(NOW) == []
    assert _said_keys(claude_home) == sorted((SESSION, NOW * 1000 - i) for i in range(2, 251))


def test_a_full_store_is_quiet_on_the_next_stop(claude_home):
    """Past the old cap of 200, the oldest came back as new on every Stop."""
    _store(claude_home)
    plugin_notes.claim(NOW)
    _store(claude_home, *[_entry(shown=(NOW - 13 * 3600) * 1000 - i) for i in range(250)])
    plugin_notes.claim(NOW)
    state = claude_home / "hobson-plugin-notes.json"
    before, lines = state.stat().st_mtime_ns, len(_said_log())
    time.sleep(0.01)
    assert plugin_notes.claim(NOW) == []
    assert state.stat().st_mtime_ns == before
    assert len(_said_log()) == lines


def test_one_note_in_two_store_files_is_one_note(claude_home):
    """A plugin update can leave the old hash's file behind."""
    _store(claude_home)
    plugin_notes.claim(NOW)
    _store(claude_home, _entry(shown=NOW * 1000 - 5000))
    _store(claude_home, _entry(shown=NOW * 1000 - 5000), name="cc-plugin-you-should-know_builtin-old999.json")
    assert len(plugin_notes.claim(NOW)) == 1


_CLAIMER = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import plugin_notes
said = plugin_notes._said
def slow():
    got = said()
    time.sleep(0.05)
    return got
plugin_notes._said = slow
if sys.argv[2] == "noflock":
    plugin_notes.fcntl.flock = lambda *a: None
print(json.dumps([n.session for n in plugin_notes.claim(float(sys.argv[3]))]))
"""


def _claim_at_once(claude_home, n, flock="flock"):
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    env = dict(os.environ, HOME=str(claude_home.parent))
    procs = [_POPEN([sys.executable, "-c", _CLAIMER, scripts, flock, str(time.time())],
                              stdout=subprocess.PIPE, env=env) for _ in range(n)]
    return [json.loads(p.communicate(timeout=30)[0]) for p in procs]


def test_hooks_at_once_claim_a_note_exactly_once(claude_home):
    _store(claude_home)
    plugin_notes.claim()
    _store(claude_home, _entry(shown=time.time() * 1000))
    claimed = _claim_at_once(claude_home, 4)
    assert sorted(len(c) for c in claimed) == [0, 0, 0, 1]


# ── A source that is not what it was ───────────────────────────────────────

def test_a_malformed_store_yields_nothing_and_logs_once(claude_home):
    d = claude_home / "plugins" / "store"
    d.mkdir(parents=True)
    (d / "cc-plugin-you-should-know_builtin-abc.json").write_text("{not json")
    assert plugin_notes.claim(NOW) == []
    assert plugin_notes.claim(NOW) == []
    assert len([line for line in _said_log() if "could not read" in line]) == 1
    assert not (claude_home / "hobson-plugin-notes.json").exists()


@pytest.mark.parametrize("body", [{"other": []}, {"sessions": "x"}, [1, 2]])
def test_a_store_without_its_sessions_is_skipped(claude_home, body):
    d = claude_home / "plugins" / "store"
    d.mkdir(parents=True)
    (d / "cc-plugin-you-should-know_builtin-abc.json").write_text(json.dumps(body))
    assert plugin_notes.claim(NOW) == []
    assert len(_said_log()) == 1


MS = NOW * 1000


@pytest.mark.parametrize("entry", [
    {"id": SESSION, "view": {"kind": "offer", "line": "", "shownAt": MS}},
    {"id": SESSION, "view": {"kind": "other", "line": "x", "shownAt": MS}},
    {"id": SESSION, "view": {"kind": "offer", "line": "x"}},
    {"id": SESSION, "view": {"kind": "offer", "line": "x", "shownAt": "soon"}},
    {"id": SESSION, "view": {"kind": "offer", "line": "x", "shownAt": NOW}},
    {"id": SESSION, "view": {"kind": "offer", "line": "x", "shownAt": float("nan")}},
    {"id": SESSION, "view": {"kind": "offer", "line": "x", "shownAt": float("inf")}},
    {"id": "../../etc", "view": {"kind": "offer", "line": "x", "shownAt": MS}},
    {"id": SESSION, "view": "nope"},
])
def test_a_view_without_the_expected_shape_is_no_note_and_is_logged_by_its_keys(entry):
    assert plugin_notes._note(entry) is None
    (line,) = _said_log()
    assert "did not have the expected shape" in line
    assert "'x'" not in line and "soon" not in line and "nope" not in line


@pytest.mark.parametrize("entry", [{"id": SESSION, "askedTurn": "a"}, {"id": SESSION, "view": None}, "nope"])
def test_an_entry_without_a_view_is_no_note_and_no_news(entry):
    assert plugin_notes._note(entry) is None
    assert _said_log() == []


def test_the_shape_is_logged_once_per_process():
    for shown in (NOW, NOW + 1):
        plugin_notes._note({"id": SESSION, "view": {"kind": "offer", "line": "x", "shownAt": shown}})
    assert len(_said_log()) == 1


def test_a_note_without_an_explanation_still_counts():
    note = plugin_notes._note({"id": SESSION, "view": {"kind": "offer", "line": "x.", "shownAt": MS}})
    assert note == plugin_notes.Note(SESSION, int(MS), "x.", "")


def test_a_note_in_seconds_or_not_a_number_skips_that_entry_and_keeps_the_rest(claude_home):
    """JSON from the plugin can carry NaN; int(NaN) used to raise past the
    reader and drop every note on every Stop."""
    _store(claude_home)
    plugin_notes.claim(NOW)
    _store(claude_home, _entry(shown=float("nan")), _entry(shown=NOW), _entry(OTHER, shown=MS - 1000))
    assert "NaN" in (claude_home / "plugins" / "store" / "cc-plugin-you-should-know_builtin-abc123.json").read_text()
    assert [n.session for n in plugin_notes.claim(NOW)] == [OTHER]
    assert not any("old" in line for line in _said_log())


# ── What is said ────────────────────────────────────────────────────────────

NOTE = plugin_notes.Note(SESSION, 1, LINE, EXPLANATION)
CONFIG = {"engine": "say"}


FOLLOW = "Run the copy first and drop the staging table after."


def test_the_notes_line_is_said_with_the_models_follow_up(fake_ollama):
    fake_ollama.chat(FOLLOW)
    text, how = plugin_notes.summary(NOTE, CONFIG)
    assert text == f"{LINE} {FOLLOW}"
    assert "follow-up" in how


def test_a_reply_about_something_else_falls_back_to_the_notes_line(fake_ollama):
    fake_ollama.chat("Redeploy the billing dashboard with kubernetes and terraform.")
    text, how = plugin_notes.summary(NOTE, CONFIG)
    assert text == LINE
    assert "not in the note" in how


def test_a_model_that_is_down_falls_back_to_the_notes_line(fake_ollama):
    fake_ollama.error(urllib.error.URLError("refused"))
    assert plugin_notes.summary(NOTE, CONFIG)[0] == LINE


def test_the_prompt_carries_the_explanation_without_its_markdown():
    prompt = plugin_notes.build_prompt(NOTE)
    assert "Heads up. The migrate script runs the drop first." in prompt
    assert "**" not in prompt and "`" not in prompt


def test_this_sessions_note_is_said_without_a_project(claude_home, fake_ollama):
    fake_ollama.chat(FOLLOW)
    _transcript(claude_home, SESSION, "/Dev/webapp")
    assert plugin_notes.phrase([NOTE], CONFIG, SESSION) == (f"{LINE} {FOLLOW}", None)


def test_another_sessions_note_names_its_project(claude_home, fake_ollama):
    fake_ollama.chat(FOLLOW)
    _transcript(claude_home, OTHER, "/Dev/webapp")
    phrase = plugin_notes.phrase([NOTE._replace(session=OTHER)], CONFIG, SESSION)
    assert phrase == (f"On webapp: the migration script drops the staging table before the copy finishes. {FOLLOW}",
                      "webapp")


def test_the_rest_are_counted(claude_home, fake_ollama):
    fake_ollama.error(urllib.error.URLError("refused"))
    _transcript(claude_home, OTHER, "/Dev/mobile-app")
    second = NOTE._replace(session=OTHER, shown_at=2)
    assert plugin_notes.phrase([NOTE, second], CONFIG, SESSION)[0] == f"{LINE} And one more, for mobile app."
    assert plugin_notes.phrase([NOTE, second, second, second], CONFIG, SESSION)[0] == f"{LINE} And three more."


def test_a_transcript_line_that_is_not_an_object_is_skipped_not_the_transcript(claude_home):
    d = claude_home / "projects" / "-some-project"
    d.mkdir(parents=True)
    (d / f"{OTHER}.jsonl").write_text('["cwd"]\n"cwd"\n{not json, "cwd"\n'
                                      + json.dumps({"cwd": "/Dev/webapp"}) + "\n")
    assert plugin_notes.project_of(OTHER) == "webapp"


def test_the_log_has_the_notes_id_and_length_and_not_its_text(claude_home, fake_ollama):
    fake_ollama.error(urllib.error.URLError("refused"))
    plugin_notes.phrase([NOTE], CONFIG, SESSION)
    body = "\n".join(_said_log())
    assert SESSION[:8] in body and str(NOTE.length) in body
    assert LINE not in body and "migrate" not in body


# ── At a Stop ───────────────────────────────────────────────────────────────

def _engine():
    return SayEngine({"engine": "say", "personality": "hobson", "events": ["stop"], "cooldown": 0,
                      "nudge": {"enabled": False}})


def _played():
    out = []
    for rec in log_record.read():
        played = log_record.parse_playback(rec.body.split("] ", 1)[1] if rec.body.startswith("[say] ") else rec.body)
        if played:
            out.append((played.kind, played.phrase))
    return out


def _stop(engine, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "All done."}]}}))
    engine.run({"hook_event_name": "Stop", "session_id": SESSION, "transcript_path": str(transcript)})


def test_a_stop_says_the_note_once_after_its_own_line(claude_home, tmp_path, monkeypatch, fake_ollama, no_face):
    import stop_outcome
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")
    fake_ollama.chat(FOLLOW)
    _store(claude_home)
    engine = _engine()
    _stop(engine, tmp_path)
    _store(claude_home, _entry(shown=time.time() * 1000))
    _stop(engine, tmp_path)
    played = _played()
    assert [kind for kind, _ in played[-2:]] == ["done", "note"]
    assert no_face[-1][:2] == (f"{LINE} {FOLLOW}", "note")
    _stop(engine, tmp_path)
    assert [kind for kind, _ in _played()].count("note") == 1
    assert [shown[1] for shown in no_face].count("note") == 1


def test_a_broken_store_never_breaks_the_stop(claude_home, tmp_path, monkeypatch):
    import stop_outcome
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")
    monkeypatch.setattr(plugin_notes, "claim", lambda: 1 / 0)
    _stop(_engine(), tmp_path)
    assert [kind for kind, _ in _played()] == ["done"]
    assert any("[plugin-notes] failed" in r.body for r in log_record.read())


# ── Its words stay out of the log, wherever it goes ─────────────────────────

WORD = "Quillfeather"


def _presence(claude_home, state):
    record = {"state": state, "source": "input", "ts": time.time(), "since": time.time(),
              "mode": "signals", "camera": "authorized", "pid": 0, "glance_ts": 0,
              "face": True, "face_only": False, "waves": True}
    (claude_home / "hobson-presence.json").write_text(json.dumps(record))


def _cycle_config():
    return {"engine": "say", "personality": "hobson", "events": ["stop"], "cooldown": 0,
            "nudge": {"enabled": False}, "presence": {"enabled": True, "mode": "signals"}}


def test_a_note_said_held_and_told_leaves_none_of_its_words_in_the_log(claude_home, tmp_path, monkeypatch,
                                                                      fake_ollama, no_face):
    """Said live; held while away and told by the return briefing, by a wave
    and by "What needs me?". The voice and the face get the words; the log
    gets the note's id and length."""
    import ask
    import log_stats
    import presence
    import recap
    import stop_outcome
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")
    fake_ollama.chat(f"Run the {WORD} copy before the drop.")
    config = _cycle_config()
    engine = SayEngine(config)
    entries = []

    def note_then_stop(state):
        entries.append(_entry(shown=time.time() * 1000 - 1000 * len(entries), line=f"The {WORD} script drops the table.",
                              explanation=f"**{WORD}** runs the drop first."))
        _store(claude_home, *entries)
        _presence(claude_home, state)
        _stop(engine, tmp_path)

    _presence(claude_home, "present")
    _store(claude_home)
    _stop(engine, tmp_path)
    note_then_stop("present")
    note_then_stop("away")
    _presence(claude_home, "present")
    presence.on_transition("away", "present", "input", away_for=600, config=config)
    note_then_stop("away")
    _presence(claude_home, "present")
    presence.on_wave(config=config)
    note_then_stop("away")
    _presence(claude_home, "present")
    ask.answer("status", config=config)

    spoken = [phrase for phrase, *_ in no_face]
    assert sum(WORD in phrase for phrase in spoken) == 4
    log = (claude_home / "hobson.log").read_text()
    assert WORD.lower() not in log.lower()
    assert [kind for kind, _ in _played() if kind in ("note", "briefing", "answer")] == \
        ["note", "briefing", "briefing", "answer"]
    lines = log.splitlines()
    assert log_stats.summarize(lines)["barked"] == sum("barked (" in line for line in lines)
    work = [str(item) for _, items in recap.recent_by_project(lines, now=time.time()) for item in items]
    assert work and not any("note" in item for item in work)


def test_the_face_reports_a_note_by_its_id_not_its_words():
    """The helper sends a line back to be logged (--face-event): it sends
    the record's `logged`, which a note's record carries."""
    import face
    phrase = log_record.Unlogged(f"The {WORD} script drops the table.", "note 11111111, 64 chars")
    record = face.compose({}, phrase, "note", None, "webapp", NOW)
    assert record["phrase"] == phrase and record["logged"] == "note 11111111, 64 chars"
    assert json.loads(json.dumps(record))["phrase"] == f"The {WORD} script drops the table."
    assert "logged" not in face.compose({}, "The build is green.", "done", None, "webapp", NOW)


def test_a_note_is_never_made_into_a_cached_clip(claude_home, tmp_path, monkeypatch, fake_ollama, no_audio):
    """On chatterbox a miss is backfilled into the cache, with the phrase on
    the generator's argv."""
    import stop_outcome
    from engines.chatterbox import ChatterboxEngine
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")
    fake_ollama.chat(FOLLOW)
    engine = ChatterboxEngine({"engine": "chatterbox", "personality": "hobson", "events": ["stop"],
                               "cooldown": 0, "nudge": {"enabled": False}})
    made = []
    monkeypatch.setattr(engine, "backfill", made.append)
    _store(claude_home)
    _stop(engine, tmp_path)
    _store(claude_home, _entry(shown=time.time() * 1000))
    _stop(engine, tmp_path)
    assert LINE not in " ".join(made)
    assert not any(LINE in " ".join(map(str, args)) for args in no_audio["popen"])


# ── Another session's note is filed under its project ───────────────────────

def test_another_sessions_note_is_held_under_its_project_and_named_once(claude_home, tmp_path, monkeypatch,
                                                                       fake_ollama, no_face):
    import presence
    import stop_outcome
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")
    fake_ollama.error(urllib.error.URLError("refused"))
    _transcript(claude_home, OTHER, "/Dev/webapp")
    config = _cycle_config()
    engine = SayEngine(config)
    _presence(claude_home, "present")
    _store(claude_home)
    _stop(engine, tmp_path)
    _store(claude_home, _entry(OTHER, shown=time.time() * 1000))
    _presence(claude_home, "away")
    _stop(engine, tmp_path)
    held = [(i["project"], i["kind"]) for i in presence.held()]
    assert ("webapp", "note") in held
    _presence(claude_home, "present")
    text = presence.on_transition("away", "present", "input", away_for=600, config=config)
    assert text.count("webapp") == 1
    assert "On webapp: the migration script drops" in text


def test_another_sessions_note_said_live_names_its_project_once_and_shows_it_in_the_corner(
        claude_home, tmp_path, monkeypatch, fake_ollama):
    import face
    import stop_outcome
    monkeypatch.setattr(stop_outcome, "classify", lambda *a, **k: "done")
    fake_ollama.error(urllib.error.URLError("refused"))
    corners = []
    monkeypatch.setattr(face, "show", lambda config, phrase, kind, audio=None, project=None, **k:
                        corners.append((kind, project, phrase)))
    _transcript(claude_home, OTHER, "/Dev/webapp")
    engine = _engine()
    _store(claude_home)
    _stop(engine, tmp_path)
    _store(claude_home, _entry(OTHER, shown=time.time() * 1000))
    _stop(engine, tmp_path)
    kind, project, phrase = corners[-1]
    assert (kind, project) == ("note", "webapp")
    assert phrase.count("webapp") == 1


# ── While Hobson is silenced ────────────────────────────────────────────────

def _muted_stop(claude_home, hobson_entry, monkeypatch, config=None, session=SESSION, message=None):
    (claude_home / "hobson.json").write_text(json.dumps({"muted": True, **(config or {})}))
    monkeypatch.setattr(hobson_entry, "load_engine", lambda cfg: (_ for _ in ()).throw(
        AssertionError("no engine while silenced")))
    monkeypatch.setattr(hobson_entry.sys, "stdin", io.StringIO(json.dumps(
        {"hook_event_name": "Stop", "session_id": session, "last_assistant_message": message})))
    hobson_entry.main()
    return (claude_home / "hobson.log").read_text()


def test_a_muted_stop_shows_the_notes_line_on_the_face_and_claims_it(claude_home, hobson_entry, monkeypatch, no_face):
    _store(claude_home)
    plugin_notes.claim()
    _store(claude_home, _entry(session=OTHER, shown=time.time() * 1000))
    log = _muted_stop(claude_home, hobson_entry, monkeypatch)
    assert no_face == [(LINE, "note", None, "silent")]
    assert f"note claimed, note {OTHER[:8]} shown" in log
    assert LINE not in log and "migrate" not in log
    _muted_stop(claude_home, hobson_entry, monkeypatch)
    assert len(no_face) == 1
    assert (OTHER, int(plugin_notes.read_store()[0].shown_at)) in _said_keys(claude_home)


def test_a_muted_stop_with_the_face_off_claims_the_note_and_shows_nothing(claude_home, hobson_entry, monkeypatch,
                                                                         no_face):
    _store(claude_home)
    plugin_notes.claim()
    _store(claude_home, _entry(shown=time.time() * 1000))
    log = _muted_stop(claude_home, hobson_entry, monkeypatch, {"face": {"enabled": False}})
    assert no_face == []
    assert "note claimed, not shown (face off)" in log
    _muted_stop(claude_home, hobson_entry, monkeypatch)
    assert no_face == []


def test_a_muted_question_stop_keeps_the_wait_on_the_face_and_claims_the_note(claude_home, hobson_entry,
                                                                            monkeypatch, no_face):
    _store(claude_home)
    plugin_notes.claim()
    _store(claude_home, _entry(shown=time.time() * 1000))
    log = _muted_stop(claude_home, hobson_entry, monkeypatch,
                      message="I drafted both versions. Which one should I ship, A or B?")
    assert [shown[1] for shown in no_face] == ["waiting"]
    assert "note claimed, not shown (a wait is on the face)" in log
    assert LINE not in log and "migrate" not in log
    assert len(plugin_notes.claim()) == 0


def test_a_muted_stop_counts_the_rest_and_shows_only_the_first(claude_home, hobson_entry, monkeypatch, no_face):
    _store(claude_home)
    plugin_notes.claim()
    now = time.time() * 1000
    _store(claude_home, _entry(shown=now - 5000, line="First note line."), _entry(shown=now, line="Second."))
    log = _muted_stop(claude_home, hobson_entry, monkeypatch)
    assert [shown[0] for shown in no_face] == ["First note line."]
    assert "2 notes claimed" in log


def test_the_muted_stop_with_a_note_imports_no_engine(claude_home, tmp_path):
    (claude_home / "hobson.json").write_text(json.dumps({"muted": True}))
    (claude_home / "hobson-presence.spawn").write_text(str(time.time()))
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "hobson.py")
    stop = json.dumps({"hook_event_name": "Stop", "session_id": SESSION}).encode()

    def run():
        proc = _POPEN([sys.executable, "-X", "importtime", entry], cwd=str(tmp_path),
                      env={**os.environ, "HOME": str(tmp_path)},
                      stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        _, err = proc.communicate(stop)
        assert proc.returncode == 0
        return {line.rsplit("|", 1)[-1].strip() for line in err.decode().splitlines()
                if line.startswith("import time:")}

    _store(claude_home)
    run()
    _store(claude_home, _entry(shown=time.time() * 1000))
    imported = run()
    assert {"plugin_notes", "face"} <= imported
    assert not {"engines", "engines.base", "phrase_gen", "decider"} & imported
    record = json.loads((claude_home / "hobson-face.json").read_text())
    assert (record["phrase"], record["kind"], record["silent"]) == (LINE, "note", True)
    assert "migrate" not in (claude_home / "hobson.log").read_text()
