"""The kind of a phrase, declared once (kinds.py)."""

import os
import re

import pytest

import kinds

FACE_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "presence", "face", "face.js")


def test_a_speaking_path_must_name_a_kind():
    assert kinds.require("done") == "done"
    for bad in (None, "", "Done", "finished"):
        with pytest.raises(ValueError):
            kinds.require(bad)


def test_an_unknown_kind_read_back_is_news():
    assert kinds.of("from-a-newer-version") == kinds.KINDS["info"]
    assert kinds.of(None) == kinds.KINDS["info"]


def test_what_is_never_held():
    """You asked, or it checks for itself: whoever is in the room."""
    assert kinds.named("never_held") >= {"briefing", "nudge", "answer"}
    assert not kinds.named("never_held") & {"waiting", "broken", "stalled", "done", "info",
                                           "commentary"}


def test_only_commentary_is_said_in_passing():
    assert kinds.named("passing") == {"commentary"}


def test_the_briefing_leads_with_what_needs_you():
    order = sorted(["info", "done", "stalled", "broken", "waiting"], key=lambda k: kinds.of(k).urgency)
    assert order == ["waiting", "broken", "stalled", "done", "info"]


def test_hobsons_own_talk_is_not_work():
    assert kinds.named("own_talk") == {"answer", "thinking", "briefing", "nudge", "stalled", "note"}


def test_only_a_note_keeps_its_words_out_of_the_log():
    """A note from cc-plugin-you-should-know is session text (plugin_notes)."""
    assert kinds.named("private") == {"note"}


@pytest.mark.parametrize("category, kind", [("question", "waiting"), ("broken", "broken"),
                                            ("done", "done"), (None, "done")])
def test_a_stop_is_spoken_as(category, kind):
    assert kinds.stop_kind(category) == kind


@pytest.mark.parametrize("hook, kind", [
    ({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}, "waiting"),
    ({"hook_event_name": "Notification", "notification_type": "idle_prompt"}, "waiting"),
    ({"hook_event_name": "Notification", "notification_type": "agent_needs_input"}, "waiting"),
    ({"hook_event_name": "Notification", "notification_type": "permission_prompt"}, "waiting"),
    ({"hook_event_name": "Notification", "notification_type": "auth_success"}, "info"),
])
def test_an_event_is_spoken_as(hook, kind):
    assert kinds.event_kind(hook) == kind


# ── The face page keeps its own copy for drawing: held to the table ────────

def _face_js():
    with open(FACE_JS, encoding="utf-8") as f:
        return f.read()


def test_the_face_draws_every_kind():
    moods = re.search(r"const MOODS = \{(.*?)\n\};", _face_js(), re.S).group(1)
    drawn = set(re.findall(r"^\s*(\w+): \{", moods, re.M))
    assert drawn == set(kinds.KINDS)


def test_the_face_offers_its_buttons_for_the_kinds_the_table_says():
    listed = re.search(r"const ACTION_KINDS = new Set\(\[(.*?)\]\)", _face_js()).group(1)
    assert set(re.findall(r'"(\w+)"', listed)) == kinds.named("actions")
