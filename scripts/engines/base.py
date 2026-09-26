"""Base engine — shared logic for all hobson TTS engines.

Handles: config loading, logging, transcript lookup, Ollama classification,
lock/cooldown, template selection (with cache-preference), and playback.
Engine subclasses only need to implement cached_audio() and backfill().
"""

import json
import sys
import os
import re
import subprocess
import glob
import random
import fcntl
import time
import hashlib
from abc import ABC, abstractmethod
from urllib.request import urlopen
from urllib.error import URLError

# Resolve project root: CLAUDE_PLUGIN_ROOT (plugin mode) or repo root
ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

sys.path.insert(0, os.path.join(ROOT, "scripts"))

CONFIG_FILE = os.path.expanduser("~/.claude/hobson.json")
BARK_LOCK_FILE = os.path.expanduser("~/.claude/hobson.lock")
COMMENTARY_LOCK_FILE = os.path.expanduser("~/.claude/hobson-commentary.lock")
LOG_FILE = os.path.expanduser("~/.claude/hobson.log")

# Hobson was called claudio until 0.3.0, and claude-bark before that. State
# kept under an old name, oldest name last, is moved to the new one the
# first time it is needed. Only a missing target is ever written: nothing is
# overwritten, and the config, the API key, months of log and session
# history all come across. Paths are resolved against CONFIG_FILE's
# directory so the test fixture's tmp home covers them too.
_LEGACY_NAMES = (
    # (new name, old names to take it from, newest first)
    ("hobson.json", ("claudio.json", "claude-bark.json")),
    ("hobson.log", ("claudio.log",)),
    ("hobson.env", ("claudio.env",)),
    ("hobson-sessions", ("claudio-sessions",)),
)

# The default personality was "alfred" until it became Hobson himself.
_PERSONALITY_ALIASES = {"alfred": "hobson"}


def migrate_legacy_state():
    """Move claudio / claude-bark state to Hobson's names. Returns what moved."""
    home = os.path.dirname(CONFIG_FILE)
    moved = []
    for new, olds in _LEGACY_NAMES:
        target = os.path.join(home, new)
        if os.path.lexists(target):
            continue
        for old in olds:
            source = os.path.join(home, old)
            if not os.path.lexists(source):
                continue
            try:
                os.rename(source, target)  # atomic; keeps claudio.env at 600
            except OSError:
                break  # another hook got there first, or the disk refused
            moved.append(f"{old} -> {new}")
            break
    if any(m.endswith("-> hobson.json") for m in moved):
        _rename_legacy_personality()
    return moved


def _rename_legacy_personality():
    """A migrated config naming "alfred" now names "hobson"."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(config, dict):
        return
    new = _PERSONALITY_ALIASES.get(config.get("personality"))
    if new:
        config["personality"] = new
        tmp = f"{CONFIG_FILE}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_FILE)

from ollama_client import DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL, build_request

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

# 600s, not 180s: measured over 11,173 real gaps between spoken phrases, a
# 180s timeout expires during 4.1% of them and each expiry costs the next
# utterance a fallback to macOS `say`, because commentary deliberately
# passes allow_cold_start=False rather than blocking a hook for a 20s model
# load. 600s drops that to 1.1% and still frees the model after a real
# break. Single source of truth for both realtime engines' in-code
# `.get()` fallback, so it cannot drift from DEFAULT_CONFIG below.
DEFAULT_DAEMON_IDLE_TIMEOUT = 600

DEFAULT_CONFIG = {
    "engine": "say",
    "personality": "hobson",
    "events": ["stop", "permission", "notification"],
    "cooldown": 2.0,
    "volume": 3,
    "muted": False,
    "mute_until": 0,
    "quiet_hours": None,
    "ollama": {
        "model": "llama3.2:3b",
        "url": "http://localhost:11434",
    },
    "kokoro": {
        "voice": "am_puck",
        "speed": 1.1,
        "daemon_port": 19849,
        # See DEFAULT_DAEMON_IDLE_TIMEOUT above for why this is 600, not 180.
        "daemon_idle_timeout": DEFAULT_DAEMON_IDLE_TIMEOUT,
    },
    "chatterbox": {
        "device": "mps",
        "exaggeration": 1.0,
    },
    "pocket_tts": {
        "voice": "charles",
        "language": "english",
        "temp": 0.7,
        "daemon_port": 19850,
        "daemon_idle_timeout": DEFAULT_DAEMON_IDLE_TIMEOUT,  # see the kokoro note above
    },
    # Off by default: afplay -r resamples, which makes speech sound robotic
    # (and degraded the macOS `say` fallback too, since that also plays via
    # afplay). See BaseEngine._afplay_args.
    "project_identity": {
        "enabled": False,
        "spread": 0.06,
    },
    "commentary": {
        "cooldown": 8.0,
        "tools": ["Agent", "Edit", "Write", "Bash"],
        # terse  — LLM-gated, only voice notable events (rare voicings)
        # normal — LLM-gated, bias toward voicing meaningful actions (default)
        # chatty — no LLM, voice every allowed tool with a templated phrase
        # With the decider on "jev", terse and normal both defer to
        # "chattiness" below instead.
        "verbosity": "normal",
        # Subagents (background workers) stamp agent_id/agent_type on their
        # events; main-session events carry neither. Measured: ~51 PreToolUse
        # events/minute from background workers the user isn't watching.
        # Commentary only — a subagent's Stop still speaks (see run()).
        "suppress_subagents": True,
        # Substance gate for batched commentary (terse/normal only — chatty
        # speaks every call). Chosen by simulating the real 16,875-event
        # PreToolUse log: 5/60s gives a 2.9x reduction in utterances vs
        # today; 3/15s (an earlier draft) only gave 1.5x because the short
        # timer fires constantly before enough calls accumulate.
        "min_tool_calls": 5,
        "min_seconds": 60.0,
        "pending_stale_seconds": 90.0,
        # Decider backend "jev" only: how much of what the gate above
        # releases gets spoken, 0 (next to nothing) to 1 (all of it). A batch
        # is spoken when P(worth) >= 1 - chattiness (gate.py), and terse and
        # normal stop differing. 0.6 caught all 7 worth-hearing batches in
        # 140 real ones while speaking a third as often as Ollama did.
        "chattiness": 0.6,
        # Anomaly mode only: how far back "back on the same thing again"
        # looks for the same action (session_state.anomaly_reason).
        "repeat_window": 180,
    },
    "nudge": {
        "enabled": True,
        "delays": [45, 120, 300],
    },
    "watchdog": {
        "enabled": True,
        "minutes": 10,
    },
    # Optional remote decision model (Jev, via OpenRouter's Decisions API).
    # See scripts/decider.py. At "local" (the default) it makes zero network
    # calls. Two questions go to it:
    #   - phrase_gen: does this phrase restate one just spoken? Sends the
    #     phrase plus up to six recently spoken ones.
    #   - risk: is this shell command destructive? Asked only for a Bash
    #     permission request the local rules can neither flag nor clear, and
    #     sends the command REDACTED (risk.redact: no heredoc bodies or long
    #     quoted text; URLs, hosts, IPs, emails, secrets, tokens and all but
    #     the last component of every path replaced).
    # Never code, file contents, or conversation.
    "decider": {
        "backend": "local",          # "local" | "jev"
        "model": "typesafe/jev-1.13",
        # Headroom, not a tuned figure. Every hook is a fresh process, so
        # each call pays a new TCP+TLS handshake to OpenRouter with no
        # connection pooling -- real latency is well above Jev's quoted
        # 70-500ms of inference whatever the link. The observations behind
        # this number (p50 0.6s, p90 3.2s) were taken on a degraded
        # connection and are an upper bound, not a measurement; 2000ms was
        # visibly timing out into local under those conditions. Re-measure
        # on a good link before treating this as tuned.
        "timeout_ms": 4000,
        "endpoint": "https://openrouter.ai/api/alpha/decisions",
        # Speak a rejected phrase when P(restates) falls below this
        # (phrase_gen._decider_rescues_duplicate).
        "dedup_restates_max": 0.5,
        # Silence commentary the local guard passed when P(restates) is at or
        # above this (phrase_gen._decider_blocks_restatement). Commentary
        # only: a Stop, permission or notification is never blocked by it.
        "dedup_block_min": 0.7,
        # Warn "Careful — this one looks hard to undo" when P(destructive)
        # for a command the local rules cannot place is at or above this
        # (risk.permission_risk).
        "permission_risk_min": 0.5,
    },
}


# Chatty commentary templates — used when commentary.verbosity == "chatty".
# Keyed by lowercased tool name. {ctx} is filled from extract_context().
COMMENTARY_TEMPLATES = {
    "edit":      ["I'm editing {ctx}.", "Updating {ctx}.", "Tweaking {ctx}."],
    "write":     ["I'm writing {ctx}.", "Creating {ctx}.", "Drafting {ctx}."],
    "multiedit": ["I'm patching {ctx}.", "Editing {ctx}."],
    "bash":      ["I'm running {ctx}.", "Executing {ctx}.", "Kicking off {ctx}."],
    "agent":     ["I'm dispatching {ctx}.", "Spawning agent for {ctx}."],
    "task":      ["I'm dispatching {ctx}."],
    "read":      ["I'm reading {ctx}.", "Checking {ctx}."],
    "grep":      ["I'm searching for {ctx}."],
    "glob":      ["I'm looking for {ctx}."],
}

COMMENTARY_GENERIC = [
    "I'm using {tool}.",
    "I'll use {tool}.",
]

# Documented Claude Code notification_type values, mapped to a plain-English
# clause. Free-text `message` alone caused a live misclassification: a bare
# "Claude needs your permission" was handed to the model with no type and it
# inferred an error where none existed. Values not listed here (including
# future ones) fall back to the raw message.
NOTIFICATION_LABELS = {
    "permission_prompt": "waiting for your approval",
    "idle_prompt": "waiting for you — nothing to do until you reply",
    "agent_needs_input": "a background agent is waiting for you",
    "agent_completed": "a background agent finished",
    "auth_success": "signed in",
}


def load_config():
    """Load config from ~/.claude/hobson.json, merged with defaults."""
    config = dict(DEFAULT_CONFIG)
    if not os.path.isfile(CONFIG_FILE):
        migrate_legacy_state()
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user_config = json.load(f)
        # Shallow merge top-level, deep merge engine sub-dicts
        for key, val in user_config.items():
            if isinstance(val, dict) and key in config and isinstance(config[key], dict):
                config[key] = {**config[key], **val}
            else:
                config[key] = val
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Migrate kokoro.realtime_events -> top-level events
    if "events" not in (user_config if 'user_config' in dir() else {}):
        rt_events = config.get("kokoro", {}).get("realtime_events")
        if rt_events:
            config["events"] = rt_events

    # Clean up deprecated key from merged config
    config.get("kokoro", {}).pop("realtime_events", None)

    config["personality"] = _PERSONALITY_ALIASES.get(config.get("personality"),
                                                     config.get("personality"))
    return config


def log(msg):
    try:
        project = derive_project_label() or "unknown"
    except Exception:
        project = "unknown"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{project}] {msg}\n")
    except Exception:
        pass


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


def last_assistant_text(path):
    text = ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    for block in entry.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block["text"]
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                continue
    return text


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


def last_transcript_context(path, max_turns=4):
    """Return the last few turns of the conversation as a context string.

    Collects up to max_turns of user prompts and assistant responses so
    short final messages like 'isDraft: false' have surrounding context.
    Only what the user typed counts as a user turn (see _user_prompt_text).
    """
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

    recent = turns[-max_turns:]
    if not recent or recent[-1][0] != "Agent":
        return "\n".join(f"{role}: {condense_turn(text)}" for role, text in recent)
    # The final message is what the Stop is about; the turns before it are
    # background. Run together as four equal turns, the model reported an
    # earlier turn's work as this one's.
    earlier = " | ".join(f"{role}: {condense_turn(text, EARLIER_TURN_CHARS)}"
                         for role, text in recent[:-1])
    last = f"{_LAST_MESSAGE} {condense_turn(recent[-1][1], LAST_MESSAGE_CHARS)}"
    return f"Earlier: {earlier}\n{last}" if earlier else last


EARLIER_TURN_CHARS = 200
LAST_MESSAGE_CHARS = 500


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


_LAST_MESSAGE = "Last message:"

# `hobson stats` counts this line.
STILL_WORKING_LOG = "[Stop] still working — waiting on its own work, not announced"


def _last_message(event_detail):
    """The agent's final message from a Stop's context
    (last_transcript_context), which condense_turn cut so that the ending
    survives, or "". Without its label, which would otherwise open the
    first sentence and defeat the patterns anchored there."""
    lines = (event_detail or "").splitlines()
    last = lines[-1] if lines else ""
    return last[len(_LAST_MESSAGE):].strip() if last.startswith(_LAST_MESSAGE) else ""


def stop_awaits_answer(event_detail):
    """Does a Stop's context end with the agent waiting on the developer?"""
    last = _last_message(event_detail)
    return bool(last) and awaits_developer(last)


def stop_works_on_its_own(event_detail):
    """Does a Stop's context end with the agent waiting on its own work?"""
    last = _last_message(event_detail)
    return bool(last) and works_on_its_own(last)


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
            log(f"{tag}classify ({model}) -> {result!r}")
            return result

        log(f"{tag}classify ({model}) -> no match (raw: {raw!r})")
    except (URLError, OSError, json.JSONDecodeError, KeyError, IndexError) as e:
        log(f"{tag}classify ({model}) -> failed ({e})")

    return None


def bark_hash(text):
    """Stable SHA-256 prefix for cache key. Must match bark_templates.bark_hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_duration(text):
    """Seconds from a human duration. Bare numbers mean minutes.

    Accepts "45", "90s", "30m", "2h", "1.5h". Returns None if unparseable —
    callers must treat None as "the user typed something wrong", never as 0.
    """
    if not text:
        return None
    text = str(text).strip().lower()
    mult = 60.0  # a bare number means minutes
    if text and text[-1] in "smh":
        mult = {"s": 1.0, "m": 60.0, "h": 3600.0}[text[-1]]
        text = text[:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return value * mult if value > 0 else None


def _in_quiet_hours(window, now):
    """True when `now` falls inside a [start_hour, end_hour] window.

    Handles windows that wrap midnight (22 -> 8). Returns False for anything
    malformed: this runs inside a hook and a hand-edited config must never
    take voice down with a traceback.
    """
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        return False  # [1, 2, 3] used to read as 1-2, silencing 01:00-02:00
    try:
        start, end = int(window[0]), int(window[1])
    except (TypeError, ValueError):
        return False
    if not (0 <= start <= 24 and 0 <= end <= 24) or start == end:
        return False
    hour = time.localtime(now).tm_hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def silence_reason(config, now=None):
    """Why voice is suppressed right now, or None if it may speak.

    Checked cheapest-first. The returned string is logged, so the user can
    always find out why hobson went quiet — a detector that silences without
    saying so is the worse bug.
    """
    now = time.time() if now is None else now
    if config.get("muted"):
        return "muted"
    until = config.get("mute_until") or 0
    try:
        remaining = float(until) - now
    except (TypeError, ValueError):
        remaining = 0
    if remaining > 0:
        return f"muted for another {int(remaining // 60)}m{int(remaining % 60):02d}s"
    window = config.get("quiet_hours")
    if window and _in_quiet_hours(window, now):
        return f"quiet hours {window[0]}-{window[1]}"
    return None


def format_tool_name(tool):
    from bark_templates import normalize_tool_name
    tool = normalize_tool_name(tool)
    if not tool:
        return "something important"
    return re.sub(r"\s+", " ", tool)[:80]


# A declaration: annotations, any number of modifiers, a declaring keyword,
# then the name -- past a Go receiver `(r *Repo)`, type parameters `<T>`, and
# an extension receiver `Foo.Companion.`, so the function is named rather
# than the type it extends. Not `val`: in Kotlin that is mostly a local
# inside the body being edited, which is not what the edit is about.
_DECL_MODIFIERS = (
    r"(?:(?:export|default|public|private|protected|internal|open|abstract|"
    r"final|static|override|suspend|inline|async|pub(?:\([a-z]+\))?|data|"
    r"sealed|enum|annotation|inner|lateinit|operator|infix|tailrec|external|"
    r"fileprivate|mutating|unsafe|value)\s+)*"
)
_SYMBOL_RE = re.compile(
    r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*" + _DECL_MODIFIERS
    + r"(?:def|class|fun|func|fn|function|const|let|var|struct|enum|interface|"
    r"object|trait|impl|type|protocol)\s+"
    r"(?:\([^)]*\)\s*)?(?:<[^>]*>\s*)?(?:[A-Za-z_]\w*(?:<[^>]*>)?\??\.)*"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)
_SHELL_FN_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{")

# Prose has sentences, not declarations: "let me explain" is not a `let`.
_PROSE_EXTS = {".md", ".markdown", ".txt", ".rst", ".adoc"}


def _code_gist(text, path=""):
    """Name the first symbol defined in a code blob, or None.

    Returns an identifier, never a source line: raw code read aloud is
    unlistenable, and every comparable tool that shipped it backed it out.

    Measured on 1,137 real Kotlin edits, the earlier one-modifier regex
    found a name for 23% (now 36%), and of those, 18 were the word "class"
    (every `enum class X`) and 46 the receiver type instead of the function.
    """
    if os.path.splitext(path or "")[1].lower() in _PROSE_EXTS:
        return None
    for line in (text or "").splitlines()[:40]:
        m = _SYMBOL_RE.match(line) or _SHELL_FN_RE.match(line)
        if m:
            return m.group(1)
    return None


def _line_delta(old, new):
    """'+4/-1 lines' when the counts differ, else None."""
    added = len((new or "").splitlines())
    removed = len((old or "").splitlines())
    if added == removed:
        return None
    return f"+{added}/-{removed} lines"


def describe_generation_failure(raw):
    """Turn phrase_gen's last_raw into a clear log verdict.

    Three outcomes get conflated if you just print raw: the model declining
    to speak (SKIP), the model erroring (timeout, connection refused), and
    the model returning something unusable. Only the first is normal. A real
    Ollama timeout logged as "skipped" reads like working-as-intended, which
    is how 366 of them sat unnoticed in the log.

    Returns (verdict, detail) where verdict is SKIP, ERROR, REJECTED or
    UNUSABLE.
    """
    text = "" if raw is None else str(raw)
    if text.startswith("(error:"):
        inner = text[len("(error:"):].rstrip(")").strip()
        if "timed out" in inner:
            return "ERROR", f"ollama timed out — {inner}"
        if "refused" in inner or "URLError" in inner:
            return "ERROR", f"ollama unreachable — {inner}"
        return "ERROR", f"ollama call failed — {inner}"
    if text.startswith("(rejected:"):
        inner = text[len("(rejected:"):].rstrip(")").strip()
        return "REJECTED", f"guard rejected — {inner}"
    stripped = text.strip().strip("'\"")
    if not stripped:
        return "ERROR", "ollama returned nothing"
    if stripped.upper() == "SKIP":
        return "SKIP", "model chose not to speak"
    return "UNUSABLE", f"could not parse a phrase from {stripped!r}"


def extract_context(tool_name, tool_input):
    """Pull a short context string from tool_input for commentary."""
    if not tool_input or not isinstance(tool_input, dict):
        return None
    tool = tool_name.lower()
    if tool in ("edit", "write", "multiedit"):
        fp = tool_input.get("file_path", "")
        name = os.path.basename(fp) if fp else None
        body = tool_input.get("new_string") or tool_input.get("content") or ""
        detail = _code_gist(body, fp)
        if not detail and tool != "write":
            detail = _line_delta(tool_input.get("old_string"), body)
        if name and detail:
            return f"{name} ({detail})"
        return name or detail or None
    if tool == "bash":
        return (tool_input.get("description")
                or (tool_input.get("command", "")[:40])) or None
    if tool == "agent":
        return tool_input.get("description") or None
    if tool == "read":
        fp = tool_input.get("file_path", "")
        return os.path.basename(fp) if fp else None
    if tool in ("grep", "glob"):
        return tool_input.get("pattern") or None
    return None


# ── Project identification ─────────────────────────────────────────────────

_PROJECT_LABEL_CACHE = None


def derive_project_label():
    """Derive a short, speakable project label from the current working directory.

    Handles workmux __worktrees pattern:
      /Dev/mobile-app                         → "mobile app"
      /Dev/mobile-app__worktrees/app-4005-...  → "mobile app, A P P 4005"

    Returns empty string if label can't be determined.
    Caches result per-process (cwd doesn't change within a hook invocation).
    """
    global _PROJECT_LABEL_CACHE
    if _PROJECT_LABEL_CACHE is not None:
        return _PROJECT_LABEL_CACHE

    try:
        cwd = os.getcwd()
    except OSError:
        _PROJECT_LABEL_CACHE = ""
        return ""

    if "__worktrees" in cwd:
        idx = cwd.index("__worktrees")
        repo_dir = cwd[:idx].rstrip("/")
        repo_name = os.path.basename(repo_dir).replace("-", " ").replace("_", " ")
        remainder = cwd[idx + len("__worktrees"):].lstrip("/")
        branch = remainder.split("/")[0] if remainder else ""

        m = re.match(r'^[a-zA-Z]{2,6}-\d+-(.+)', branch)
        if m:
            # Strip ticket prefix (e.g. "app-4005-") and use the task description
            task = m.group(1).replace("-", " ")
            # Keep first 4 words max for TTS brevity
            task = " ".join(task.split()[:4])
            label = f"{repo_name}, {task}"
        elif branch:
            label = f"{repo_name}, {branch.replace('-', ' ')[:25]}"
        else:
            label = repo_name
    else:
        name = os.path.basename(cwd.rstrip("/"))
        label = name.replace("-", " ").replace("_", " ")

    _PROJECT_LABEL_CACHE = label
    return label


def project_rate(label, spread=0.06, steps=5):
    """A stable playback-rate offset for a project label.

    Gives each project a recognisable sound without changing the configured
    voice — the live voice is a custom clone and must survive. Rate shifts
    pitch and tempo together, so keep the spread small. `steps` is odd so
    exactly one bucket lands on 1.0 and some project sounds untouched.
    """
    if not label:
        return 1.0
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % steps
    middle = steps // 2
    return round(1.0 + (bucket - middle) * (spread * 2 / (steps - 1)), 4)


# ── TTS text normalization ─────────────────────────────────────────────────

_TTS_EXT_MAP = {
    "kt": "K T", "py": "P Y", "js": "J S", "ts": "T S",
    "tsx": "T S X", "jsx": "J S X", "json": "JSON", "yaml": "YAML",
    "yml": "YAML", "xml": "X M L", "html": "H T M L", "css": "C S S",
    "scss": "S C S S", "md": "M D", "sh": "S H", "rb": "R B",
    "go": "Go", "rs": "R S", "cpp": "C plus plus", "swift": "Swift",
    "gradle": "Gradle", "toml": "TOML", "txt": "text", "csv": "C S V",
    "sql": "S Q L", "env": "E N V",
}

# Initialisms that TTS mispronounces when title-cased ("Kyc" -> "kick").
# Applied after de-camelling, so the standalone token is what gets matched.
_TTS_INITIALISMS = {
    "Kyc": "KYC", "Api": "API", "Url": "URL", "Uri": "URI", "Pr": "PR",
    "Id": "ID", "Ui": "UI", "Ux": "UX", "Tts": "TTS", "Json": "JSON",
    "Http": "HTTP", "Https": "HTTPS", "Sql": "SQL", "Css": "CSS",
    "Html": "HTML", "Cli": "CLI", "Sdk": "SDK", "Jwt": "JWT",
}

# Product names that must survive de-camelling intact. Protected by
# placeholder substitution before the CamelCase pass, restored after.
_TTS_PROPER_NOUNS = (
    "GitHub", "GitLab", "JavaScript", "TypeScript", "PostgreSQL", "GraphQL",
    "MacBook", "iOS", "macOS", "iPadOS", "OpenAI", "YouTube", "WebSocket",
    "OAuth", "JavaDoc", "PyPI", "npm", "ESLint",
)


def tts_normalize(text):
    """Normalize code-related terms in text for TTS pronunciation.

    Handles: URLs (keep hostname only), file paths (reduce to basename),
    file extensions (.kt → dot K T), version numbers (v2.1 → version 2 1),
    snake_case (underscores → spaces), CamelCase (insert spaces).
    Call before sending text to any TTS engine.
    """
    if not text:
        return text

    # Protect product names from the CamelCase pass below.
    protected = {}
    for i, noun in enumerate(_TTS_PROPER_NOUNS):
        token = f"\x00{i}\x00"
        if noun in text:
            protected[token] = noun
            text = text.replace(noun, token)

    # URLs: keep only the hostname
    text = re.sub(
        r'https?://([a-zA-Z0-9._-]+)(?:[:/][^\s]*)?',
        lambda m: m.group(1),
        text,
    )

    # Absolute/home file paths: reduce to basename.
    # Requires 3+ absolute components or ~ prefix to avoid false-positives.
    text = re.sub(
        r'(?:~(?:/[a-zA-Z0-9._-]+)+|(?:/[a-zA-Z0-9._-]+){3,})/([a-zA-Z0-9._-]+)',
        r'\1', text
    )

    # File extensions: word.ext → word dot EXT
    def _ext_sub(m):
        base, ext = m.group(1), m.group(2).lower()
        spoken = _TTS_EXT_MAP.get(ext, " ".join(ext.upper()))
        return f"{base} dot {spoken}"

    text = re.sub(r'\b([A-Za-z0-9_]+)\.([a-zA-Z]{1,6})\b', _ext_sub, text)

    # Version numbers: v2.1.0 → version 2 1 0
    text = re.sub(
        r'\bv(\d[\d.]*)\b',
        lambda m: "version " + m.group(1).replace(".", " "),
        text,
    )

    # Ticket numbers: APP-4005 -> stripped. Keep any trailing slug, which
    # carries the human task name ("APP-4005-dark-mode" -> "dark mode").
    text = re.sub(
        r'\b[A-Z]{2,6}-\d+(?:-([-\w]+))?\b',
        lambda m: (m.group(1) or "").replace("-", " "),
        text,
    )

    # Hex blobs (task/agent ids) are unspeakable — drop them.
    text = re.sub(r'\b[0-9a-f]{8,}\b', '', text)

    # snake_case: underscores between word chars → spaces
    text = re.sub(r'(?<=\w)_(?=\w)', ' ', text)

    # CamelCase/PascalCase: insert spaces before uppercase transitions
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)

    # Recase initialisms now that de-camelling has left them standalone.
    text = re.sub(
        r'\b([A-Za-z]{2,5})\b',
        lambda m: _TTS_INITIALISMS.get(m.group(1).capitalize(), m.group(1)),
        text,
    )

    for token, noun in protected.items():
        text = text.replace(token, noun)

    # Collapse whitespace left behind by stripped tickets and hex ids, and
    # tidy punctuation that lost its word.
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s+([.,!?])', r'\1', text)
    return text.strip()


class BaseEngine(ABC):
    """Abstract base for TTS engines.

    Subclasses must set:
        templates_module: "bark_templates"
        cache_dir: path to cache directory (or None)
        cache_ext: "wav" or None
        engine_name: short name for log lines (e.g. "kokoro-rt")

    Subclasses must implement:
        backfill(text): spawn background process to generate audio for cache miss
    """

    templates_module = "bark_templates"
    cache_dir = None
    cache_ext = None
    engine_name = "base"

    # Map hook event names to config event keys
    EVENT_MAP = {
        "Stop": "stop",
        "PermissionRequest": "permission",
        "Notification": "notification",
        "PreToolUse": "commentary",
    }

    def __init__(self, config):
        self.config = config
        self.cooldown = config.get("cooldown", 2.0)
        # Config scale 0-10 mapped to afplay's 0.0-1.0 (1.0 = unity gain)
        self.volume = max(0.0, min(10.0, float(config.get("volume", 3)))) / 10.0
        self._templates = None
        # Personality say voice for macOS say fallback
        self._say_voice = self._load_say_voice(config)
        # Event filtering
        self.enabled_events = set(config.get("events", ["stop", "permission", "notification"]))
        # Ollama config
        ollama_cfg = config.get("ollama", {})
        self._ollama_model = ollama_cfg.get("model", "llama3.2:3b")
        self._ollama_url = ollama_cfg.get("url", "http://localhost:11434")
        # Commentary config
        commentary_cfg = config.get("commentary", {})
        self.commentary_cooldown = commentary_cfg.get("cooldown", 8.0)
        self.commentary_tools = set(
            t.lower() for t in commentary_cfg.get(
                "tools", ["Agent", "Edit", "Write", "Bash"]
            )
        )
        verbosity = (commentary_cfg.get("verbosity") or "normal").lower()
        if verbosity not in ("terse", "normal", "chatty", "anomaly"):
            verbosity = "normal"
        self.commentary_verbosity = verbosity

    @staticmethod
    def _load_say_voice(config):
        """Get the macOS say voice from the active personality JSON."""
        personalities_dir = os.path.join(ROOT, "scripts", "personalities")
        name = config.get("personality", "hobson")
        path = os.path.join(personalities_dir, name, "personality.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("say_voice", "Daniel")
        except (FileNotFoundError, json.JSONDecodeError):
            return "Daniel"

    def _log(self, msg):
        """Log with [engine_name] prefix for structured monitor output."""
        log(f"[{self.engine_name}] {msg}")

    def _afplay_args(self, path):
        """Build the afplay argv for one clip. Single source of truth.

        Four call sites used to assemble this independently, so anything that
        alters playback has to be added here or it reaches only some of them.

        Playback-rate identity is OFF by default and should stay off. `afplay
        -r` resamples, shifting pitch and tempo together like tape speed, and
        on speech even a 3% shift is audibly robotic — it moves the formants.
        It also silently degraded the macOS `say` fallback, because
        _say_with_volume renders to AIFF and plays it through this same argv.
        Per-project identity belongs in voice selection (which the kokoro
        daemon supports per request), not in resampling the output.
        """
        args = ["afplay", "-v", str(self.volume)]
        identity = self.config.get("project_identity") or {}
        if identity.get("enabled", False):
            rate = project_rate(derive_project_label(),
                                spread=identity.get("spread", 0.06))
            if rate != 1.0:
                args += ["-r", str(rate), "-q", "1"]
        return args + [path]

    def _afplay(self, path):
        """Play a clip, detached — it must survive the hook process exiting."""
        subprocess.Popen(self._afplay_args(path))

    # ── Nudge (Task 7) ──────────────────────────────────────────────────

    def _maybe_start_nudge(self, subject):
        """Spawn a detached nudge for a session that is waiting on the user.

        Guarded end-to-end — nudge.enabled and any failure to spawn must
        never break the caller, which runs inline in an async hook. The
        spawned process is responsible for its own one-per-project lock.
        Its argv is round-tripped through nudge.py's parser in the tests:
        the child runs with stderr discarded, so an argument it does not
        accept would kill it without a trace.
        """
        try:
            cfg = self.config.get("nudge") or {}
            if not cfg.get("enabled", True):
                return
            project = derive_project_label()
            nudge_script = os.path.join(ROOT, "scripts", "nudge.py")
            subprocess.Popen(
                [sys.executable, nudge_script, "--project", project,
                 "--subject", subject or ""],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._log(f"nudge: spawned for {project!r}")
        except Exception as e:
            self._log(f"nudge: spawn failed ({e})")

    # ── Watchdog (Task 9) ───────────────────────────────────────────────

    def _maybe_start_watchdog(self, baseline=None):
        """Ensure a hang-detector is running for this project.

        Spawned lazily on a commentary flush rather than on every event, and
        guarded on both ends: the parent probes the watchdog's own lock file
        first (a quick non-blocking flock, released immediately) so a busy
        project does not fork a subprocess it already knows will exit; the
        child then takes the same lock for real before it starts polling.
        The probe is a courtesy, not the source of truth — the child's own
        lock is what actually enforces one-per-project. Guarded end-to-end:
        a failed spawn (or a failed probe) must never break the flush it
        rides on.

        `baseline` should be the caller's in-memory last_event_time — the
        save_session() call that persists it to disk happens after this
        spawn on the flush path, so a child that re-read the baseline from
        disk would always see the stale, pre-flush value and exit on its
        first poll no matter what. Passed straight through as `--baseline`
        so there is no race to lose.
        """
        try:
            cfg = self.config.get("watchdog") or {}
            if not cfg.get("enabled", True):
                return
            project = derive_project_label()

            try:
                from nudge import _lock_path
                probe = open(_lock_path("watchdog", project), "a+", encoding="utf-8")
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe, fcntl.LOCK_UN)
                except BlockingIOError:
                    probe.close()
                    return  # a watchdog is already running for this project
                probe.close()
            except (ImportError, OSError):
                pass  # probe itself failed — fall through, the child's own
                      # lock is still authoritative either way

            nudge_script = os.path.join(ROOT, "scripts", "nudge.py")
            argv = [sys.executable, nudge_script, "--watchdog", "--project", project,
                    "--minutes", str(cfg.get("minutes", 10))]
            if baseline is not None:
                argv += ["--baseline", str(baseline)]
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._log(f"watchdog: spawn requested for {project!r}")
        except Exception as e:
            self._log(f"watchdog: spawn failed ({e})")

    @staticmethod
    def _waiting_type(hook_input):
        """This Notification's type if it means the session is blocked on the
        user (nudge.WAITING_TYPES), else None."""
        try:
            from nudge import WAITING_TYPES
        except ImportError:
            return None
        ntype = hook_input.get("notification_type") or ""
        return ntype if ntype in WAITING_TYPES else None

    def _maybe_nudge_from_notification(self, hook_input):
        """Start a 'waiting' nudge when this Notification means the session
        is blocked on the user — idle_prompt / agent_needs_input are that
        signal already, free, with no timer and no inference required.

        Except idle_prompt after a turn that simply finished. It fires
        whenever the input sits idle, and on the first day nudges worked all
        16 followed a Stop classified "done" -- none a question -- so every
        finished task you walked away from started "nothing moves until you
        reply", which was not true. A question or a failure nudges; so does
        a turn whose category is unknown, since a failed classification must
        not quietly switch nudges off. agent_needs_input always nudges: a
        background agent that asks is blocked by definition.
        """
        ntype = self._waiting_type(hook_input)
        if not ntype:
            return
        if self._idle_after_finished_turn(hook_input):
            self._log("nudge: not started — the last turn finished, nothing is waiting on you")
            return
        self._maybe_start_nudge(NOTIFICATION_LABELS.get(ntype, ntype))

    def _idle_after_finished_turn(self, hook_input, state=None):
        """True for an idle_prompt that follows a turn classified "done", or
        one that ended waiting on the agent's own work ("working").

        It fires whenever the input sits idle, and after either of those
        nothing is waiting on you -- the Stop already said it finished, or
        the agent's subagents will wake it. The one gate for everything a
        waiting notification would otherwise start: the nudge, the realtime
        waiting line, the static bark. A question, a failure or an unknown
        ending does not match, and neither does agent_needs_input.
        """
        if self._waiting_type(hook_input) != "idle_prompt":
            return False
        if state is None:
            try:
                from session_state import load_session
                state = load_session(derive_project_label())
            except Exception:
                return False
        return state.get("last_stop_category") in ("done", "working")

    @staticmethod
    def _record_stop_category(category):
        """Remember how this turn ended, for the nudge to read. `category` is
        the classification itself, or None when there was none -- never a
        default filled in for the sake of picking a phrase."""
        try:
            from session_state import load_session, save_session
            state = load_session(derive_project_label())
            state["last_stop_category"] = category
            save_session(state)
        except Exception:
            pass

    def _speak_risky_permission(self, hook_input, state=None):
        """Announce a destructive permission request with a warning of its
        own. True if this was one (now spoken, and recorded in `state` when
        given). `rm -rf` and `ls` used to sound identical: the phrase was
        built from the tool name alone.

        A fixed sentence, not the model -- the same lesson as _speak_waiting:
        a fact the user must hear is not left to phrasing that can lose it.
        The log line carries the reason, never the command.
        """
        if hook_input.get("hook_event_name") != "PermissionRequest":
            return False
        try:
            from risk import permission_risk
            reason = permission_risk(hook_input.get("tool_name"),
                                     hook_input.get("tool_input"), self.config)
        except Exception as e:
            self._log(f"[PermissionRequest] risk check failed ({e})")
            return False
        if not reason:
            return False
        phrase = f"Careful — this one {reason}. It needs your approval."
        label = f"[PermissionRequest] destructive ({reason})"
        if state is not None:
            self._speak_fixed(phrase, state, label)
        else:
            self._log(f"{label} -> {phrase!r}")
            self.speak_dynamic(phrase, allow_cold_start=False)
        return True

    def _speak_waiting(self, hook_input, state):
        """Announce a blocked-on-you Notification from a template, never the
        model. True if this was one (now spoken and recorded in `state`).

        For the realtime engines, which otherwise generate every phrase. In
        ten days of real idle_prompt notifications the model mostly lost the
        meaning: it restated the last completion ("I finished the order
        history view.") or named the wrong wait ("waiting on your approval" for
        an idle prompt), and when its phrase repeated something recent the
        dedup guard silenced the announcement outright. The event means one
        fixed thing. Rotating past templates said recently keeps it from
        sounding canned; when all were said recently, one is spoken anyway —
        being blocked again is news even in the same words.
        """
        if not self._waiting_type(hook_input):
            return False
        if self._idle_after_finished_turn(hook_input, state):
            # Handled, by saying nothing -- falling through would hand the
            # event to the model, which is worse than either.
            from session_state import save_session
            save_session(state)
            self._log("[Notification] not announced — the last turn finished")
            return True
        templates = list(self.templates.NOTIFICATION_TEMPLATES)
        if not templates:
            return False
        self._speak_fixed(self._fresh_choice(templates, state), state,
                          "[Notification] waiting template")
        return True

    def _speak_question(self, hook_input, state=None):
        """Announce a question dialog -- AskUserQuestion or ExitPlanMode, which
        reach hobson as a PermissionRequest -- with the personality's
        "question" phrases, never the model. True if this was one.

        87 of them in the log, phrased by the model from "Tool:
        AskUserQuestion" alone: "I finished fixing hobson.", "I'm applying
        review fixes." The static engines said their permission template
        for a tool named AskUserQuestion.

        Static engines pass no state and bark a template, preferring cached
        audio. Realtime engines pass `state`, rotate past what was just said,
        and speak through their daemon.
        """
        if hook_input.get("hook_event_name") != "PermissionRequest":
            return False
        try:
            from nudge import USER_WAIT_TOOLS
        except ImportError:
            return False
        if hook_input.get("tool_name") not in USER_WAIT_TOOLS:
            return False
        if state is None:
            bark = self.pick("question")
            self._log(f"[PermissionRequest] question dialog -> {bark!r}")
            self.try_bark(bark)
            return True
        group = self.templates.CATEGORIES.get("question") or {}
        candidates = [f"{lead} {tail}" for lead in group.get("lead", [])
                      for tail in group.get("tail", [])]
        if not candidates:
            return False
        self._speak_fixed(self._fresh_choice(candidates, state), state,
                          "[PermissionRequest] question dialog")
        return True

    @staticmethod
    def _fresh_choice(candidates, state):
        """A random candidate that is not a near-duplicate of anything said
        recently -- or any candidate, if every one was: the event is news
        even in the same words."""
        from phrase_gen import is_near_duplicate
        recent = state.get("recent_voiced", [])
        fresh = [c for c in candidates if not is_near_duplicate(c, recent)]
        return random.choice(fresh or candidates)

    def _speak_fixed(self, phrase, state, label):
        """Speak a template chosen for a fixed-meaning event on a realtime
        engine, recording it so the next choice rotates past it."""
        from session_state import record_voiced, save_session
        record_voiced(state, phrase)
        save_session(state)
        self._log(f"{label} -> {phrase!r}")
        self.speak_dynamic(phrase, allow_cold_start=False)

    def _say_with_volume(self, text):
        """Speak via macOS `say` honoring self.volume.

        `say` has no volume flag, so render to a temp AIFF and play with
        `afplay -v`. Temp file is cleaned up after playback.
        """
        import shlex
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            prefix="hobson-say-", suffix=".aiff", delete=False
        )
        tmp.close()
        afplay_cmd = " ".join(shlex.quote(a) for a in self._afplay_args(tmp.name))
        cmd = (
            f"say -v {shlex.quote(self._say_voice)} "
            f"-o {shlex.quote(tmp.name)} {shlex.quote(text)} && "
            f"{afplay_cmd}; "
            f"rm -f {shlex.quote(tmp.name)}"
        )
        subprocess.Popen(["sh", "-c", cmd])

    @property
    def templates(self):
        """Lazy-load the templates module."""
        if self._templates is None:
            import importlib
            self._templates = importlib.import_module(self.templates_module)
        return self._templates

    def cached_audio(self, text):
        """Return path to cached audio file if it exists, else None."""
        if not self.cache_dir or not self.cache_ext:
            return None
        path = os.path.join(self.cache_dir, f"{bark_hash(text)}.{self.cache_ext}")
        return path if os.path.isfile(path) else None

    @abstractmethod
    def backfill(self, text):
        """Spawn background process to generate audio for a cache miss."""
        pass

    def _pick_prefer_cached(self, candidates):
        """Pick randomly from cached candidates if any exist, else random from all."""
        if self.cache_dir:
            cached = [t for t in candidates if self.cached_audio(t)]
            if cached:
                return random.choice(cached)
        return random.choice(candidates)

    def pick(self, category):
        t = self.templates
        group = t.CATEGORIES.get(category, t.CATEGORIES["done"])
        if self.cache_dir:
            all_barks = [f"{l} {t_}" for l in group["lead"] for t_ in group["tail"]]
            return self._pick_prefer_cached(all_barks)
        return f"{random.choice(group['lead'])} {random.choice(group['tail'])}"

    def pick_permission(self, tool):
        t = self.templates
        tool = format_tool_name(tool)
        if self.cache_dir:
            all_barks = [
                f"{l} {a.format(tool=tool)}"
                for l in t.PERMISSION_LEADS for a in t.PERMISSION_ACTIONS
            ]
            cached = [b for b in all_barks if self.cached_audio(b)]
            if cached:
                return random.choice(cached)
            return self._pick_prefer_cached(t.GENERIC_PERMISSION_PHRASES)
        else:
            # No cache (say engine) — skip MCP/internal tool names
            if "_" in tool:
                return random.choice(t.GENERIC_PERMISSION_PHRASES)
            return f"{random.choice(t.PERMISSION_LEADS)} {random.choice(t.PERMISSION_ACTIONS).format(tool=tool)}"

    def pick_notification(self):
        return self._pick_prefer_cached(self.templates.NOTIFICATION_TEMPLATES)

    def try_bark(self, bark, lock_file=None, cooldown=None, do_backfill=True):
        """Speak bark via cached audio or fallback to macOS say.

        Priority: cached audio + afplay > macOS say > silent
        On cache miss: speak via say immediately, backfill cache in background.

        Optional params for commentary: separate lock_file, different cooldown,
        and do_backfill=False to skip cache backfill for dynamic phrases.
        """
        lock_file = lock_file or BARK_LOCK_FILE
        cooldown = cooldown if cooldown is not None else self.cooldown

        fd = None
        try:
            fd = open(lock_file, "a+", encoding="utf-8")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            fd.seek(0)
            content = fd.read().strip()
            now = time.time()

            if content:
                try:
                    if now - float(content) < cooldown:
                        self._log(f"skipped (cooldown) -> {bark!r}")
                        return
                except ValueError:
                    pass

            fd.seek(0)
            fd.truncate()
            fd.write(str(now))
            fd.flush()

            audio = self.cached_audio(bark)
            if audio:
                self._afplay(audio)
                self._log(f"barked (template-cache) -> {bark!r}")
            else:
                self._say_with_volume(bark)
                if self.cache_dir:
                    self._log(f"barked (say-fallback: cache miss) -> {bark!r}")
                else:
                    self._log(f"barked (say) -> {bark!r}")
                if do_backfill:
                    self.backfill(bark)

        except BlockingIOError:
            self._log(f"skipped (locked) -> {bark!r}")
        except Exception as e:
            self._log(f"bark failed ({e}) -> {bark!r}")
        finally:
            if fd is not None:
                try:
                    fd.close()
                except Exception:
                    pass

    def _is_event_enabled(self, hook_input):
        """Check if this hook event is enabled in config. Returns False + logs if disabled."""
        event = hook_input.get("hook_event_name")
        event_key = self.EVENT_MAP.get(event)
        if event_key and event_key not in self.enabled_events:
            self._log(f"skipped (event {event_key!r} disabled)")
            return False
        return True

    # ── Dynamic phrase playback (overridable seam) ─────────────────────

    def speak_dynamic(self, phrase, allow_cold_start=True):
        """Speak a dynamic, non-cacheable phrase.

        Default: macOS `say` with the personality voice, gated by the bark
        lock to avoid overlapping other engines' audio. Realtime engines
        override this to route through their daemon (with say as last resort).
        """
        try:
            fd = open(BARK_LOCK_FILE, "a+", encoding="utf-8")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._log(f"skipped (locked) -> {phrase!r}")
                fd.close()
                return
            fd.seek(0); fd.truncate()
            fd.write(str(time.time())); fd.flush(); fd.close()
        except OSError:
            pass
        self._say_with_volume(phrase)
        self._log(f"barked (say-dynamic) -> {phrase!r}")

    # ── Commentary (PreToolUse) — shared across all engines ────────────

    def _acquire_commentary_lock(self):
        """Acquire the commentary lock + check cooldown. Returns True on success."""
        try:
            fd = open(COMMENTARY_LOCK_FILE, "a+", encoding="utf-8")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.seek(0)
            content = fd.read().strip()
            if content:
                try:
                    if time.time() - float(content) < self.commentary_cooldown:
                        fd.close()
                        return False
                except ValueError:
                    pass
            fd.seek(0); fd.truncate()
            fd.write(str(time.time())); fd.flush(); fd.close()
            return True
        except (BlockingIOError, OSError):
            return False

    def _pick_chatty_commentary(self, tool, tool_input):
        """Build a chatty commentary phrase from a template + extracted context."""
        tool_lower = (tool or "").lower()
        ctx = extract_context(tool, tool_input or {})
        templates = COMMENTARY_TEMPLATES.get(tool_lower)
        if templates and ctx:
            return random.choice(templates).format(ctx=tts_normalize(ctx))
        return random.choice(COMMENTARY_GENERIC).format(tool=format_tool_name(tool))

    def _is_subagent_event(self, hook_input):
        """True when this event came from a subagent rather than the main session.

        Claude Code stamps `agent_id`/`agent_type` on events originating in a
        subagent; main-session events carry neither. Measured: ~51 PreToolUse
        events per minute during background worker activity.
        """
        return bool(hook_input.get("agent_id"))

    def _handle_commentary(self, hook_input):
        """PreToolUse commentary — verbosity-aware dispatch shared by all engines.

        - terse  : LLM-gated with SKIP-heavy bias (current behavior)
        - normal : LLM-gated with relaxed bias (voice meaningful actions)
        - chatty : no LLM call; pick a template phrase and speak it
        """
        tool = hook_input.get("tool_name", "")
        if (tool or "").lower() not in self.commentary_tools:
            return

        if (self.config.get("commentary", {}).get("suppress_subagents", True)
                and self._is_subagent_event(hook_input)):
            self._log("[PreToolUse] skipped (subagent "
                      f"{hook_input.get('agent_type') or 'unknown'})")
            return

        verbosity = self.commentary_verbosity

        if verbosity == "chatty":
            # chatty speaks immediately on every call — no batching, so the
            # lock keeps its original job of gating speech directly.
            if not self._acquire_commentary_lock():
                self._log("[PreToolUse] skipped (commentary cooldown)")
                return
            tool_input = hook_input.get("tool_input", {})
            phrase = self._pick_chatty_commentary(tool, tool_input)
            self._log(f"[PreToolUse] chatty template -> {phrase!r}")
            self.speak_dynamic(phrase, allow_cold_start=False)
            return

        # terse / normal — append-to-queue must always succeed, so the lock
        # is acquired only once should_flush() opens the gate (inside
        # _handle_commentary_llm), never here.
        self._handle_commentary_llm(hook_input, verbosity)

    def _custom_phrase_prompt(self):
        """Optional per-engine custom persona prompt passed to phrase_gen.

        Override in subclasses to inject a per-engine persona prompt.
        """
        return None

    def _handle_commentary_llm(self, hook_input, verbosity):
        """Append this tool call to the pending batch; flush (LLM + speak)
        once the substance gate opens.

        Queueing must always succeed — the commentary lock is acquired only
        once should_flush() says the batch has earned an utterance, so it
        gates the flush/speak path, never the append path. Otherwise the
        lock would silently drop tool calls out of the batch instead of
        merely delaying the announcement of them.
        """
        try:
            import gate
            from phrase_gen import generate_or_skip, last_raw  # noqa: F401
            from session_state import (
                load_session, save_session,
                record_voiced, record_skipped, build_session_context,
                seconds_since_last_voiced,
                record_pending, should_flush, take_pending, pending_summary,
                record_flush_attempt,
                record_fingerprint, mark_repeat_announced, anomaly_reason,
                action_identity,
                PENDING_STALE_SECONDS,
            )
        except ImportError as e:
            self._log(f"[PreToolUse] LLM gating unavailable ({e})")
            return

        project = derive_project_label()
        state = load_session(project)
        # Every handled event marks the session alive — the watchdog reads
        # this to notice when nothing has moved for a while (Task 9). No
        # extra I/O: this state was already being loaded and saved.
        state["last_event_time"] = time.time()

        tool = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input") or {}
        context = extract_context(tool, tool_input)

        cfg = self.config.get("commentary", {})
        min_calls = cfg.get("min_tool_calls", 5)
        min_seconds = cfg.get("min_seconds", 60.0)
        stale = cfg.get("pending_stale_seconds", PENDING_STALE_SECONDS)

        record_pending(state, tool, context)

        anomaly = None

        if verbosity == "anomaly":
            # Count/timer gates are ignored entirely in anomaly mode — the
            # only reasons to speak are: something is repeated, or a context
            # is being touched for the first time. Repetition is judged on
            # the action itself (action_identity), not on `context`, the
            # short display string: two commands sharing a description are
            # two actions, not one repeated.
            identity = action_identity(tool, tool_input)
            record_fingerprint(state, tool, identity)
            anomaly = anomaly_reason(state, tool, context, cfg, identity=identity)
            if not anomaly:
                save_session(state)
                self._log(f"[PreToolUse] queued (anomaly, nothing notable) {tool}: {context}")
                return
        elif not should_flush(state, min_calls=min_calls, min_seconds=min_seconds):
            save_session(state)
            self._log(f"[PreToolUse] queued ({len(state['pending'])} pending) {tool}: {context}")
            return

        if not self._acquire_commentary_lock():
            # Batch has earned an utterance, but another flush spoke too
            # recently. Leave the queue intact — do not consume it — so the
            # next tool call gets another chance to flush it.
            save_session(state)
            self._log("[PreToolUse] flush ready but commentary locked; deferred")
            return

        # The flush is going ahead now — time the gate off this attempt, not
        # off whether it ends up producing speech (see record_flush_attempt).
        record_flush_attempt(state)
        self._maybe_start_watchdog(baseline=state["last_event_time"])

        items = take_pending(state, stale_seconds=stale)
        event_detail = pending_summary(items)
        if not event_detail:
            save_session(state)
            self._log("[PreToolUse] flush found only stale items, dropped")
            return

        if anomaly:
            if not anomaly.startswith("first time"):
                # Mark before generating, so a failed generation cannot loop:
                # a repeat must not re-fire on every further occurrence of
                # the same action.
                mark_repeat_announced(state, tool, identity)
            event_detail = f"{anomaly[0].upper()}{anomaly[1:]}. {event_detail or ''}".strip()

        # phrase_gen has no "anomaly" prompt bias (it would fall back to the
        # SKIP-heavy terse rules) — by the time we've forced a flush here,
        # the gating decision is already made, so use the relaxed "normal"
        # bias for the phrasing call itself rather than risk the model
        # SKIPping something we deliberately decided is worth voicing.
        gen_verbosity = "normal" if verbosity == "anomaly" else verbosity

        p_worth = None
        if not anomaly:
            # With the decider on "jev", whether a released batch is worth
            # hearing is its call, against commentary.chattiness, not
            # Ollama's; terse and normal then differ in nothing. No opinion
            # (backend local, any failure) leaves the verbosity's own bias.
            p_worth = gate.worth_probability(event_detail, self.config)
            if p_worth is not None:
                bar = gate.threshold(cfg.get("chattiness", gate.DEFAULT_CHATTINESS))
                if p_worth < bar:
                    record_skipped(state, "PreToolUse")
                    save_session(state)
                    self._log(f"[PreToolUse] batch of {len(items)} held back "
                              f"(decider worth={p_worth:.2f} < {bar:.2f})")
                    return
                gen_verbosity = "decided"

        session_context = build_session_context(state)

        category, phrase = generate_or_skip(
            "PreToolUse", event_detail, session_context,
            project=project, model=self._ollama_model,
            ollama_url=self._ollama_url,
            # ponytail: must exceed a cold model load (~8s for llama3.2:3b) —
            # a shorter timeout aborts the load, Ollama cancels it, and every
            # later call is cold again, so commentary never recovers. Hooks are
            # async so nothing user-facing blocks on this.
            timeout=20,
            verbosity=gen_verbosity,
            custom_prompt=self._custom_phrase_prompt(),
            recent_voiced=state.get("recent_voiced", []),
            seconds_since_last_voiced=seconds_since_last_voiced(state),
        )

        if phrase:
            record_voiced(state, phrase)
            save_session(state)
            decided = "" if p_worth is None else f", worth={p_worth:.2f}"
            self._log(f"[PreToolUse] batch of {len(items)} "
                      f"({self._ollama_model}, {verbosity}{decided}) -> {category} -> {phrase!r}")
            self.speak_dynamic(phrase, allow_cold_start=False)
        else:
            record_skipped(state, "PreToolUse")
            save_session(state)
            from phrase_gen import last_raw as raw
            verdict, detail = describe_generation_failure(raw)
            self._log(f"[PreToolUse] batch of {len(items)} {verdict}: {detail}")

    def _stop_still_working(self, state):
        """A Stop that ended waiting on the agent's own work (works_on_its_own):
        recorded as "working", so the idle prompt after it nudges nobody, and
        not announced -- it is not a finish, and saying one was buried the
        real finish among them."""
        from session_state import record_skipped, save_session
        state["last_stop_category"] = "working"
        record_skipped(state, "Stop")
        save_session(state)
        self._log(STILL_WORKING_LOG)

    def _drop_pending_on_stop(self, state):
        """Discard any queued commentary — a finished turn supersedes it.

        A mid-work remark spoken after Stop is stale by construction: "a
        delayed glance is a wrong glance." Takes an already-loaded session
        state (mutated in place); the caller is responsible for saving it.
        Shared by BaseEngine.run() and both realtime engines' run()
        overrides, which each reach the Stop event through their own path.

        Note: take_pending(state, stale_seconds=0.0)'s return value is the
        *fresh* (non-stale) items — with a 0.0 threshold that is always
        empty in practice, since any nonzero time elapsed since queueing
        makes an item "stale". It still unconditionally empties
        state["pending"], which is the actual effect wanted here (Stop
        discards everything, it does not summarise the batch) — but the
        count for logging has to be read before the call, not from its
        return value.
        """
        try:
            from session_state import take_pending
        except ImportError:
            return
        count = len(state.get("pending") or [])
        take_pending(state, stale_seconds=0.0)
        if count:
            self._log(f"[Stop] dropped {count} pending commentary items")

    def _describe_event(self, hook_input):
        """Build a description string for the current event."""
        event = hook_input.get("hook_event_name", "")

        if event == "Stop":
            transcript = find_transcript(hook_input)
            if transcript:
                return last_transcript_context(transcript, max_turns=4)
            return None

        if event == "PermissionRequest":
            tool = format_tool_name(hook_input.get("tool_name") or "something")
            return f"Tool: {tool}"

        if event == "PreToolUse":
            tool = hook_input.get("tool_name", "")
            tool_input = hook_input.get("tool_input", {})
            context = extract_context(tool, tool_input)
            return f"{tool}: {context}" if context else f"Using {tool}"

        if event == "Notification":
            ntype = hook_input.get("notification_type") or ""
            label = NOTIFICATION_LABELS.get(ntype)
            message = hook_input.get("message") or ""
            if label:
                return f"Status: {label}"
            return f"Message: {message or 'attention_required'}"

        return None

    def run(self, hook_input):
        """Main dispatch — called by hobson.py entrypoint."""
        if not self._is_event_enabled(hook_input):
            return

        event = hook_input.get("hook_event_name")

        if event == "PreToolUse":
            return self._handle_commentary(hook_input)

        if event == "PermissionRequest":
            if self._speak_risky_permission(hook_input) or self._speak_question(hook_input):
                return
            tool = hook_input.get("tool_name") or "something important"
            bark = self.pick_permission(tool)
            self._log(f"permission ({tool!r}) template -> {bark!r}")
            self.try_bark(bark)
            return

        if event == "Notification":
            self._maybe_nudge_from_notification(hook_input)
            if self._idle_after_finished_turn(hook_input):
                self._log("notification: not announced — the last turn finished")
                return
            bark = self.pick_notification()
            self._log(f"notification template -> {bark!r}")
            self.try_bark(bark)
            return

        # Stop event — a finished turn supersedes any queued mid-work
        # commentary, so drop it before classifying the assistant's message.
        try:
            from session_state import load_session, save_session
            pending_state = load_session(derive_project_label())
            pending_state["last_event_time"] = time.time()
            pending_state["last_stop_time"] = time.time()
            self._drop_pending_on_stop(pending_state)
            save_session(pending_state)
        except Exception as e:
            self._log(f"[Stop] pending-drop skipped ({e})")

        classified = None
        transcript = find_transcript(hook_input)
        text = last_assistant_text(transcript) if transcript else None
        if text and awaits_developer(text):
            classified = "question"
        elif text and works_on_its_own(text):
            self._record_stop_category("working")
            self._log(STILL_WORKING_LOG)
            return
        elif text:
            classified = classify(text, engine_tag=self.engine_name,
                                  model=self._ollama_model,
                                  ollama_url=self._ollama_url)
        self._record_stop_category(classified)
        category = classified or "done"
        bark = self.pick(category)

        self._log(f"template ({category}) -> {bark!r}")
        self.try_bark(bark)
