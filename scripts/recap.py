"""recap.py — build and speak a summary of recent activity for `hobson recap`.

Pull, not push: the user asked "what have you been up to", so this reads
recent log lines for the current project, has Ollama summarise them as one
natural first-person paragraph, and speaks it once.

Deliberately does NOT go through engines.base.generate_or_skip /
phrase_gen.generate_or_skip — that path's word budget (4-12 words), dedup,
and reject-to-silence guards are all tuned for push notifications the user
did not ask for. A pull answer can be longer and must never go silent just
because it resembles something said earlier. Calls phrase_gen._chat directly
instead.
"""

import re
import sys
import time

import engines.base as base
import home
import log_record
import phrase_gen

DEFAULT_MINUTES = 10

# `hobson ask recap`: every project, over a longer window, the busiest few.
ALL_MINUTES = 30
ALL_PROJECTS = 3

# Cap the prompt size — a long-running project can log thousands of lines.
MAX_ITEMS = 12

# ~25-35 spoken words; give the model a little headroom over that.
NUM_PREDICT = 70

# Deterministic backstop on length — the prompt asks for under 35 words and
# observed replies ran 41-44.
MAX_RECAP_WORDS = 38
CHAT_TIMEOUT = 20


def _extract_phrase(body):
    """The spoken phrase in one record, else, from a generation trace, what
    the model was told had happened."""
    trace = log_record.parse_trace(body)
    if trace is None:
        text = (log_record.tail_phrase(body) or "").strip()
    else:
        text = (trace.spoken or "").strip() or (trace.detail or "").strip()
    return text or None


# What Hobson says for himself, not about the work: a briefing, a greeting,
# an answer, a nudge. Read back as activity, one recap summarised the last,
# and a briefing was retold as something a session had done.
OWN_KINDS = frozenset({"answer", "thinking", "briefing", "nudge", "stalled"})
_OWN_TAGS = ("[presence]", "[face]", "[ask]", "[nudge]", "[watchdog]", "[read]")


def _own_talk(body):
    if body.startswith(_OWN_TAGS):
        return True
    played = log_record.parse_playback(body)
    return played is not None and played.kind in OWN_KINDS


def _activity(lines, minutes, now):
    """(ts, project, phrase) for every piece of work in the window. A phrase
    Hobson said for himself is dropped wherever it appears: the playback
    lines older than the kind tag do not say what they were, but the line
    that composed it ("[presence] briefing ... -> '...'") does."""
    window_seconds = minutes * 60
    own, found = set(), []
    for record in log_record.records(lines):
        if _own_talk(record.body):
            phrase = log_record.tail_phrase(record.body)
            if phrase:
                own.add(phrase.strip())
            continue
        ts = record.timestamp(now)
        if ts is None or not 0 <= now - ts <= window_seconds:
            continue
        phrase = _extract_phrase(record.body)
        if phrase:
            found.append((ts, record.project, phrase))
    return [f for f in found if f[2] not in own]


def _items(found):
    """Oldest first, each once -- a spoken phrase is logged by the generation
    trace, the event line and the playback line -- the MAX_ITEMS most recent."""
    found.sort(key=lambda f: f[0])
    return list(dict.fromkeys(f[2] for f in found))[-MAX_ITEMS:]


def recent_activity(lines, project, minutes=DEFAULT_MINUTES, now=None):
    """Spoken phrases / detail fragments for `project` within the last `minutes`.

    Pure and defensive -- malformed lines and other projects' lines are
    skipped, never raised on. Returns items oldest-first, each once, capped
    at the MAX_ITEMS most recent so the recap prompt stays small.
    """
    now = time.time() if now is None else now
    return _items([f for f in _activity(lines, minutes, now) if f[1] == project])


def recent_by_project(lines, minutes=ALL_MINUTES, now=None, limit=ALL_PROJECTS):
    """[(project, items)] for the `limit` projects most recently at work in
    the last `minutes`, most recent first; items as recent_activity. Pure."""
    now = time.time() if now is None else now
    by_project = {}
    for f in _activity(lines, minutes, now):
        if f[1] and f[1] != "unknown":
            by_project.setdefault(f[1], []).append(f)
    latest = sorted(by_project, key=lambda p: max(f[0] for f in by_project[p]), reverse=True)
    return [(p, _items(by_project[p])) for p in latest[:limit]]


def build_prompt(items):
    """The recap instruction sent to Ollama, or None when there's nothing to say."""
    if not items:
        return None
    activity = "\n".join(f"- {item}" for item in items)
    return (
        "You are an engineer. Another engineer just asked what you've been "
        "doing. Below is your recent activity, oldest first.\n\n"
        "This is NOT a status update, NOT a standup report, and NOT a summary "
        "of your progress. It is one engineer telling another what they just "
        "did, out loud, in passing.\n\n"
        "Wrong -- reads like a project manager:\n"
        "  \"I've had a big shift with my research, which changed everything, "
        "before getting back to refactoring some scripts and making progress "
        "on the remaining items.\"\n\n"
        "Right -- reads like an engineer:\n"
        "  \"Reordered the plan tasks and reran the suite. The flush gate's "
        "batching properly now.\"\n\n"
        "Rules: first person. At most two sentences, each under 15 words. "
        "Name the actual files, commands and tests. Never say you 'worked on', "
        "'looked at', 'made progress on', or 'had a shift in' anything -- say "
        "what changed.\n"
        "Short plain sentences. No jargon, no abstract nouns, no stacked "
        "clauses. Use the words you'd say to someone at the next desk, not "
        "the words in a design document. If a shorter word exists, use it.\n"
        "No preamble, no hedging, no reading the log back.\n\n"
        f"Recent activity:\n{activity}"
    )


def build_project_prompt(items):
    """For one project of a recap across all of them: one sentence, no
    subject, and nothing that is not in the notes. Not build_prompt's
    engineer register: its "name the actual files" rule, given a few thin
    notes, had llama3.2:3b invent file names ("odio dot P Y"), and it copied
    the examples ("the flush gate") into projects that never had one. So no
    examples at all."""
    if not items:
        return None
    notes = "\n".join(f"- {item}" for item in items)
    return (
        "Below are notes, oldest first, on what one coding session did in the last half hour.\n"
        "Write ONE sentence, under 16 words, saying what it did. Start with a past-tense verb "
        "and give it no subject: never I, we or they.\n"
        "Use only what the notes say. Add no file, test, tool, command or name that is not in "
        "them. If the notes are vague, be vague.\n"
        "No preamble, no quotes, nothing else.\n\n"
        f"Notes:\n{notes}"
    )


def _echoes_the_prompt(text, prompt):
    """The reply repeats the prompt's instructions rather than the notes:
    four words running that are in the instructions and not in the notes."""
    instructions, _, notes = prompt.partition("Notes:")
    words = lambda t: re.findall(r"[a-z']+", t.lower())  # noqa: E731
    grams = lambda w: {tuple(w[i:i + 4]) for i in range(len(w) - 3)}  # noqa: E731
    return bool(grams(words(text)) & (grams(words(instructions)) - grams(words(notes))))


# Verbs a summary may use that the notes need not (as four-letter stems,
# phrase_gen._content_stems): what was done, not what it was done to.
_SUMMARY_VERBS = frozenset(
    "fixe adde upda wrot ran rera chan move remo chec revi buil made star fini ship push comm "
    "edit read foun trie set clea rena test look work inve spli repl wire".split())


def _not_in_the_notes(text, items):
    """The reply's content words the notes do not have, beyond summary
    verbs: two or more is a reply about something else."""
    stray = phrase_gen._content_stems(text) - phrase_gen._content_stems(" ".join(items)) - _SUMMARY_VERBS
    return sorted(stray) if len(stray) >= 2 else None


_SAYABLE = re.compile(r"^[A-Z][^;:()\[\]]*[.!?]$")


def _fallback(items):
    """The session's own latest line, when the model's will not do: said
    already, so it is true. A batch summary ("2 Bash (...); Write: x") is
    not a sentence to say."""
    for item in reversed(items):
        if _SAYABLE.match(item.strip()):
            return item.strip()
    return None


# A recap across projects: each sentence this long at most, and the whole
# answer inside this budget (the face's "One moment." waits as long).
PROJECT_WORDS = 18
ALL_BUDGET_SECONDS = 25.0


def summarise(prompt, config, timeout, limit=MAX_RECAP_WORDS, check=None):
    """One recap from the model, cleaned and capped: (text, error). `check`
    sees the reply as the model wrote it, before it is made speakable, and
    returns why it will not do, or None."""
    ollama_cfg = config.get("ollama", {})
    accumulated, err = phrase_gen._chat(
        [{"role": "user", "content": prompt}], ollama_cfg.get("model"), timeout,
        ollama_cfg.get("url"), num_predict=NUM_PREDICT)
    text = _clean_recap(accumulated)
    if not text:
        return None, err or "empty reply"
    why = check(text) if check else None
    if why:
        return None, why
    # Plain words before length: shortening first would preserve jargon and
    # cut the substance instead.
    text = phrase_gen._plain_verbs(base.tts_normalize(text))
    text, stripped = _strip_pm_speak(text)
    if stripped:
        log_record.write(f"recap: stripped status-report phrasing {stripped}")
    return _trim_to_words(text, limit=limit), None


def _sentence(text):
    text = (text or "").strip()
    return text if not text or text[-1] in ".!?" else text + "."


def _after_colon(text):
    """After "On webapp:", "Fixed the redirect." reads on as "fixed the
    redirect."; an acronym ("API checks pass.") keeps its capitals."""
    first = text.split(" ", 1)[0]
    if len(first) > 1 and first[0].isupper() and first[1:].islower():
        return text[0].lower() + text[1:]
    return text


def across_projects(lines, config, now=None, clock=time.monotonic):
    """The recap of every project: (text, notes). One model call per
    project, joined here, not by the model: a 3B model asked about three
    projects at once blends them. `notes` says how each call went ("webapp
    1.8s", "hobson timeout"), for the log. A project whose call fails is
    left out; text is None when nothing came back, "" when nothing happened."""
    groups = recent_by_project(lines, now=now)
    if not groups:
        return "", []
    deadline = clock() + ALL_BUDGET_SECONDS
    told, notes = [], []
    for project, items in groups:
        left = deadline - clock()
        if left < 1:
            notes.append(f"{project} out of time")
            continue
        began = clock()
        prompt = build_project_prompt(items)

        def check(reply, prompt=prompt, items=items):
            if _echoes_the_prompt(reply, prompt):
                return f"repeated the instructions: {reply!r}"
            stray = _not_in_the_notes(reply, items)
            return f"not in the notes {stray}: {reply!r}" if stray else None

        text, err = summarise(prompt, config, min(CHAT_TIMEOUT, left), limit=PROJECT_WORDS, check=check)
        took = clock() - began
        if text:
            told.append(f"On {project}: {_after_colon(text)}")
            notes.append(f"{project} {took:.1f}s")
            continue
        said = _fallback(items)
        if said:
            told.append(f"On {project}: {_sentence(base.tts_normalize(said))}")
            notes.append(f"{project} said as logged, the model's reply dropped ({err}) after {took:.1f}s")
        else:
            notes.append(f"{project} failed ({err}) after {took:.1f}s")
    return (" ".join(told) or None), notes


# Phrases that mark status-report register rather than an engineer talking.
# Substitutions are conservative: each one shortens the sentence without
# inventing detail the model did not supply.
_PM_FILLER = (
    (re.compile(r"\bI've been working on\b", re.I), "I"),
    (re.compile(r"\bI have been working on\b", re.I), "I"),
    (re.compile(r"\bI(?:'ve)? made progress on\b", re.I), "I moved on"),
    (re.compile(r"\bI(?:'ve)? spent (?:some )?time on\b", re.I), "I"),
    (re.compile(r"\bmostly (?:bug fixes and )?minor tweaks\b", re.I), "small fixes"),
    (re.compile(r",? multiple times actually,?", re.I), ""),
    (re.compile(r"\bwhich changed everything,?\s*", re.I), ""),
    (re.compile(r"\bI(?:'ve)? had a big shift (?:with|in)\b", re.I), "I changed"),
    (re.compile(r"\bcircled back to\b", re.I), "went back to"),
    (re.compile(r"\btouched base on\b", re.I), "checked"),
    (re.compile(r"\bmy research direction\b", re.I), "the research"),
)


def _strip_pm_speak(text):
    """Rewrite status-report register into plain engineer speech.

    The prompt already forbids this, but a prompt rule is a bias, not a
    guarantee. Returns (text, stripped) so callers can log when the model
    drifted -- silent correction would hide that the prompt is losing.
    """
    if not text:
        return text, ()
    stripped = []
    for pattern, replacement in _PM_FILLER:
        new = pattern.sub(replacement, text)
        if new != text:
            stripped.append(pattern.pattern)
            text = new
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text, tuple(stripped)


def _trim_to_words(text, limit=MAX_RECAP_WORDS):
    """Hard cap on recap length, cutting at a sentence boundary where possible.

    The prompt asks for under 35 words; observed replies ran 41-44 and rambled.
    A prompt rule is a bias, not a guarantee -- every hard constraint needs a
    deterministic backstop, and this is it.
    """
    if not text:
        return text
    words = text.split()
    if len(words) <= limit:
        return text
    clipped = " ".join(words[:limit])
    cut = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
    # Only honour a sentence end that keeps most of the content; otherwise the
    # trim would throw away the substance to gain tidiness.
    if cut > len(clipped) // 2:
        return clipped[:cut + 1]
    return clipped.rstrip(",;:-— ") + "."


def _clean_recap(raw):
    """Strip think-tags, wrapping quotes, and stray formatting from the reply."""
    if not raw:
        return None
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    text = text.strip("\"'").strip("*").strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _read_log_lines():
    try:
        with open(home.log_file(), encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError:
        return []


def _parse_minutes(argv):
    if len(argv) > 1:
        parsed = home.parse_duration(argv[1])
        if parsed is not None:
            return parsed / 60.0
        print(f"Ignoring unparseable duration {argv[1]!r}; using {DEFAULT_MINUTES}m.")
    return DEFAULT_MINUTES


def main():
    minutes = _parse_minutes(sys.argv)

    config = home.load_config()
    project = home.derive_project_label() or "this project"

    items = recent_activity(_read_log_lines(), project, minutes=minutes)
    if not items:
        print(f"Nothing to recap for {project} in the last {int(minutes)} minute(s).")
        return

    text, err = summarise(build_prompt(items), config, CHAT_TIMEOUT)
    if not text:
        print(f"Recap failed{f' ({err})' if err else ''}.")
        return
    print(text)

    reason = home.silence_reason(config)
    if reason:
        print(f"(not spoken — {reason})")
        return

    import hobson
    engine = hobson.load_engine(config)
    engine.speak_dynamic(text, allow_cold_start=True)


if __name__ == "__main__":
    main()
