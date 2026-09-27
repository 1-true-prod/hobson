"""decider.py — the seam for hobson's optional remote decision model (Jev),
and the one place where anything Hobson knows leaves the Mac for it.

Jev is a non-generative classifier reached through OpenRouter's Decisions
API. It answers a typed question with a calibrated probability, instead of
generating text. Hobson asks it one shape, a choice, through probability(),
which dispatches on config["decider"]["backend"]:

  - "local" (the default): return None, i.e. "no opinion". Callers keep
    whatever local heuristic they already have. This module never
    reimplements that heuristic (e.g. Ollama classification) itself.
  - "jev": make the HTTP call below.

Three questions are asked today. phrase_gen._p_restates: does this phrase
restate one just spoken? (releases a phrase the near-duplicate guard
rejected, or blocks commentary it missed as reworded).
risk.remote_destructive_probability: is this shell command destructive?
(only for a Bash permission request the local rules cannot place). And
gate.worth_probability: is this commentary batch worth hearing? (once per
flush, on the batch summary). With the default config this module makes
zero network calls.

## What may leave

A question's state is a list of parts. A plain str is Hobson's own framing,
sent as written. Anything from the session is wrapped by its kind --
Command, Summary, Phrase -- and redact() runs on it here, to the length its
question was calibrated on: heredoc bodies and long quoted text dropped;
URLs, hosts, IPs, emails, logins, secrets, long tokens and all but the last
component of every path replaced. Redaction used to be each caller's job,
and the duplicate check never did it: spoken phrases went out as spoken.
The key goes only to an https endpoint (or plain http on loopback, for a
local stand-in).

Fails closed, always. Every failure path -- no key, timeout, URLError,
non-200, unparseable body, a response shape that doesn't match what we
expect -- logs one line and returns None so the caller falls back to its own
local result. This must never raise into a caller: a hook that hangs or
crashes is far worse than one that stays quiet.

## Wire format (confirmed against the live API, 2026-09-22)

    POST https://openrouter.ai/api/alpha/decisions
    Authorization: Bearer $OPENROUTER_API_KEY

    {"model": "typesafe/jev-1.13", "state": "<context>",
     "questions": {"answer": {"type": ..., "instructions": ..., "criteria": ...}}}

`instructions` is required on every question. `criteria` is a record
{key: description} for choice, an array of ordered levels for score, and
absent for noul (whose `instructions` is the yes/no statement itself).

Responses key the per-question result by the question name, and the value
by the question TYPE, not by "answer":

    noul   -> {"type": "noul",   "noul": 0.27}
    choice -> {"type": "choice", "choice": "progress",
               "probabilities": {...}, "confidence": 0.66}
    score  -> {"type": "score",  "score": 0.98, "legend": {"0": ...},
               "probabilities": {...}, "confidence": 0.96}

Note noul carries NO confidence field -- only choice and score do. An
earlier version of this module required confidence on all three, which
made every noul call fail parsing and fall back to local silently, which
is indistinguishable from working. check_key() sends a noul; nothing reads
its confidence.

## Key handling

Read from the OPENROUTER_API_KEY environment variable, falling back to
~/.claude/hobson.env parsed as simple KEY=value lines. Never read from, or
written to, ~/.claude/hobson.json -- that file is printed verbatim by
`hobson config show`. Never logged, in whole or in part.
"""

import json
import os
import re
import time
from collections import namedtuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import home
import log_record

# Set once the "no key" warning has been logged, so a standing condition is
# reported once per process rather than on every decision.
_warned_no_key = False

# Fallbacks for a config that lacks a key, read from DEFAULT_CONFIG so they
# cannot drift from it: the timeout here once said 2000 while the config
# said 4000.
_DEFAULTS = home.DEFAULT_CONFIG["decider"]
DEFAULT_ENDPOINT = _DEFAULTS["endpoint"]
DEFAULT_MODEL = _DEFAULTS["model"]
DEFAULT_TIMEOUT_MS = _DEFAULTS["timeout_ms"]

# Session text, by kind (see "What may leave" above), and how much of each
# may go: 600 covers 99% of real commands once redacted, 220 is what the
# commentary gate was calibrated on, and a spoken phrase is a dozen words.
Command = namedtuple("Command", ["text"])
Summary = namedtuple("Summary", ["text"])
Phrase = namedtuple("Phrase", ["text"])
_LIMITS = {Command: 600, Summary: 220, Phrase: 200}


def find_key():
    """OPENROUTER_API_KEY from the environment, else ~/.claude/hobson.env.

    Never reads hobson.json. Returns None, never an empty string, when no
    key is found.
    """
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        with open(home.env_file(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "OPENROUTER_API_KEY":
                    v = v.strip().strip('"').strip("'")
                    return v or None
    except OSError:
        pass
    return None


# ── Redaction: what may leave the machine ──────────────────────────────────

_TLDS = r"(?:com|net|org|io|dev|ai|co|app|cloud|internal|local|lan|corp|xyz|us|uk|de|eu)"
_REDACTIONS = [
    (re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+"), "<url>"),
    (re.compile(r"(?i)\bbearer\s+[^\s'\"]+"), "Bearer <secret>"),
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|cookie|x-auth-token)\s*:\s*[^'\"\n]+"),
     r"\1: <secret>"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|API_?KEY|ACCESS_?KEY"
                r"|PRIVATE_?KEY|AUTH|CREDENTIALS?|SESSION|COOKIE)[A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1=<secret>"),
    # The same names as a separate argument: `aws configure set aws_secret_access_key X`.
    (re.compile(r"(?i)\b(aws_secret_access_key|aws_session_token)\s+(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1 <secret>"),
    (re.compile(r"(?i)(--?(?:token|password|passwd|pass|secret|api-?key|key|auth|access-key"
                r"|private-key|client-secret)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)"), r"\1<secret>"),
    # Short flags that take a password: mysql's glued -pSECRET, and -p SECRET
    # after sshpass or a registry login. Anywhere else -p is a port or a parent.
    (re.compile(r"(\b(?:mysql\w*|mariadb\w*)\b[^|;&\n]*?\s-p)(?=[^\s-])(\"[^\"]*\"|'[^']*'|\S+)"), r"\1<secret>"),
    (re.compile(r"(\b(?:sshpass|(?:docker|podman|helm|oras|skopeo)\s+(?:\S+\s+)*?login\b[^|;&\n]*?)"
                r"\s-p\s*)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1<secret>"),
    # user:password handed to -u / --user (curl, wget): a bare -u NAME is left alone.
    (re.compile(r"(\s(?:-u|--user)(?:=|\s+))(?:\"[^\"]*:[^\"]*\"|'[^']*:[^']*'|[^\s:]*:\S+)"), r"\1<secret>"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"
                r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_\w{20,}"
                r"|\bxox[abprs]-[\w-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[\w-]{30,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+"),
     "<secret>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    # scp/ssh's user@host: where the host has no dot, so the email rule missed it.
    (re.compile(r"[\w.+-]+@[\w-]+(?=:)"), "<login>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+" + _TLDS + r"\b(?::\d+)?"), "<host>"),
    (re.compile(r"\b[0-9a-f]{24,}\b|\b[A-Za-z0-9+/_-]{32,}={0,2}"), "<token>"),
    # Any path keeps only its last component, which says what is touched;
    # the directories above it say whose machine and which project. Not an
    # Edit's line count, "+3/-2", which is no path.
    (re.compile(r"(?<![\w.@%+*-])(?!\+\d+/-\d+\b)"
                r"(?:~|\$\{?\w+\}?)?(?:\.{0,2}/)?(?:[\w.@%+-]+/)+([\w.@%+*-]+)"), r"<path>/\1"),
]
_LONG_QUOTED = re.compile(r"'[^']{60,}'|\"(?:[^\"\\]|\\.){60,}\"")
# Group 3 is the rest of the opening line: `cat <<EOF && redis-cli FLUSHALL`
# must still show the FLUSHALL.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?\n\s*\2\s*(?:\n|$)", re.S)


def redact(text, limit=600):
    """`text` as it may be shown to a third party, at most `limit`
    characters. Truncating a command at 300 first hid the tail, which is
    where a destructive step in a compound command sits. Heredoc bodies are
    always dropped, even ones an interpreter reads: a script can contain
    anything, so it never leaves the machine."""
    text = _HEREDOC.sub(lambda m: "<<heredoc" + m.group(3) + "\n", text or "")
    text = _LONG_QUOTED.sub("<text>", text)
    for rx, replacement in _REDACTIONS:
        text = rx.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + " …"


def _render(state):
    """The state as sent: framing as written, session text redacted by its kind."""
    out = []
    for part in state:
        if isinstance(part, str):
            out.append(part)
            continue
        limit = _LIMITS.get(type(part))
        if limit is None:
            raise TypeError(f"decider state part must be framing (str) or session text "
                            f"(Command, Summary, Phrase), not {type(part).__name__}")
        out.append(redact(part.text, limit))
    return "".join(out)


# ── The request ──────────────────────────────────────────────────────────


def _endpoint_ok(url):
    """Whether the key may go to `url`: https, or http to this machine only."""
    parts = urlsplit(url or "")
    return parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"))


def _post(payload, key, endpoint, timeout):
    """The one request that carries the key. Returns the parsed body; raises
    whatever urlopen and json raise."""
    req = Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def check_key(key, config=None):
    """One real call with `key`, whatever the backend: does it work?

    For the setup wizard, which tests a key before it writes backend "jev",
    and so has to say *why* one failed, where _ask only falls back. A noul
    costs about $0.000017. Returns {"ok": True, "ms": .., "cost": ..} or
    {"ok": False, "reason": ..}. Never raises; never logs the key.
    """
    if not key:
        return {"ok": False, "reason": "no key"}
    decider_cfg = _decider_config(config)
    if not _endpoint_ok(decider_cfg["endpoint"]):
        return {"ok": False, "reason": "the endpoint is not https"}
    payload = {"model": decider_cfg["model"], "state": "Hobson's setup is checking this key.",
               "questions": {"answer": {"type": "noul", "instructions": "This is a connection test."}}}
    started = time.monotonic()
    try:
        # Longer than a hook's budget: someone is waiting on this one, and a
        # slow first handshake should not read as a bad key.
        body = _post(payload, key, decider_cfg["endpoint"],
                     max(8.0, decider_cfg["timeout_ms"] / 1000.0))
    except HTTPError as exc:
        return {"ok": False, "reason": _HTTP_REASONS.get(exc.code, f"HTTP {exc.code}")}
    except (URLError, OSError) as exc:
        return {"ok": False, "reason": f"no connection ({type(exc).__name__})"}
    except ValueError:
        return {"ok": False, "reason": "the answer was not JSON"}
    ms = int((time.monotonic() - started) * 1000)
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict) or not isinstance(answers.get("answer"), dict):
        return {"ok": False, "reason": "an answer in a shape Hobson doesn't know"}
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    cost = usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None
    log_record.write(f"[decider] key check model={decider_cfg['model']} ok in {ms}ms"
                     + (f" cost=${cost:.6f}" if cost is not None else ""))
    return {"ok": True, "ms": ms, "cost": cost}


_HTTP_REASONS = {
    401: "the key was refused (401)",
    402: "the account has no credit (402)",
    403: "the key may not use this model (403)",
    429: "rate limited (429): try again in a moment",
}


def _decider_config(config):
    """Resolve the decider sub-config, defaulting anything the caller left out.

    Accepts a full hobson config dict (as load_config() returns) or None,
    in which case load_config() is called. Never mutates the config passed
    in -- load_config()'s DEFAULT_CONFIG is a shared object across calls.
    """
    config = config if config is not None else home.load_config()
    decider_cfg = config.get("decider") or {}
    return {
        "backend": decider_cfg.get("backend", "local"),
        "model": decider_cfg.get("model", DEFAULT_MODEL),
        "timeout_ms": decider_cfg.get("timeout_ms", DEFAULT_TIMEOUT_MS),
        "endpoint": decider_cfg.get("endpoint", DEFAULT_ENDPOINT),
    }


def _ask(state, question, decider_cfg):
    """POST one question about `state` (already rendered) to the Decisions API.

    `question` is the body of a single entry in the "questions" dict (e.g.
    {"type": "choice", "criteria": {...}}). Returns the parsed per-question
    answer dict on success, or None on any failure -- already logged, never
    raised.

    Never logs the key. Never logs the state text -- the log is not the
    place to keep a copy of what left; only the question type, model and
    cost are logged.
    """
    if not _endpoint_ok(decider_cfg["endpoint"]):
        log_record.write("[decider] the endpoint is not https -- falling back to local")
        return None
    key = find_key()
    if not key:
        # Once per process, not once per question. A missing key is a
        # standing condition, not an event: repeating it on every decision
        # would bury the log it shares with everything else hobson reports.
        global _warned_no_key
        if not _warned_no_key:
            _warned_no_key = True
            log_record.write("[decider] jev backend selected but no OPENROUTER_API_KEY "
                             "found (checked env and hobson.env) -- falling back to "
                             "local for the rest of this process")
        return None

    payload = {"model": decider_cfg["model"], "state": state, "questions": {"answer": question}}
    try:
        body = _post(payload, key, decider_cfg["endpoint"], decider_cfg["timeout_ms"] / 1000.0)
    except (URLError, OSError) as exc:
        log_record.write(f"[decider] request failed ({decider_cfg['model']}, "
                         f"type={question.get('type')}): {type(exc).__name__}")
        return None
    except ValueError:
        log_record.write(f"[decider] response body was not valid JSON ({decider_cfg['model']})")
        return None

    if not isinstance(body, dict):
        log_record.write("[decider] response was not a JSON object")
        return None

    answers = body.get("answers")
    if not isinstance(answers, dict):
        answers = body.get("results")
    if not isinstance(answers, dict):
        log_record.write("[decider] response missing an 'answers'/'results' object")
        return None

    answer = answers.get("answer")
    if not isinstance(answer, dict):
        log_record.write("[decider] response missing the 'answer' question's result")
        return None

    # The cost of this call, as the API reports it, goes in the log line so
    # spend to date is recoverable by summing the log -- no separate tally
    # file to drift out of step with what actually happened. `hobson
    # monitor` reads it back out.
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    cost = usage.get("cost")
    cost_note = ""
    if isinstance(cost, (int, float)):
        cost_note = f" cost=${cost:.6f}"
    log_record.write(f"[decider] asked type={question.get('type')} model={decider_cfg['model']} "
                     f"-> confidence={answer.get('confidence')!r}{cost_note}")
    return answer


def probability(label, state, instructions, criteria, config=None):
    """P(`label`), one key of `criteria` (a {key: description} record), for
    `state`: a list of framing strings and Command / Summary / Phrase parts
    (see "What may leave" above).

    Returns a float, or None -- for backend "local" (always), or for backend
    "jev" on any failure or a response without that probability. Callers
    must treat None as "no opinion" and fall back to their own heuristic.
    """
    decider_cfg = _decider_config(config)
    if decider_cfg["backend"] != "jev":
        return None
    answer = _ask(_render(state), {"type": "choice", "instructions": instructions,
                                   "criteria": dict(criteria)}, decider_cfg)
    if answer is None:
        return None
    try:
        return float(answer["probabilities"][label])
    except (KeyError, TypeError, ValueError):
        log_record.write("[decider] malformed choice response")
        return None
