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
import phrase_gen

DEFAULT_MINUTES = 10

# Cap the prompt size — a long-running project can log thousands of lines.
MAX_ITEMS = 12

# ~25-35 spoken words; give the model a little headroom over that.
NUM_PREDICT = 70

# Deterministic backstop on length — the prompt asks for under 35 words and
# observed replies ran 41-44.
MAX_RECAP_WORDS = 38
CHAT_TIMEOUT = 20

# Log lines look like "[HH:MM:SS] [project] <rest>" (engines/base.py:log()).
# No date is recorded, so window selection has to reconstruct one from `now`.
_LINE_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s+\[([^\]]+)\]\s+(.*)$")

# Three shapes the spoken phrase / detail can appear in, depending on which
# code path logged the line:
#   ...-> done -> 'I edited the parser.'          (BaseEngine._log / run())
#   ...detail='Bash: run the tests' ... spoken='I ran the tests.'  (phrase_gen trace)
_SPOKEN_RE = re.compile(r"spoken='([^']*)'\s*$")
_ARROW_QUOTE_RE = re.compile(r"->\s*'([^']*)'\s*$")
_DETAIL_RE = re.compile(r"detail='([^']*)'")


def _extract_phrase(rest):
    """Pull the spoken text (or a detail fallback) out of one log line's tail."""
    for pattern in (_SPOKEN_RE, _ARROW_QUOTE_RE):
        m = pattern.search(rest)
        if m and m.group(1).strip():
            return m.group(1).strip()
    m = _DETAIL_RE.search(rest)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return None


def _candidate_timestamp(hour, minute, second, now):
    """Reconstruct a full timestamp from a bare HH:MM:SS using now's date.

    Log lines carry no date. If the reconstructed time falls after `now`,
    the entry must actually be from the previous day (a run spanning
    midnight) rather than from the future.
    """
    today = time.localtime(now)
    try:
        candidate = time.mktime((
            today.tm_year, today.tm_mon, today.tm_mday,
            hour, minute, second, 0, 0, -1,
        ))
    except (ValueError, OverflowError):
        return None
    if candidate > now:
        candidate -= 86400
    return candidate


def recent_activity(lines, project, minutes=DEFAULT_MINUTES, now=None):
    """Spoken phrases / detail fragments for `project` within the last `minutes`.

    Pure and defensive — malformed lines and other projects' lines are
    skipped, never raised on. Returns items oldest-first, capped at the
    MAX_ITEMS most recent so the recap prompt stays small.
    """
    now = time.time() if now is None else now
    window_seconds = minutes * 60
    found = []

    for line in lines:
        m = _LINE_RE.match(line)
        if not m:
            continue
        hh, mm, ss, tag, rest = m.groups()
        if tag != project:
            continue
        try:
            hh, mm, ss = int(hh), int(mm), int(ss)
        except ValueError:
            continue
        ts = _candidate_timestamp(hh, mm, ss, now)
        if ts is None:
            continue
        age = now - ts
        if age < 0 or age > window_seconds:
            continue
        phrase = _extract_phrase(rest)
        if phrase:
            found.append((ts, phrase))

    found.sort(key=lambda pair: pair[0])
    return [phrase for _, phrase in found][-MAX_ITEMS:]


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
        with open(base.LOG_FILE, encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []


def _parse_minutes(argv):
    if len(argv) > 1:
        parsed = base.parse_duration(argv[1])
        if parsed is not None:
            return parsed / 60.0
        print(f"Ignoring unparseable duration {argv[1]!r}; using {DEFAULT_MINUTES}m.")
    return DEFAULT_MINUTES


def main():
    minutes = _parse_minutes(sys.argv)

    config = base.load_config()
    project = base.derive_project_label() or "this project"

    items = recent_activity(_read_log_lines(), project, minutes=minutes)
    if not items:
        print(f"Nothing to recap for {project} in the last {int(minutes)} minute(s).")
        return

    prompt = build_prompt(items)
    ollama_cfg = config.get("ollama", {})
    messages = [{"role": "user", "content": prompt}]
    accumulated, err = phrase_gen._chat(
        messages,
        ollama_cfg.get("model"),
        CHAT_TIMEOUT,
        ollama_cfg.get("url"),
        num_predict=NUM_PREDICT,
    )

    text = _clean_recap(accumulated)
    if not text:
        print(f"Recap failed{f' ({err})' if err else ''}.")
        return

    # Plain words before length: shortening first would preserve jargon and
    # cut the substance instead.
    text = phrase_gen._plain_verbs(base.tts_normalize(text))
    text, stripped = _strip_pm_speak(text)
    text = _trim_to_words(text)
    if stripped:
        base.log(f"recap: stripped status-report phrasing {stripped}")
    print(text)

    reason = base.silence_reason(config)
    if reason:
        print(f"(not spoken — {reason})")
        return

    import hobson
    engine = hobson.load_engine(config)
    engine.speak_dynamic(text, allow_cold_start=True)


if __name__ == "__main__":
    main()
