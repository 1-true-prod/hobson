"""What a spoken phrase is about -- its kind -- declared once.

Every phrase Hobson says carries a kind, and half a dozen modules make
decisions on it: presence holds or drops it, the voice drops it while
something plays, the face holds it until you type and offers its buttons,
recap reads it as Hobson's own talk rather than work. Each used to spell its
own subset of the names, and a phrase spoken with no kind became "info" by
default -- which is how `hobson recap` came to be held for your return and
retold by the next recap as work. Here each kind has every property those
modules ask about, and a speaking path must name one (`require`).

The face page (presence/face/face.js) keeps its own MOODS and ACTION_KINDS
for drawing; tests/test_kinds.py holds them to this table.
"""

from typing import NamedTuple

import log_record


class Kind(NamedTuple):
    urgency: int          # the briefing's order, most urgent first (presence)
    never_held: bool      # spoken whoever is in the room: you asked, or it checks for itself
    passing: bool         # said in passing: dropped, never held or queued; takes no look;
                          # off the face unless face.commentary
    waits_on_you: bool    # the face holds it until you type in that session
    actions: bool         # the face offers Skip / Take me there: a nudge may still run
    own_talk: bool        # Hobson's own talk, not the work (recap)
    names_project: bool   # the face names the session it is about
    private: bool = False  # session text: said and shown, but the log gets its length,
                           # never its words, and no clip of it is cached (log_record.Unlogged)


KINDS = {
    #                urgency never_held passing waits_on_you actions own_talk names_project
    "waiting":    Kind(0, False, False, True,  True,  False, True),
    "broken":     Kind(1, False, False, False, True,  False, True),
    "stalled":    Kind(2, False, False, False, True,  True,  True),
    "done":       Kind(3, False, False, False, False, False, True),
    "info":       Kind(4, False, False, False, False, False, True),
    "commentary": Kind(4, False, True,  False, False, False, True),
    # A nudge checks for itself and pauses rather than piling up (nudge._run).
    "nudge":      Kind(0, True,  False, True,  True,  True,  True),
    "briefing":   Kind(4, True,  False, False, False, True,  True),
    # You asked (ask.py, `hobson say`, `hobson coach`, `hobson recap`): about
    # every session, or none.
    "answer":     Kind(4, True,  False, False, False, True,  False),
    # "One moment." on the face while an answer is put together; never spoken.
    "thinking":   Kind(4, True,  False, False, False, True,  False),
    # A note cc-plugin-you-should-know left (plugin_notes): the plugin's
    # words about a session, not its work.
    "note":       Kind(4, False, False, False, False, True,  True,  private=True),
}


def require(name):
    """`name`, which a speaking path must give: a kind of KINDS. Raises
    ValueError for anything else, None included."""
    if name not in KINDS:
        raise ValueError(f"not a kind of phrase: {name!r}")
    return name


def of(name):
    """The properties of `name`; an unknown one (a record from an older
    version) is taken as "info"."""
    return KINDS.get(name) or KINDS["info"]


def named(prop):
    """The kinds with property `prop` set, as a frozenset."""
    return frozenset(k for k, v in KINDS.items() if getattr(v, prop))


def guarded(phrase, kind):
    """`phrase` as the log may see it: for a private kind, an Unlogged that
    logs as its kind and length, unless it already says how it logs."""
    if not of(kind).private or isinstance(phrase, log_record.Unlogged):
        return phrase
    return log_record.Unlogged(phrase, f"{kind}, {len(phrase)} chars")


def stop_kind(category):
    """What a Stop's phrase is about: a question waits on you, a failure is
    urgent, anything else (unknown included) is a finish."""
    return {"question": "waiting", "broken": "broken"}.get(category, "done")


def event_kind(hook_input):
    """What a permission request or notification is about. A permission
    request and a waiting notification wait on you; any other notification
    is news."""
    if hook_input.get("hook_event_name") == "PermissionRequest":
        return "waiting"
    import session_status
    if hook_input.get("notification_type") in session_status.WAITING_NOTIFICATIONS:
        return "waiting"
    return "info"
