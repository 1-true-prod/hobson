#!/usr/bin/env python3
"""ask.py — Hobson answers when asked: from the menu bar ("Ask Hobson") or
`hobson ask`.

Everything else Hobson says is pushed: something happened, so he speaks.
These are pulled: you asked, so he answers, always, and about every session
at once rather than the one a hook fired in. Three questions:

    status   What needs me?              who is waiting on you, what failed,
                                         what finished, what is still working,
                                         and anything held for your return
    failed   What failed?                each failure, with what was said of it
    recap    What have you been doing?   the last half hour across projects
                                         (recap.across_projects: the model)

status and failed are read from what Hobson already keeps (session state,
the salver, the log) and never go near a model: they cannot make anything
up. An answer is kind "answer": never held for your return (you are at the
menu, whatever the camera thinks), shown on the face with no project (it is
about all of them). Muted, it is shown on the face and not spoken, even
with the face off: you asked. The face shows "One moment." first, since
even an instant answer takes a few seconds to be heard.

Every step is logged under [ask] for `hobson monitor`, the model calls with
their timings.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home  # noqa: E402
import kinds  # noqa: E402
import log_record  # noqa: E402
import session_status  # noqa: E402

QUESTIONS = {"status": "What needs me?", "failed": "What failed?",
             "recap": "What have you been doing?"}

THINKING_LINE = "One moment."
QUIET_LINE = "All quiet. Nothing needs you."
NO_FAILURES = "Nothing has failed in the last two hours."
NOTHING_DONE = "Nothing has happened in the last half hour."
RECAP_FAILED = "I could not put that together just now."

# Two told in full, the rest counted, as the briefing does.
TOLD = 2

_NUMBERS = {2: "two", 3: "three", 4: "four", 5: "five"}


def _number(n):
    return _NUMBERS.get(n, str(n))


# ── What there is to tell ──────────────────────────────────────────────────

def compose_status(read, held):
    """The answer to "What needs me?". Pure: `read` is session_status.reading(), `held` the
    salver's items (told once, as a wave's answer tells them). Waits first,
    then what was held, then failures; two told and the rest counted; then
    what finished and what is still working, as counts past one."""
    import presence
    urgent = [f"{_capital(project or 'a session')} is waiting on {presence.NEED.get(need, 'you')}."
              for project, need in read["waiting"]]
    waiting = {project for project, _ in read["waiting"]}
    for item in sorted(held, key=lambda i: (kinds.of(i.get("kind")).urgency, -float(i.get("ts") or 0))):
        project = item.get("project") or ""
        if item.get("kind") == "waiting" and project in waiting:
            continue  # already told, by the wait itself
        urgent.append(presence.told(item, project))
    urgent += [f"{_capital(project)} stopped on a failure." for project in read["failed"]]
    told = urgent[:TOLD]
    if len(urgent) > TOLD:
        told.append(f"Plus {len(urgent) - TOLD} more.")

    after = []
    finished, working = read["finished"], read["working"]
    if finished:
        after.append(f"{finished[0]} finished" if len(finished) == 1
                     else f"{_number(len(finished))} sessions finished")
    if working:
        after.append(f"{working[0]} is still working" if len(working) == 1
                     else f"{_number(len(working))} are still working")
    if not told and not after:
        return QUIET_LINE
    lead = [] if told else ["Nothing needs you."]
    tail = [_capital(", and ".join(after)) + "."] if after else []
    return log_record.joined(lead + told + tail)


def compose_failed(read, lines, now=None):
    """The answer to "What failed?": each failed session with the last thing
    said of its failure, from the log. Pure over `lines`."""
    now = time.time() if now is None else now
    if not read["failed"]:
        return NO_FAILURES
    last = {}
    for record in log_record.records(lines):
        if record.project not in read["failed"]:
            continue
        ts = record.timestamp(now)
        if ts is None or now - ts > session_status.FAILED_WITHIN:
            continue
        outcome = log_record.parse_outcome(record.body)
        played = log_record.parse_playback(record.body)
        if outcome and outcome.category == "broken":
            last[record.project] = outcome.phrase
        elif played and played.kind == "broken":
            last[record.project] = played.phrase
    told = []
    for project in read["failed"][:TOLD]:
        said = last.get(project)
        told.append(f"On {project}: {_sentence(said)}" if said else f"{_capital(project)} stopped on a failure.")
    if len(read["failed"]) > TOLD:
        told.append(f"Plus {len(read['failed']) - TOLD} more.")
    return " ".join(told)


def _capital(text):
    return text[:1].upper() + text[1:]


def _sentence(text):
    text = (text or "").strip()
    return text if not text or text[-1] in ".!?" else text + "."



# ── Answering ──────────────────────────────────────────────────────────────

def compose(question, config, now=None):
    """The answer's text, and how it came about (for the log). Never raises."""
    now = time.time() if now is None else now
    if question == "recap":
        import recap
        text, notes = recap.across_projects(log_record.lines(), config, now=now)
        if text is None:
            return RECAP_FAILED, f"recap failed: {', '.join(notes) or 'no reply'}"
        if not text:
            return NOTHING_DONE, "recap: nothing in the last 30 minutes"
        return text, f"recap: {', '.join(notes)}"
    read = session_status.reading(now)
    if question == "failed":
        return compose_failed(read, log_record.lines(), now), f"failed: {len(read['failed'])}"
    import presence
    held = presence.take_held()
    counts = {k: len(v) for k, v in read.items()}
    return compose_status(read, held), f"status: {counts}, {len(held)} held"


def answer(question, source="menu", config=None):
    """Answer one of QUESTIONS aloud and on the face. Returns the text."""
    import face
    config = home.load_config() if config is None else config
    question = question if question in QUESTIONS else "status"
    log_record.write(f"[ask] asked {QUESTIONS[question]!r} ({source})")
    reason = home.silence_reason(config)
    # On the face first: even an instant answer takes a few seconds to be
    # heard. Only if the answer will be shown too, or it would sit there.
    visible = bool(reason) or face.settings(config).get("enabled")
    if visible:
        face.show(config, THINKING_LINE, "thinking", asked=bool(reason))
    began = time.time()
    try:
        text, how = compose(question, config)
    except Exception as e:  # an answer is owed: say so rather than nothing
        text, how = RECAP_FAILED, f"failed ({e})"
    log_record.write(f"[ask] answer to {QUESTIONS[question]!r} in {time.time() - began:.1f}s, {how}: {text!r}")
    if reason:
        log_record.write(f"[ask] muted ({reason}): shown on the face, not spoken")
        face.show(config, text, "answer", asked=True)
        return text
    import hobson
    hobson.load_engine(config).speak(text, "answer", cold_start=True)
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="?", default="status", choices=sorted(QUESTIONS))
    parser.add_argument("--source", default="cli")
    args = parser.parse_args(argv)
    home.migrate_legacy_state()
    print(answer(args.question, source=args.source))


if __name__ == "__main__":
    main()
