"""phrase_gen.py — Session-aware phrase generation via Ollama.

Single unified function: generate_or_skip() handles ALL event types.
One Ollama call decides whether to voice or skip, and generates the phrase.
Uses /api/chat for model-agnostic formatting.

Returns (category, phrase) or (None, None) for SKIP/failure.
"""

import json
import re
import time
from urllib.request import urlopen
from urllib.error import URLError

import home
import log_record
from ollama_client import DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL, build_request
from stop_outcome import FAILURE_ALTS as _FAILURE_ALTS

VALID_CATEGORIES = {"done", "broken", "question"}

# Word budget. Stated identically here and in the prompt rules so that
# "compliance" is a single well-defined number.
MIN_WORDS = 4
MAX_WORDS = 12


def _fits_budget(written, spoken):
    """Is a phrase within the word budget? Too long only if too long both as
    the model wrote it and as it will be spoken: spelling a name out is ours,
    not the model's, and 5 of the 25 over-budget rejections in the log were
    within budget as written, three of them Stops that then went silent --
    "CLAUDE.md" spoken as "CLAUDE dot M D" took 10 words to 13. Too short
    counts what is heard: "I'm starting on APP-1201." is spoken "I'm starting
    on." once the ticket number is dropped."""
    return spoken >= MIN_WORDS and min(written, spoken) <= MAX_WORDS


# Last raw Ollama output for debugging
last_raw = None


def _truncate_detail(text, limit=120):
    """Trim event_detail for the log. Real Bash commands reach ~780 chars."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


# --------------- unified prompt ---------------

_TERSE_RULES = (
    "- SKIP if: editing/reading/writing files, requesting permissions, running commands, "
    "or anything routine that doesn't change the big picture\n"
    "- VOICE if: task completed, error/failure, needs user decision, or major milestone\n\n"
    "Most events should be SKIP. Only voice what the developer actually needs to hear.\n\n"
)

_NORMAL_RULES = (
    "- SKIP only pure noise: file reads, grep/glob searches, status checks, ls\n"
    "- VOICE the agent's meaningful actions: edits, writes, running commands, "
    "dispatching subagents, completions, errors, decisions, milestones\n\n"
    "Lean toward VOICE — the developer wants narration of what the agent is doing.\n\n"
)

# Decided: the decider (gate.py) has already judged this batch worth
# hearing, so the model only phrases it. Left a SKIP, Ollama declined 2 of
# the 7 batches the calibration rated worth hearing. Without the no-failure
# line, dropping the VOICE/SKIP rules made it worse at the rest: over 36
# generations (12 above-bar batches x 3) it invented a failure 12 times and
# left first person 12 times, against normal's 8 and 4. With it: 5 and 2.
# The same line on the normal prompt moved nothing (7 invented).
_DECIDED_RULES = (
    "- This batch has already been judged worth a spoken update. Do not SKIP it.\n"
    "- It has not run yet, so nothing has failed: never mention an error or a failure.\n"
    "- Say what it is doing, in plain words.\n\n"
)

# Terse-mode few-shots: most PreToolUse + Permission events skip, only milestones voice.
_TERSE_FEWSHOTS = [
    # SKIP: routine edit
    ({"role": "user", "content": (
        "Session: Recent: \"I pushed the hobson changes.\"\nSince then: 1 Edit.\nLast announced 30s ago.\n\n"
        "Event: [PreToolUse] [Project: hobson] Edit: base.py"
    )}, {"role": "assistant", "content": "SKIP"}),
    # SKIP: routine read
    ({"role": "user", "content": (
        "Session: Recent: \"I pushed the changes.\"\n\n"
        "Event: [PreToolUse] [Project: hobson] Read: config.json"
    )}, {"role": "assistant", "content": "SKIP"}),
    # SKIP: permission request
    ({"role": "user", "content": (
        "Session: Recent: \"I finished reviewing the search filters PR.\"\n\n"
        "Event: [PermissionRequest] [Project: hobson] Bash"
    )}, {"role": "assistant", "content": "SKIP"}),
    # SKIP: routine bash
    ({"role": "user", "content": (
        "Session: Recent: \"I started on the auth module.\"\nSince then: 3 Edit, 1 Bash.\n\n"
        "Event: [PreToolUse] [Project: hobson] Bash: running git status"
    )}, {"role": "assistant", "content": "SKIP"}),
]

# Normal-mode few-shots: edits/writes/commands voice; only pure noise skips.
_NORMAL_FEWSHOTS = [
    # VOICE: edit on a meaningful file
    ({"role": "user", "content": (
        "Session: Recent: \"I started on the auth refactor.\"\n\n"
        "Event: [PreToolUse] [Project: hobson] Edit: base.py"
    )}, {"role": "assistant", "content": "done | I'm editing base."}),
    # VOICE: write a new file
    ({"role": "user", "content": (
        "Session: Recent: \"I'm scaffolding the new module.\"\n\n"
        "Event: [PreToolUse] [Project: hobson] Write: phrase_gen.py"
    )}, {"role": "assistant", "content": "done | I'm writing the phrase generator."}),
    # VOICE: bash with description
    ({"role": "user", "content": (
        "Session: Recent: \"I finished the changes.\"\n\n"
        "Event: [PreToolUse] [Project: hobson] Bash: running the test suite"
    )}, {"role": "assistant", "content": "done | I'm running the test suite."}),
    # SKIP: pure noise (status check)
    ({"role": "user", "content": (
        "Session: Recent: \"I pushed the changes.\"\n\n"
        "Event: [PreToolUse] [Project: hobson] Bash: git status"
    )}, {"role": "assistant", "content": "SKIP"}),
]

# Shared few-shots used in both modes: Stop, Notification, Agent dispatch.
_SHARED_FEWSHOTS = [
    # VOICE: Stop with completion
    ({"role": "user", "content": (
        "Session: No announcements yet.\n\n"
        "Event: [Stop] [Project: hobson] "
        "Earlier: User: push the changes.\nLast message: Pushed to origin/main."
    )}, {"role": "assistant", "content": "done | I pushed the hobson changes."}),
    # VOICE: Stop with PR review done
    ({"role": "user", "content": (
        "Session: Recent: \"I started reviewing the PR.\"\nSince then: 8 Read.\n\n"
        "Event: [Stop] [Project: mobile app, search result filters] "
        "Earlier: User: review the PR.\nLast message: 13 files reviewed, changes look clean."
    )}, {"role": "assistant", "content": "done | I finished reviewing the search filters PR."}),
    # VOICE: Stop with error
    ({"role": "user", "content": (
        "Session: Task: add the photo upload sheet to profile\n"
        "Recent: \"I started the build.\"\nSince then: 3 Bash.\n\n"
        "Event: [Stop] [Project: mobile app, profile photo upload] "
        "Last message: Build failed with type error in PhotoUploadSheet.kt line 42."
    )}, {"role": "assistant", "content": "broken | I hit a build error in photo upload."}),
    # VOICE: Stop with question
    ({"role": "user", "content": (
        "Session: Recent: \"I started on the auth refactor.\"\nSince then: 5 Edit, 2 Bash.\n\n"
        "Event: [Stop] [Project: hobson] "
        "Last message: Should I extract the config into a separate module or keep it inline?"
    )}, {"role": "assistant", "content": "question | I need a decision on the config approach."}),
    # VOICE: Notification (always)
    ({"role": "user", "content": (
        "Session: Recent: \"I'm researching token price APIs.\"\nSince then: 2 Edit.\n\n"
        "Event: [Notification] [Project: hobson] attention_required"
    )}, {"role": "assistant", "content": "question | I need your attention on hobson."}),
    # VOICE: dispatching subagent (notable action — voiced in both modes)
    ({"role": "user", "content": (
        "Session: No announcements yet.\n\n"
        "Event: [PreToolUse] [Project: mobile app, search result filters] "
        "Agent: researching token price API options"
    )}, {"role": "assistant", "content": "done | I'm researching token price APIs."}),
]


# Stop only. On a replay of 80 real commentary batches, blind-judged, the
# old prompt beat this one 36-25: given the request, the model spoke the
# developer's words as the action ("I'm opening a report for you") and
# traded the concrete step for a vaguer purpose, and the rule alone, with
# no Task to read, made it guess one -- as a question or an invented
# problem. On 39 real Stops that got a Task line the new prompt won 22-11.
_TASK_LINE = re.compile(r"^Session: Task: [^\n]*\n")

_TASK_RULE = (
    "- Task is what the developer asked for. Use it to say what the "
    "work is for, in your own words.\n"
    "- Report what the Last message says. Earlier is background: never "
    "report it as this turn's work.\n"
    "- The Project tag only says which tab this is. Say what was done from "
    "the messages, never from the project name.\n"
)


_AWAITING_ANSWER_NOTE = (
    "\n\nThe agent's last message asks the developer a question and is "
    "waiting for the answer. Reply question | and say what you need them to decide."
)


def _build_messages(event_type, event_detail, session_context, project=None,
                    custom_prompt=None, verbosity="terse", awaiting_answer=False):
    """Build chat messages for the unified decide+generate call.

    verbosity:
      "terse"   — current SKIP-heavy bias (only voice notable events)
      "normal"  — bias toward voicing meaningful actions, skip only pure noise
      "decided" — the decider already chose to speak; phrase it, never SKIP

    awaiting_answer: a Stop that ends on the agent's question (see
    stop_outcome.StopReading). Said next to the event, not among the rules,
    so the shared prefix Ollama caches stays the same.
    """
    project_tag = f"[Project: {project}] " if project else ""

    persona = (
        "You are the voice of an AI coding agent, reporting to a developer "
        "who is monitoring multiple agents across projects. First person — start with 'I'."
    )
    if custom_prompt:
        persona += f" Speak with this personality: {custom_prompt}"

    decided = verbosity == "decided"
    if decided:
        rules = _DECIDED_RULES
        fewshots = [(u, a) for u, a in _NORMAL_FEWSHOTS if a["content"] != "SKIP"]
    else:
        rules = _NORMAL_RULES if verbosity == "normal" else _TERSE_RULES
        fewshots = _NORMAL_FEWSHOTS if verbosity == "normal" else _TERSE_FEWSHOTS

    messages = [
        {"role": "user", "content": (
            f"{persona}\n\n"
            + ("Given the session context and new event:\n" if decided
               else "Given the session context and new event, decide:\n")
            + rules
            + ("Reply: category | phrase\n" if decided
               else "If voicing, reply: category | phrase\n")
            + f"Rules: {MIN_WORDS}-{MAX_WORDS} words, starts with 'I', "
            "use the task name not ticket numbers\n"
            "- Describe what a thing does in plain words; never speak class "
            "names, file names, or identifiers. Say 'the account settings screen', "
            "not 'AccountSettingsFragment'.\n"
            + (_TASK_RULE if event_type == "Stop" else "")
            + "- Do not repeat or reword anything under Recent. Say something "
            + ("new.\n" if decided else "new or reply SKIP.\n")
            + ("- This action HAS NOT HAPPENED YET. Use present tense: "
               "\"I'm checking...\", never \"I checked...\".\n"
               if event_type == "PreToolUse" else "")
            + "Categories: done, broken, question"
        )},
        {"role": "assistant", "content": "Understood."},
    ]
    for u, a in fewshots:
        messages.append(u)
        messages.append(a)
    for u, a in _SHARED_FEWSHOTS:
        if event_type != "Stop":
            u = {**u, "content": _TASK_LINE.sub("Session: ", u["content"], count=1)}
        messages.append(u)
        messages.append(a)
    messages.append({"role": "user", "content": (
        f"Session: {session_context}\n\n"
        f"Event: [{event_type}] {project_tag}{event_detail}"
        + (_AWAITING_ANSWER_NOTE if awaiting_answer else "")
    )})
    return messages


# --------------- Ollama chat call ---------------

# Context window. At 1024 the prompt already had no room: the instructions
# and few-shots alone are ~590 tokens (llama3.2:3b), a commentary prompt ran
# to ~880 and a Stop with four full turns to ~1010, before the 32 tokens
# generated. Past the window Ollama drops the *earliest* message first --
# which is the one holding the rules -- so a long prompt silently lost its
# instructions (1085 tokens went in as 883). 2048 costs ~115 MB of KV cache.
NUM_CTX = 2048


def _chat(messages, model, timeout, ollama_url, num_predict=40, temperature=0.7):
    """Call /api/chat and stream tokens. Returns (accumulated_text, error_str)."""
    accumulated = ""
    prompt_tokens = None
    try:
        req = build_request("/api/chat", {
            "model": model or DEFAULT_OLLAMA_MODEL,
            "stream": True,
            "think": False,
            "keep_alive": -1,
            "messages": messages,
            "options": {"temperature": temperature, "num_predict": num_predict, "top_p": 0.9,
                        "num_ctx": NUM_CTX},
        }, ollama_url)
        with urlopen(req, timeout=timeout) as resp:
            for line in resp:
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                accumulated += chunk.get("message", {}).get("content", "")
                if chunk.get("done", False):
                    prompt_tokens = chunk.get("prompt_eval_count")
                    break
    except (URLError, OSError, json.JSONDecodeError, KeyError) as exc:
        return accumulated or None, f"(error: {exc})"

    # Ollama reports the full prompt size even when its prefix is cached,
    # and the size *after* truncation when it cut, so a prompt at the edge
    # of the window is the only visible sign that the rules were dropped.
    if isinstance(prompt_tokens, int) and prompt_tokens + num_predict >= NUM_CTX:
        log_record.write(f"prompt at the context limit: {prompt_tokens} tokens + {num_predict} "
                         f"to generate >= num_ctx {NUM_CTX}; the instructions may have been cut")

    return accumulated, None


# --------------- phrase cleaning ---------------

# Sentence-initial verbs common in agent status phrases. Used to decide whether
# a phrase missing its leading "I" is a subject-less verb phrase we can safely
# repair ("Finished the migration." -> "I finished the migration.") vs. one that
# already has its own subject ("The build failed.") and must be left alone.
_LEAD_VERBS = {
    "finished", "completed", "added", "fixed", "pushed", "merged", "ran",
    "wrote", "made", "built", "removed", "updated", "renamed", "shipped",
    "wrapped", "started", "found", "hit", "need", "needs", "created",
    "deleted", "refactored", "implemented", "set", "got", "caught", "broke",
    "tested", "verified", "committed", "reverted", "moved", "split",
}


def _looks_like_lead_verb(word):
    """Heuristic: does this leading word look like a verb (so 'I ' + it reads)?

    Matches a curated set plus the reliable past-tense '-ed' signal. Kept tight
    on purpose — '-ing' forms ("Running...") are excluded, since "I running" is
    ungrammatical and "Something"/"Nothing" would be false positives.
    """
    w = word.lower().rstrip(".,!?:;")
    return w in _LEAD_VERBS or (len(w) > 3 and w.endswith("ed"))


def _is_past_tense_verb(word):
    """Past-tense verb check for a word that cannot be anything else.

    Wider than _looks_like_lead_verb because callers use it on words in
    unambiguous verb positions -- directly after "I" or after "and" -- where
    "left" or "read" can only be verbs. Sentence-initially they cannot:
    prepending to "Left pane is broken." would give "I left pane is broken.",
    which is why _ensure_first_person keeps the narrower set.

    Anything _IRREGULAR_PROGRESSIVE can convert is by definition a past-tense
    verb, so detection and repairability stay in step automatically.
    """
    w = word.lower().rstrip(".,!?:;")
    return _looks_like_lead_verb(w) or w in _IRREGULAR_PROGRESSIVE


def _ensure_first_person(raw):
    """Prepend 'I ' only when the phrase is a subject-less verb phrase.

    The prompt asks for first-person phrases starting with 'I'. When the model
    drops that leading 'I' before a verb ("Finished the task."), restore it.
    Phrases that already start with 'I' or carry their own subject ("The build
    failed.") are left untouched rather than mangled ("I the build failed.").
    """
    if not raw or raw.lower().startswith(("i ", "i'")):
        return raw
    words = raw.split()
    if words and _looks_like_lead_verb(words[0]):
        return "I " + raw[0].lower() + raw[1:]
    return raw


# "need"/"needs" live in _LEAD_VERBS only so _ensure_first_person can
# restore a dropped subject on present-tense phrases ("Need your decision."
# -> "I need your decision."). They are not past tense and must not trip
# looks_past_tense -- this is an exceptions list, not a second vocabulary.
_PRESENT_TENSE_LEAD_VERBS = {"need", "needs"}


def looks_past_tense(phrase):
    """Does this phrase claim a completed action?

    Used to reject PreToolUse commentary, which fires *before* the tool
    runs -- "I inspected the SSH state" is a false claim at that point.
    Reuses _LEAD_VERBS so the past-tense vocabulary has one definition.
    """
    if not phrase:
        return False
    words = phrase.split()
    if len(words) < 2 or words[0].lower() not in ("i", "i've"):
        return False
    # "I'm ...", "I am ..." are present-progressive and always fine.
    if words[0].lower() in ("i'm",) or words[1].lower() == "am":
        return False
    verb = words[1].lower().rstrip(".,!?:;")
    if verb in _PRESENT_TENSE_LEAD_VERBS:
        return False
    return _is_past_tense_verb(words[1])


# Past-tense verbs whose progressive form the -ed rules below cannot derive.
# Only these need listing; every regular -ed verb is handled mechanically.
_IRREGULAR_PROGRESSIVE = {
    "found": "finding", "ran": "running", "hit": "hitting", "wrote": "writing",
    "set": "setting", "made": "making", "got": "getting", "read": "reading",
    "built": "building", "sent": "sending", "took": "taking", "saw": "seeing",
    "went": "going", "broke": "breaking", "caught": "catching", "left": "leaving",
    "lost": "losing", "put": "putting", "spent": "spending", "told": "telling",
    "thought": "thinking", "began": "beginning", "held": "holding",
    "kept": "keeping", "split": "splitting", "understood": "understanding",
}


def _progressive_form(verb):
    """Present participle of a past-tense verb, or None if not derivable.

    Two mechanical rules cover every verb observed in real guard rejections
    (27 distinct verbs, 91 occurrences, 100% coverage):

      -ied  ->  -ying     verified -> verifying
      -ed   ->  -ing      checked  -> checking

    Dropping the final two characters rather than just the "d" is what makes
    the second rule work across English spelling changes without special
    cases: staged -> stag + ing -> "staging" (silent-e elision), and
    committed -> committ + ing -> "committing" (doubled consonant kept).
    """
    v = (verb or "").lower()
    if v in _IRREGULAR_PROGRESSIVE:
        return _IRREGULAR_PROGRESSIVE[v]
    if v.endswith("ied") and len(v) > 4:
        return v[:-3] + "ying"
    if v.endswith("ed") and len(v) > 3:
        return v[:-2] + "ing"
    return None


def _repair_verbs_after_and(words):
    """Convert past-tense verbs in "and <verb>" position to progressive.

    Scoped to the position directly after "and" on purpose. A past participle
    anywhere else is usually an adjective -- "polluted" in "I'm finding
    polluted tests" modifies the noun and must not be touched.

    Returns (words, changed).
    """
    out = list(words)
    changed = False
    for i in range(1, len(out)):
        if out[i - 1].rstrip(".,!?:;").lower() != "and":
            continue
        core = out[i].rstrip(".,!?:;")
        trailing = out[i][len(core):]
        if not _is_past_tense_verb(core):
            continue
        progressive = _progressive_form(core)
        if progressive:
            out[i] = progressive + trailing
            changed = True
    return out, changed


def repair_tense(phrase):
    """Rewrite a past-tense claim as present progressive, or None if unchanged.

    Repairs both the leading verb and any verb in "and <verb>" position.
    Fixing only the leading one produced broken grammar on compound
    sentences -- "I'm checking tests and updated log format" -- in 3 of 21
    real repaired phrases.
    """
    if not phrase:
        return None
    lead = to_present_progressive(phrase)
    words = (lead or phrase).split()
    words, tail_changed = _repair_verbs_after_and(words)
    if lead is None and not tail_changed:
        return None
    return " ".join(words)


def to_present_progressive(phrase):
    """Rewrite "I checked X" / "I've checked X" as "I'm checking X".

    Returns None when the phrase is not a repairable past-tense claim, so
    callers can fall back to rejecting it.

    Exists because rejecting past-tense PreToolUse phrases converted the
    defect into silence -- 77% of generations were dropped, most of them
    commentary the developer would otherwise have heard. Repairing the tense
    keeps the information and drops the false claim, the same way
    _ensure_first_person repairs a missing subject instead of discarding.
    """
    if not looks_past_tense(phrase):
        return None
    words = phrase.split()
    if len(words) < 3:
        return None
    core = words[1].rstrip(".,!?:;")
    trailing = words[1][len(core):]
    progressive = _progressive_form(core)
    if not progressive:
        return None
    return " ".join(["I'm", progressive + trailing] + words[2:])


_DUPE_STOPWORDS = {
    "i", "i'm", "im", "a", "an", "the", "to", "for", "of", "in", "on",
    "and", "is", "it", "my", "me", "at", "with", "into", "still",
}


def _content_tokens(phrase):
    """Lowercase word tokens, minus stopwords that carry no topic signal."""
    words = re.findall(r"[a-z']+", (phrase or "").lower())
    return {w for w in words if w not in _DUPE_STOPWORDS}


def is_near_duplicate(phrase, recent, threshold=0.6, decay_seconds=None, now=None):
    """Is this phrase a near-repeat of anything recently spoken?

    Jaccard overlap on content tokens -- the same measure used to find the
    19.3% near-duplicate rate in the logs, so the guard and the metric
    agree by construction.

    `recent` entries may be `(phrase, ts)` pairs (the current session_state
    shape) or bare strings (the legacy shape, and what
    scripts/log_analyse.py still passes for its duplicate-rate metric --
    that call site must keep working unmodified). When `decay_seconds` is
    given, a pair older than that is skipped -- repetition only grates
    back-to-back. A bare string, or a pair with ts=0.0, has no known age:
    per the "unknown means unknown" convention documented on
    seconds_since_last_voiced(), that stays conservative and is never
    treated as stale.
    """
    tokens = _content_tokens(phrase)
    if not tokens:
        return False
    now = time.time() if now is None else now
    for prior in recent or ():
        if isinstance(prior, (list, tuple)):
            prior_phrase, prior_ts = prior[0], prior[1]
        else:
            prior_phrase, prior_ts = prior, None
        if decay_seconds is not None and prior_ts and (now - prior_ts) > decay_seconds:
            continue
        prior_tokens = _content_tokens(prior_phrase)
        if not prior_tokens:
            continue
        overlap = len(tokens & prior_tokens) / max(len(tokens), len(prior_tokens))
        if overlap >= threshold:
            return True
    return False


# Latinate verbs the model reaches for, mapped to the plainer word. Derived
# from measured usage: of 25 slop hits in 300 generations, "completed" was
# 13 and "verified" 6, so this short list covers most of the register gap.
# No bare "complete": unlike the other infinitives here, "complete" is
# routinely an adjective ("the task is complete") rather than a verb, and
# swapping it unconditionally produces "the task is finish" -- caught by
# pre-existing tests, not something the measured slop data asked for
# (the measured hit was specifically "completed", the unambiguous verb).
_PLAIN_VERBS = {
    "completed": "finished",
    "verified": "checked",
    "verify": "check",
    "implemented": "built",
    "implement": "build",
    "utilized": "used",
    "utilize": "use",
    "encountered": "hit",
    "modified": "changed",
    "initiated": "started",
    "obtained": "got",
}


def _plain_verbs(phrase):
    """Swap Latinate verbs for plainer ones, preserving capitalization."""
    def sub(m):
        word = m.group(0)
        plain = _PLAIN_VERBS.get(word.lower())
        if not plain:
            return word
        return plain.capitalize() if word[0].isupper() else plain

    return re.sub(r"\b[A-Za-z]+\b", sub, phrase)


def _clean_phrase(raw):
    """Strip quotes, markdown, think tags, and take just the first sentence.

    An over-budget result is not simply discarded (see MAX_WORDS below): if
    dropping a short throwaway lead-in ("Ok.", "Sure thing.") the model
    tacked on before the real sentence brings the rest within budget, that
    shorter-but-complete sentence is used instead. Mirrors _repair_phrase's
    philosophy -- fix what is mechanically fixable so the guard rejects
    less -- but never hard-truncates: a sentence cut mid-clause read aloud
    is worse than silence, so salvage only ever keeps a complete sentence.
    """
    if not raw:
        return None
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    raw = raw.strip().strip('"\'').strip("*").strip()
    raw = re.sub(r'\*+', '', raw)
    raw = re.sub(r'`+', '', raw)
    raw = re.sub(r'^(?:done|broken|question|confirmed|complex)\s*\|\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'^I\s+skip[^|]*\|\s*', '', raw, flags=re.IGNORECASE)

    from engines.base import tts_normalize

    # First sentence at a boundary (>=3 words). lead_in_end remembers where
    # any short *leading* sentence(s) ended (the ones skipped here for
    # having <3 words, e.g. "Ok." or "Sure thing."), so an over-budget
    # candidate below can try dropping just that lead-in instead of being
    # discarded whole.
    padded = raw + " "
    candidate = None
    lead_in_end = 0
    for m in re.finditer(r'[.!?]\s', padded):
        candidate = padded[:m.start() + 1]
        if len(candidate.split()) >= 3:
            break
        lead_in_end = m.start() + 1
    if candidate:
        raw = candidate

    # Speakability: the LLM path never had this applied, which is why
    # identifiers and ticket IDs reached TTS. The word gate below reads the
    # count both before and after, since de-camelling changes it.
    before = raw
    raw = tts_normalize(raw)
    raw = _plain_verbs(raw)
    if raw != before:
        # Normalization is otherwise invisible after the fact, so a mangled
        # de-camelling ("GitHub" -> "Git Hub") looks like a model error.
        log_record.write(f"norm: {before!r} -> {raw!r}")

    words = raw.split()

    # A trailing single-character "word" (other than "a"/"I") is what a
    # token-cutoff mid-word looks like -- "...investigating l" left over
    # from "investigating locally". Word count alone would let this
    # through (it can easily land inside the budget), so this needs its
    # own guard rather than folding into the MAX_WORDS check below.
    #
    # Only when the phrase does not end in terminal punctuation, though: a
    # cut-off sentence never reached its full stop, while "I'm implementing
    # Phase 1." and "I reviewed the spec for Task 2." are perfectly good
    # speech that happen to end on a bare digit. Checked against every
    # phrase hobson has actually spoken: without this condition the guard
    # silences 75 real utterances to catch 2 defects; with it, 0 and 2.
    if words and not raw.rstrip().endswith((".", "!", "?")):
        last = words[-1].strip(".,!?;:")
        if len(last) == 1 and last.lower() not in ("a", "i"):
            log_record.write(f"truncated: trailing fragment {words[-1]!r} in {raw!r}")
            return None

    n_written = len(before.split())
    if min(len(words), n_written) > MAX_WORDS:
        salvaged = None
        rwords = None
        if lead_in_end and candidate:
            written = candidate[lead_in_end:].strip()
            if written:
                remainder = _plain_verbs(tts_normalize(written))
                rwords = remainder.split()
                if _fits_budget(len(written.split()), len(rwords)):
                    salvaged = remainder
        if salvaged is None:
            log_record.write(f"over budget: {len(words)} words - {raw!r}")
            return None
        log_record.write(f"salvage: dropped lead-in, over budget ({len(words)} words) -> {salvaged!r}")
        raw, words = salvaged, rwords
    elif not _fits_budget(n_written, len(words)):
        return None

    raw = _ensure_first_person(raw.strip())
    return raw if raw else None


def _parse_response(text):
    """Parse 'category | phrase' or 'SKIP' from Ollama output."""
    if not text:
        return None

    stripped = text.strip()
    if stripped.upper() == "SKIP":
        return None

    if "|" in stripped:
        parts = stripped.split("|", 1)
        category = parts[0].strip().lower().rstrip(".,!?")
        if "skip" in category:
            return None
        phrase = _clean_phrase(parts[1])
        if not phrase:
            return None
        # An unrecognised category still must not reach TTS — speak only the
        # right-hand side and default the category.
        if category not in VALID_CATEGORIES:
            log_record.write(f"guard: invalid category {category!r}, defaulting to 'done'")
            return "done", phrase
        return category, phrase

    phrase = _clean_phrase(stripped)
    if phrase:
        return "done", phrase

    return None


# --------------- unified generation ---------------

# Events that fire *before* the thing they describe happens, so a completed
# claim is false at the moment it is spoken. PermissionRequest belongs here
# for the same reason PreToolUse does: the permission has not been granted
# yet. Stop is deliberately absent -- there the work really is done.
_PRE_ACTION_EVENTS = ("PreToolUse", "PermissionRequest")


def _repair_phrase(event_type, phrase):
    """Fix what is mechanically fixable, so the guard rejects less.

    Repair runs before the reject check: a past-tense pre-action phrase
    carries the right information with the wrong tense, and silence loses
    both. Returns (phrase, repair_note_or_None).
    """
    if event_type in _PRE_ACTION_EVENTS:
        repaired = repair_tense(phrase)
        if repaired:
            return repaired, f"tense repaired from {phrase!r}"
    return phrase, None


# How long a spoken phrase suppresses near-repeats of itself. Repetition
# grates when it is back-to-back -- "I'm navigating to Account Settings" three times
# in a row -- not when the same phrase recurs minutes later. Past this window
# the duplicate check is skipped, which recovers speech that would otherwise
# be silence.
DUPE_DECAY_SECONDS = 120

# Speak an otherwise-rejected phrase when the decider puts the probability
# of it being a restatement below this -- i.e. when it is more likely new
# than repeated. Measured over 13 real logged rejections: 0.3 misses a
# genuine completion sitting at 0.44, 0.7 starts releasing true repeats at
# 0.65, and 0.5 catches both completions while silencing every restatement.
DEDUP_RESTATES_MAX = 0.5

# Block a commentary phrase the local guard let through when the decider
# puts P(restates) at or above this. Scored over 148 real spoken phrases
# that had another said in the previous two minutes: every commentary
# phrase at 0.75 or above was a genuine repeat, the next one down (0.56) an
# arguable re-run -- 0.7 sits in that gap, and blocked 5 of 89, all right.
DEDUP_BLOCK_MIN = 0.7

# How many prior phrases to show the decider. Matches MAX_RECENT_VOICED --
# the same window the local guard compares against, so both see one story.
MAX_PRIOR_CONTEXT = 6


def _decider_rescues_duplicate(phrase, recent_voiced, config=None):
    """Does the decider think this says something the recent phrases did not?

    Jaccard overlap is blind to tense, so the completion of a thing you just
    announced starting -- "I fixed the invoice sync" against "I'm fixing the
    invoice sync" -- scores as a near-duplicate and is silenced. That is
    exactly the utterance worth hearing, and no threshold fixes it: the two
    phrases genuinely share almost every content token.

    Measured on the three real shapes from the log: the tense transition
    comes back "progress" at 0.97 (confidence 0.96), a verbatim repeat
    "restates" at 1.0 (confidence 1.0), and unrelated work "unrelated" at
    0.95 (confidence 0.92).

    Consulted ONLY when the local guard has already decided to suppress,
    and able only to overturn that -- never to cause one. So this is
    strictly additive: with the backend left at "local", no key, a timeout
    or any other failure, behaviour is exactly what it was. It also bounds
    the call volume to the rejection count (766 across the whole log)
    rather than to the event count (28,000+).

    Decided on P(restates) directly rather than on the argmax plus a
    confidence, because the calibrated probability is the thing Jev exists
    to give and the argmax throws it away. Measured on 13 real logged
    rejections: "Review of sealed args in settings navigator finished" comes
    back argmax=progress but confidence 0.35, so an argmax rule silences a
    genuine completion, while its P(restates) of 0.44 reads correctly as
    "more likely new than not".

    Returns True only when the candidate is more likely new than a repeat.
    No opinion, a missing probability, or any failure returns False and the
    rejection stands.
    """
    priors = [p[0] if isinstance(p, (list, tuple)) else p
              for p in (recent_voiced or ())]
    config = config if config is not None else home.load_config()
    p_restates = _p_restates(phrase, priors, config)
    # No opinion means we learned nothing -- keep the rejection, which
    # preserves today's behaviour rather than releasing speech on a
    # response we did not understand.
    if p_restates is None:
        return False
    restates_max = (config.get("decider") or {}).get(
        "dedup_restates_max", DEDUP_RESTATES_MAX)
    return p_restates < restates_max


def _p_restates(phrase, priors, config):
    """P(the candidate restates something in `priors`), per the decider, or
    None for no opinion (backend local, no priors, any failure, or a
    response without the probability). One question, shared by the rescue
    and the block, so both are judged on the same scale.

    Asked as a `choice` rather than a yes/no, because the distinction that
    matters is three-way -- a restatement, new progress on the same work, or
    different work entirely -- and only choice/score carry a confidence.
    """
    priors = [p for p in (priors or ()) if p]
    if not priors:
        return None
    try:
        import decider
    except ImportError:
        return None
    # Phrases retell the session, so each goes as a decider.Phrase: redacted.
    state = ["Announcements already spoken, most recent last:\n"]
    for prior in priors[-MAX_PRIOR_CONTEXT:]:
        state += ["- ", decider.Phrase(prior), "\n"]
    state += ["\nCandidate announcement:\n- ", decider.Phrase(phrase)]
    return decider.probability(
        "restates",
        state,
        ("Decide how the candidate announcement relates to what was already "
         "spoken, for a voice assistant deciding whether saying it aloud "
         "would be repetitive."),
        {"restates": "says the same thing as an earlier announcement, "
                     "adding nothing new",
         "progress": "reports a new development on work already mentioned, "
                     "such as finishing what was started",
         "unrelated": "is about different work entirely"},
        config=config,
    )


# Anything that could be a failure (stop_outcome.FAILURE_ALTS), and the
# model's usual ways of inventing trouble for commentary.
_TROUBLE_WORDS = re.compile(
    rf"\b(?:{_FAILURE_ALTS}|breaking|issues?|trouble|struggl\w*|problems?|conflicts?|bugs?"
    r"|wrong|loop)\b", re.IGNORECASE)
# Code that is about failure, not a claim that something failed.
_ABOUT_FAILURE = re.compile(
    r"\berror (?:handling|messages?|states?|cases?|codes?|paths?|screens?|views?|types?|copy)\b",
    re.IGNORECASE)

# Words that say nothing about what the work is.
_FUNCTION_WORDS = frozenset("""
    i im i'm i've ive the a an and or of to in on for with from into at by is are was be
    it its this that these those my our your now next then just also still again more
    some all any new up out over about as via so not no it's what how which who need needs
""".split())

_FILE_EXTENSION = re.compile(
    r"\.(?:kt|kts|py|md|html|json|jsonl|xml|ts|tsx|js|swift|sh|yaml|yml|txt|png|csv)\b",
    re.IGNORECASE)


def _content_stems(text):
    """A text's content words, cut to four letters so that "committing"
    meets "commit", and InvoiceService.kt meets "the invoice service"."""
    text = _FILE_EXTENSION.sub(" ", re.sub(r"([a-z])([A-Z])", r"\1 \2", text or ""))
    return {w[:4] for w in re.split(r"[^a-z0-9]+", text.lower())
            if len(w) >= 3 and not w.isdigit() and w not in _FUNCTION_WORDS}


def _off_batch_reason(phrase, event_detail):
    """Why a commentary phrase is not about the batch it was spoken for, or
    None.

    The model reworded something said a minute ago ("I'm committing the
    updated CLAUDE.md" for a batch that built a labelling set), spoke the
    branch name as the action ("I'm widening the search filters" for a
    `cd`), or invented trouble for work that has not run yet ("I'm hitting a
    merge conflict" for an `ls`). Blind-labelled, 130 of 399 spoken
    commentary phrases did not describe their batch. Sharing no content word
    with it caught 64 of them and 4 good phrases; claiming a failure it does
    not mention caught 48 more that did share one, for 3. Together, 92 of
    130, and 6 good phrases lost: 96% and 91% precision on the two halves.
    """
    if not _content_stems(phrase) & _content_stems(event_detail):
        return "not about this batch"
    # "webcam_stuck_capture.md" shows the trouble it is about.
    if _TROUBLE_WORDS.search(_ABOUT_FAILURE.sub(" ", phrase)) and not _TROUBLE_WORDS.search(
            re.sub(r"[_\-/.]", " ", event_detail)):
        return "a failure the batch does not show"
    return None


def _guard_reject_reason(event_type, phrase, recent_voiced,
                         seconds_since_last_voiced=None, event_detail=None):
    """Why this phrase must not be spoken, or None if it is fine.

    seconds_since_last_voiced is advisory only -- it is time since *any*
    phrase was voiced, not since the matching one, so it can no longer gate
    the whole duplicate check (that used to make `stale` nearly always
    false in a busy session, since something is voiced every few tens of
    seconds). Decay is now per-match, inside is_near_duplicate, keyed off
    each recent entry's own timestamp. The parameter stays in the signature
    because three call sites pass it and removing it is a separate change.

    event_detail is what the model was asked about; commentary must be
    about it (_off_batch_reason). Checked before the duplicate guard, which
    may ask the decider and costs a network call.
    """
    if event_type in _PRE_ACTION_EVENTS and looks_past_tense(phrase):
        return "past tense on a not-yet-run action"
    # Commentary says what the agent is about to do; it has nothing to ask.
    # 24 of 1,574 spoken commentary phrases in the log were questions ("Is
    # the weekend really coming?", "Should the build quest guide be
    # shown?"), and on a replay of 80 real batches the Task line nudged the
    # rate from about 12 to 18 in 160. Not PermissionRequest: "Should I push
    # the branch?" is a fair way to ask for approval.
    if event_type == "PreToolUse" and phrase.rstrip().endswith("?"):
        return "a question, for an action about to run"
    if event_type == "PreToolUse" and event_detail:
        reason = _off_batch_reason(phrase, event_detail)
        if reason:
            return reason
    if is_near_duplicate(phrase, recent_voiced, decay_seconds=DUPE_DECAY_SECONDS):
        if _decider_rescues_duplicate(phrase, recent_voiced):
            log_record.write(f"decider: overturned near-duplicate -> {phrase!r}")
            return None
        return "near-duplicate of a recent phrase"
    if event_type == "PreToolUse" and _decider_blocks_restatement(phrase, recent_voiced):
        return "restates a recent phrase (decider)"
    return None


def _decider_blocks_restatement(phrase, recent_voiced, config=None):
    """Does the decider think this commentary rewords something just said?

    The other half of the rescue. Jaccard misses paraphrase -- "I'm creating
    run dirs." after "I'm creating run dirs and froze grading criteria."
    shares too few tokens to trip it -- so a reworded repeat reached the
    speaker, and the decider, consulted only on rejections, never saw it.

    Commentary only. A Stop is also the signal that the turn ended, which
    is news whatever its wording, and in the calibration one of its two
    scores >= 0.8 was a completion ("I confirmed ..." after "I'm confirming
    ..."). A permission or notification is a new thing for you to act on.

    Compared only against phrases inside DUPE_DECAY_SECONDS whose age is
    known: this silences, so an entry of unknown age is not held against a
    phrase. With nothing recent, no question is asked, which is most of the
    time. No opinion or any failure speaks.
    """
    now = time.time()
    fresh = [p[0] for p in (recent_voiced or ())
             if isinstance(p, (list, tuple)) and len(p) > 1 and p[1]
             and 0 <= now - p[1] <= DUPE_DECAY_SECONDS]
    if not fresh:
        return False
    config = config if config is not None else home.load_config()
    p_restates = _p_restates(phrase, fresh, config)
    if p_restates is None:
        return False
    block_min = (config.get("decider") or {}).get("dedup_block_min", DEDUP_BLOCK_MIN)
    if p_restates < block_min:
        return False
    log_record.write(f"decider: blocked a restatement (P={p_restates:.2f}) -> {phrase!r}")
    return True


# Stop only. Measured against 101 hand-labelled real Stops (52 done, 49
# waiting on the developer, none broken): at 0.7 the model called 10 of 202
# generations "broken", at 0.3 it called 5, and category accuracy went from
# 144 to 150 of 202. Commentary stays at 0.7: it needs variety to get past
# the near-duplicate guard.
STOP_TEMPERATURE = 0.3


def _words_over_budget(raw):
    """The phrase's word count when a reply failed only on length, else None.

    A Stop's richer context makes for longer sentences: 32 of 33 silent
    replayed Stops had been dropped for running past MAX_WORDS.
    """
    if not raw or "|" not in raw:
        return None
    n = len(raw.split("|", 1)[1].split())
    return n if n > MAX_WORDS else None


def _retry_turn(raw, note):
    """Messages that show the model its rejected reply and why. At a low
    temperature, asking the same thing again returns the same sentence."""
    return [{"role": "assistant", "content": (raw or "").strip()},
            {"role": "user", "content": note}]


def generate_or_skip(event_type, event_detail, session_context, project=None,
                     model=None, timeout=8, ollama_url=None, custom_prompt=None,
                     verbosity="terse", recent_voiced=None,
                     seconds_since_last_voiced=None, stop=None):
    """Single Ollama call: decide voice-or-skip AND generate the phrase.

    Returns (category, phrase); (category, None) when the event was
    classified but every phrase for it was rejected by a guard -- the
    classification is still true, and the nudge reads it for Stop; or
    (None, None) for SKIP or failure.

    verbosity controls the prompt bias (see _build_messages). Only set to
    "normal" for PreToolUse commentary when commentary.verbosity is "normal",
    or to "decided" once the decider has chosen to speak a batch;
    Stop/Permission/Notification should keep the default "terse" prompt.

    Rejection policy is split by event: PreToolUse rejects to silence
    (commentary is already routinely skipped and the hot path must stay
    fast), Stop regenerates once then falls silent (silence there defeats
    the notification's purpose).

    `stop` is the Stop's stop_outcome.StopReading (event_detail is its
    context), None for every other event. Its rules decide the category from
    the model's verdict (StopReading.settle): waiting on the developer is a
    question, SKIP and failure included; a SKIP otherwise is "done"; a
    "broken" whose last message names no failure is asked again, then
    "done". A failed call stays unknown.
    """
    global last_raw

    import time as _time

    attempts = 2 if event_type == "Stop" else 1
    rejected_category = None
    awaiting_answer = stop.awaiting_answer if stop is not None else None
    temperature = STOP_TEMPERATURE if event_type == "Stop" else 0.7
    retry = []  # the rejected reply and why, so the second try is not the first again

    for attempt in range(attempts):
        messages = _build_messages(
            event_type, event_detail, session_context, project, custom_prompt,
            verbosity=verbosity, awaiting_answer=awaiting_answer,
        ) + retry
        started = _time.monotonic()
        # 25 was too tight: a full MAX_WORDS=12 phrase plus the "category | "
        # prefix (~3 tokens) needs ~20+ tokens on its own at ~1.3-1.5
        # tokens/word for ordinary English, and technical terms (identifiers,
        # file names) run higher -- so 25 cut the model off mid-word before
        # it could finish, and the truncated remainder then failed the word
        # gate below. 32 gives a full phrase comfortable headroom (roughly
        # the middle of the doc's suggested 30-40 range) without opening the
        # door to rambling on this latency-sensitive hook path.
        accumulated, err = _chat(messages, model, timeout, ollama_url, num_predict=32,
                                 temperature=temperature)
        elapsed = _time.monotonic() - started

        # One diagnostic line per Ollama call. Carries the three things that
        # were previously unknowable after the fact: how long generation took,
        # what the model actually emitted, and (below) what happened to it on
        # the way to the speaker.
        trace = log_record.trace(event_type, elapsed, attempt + 1, attempts,
                                 model or DEFAULT_OLLAMA_MODEL,
                                 _truncate_detail(event_detail), accumulated or "")

        if err and not accumulated:
            last_raw = err
            log_record.write(f"{trace} FAILED {err}")
            return (stop.settle(None, failed=True)[0] if stop is not None else None), None

        last_raw = accumulated
        result = _parse_response(accumulated or "")
        if not result:
            too_long = _words_over_budget(accumulated)
            if too_long and attempt + 1 < attempts:
                log_record.write(f"{trace} -> over budget, retrying")
                retry = _retry_turn(accumulated, f"That is {too_long} words. Say it in "
                                                 f"at most {MAX_WORDS} words.")
                continue
            log_record.write(f"{trace} -> SKIP")
            return (stop.settle(None)[0] if stop is not None else None), None

        category, phrase = result
        steps = []
        if stop is not None:
            if stop.doubts(category) and attempt + 1 < attempts:
                log_record.write(f"{trace} -> broken, but nothing in the last message failed; retrying")
                retry = _retry_turn(accumulated, "Nothing in the Last message failed, so it "
                                                 "is not broken. Say what was done.")
                continue
            category, note = stop.settle(category)
            if note:
                steps.append(note)
        parsed = phrase
        phrase, repair_note = _repair_phrase(event_type, phrase)

        if parsed != phrase:
            steps.append(f"repaired={phrase!r}")
        if repair_note:
            steps.append("(tense)")

        reason = _guard_reject_reason(event_type, phrase, recent_voiced,
                                      seconds_since_last_voiced, event_detail=event_detail)
        if reason is None:
            log_record.write(f"{trace} {log_record.verdict(category, steps, phrase)}")
            return category, phrase

        log_record.write(f"{trace} {log_record.verdict(category, steps, phrase, reason)}")
        last_raw = f"(rejected: {reason})"
        rejected_category = category
        retry = _retry_turn(accumulated, f"Not that: it is a {reason}. Say something else.")

    return rejected_category, None


# CLI testing
if __name__ == "__main__":
    import sys
    import time
    text = sys.stdin.read().strip() if not sys.stdin.isatty() else "I fixed the bug and all tests pass now."
    t0 = time.monotonic()
    cat, phrase = generate_or_skip("Stop", text, "No prior context.", "hobson")
    elapsed = time.monotonic() - t0
    print(f"category: {cat}")
    print(f"phrase:   {phrase}")
    print(f"raw:      {last_raw!r}")
    print(f"time:     {elapsed:.1f}s")
