"""stop_outcome.py — how a turn ended, read once from its transcript.

A Stop is the agent handing the turn back. What it means for the developer
is decided here, for every engine: whether the agent is waiting on you, is
waiting on its own work (subagents, a build), or has finished -- and, once a
model has given its verdict, which category the turn gets.

    reading = read(hook_input)        # None: no transcript, or nothing in it
    reading.context                   # what a model is shown
    reading.settle(model_category)    # the rules that override the model

The static engines ask `classify` for a verdict and speak a template; the
realtime engines ask phrase_gen, which both judges and phrases. Both feed
the same reading, so the detectors see the same text whichever engine runs.
"""

import glob
import json
import os
import re
from typing import NamedTuple, Optional
from urllib.request import urlopen
from urllib.error import URLError

import log_record
from ollama_client import DEFAULT_OLLAMA_MODEL, build_request


# ── Reading the transcript ──────────────────────────────────────────────

def find_transcript(hook_input):
    # Claude Code passes the transcript path directly on most events — use it
    # and skip the filesystem walk entirely (this runs on every Stop event).
    path = hook_input.get("transcript_path")
    if path:
        path = os.path.expanduser(path)
        if os.path.isfile(path):
            return path

    # Fallback: transcripts live one level deep as
    # ~/.claude/projects/<project-dir>/<session_id>.jsonl, so a single-level
    # glob is enough — avoid the full recursive tree walk on the hot path.
    base = os.path.expanduser("~/.claude/projects")
    for key in ("session_id", "sessionId"):
        sid = hook_input.get(key)
        if sid:
            matches = glob.glob(f"{base}/*/{sid}.jsonl")
            if matches:
                return matches[0]

    all_files = glob.glob(f"{base}/*/*.jsonl")
    return max(all_files, key=os.path.getmtime) if all_files else None


# Pasted blocks are text the user carried in, not their words; a
# system-reminder is the harness talking. Either can run to thousands of
# characters and push the actual request out of a 300-character turn.
_INJECTED_BLOCK = re.compile(
    r"<(pasted_content|system-reminder)\b[^>]*>.*?</\1>", re.DOTALL)


def _user_prompt_text(entry):
    """The text the user typed, or None if this entry is not a prompt.

    A transcript's "user" entries are mostly not the user. Tool results
    are one kind; the rest are what the harness injects: a skill's body, a
    subagent's hand-back, a caveat (all marked isMeta), a compaction
    summary, a background task's <task-notification>, a slash command's or
    `!` command's echo (<command-name>, <local-command-stdout>,
    <bash-input>), and the interruption marker. Counting those as turns is
    how a Stop that finished the Jev commentary gate came out as "I finished
    grabbing attention in hobson": the grab-attention skill's body had
    taken one of its four turns.

    Recent Claude Code marks what was typed with origin {"kind": "human"};
    older transcripts have no origin, so the text checks stay.
    """
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return None
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        content = next((b.get("text") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), None)
    if not isinstance(content, str):
        return None
    text = _INJECTED_BLOCK.sub(
        lambda m: "[pasted text]" if m.group(1) == "pasted_content" else "", content)
    text = " ".join(text.split())
    # Every harness echo opens with a tag; nobody types one first.
    if not text or text.startswith(("<", "[Request interrupted")):
        return None
    return text


_FENCED_CODE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://[^\s)\]]*[^\s.,;:!?)\]]")
_TRAILING_SOURCES = re.compile(
    r"\s*(?:\*\*)?(?:Sources|References)(?:\*\*)?:(?:\*\*)?\s*(?:-\s*\S.*)?$",
    re.IGNORECASE | re.DOTALL)


def _prose(text):
    """An agent message as prose: no code blocks, link targets, bare URLs or
    trailing source list. They are useless to a spoken summary and ate the
    300 characters meant for the message's ending -- one real Stop's
    ending was its "Sources:" list of three URLs."""
    text = _FENCED_CODE.sub(" [code] ", text or "")
    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("a link", text)
    text = _TRAILING_SOURCES.sub("", text)
    return " ".join(text.replace("**", "").split())


def _assistant_text(entry):
    """The last text block of an assistant entry, as prose, or None."""
    if entry.get("type") != "assistant":
        return None
    text = None
    for block in entry.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
    return _prose(text) or None


def condense_turn(text, limit=300):
    """Shorten a turn to `limit` characters, keeping its ending.

    The old cut, snippet(text)[:300], kept the opening and dropped the
    rest, which is backwards for a Stop: the verdict ("tests pass", "should
    I ...?") is at the end of the final message. The ending gets 60% of the
    budget, the opening what is left.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    sep = " [...] "
    tail_budget = int(limit * 0.6)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    tail = []
    for s in reversed(sentences):
        if len(" ".join([s] + tail)) > tail_budget:
            break
        tail.insert(0, s)
    tail_text = " ".join(tail) if tail else "..." + text[-(tail_budget - 3):]
    head_budget = limit - len(tail_text) - len(sep)
    head = text[:head_budget].rsplit(" ", 1)[0] if head_budget > 0 else ""
    return f"{head}{sep}{tail_text}" if head else tail_text[-limit:]


def _turns(path):
    """Every (role, text) turn in a transcript: user prompts and agent
    prose. Only what the user typed counts as a user turn (see
    _user_prompt_text)."""
    turns = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            try:
                text = _user_prompt_text(entry)
                if text:
                    turns.append(("User", text))
                    continue
                text = _assistant_text(entry)
                if text:
                    turns.append(("Agent", text))
            except (AttributeError, TypeError):
                continue
    return turns


_LAST_MESSAGE = "Last message:"
EARLIER_TURN_CHARS = 200
LAST_MESSAGE_CHARS = 500


def _format(recent):
    """The model's context for these turns, its last line, and the agent's
    last message as data ("" when the last turn is not the agent's)."""
    if not recent or recent[-1][0] != "Agent":
        lines = [f"{role}: {condense_turn(text)}" for role, text in recent]
        return "\n".join(lines), (lines[-1] if lines else ""), ""
    # The final message is what the Stop is about; the turns before it are
    # background. Run together as four equal turns, the model reported an
    # earlier turn's work as this one's.
    earlier = " | ".join(f"{role}: {condense_turn(text, EARLIER_TURN_CHARS)}"
                         for role, text in recent[:-1])
    message = condense_turn(recent[-1][1], LAST_MESSAGE_CHARS)
    last = f"{_LAST_MESSAGE} {message}"
    return (f"Earlier: {earlier}\n{last}" if earlier else last), last, message.strip()


def last_transcript_context(path, max_turns=4):
    """Return the last few turns of the conversation as a context string.

    Up to max_turns of user prompts and assistant responses, so short final
    messages like 'isDraft: false' have surrounding context.
    """
    return _format(_turns(path)[-max_turns:])[0]


# Short replies ("yes", "go ahead", "ah?") answer the agent; they say
# nothing about the work, so the request is the last prompt longer than this.
_MIN_REQUEST_WORDS = 3

# How far back to look for the last request. A prompt followed by a long
# autonomous run can sit megabytes back in a transcript (they reach 14MB);
# past this, no request is better than a slow hook.
_REQUEST_TAIL_BYTES = 2 * 1024 * 1024


def last_user_request(path, limit=200):
    """The user's most recent request, condensed, or None.

    Reads only the transcript's tail. This is what the work is *for*: a
    commentary batch otherwise knows only "3 Bash (Push and confirm)", and
    a Stop's last four turns are often all the agent's own narration.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _REQUEST_TAIL_BYTES))
            if size > _REQUEST_TAIL_BYTES:
                f.readline()  # drop the partial line the seek landed in
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
            text = _user_prompt_text(entry) if isinstance(entry, dict) else None
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
        if text and len(text.split()) >= _MIN_REQUEST_WORDS:
            return condense_turn(text, limit)
    return None


def request_for_prompt(hook_input, event_detail=None):
    """The Task line for a Stop's prompt, or None.

    Stop only: on commentary it made the model speak the request as the
    action (see phrase_gen._TASK_RULE for the replay).

    Only this hook's own transcript_path: find_transcript's last resort is
    the newest transcript of *any* project, which would put another tab's
    request into this project's voice. None when event_detail already holds
    the request, as a Stop's last turns often do -- both are condensed from
    the same start, so a shared opening is the same prompt.
    """
    path = hook_input.get("transcript_path")
    request = last_user_request(os.path.expanduser(path)) if path else None
    if request and event_detail and request[:60] in event_detail:
        return None
    return request


# ── Detectors: read from the last message's closing sentences ───────────

# Waiting on the developer without a question mark: "Waiting for your
# go-ahead", "Tell me the two codes and I will ...", "Close it without
# annotations to approve it". An offer after finished work ("say the word",
# "tell me if you want that") is not waiting, and is left out on purpose.
_WAITING_ON_YOU = re.compile(
    r"\b(?:waiting (?:for|on) (?:your|you)\b|your (?:go-ahead|call|pick|answer|decision)\b"
    r"|(?<!without )your approval|should i\b|shall i\b"
    r"|tell me (?:which|what|the|whether)\b|let me know (?:which|what|whether)\b"
    r"|suggested reply|reply with\b|to approve\b|approve it\b"
    r"|say \W{0,2}(?:go|push|yes)\b|go ahead and\b|when you'?re ready\b"
    r"|you (?:must|need to|have to|'ll need to) (?:decide|choose|pick|confirm|approve|run|plug)"
    r"|if you don'?t answer|annotate (?:what|anything|it)\b"
    r"|recommendation\W{0,4}[A-D]\b)",
    re.IGNORECASE)

# An offer after finished work -- "Want me to draft the Slack reply?" --
# is not waiting. Counted as waiting, it started a nudge for work you
# never asked for: on 100 held-out Stops it took the detector from 11 of
# 14 flags right to 15 of 24.
_OFFER = re.compile(
    r"^\W*(?:do you )?(?:want|would you like) (?:me to|the|a|an|it)\b|^\W*should i also\b",
    re.IGNORECASE)

# How many closing sentences to read. The ask is often followed by a remark
# or two ("Want me to apply it? And the OBS stream is still worth keeping.
# I should have measured first.").
_WAITING_TAIL_SENTENCES = 4


def _closing_sentences(text):
    """A message's last few sentences, as prose."""
    text = _prose(text).rstrip("*_`")
    # condense_turn's " [...] " is a boundary too: it joins a turn's opening
    # to its ending, and hid that "Want the two drafted?" opened a sentence.
    return [s.strip().rstrip("*_`") for s in
            re.split(r"(?<=[.!?])\s+|\s\[\.\.\.\]\s", text)[-_WAITING_TAIL_SENTENCES:]]


def awaits_developer(text):
    """Did the agent stop waiting on the developer -- a question, a choice,
    an approval? Read from the message's last few sentences.

    The model does not see this: of 23 real Stops ending in a question to
    the developer ("A?", "Go?", "Should I fix that, and push these six
    commits?"), it labelled 45 of 46 generations "done", which also told the
    nudge the turn had finished. Hand-labelled, 201 real Stops (101 tuned
    on, 100 held out): 49 of 52 flags right, 49 of 77 waits caught, where
    the model caught about 5 in 100.
    """
    return _asks_developer(_closing_sentences(text))


def _asks_developer(sentences):
    for sentence in sentences:
        if _OFFER.search(sentence):
            continue
        if sentence.endswith("?") or _WAITING_ON_YOU.search(sentence):
            return True
    return False


# Work the agent started and is waiting on: its subagents, reviewers, builds,
# runs. What may be running (_RUNNING) and what may be waited for (_AWAITED)
# differ on purpose: "waiting for review" is a PR sitting with other people,
# which is finished work, and the emulator or a watcher is "still running"
# long after the agent stopped waiting on anything.
_RUNNING = (r"(?:build|compile|compilation|review|reviewers?|tests?|suite|runs?|jobs?"
            r"|agents?|subagents?|workers?|pass|audit|discovery|census|critic|fork|lanes?"
            r"|arms?|renders?|replay|judges?|eval|benchmark|verifiers?|[A-Z]\d+)")
_AWAITED = (r"(?:runs?|agents?|subagents?|arms?|workers?|verifiers?|reviewers?|builds?"
            r"|compiles?|jobs?|lanes?|pass|report|notifications?|results?|findings|verdict"
            r"|critic|census|fork|suite|tests?|synthesis)")
_LANDS = (r"(?:is |are )?(?:lands?|reports?|finish(?:es)?|completes?|returns?|comes? back"
          r"|is green|goes green|drains?)\b")

# Without a copula, "running" must end the clause: "Compile running." is
# work in flight, "any agent running `pgrep` here" is a sentence about agents.
_CLAUSE_ENDS = r"(?=\s*(?:$|[.,;:(—–-]|now\b|again\b|in the background|background\b))"

_WORKING_ON_ITS_OWN = re.compile(
    r"\b(?:"
    # "Compile running.", "Two more agents (Quests, Profile) still running."
    rf"{_RUNNING}(?: \([^)]*\))?(?:(?:'s| is| are) (?:still )?(?:running|in flight|in progress"
    rf"|underway)|(?: still)? (?:running|in flight|in progress|underway){_CLAUSE_ENDS})"
    r"|(?<!nothing )(?<!nothing's )(?<!no )(?<!was )(?<!were )in flight"
    # "Once it lands I'll verify ...", "Consolidation once C and E land." Not
    # "after": "the lock is released after the build finishes" describes code.
    r"|(?:once|when) (?:it|they|that|those|both|each|all (?:of them|\w+)"
    r"|the (?:\w+ ){0,2}(?:runs?|agents?|arms?|workers?|verifiers?|reviewers?|reviews?"
    r"|builds?|compiles?|jobs?|suite|tests?|report|critic|census|lanes?)"
    rf"|[A-Z]\w? and [A-Z]\w?) {_LANDS}"
    r"|once (?:it'?s )?green|after (?:its|their) (?:findings|report|verdict|results)"
    r"|(?:one|two|three|\d+) (?:more )?(?:runs?|agents?|arms?|lanes?|reviewers?|verifiers?"
    r"|workers?) (?:left|to go)\b"
    # "Still waiting on the data/domain and presentation/Compose verifiers."
    # The determiner is required: "waiting on reviewers" is a PR with people.
    rf"|waiting (?:on|for) (?:the|its|their|those|these|all|both|remaining|the last"
    rf"|the other|\d+|two|three|four)\s(?:[\w/]+[ -]){{0,4}}{_AWAITED}\b"
    r"|waiting (?:on|for) (?:it|them)\b|waiting on `?@"
    # The agent reporting, not asked to be told: "I'll relay it when it lands",
    # never "confirm when it looks right".
    r"|report back when|(?:i'?ll|i will|will) (?:report|relay|flag|confirm|batch)"
    r" (?:\w+ ){0,4}when (?:it|they|all|both|each|done)"
    # A coordinator between its workers' reports.
    r"|nothing (?:else )?to (?:act on|run|launch)(?: yet| until)|still holding|^holding\.?$"
    r"|heartbeats?\b|(?:waiters?|workers?) (?:is |are )?alive|letting it finish"
    r"|wait for it\b|i'?ll wait for (?:the|its|their)|yielding until"
    r"|until (?:it|they|its|their) (?:\w+ )?(?:reports?|lands?|finish(?:es)?|verdict"
    r"|results?|returns?)"
    r"|not concluding until|nothing (?:else )?(?:needed from (?:me|you)|for you to do) until)",
    re.IGNORECASE)

# The wait is on you after all: Plannotator is a review only you can close,
# "another agent" is someone else's work, and "let me know when it
# finishes" is you doing the work.
_YOUR_TURN = re.compile(
    r"plannotator|annotat|another agent|your (?:close|review|browser|tap|screenshot|pin|go"
    r"|machine|nod|ok|okay|call|decision|answer|input|eyes)\b|close (?:it|the tab|the review)\b"
    r"|until you (?:reply|answer|decide|say|confirm|approve)|without you\b"
    r"|let me know|tell me\b|please\b|(?:once|when|after|until) you\b|\bis installed\b"
    r"|needs? (?:from you|you)\b|go ahead\b",
    re.IGNORECASE)


def works_on_its_own(text):
    """Did the agent stop to wait on work it started -- subagents, a build,
    a review -- and will carry on when that reports, needing nothing from
    you? Such a turn is neither done nor waiting on you.

    A coordinator that dispatches subagents ends a turn after every launch
    and every hand-back. Each one was announced as finished ("Rendering
    hard-case pairs complete", four times in five minutes, while the runs
    were still going), which buried the one real completion among them.
    Hand-labelled, 380 real Stops, 88 of them this kind (190 tuned on, 190
    held out): held out, 30 of 30 flags right and 30 of 48 caught. An audit
    of the 135 other Stops it flagged found 10 that waited on you, before
    the question and "your nod" vetoes; 3 remain.
    """
    sentences = _closing_sentences(text)
    # Any question vetoes, offers included: "Want me to dispatch APP-1204
    # now?" starts no nudge, but it is not a turn to keep quiet about.
    if any(s.endswith("?") or _YOUR_TURN.search(s) for s in sentences) \
            or _asks_developer(sentences):
        return False
    return any(_WORKING_ON_ITS_OWN.search(s) for s in sentences)


# Anything that could be a failure. Deliberately wide: it only decides
# whether a claim of failure has something to stand on. phrase_gen's
# commentary guard builds on the same list.
FAILURE_ALTS = (r"fail(?:ed|s|ing|ures?)?|errors?|broken?|crash(?:ed|es|ing)?|can'?t|cannot"
                r"|couldn'?t|unable|blocked|stuck|doesn'?t work|not working|regress(?:ion|ed)?"
                r"|exception|timed? ?out")
_FAILURE_WORDS = re.compile(rf"\b(?:{FAILURE_ALTS})\b", re.IGNORECASE)


# ── The reading, and the rules that override a model's verdict ──────────

# `hobson stats` counts this line.
STILL_WORKING_LOG = "[Stop] still working — waiting on its own work, not announced"


class StopReading(NamedTuple):
    """How a turn ended, from its transcript alone -- before any model.

    context          what a model is shown: "Earlier: … | …" then
                     "Last message: …", the last message condensed so its
                     ending survives
    last_message     that last message, as data ("" when the last turn is
                     not the agent's) -- what the detectors read
    task             the user's latest request (request_for_prompt), or None;
                     not looked up for a turn that is still working
    awaiting_answer  the agent stopped waiting on the developer
    still_working    the agent stopped waiting on its own work, asking
                     nothing of the developer: not announced
    failure_reported the last line the model sees names a failure
    """
    context: str
    last_message: str = ""
    task: Optional[str] = None
    awaiting_answer: bool = False
    still_working: bool = False
    failure_reported: bool = False

    def settle(self, category, failed=False):
        """The turn's category, given a model's verdict: `category` is what
        it said (None for a SKIP), `failed` that the call itself failed.
        Returns (category, note); the note says which rule overrode the
        model, for the log, or is None. None as the category means unknown,
        and unknown still nudges.

        - Waiting on the developer is a question, whatever the model said,
          SKIP and failure included. The model called 45 of 46
          question-ending Stops "done", which also told the nudge the turn
          had finished.
        - A failed call stays unknown.
        - A SKIP is "done": on 201 hand-labelled real Stops, all 10 the
          model skipped with the waiting check clear were done.
        - "broken" with no failure named in the last message is "done": the
          model said broken 26 times against 2 that were, and all 18 whose
          last message named no failure were wrong.
        """
        if self.awaiting_answer:
            if category is not None and category != "question" and not failed:
                return "question", f"(category {category}->question: ends on a question)"
            return "question", None
        if failed:
            return None, None
        if category is None:
            return "done", None
        if category == "broken" and not self.failure_reported:
            return "done", "(category broken->done: nothing in the last message failed)"
        return category, None

    def doubts(self, category):
        """Would settle() overrule this "broken"? A phrase generator with an
        attempt left asks again instead, saying why -- the phrase for a
        "broken" says something failed, which a relabel cannot take back."""
        return category == "broken" and not self.awaiting_answer and not self.failure_reported


def read(hook_input):
    """Read how this Stop's turn ended. None when there is no transcript, or
    nothing in it to go on."""
    transcript = find_transcript(hook_input)
    if not transcript:
        return None
    context, last_line, message = _format(_turns(transcript)[-4:])
    if not context:
        return None
    still_working = bool(message) and works_on_its_own(message)
    return StopReading(
        context=context,
        last_message=message,
        task=None if still_working else request_for_prompt(hook_input, context),
        awaiting_answer=bool(message) and awaits_developer(message),
        still_working=still_working,
        failure_reported=bool(_FAILURE_WORDS.search(last_line)),
    )


# ── The static engines' model verdict ─────────────────────────────────

EXAMPLES = [
    ("I added the ViewModel and wired it up. The screen now renders correctly.", "done"),
    ("All tests pass and the build is clean.", "done"),
    ("The PR is merged and the commit is live on develop.", "done"),
    ("Pushed the commit to origin.", "done"),
    ("The build failed with a gradle compile error on line 42.", "broken"),
    ("I ran into a NullPointerException in the sync task.", "broken"),
    ("Which approach do you prefer, option A or option B?", "question"),
    ("Should I delete the old implementation or keep it as a fallback?", "question"),
]

PROMPT_HEADER = (
    "<start_of_turn>user\n"
    "Classify messages into: done, broken, question. Reply with one word only."
    "<end_of_turn>\n"
    "<start_of_turn>model\n"
    "Ok."
    "<end_of_turn>\n"
)


def snippet(text):
    """First 2 + last 3 sentences for context on both topic and conclusion."""
    text = text.strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= 5:
        return text[-800:]
    return " ".join(sentences[:2]) + " [...] " + " ".join(sentences[-3:])


def build_prompt(text):
    prompt = PROMPT_HEADER
    for user_text, answer in EXAMPLES:
        prompt += (
            f"<start_of_turn>user\n{user_text}<end_of_turn>\n"
            f"<start_of_turn>model\n{answer}<end_of_turn>\n"
        )
    prompt += f"<start_of_turn>user\n{snippet(text)}<end_of_turn>\n<start_of_turn>model\n"
    return prompt


def classify(text, engine_tag=None, model=None, ollama_url=None):
    """Classify assistant output as done/broken/question via Ollama."""
    from bark_templates import CATEGORIES

    model = model or DEFAULT_OLLAMA_MODEL
    tag = f"[{engine_tag}] " if engine_tag else ""

    try:
        req = build_request("/api/generate", {
            "model": model,
            "stream": False,
            "raw": True,
            "keep_alive": -1,
            "options": {"temperature": 0.0, "top_k": 1, "num_predict": 5, "num_ctx": 512},
            "prompt": build_prompt(text),
        }, ollama_url)
        with urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())["response"].strip().lower()
            # In raw mode Ollama doesn't strip stop tokens, so the answer can
            # arrive glued to one (e.g. "done<end_of_turn>"). Match the category
            # keyword anywhere in the output rather than splitting on whitespace.
            result = next((c for c in CATEGORIES if c in raw), None)

        if result:
            log_record.write(f"{tag}classify ({model}) -> {result!r}")
            return result

        log_record.write(f"{tag}classify ({model}) -> no match (raw: {raw!r})")
    except (URLError, OSError, json.JSONDecodeError, KeyError, IndexError) as e:
        log_record.write(f"{tag}classify ({model}) -> failed ({e})")

    return None
