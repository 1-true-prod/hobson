"""decider.py — the seam for hobson's optional remote decision model (Jev).

Jev is a non-generative classifier reached through OpenRouter's Decisions
API. It answers one of three typed question shapes with a calibrated
probability, instead of generating text. This module exposes those three
shapes -- choice(), score(), noul() -- and dispatches on
config["decider"]["backend"]:

  - "local" (the default): return None, i.e. "no opinion". Callers keep
    whatever local heuristic they already have. This module never
    reimplements that heuristic (e.g. Ollama classification) itself.
  - "jev": make the HTTP call below.

Three questions are asked today. phrase_gen._p_restates: does this phrase
restate one just spoken? (releases a phrase the near-duplicate guard
rejected, or blocks commentary it missed as reworded).
risk.remote_destructive_probability: is this shell command destructive?
(only for a Bash permission request the local rules cannot place, and only
ever on the command as risk.redact() leaves it). And gate.worth_probability:
is this commentary batch worth hearing? (once per flush, on the batch
summary as risk.redact() leaves it). With the default config this module
makes zero network calls.

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
is indistinguishable from working. Hence NoulResult.confidence is None.

## Key handling

Read from the OPENROUTER_API_KEY environment variable, falling back to
~/.claude/hobson.env parsed as simple KEY=value lines. Never read from, or
written to, ~/.claude/hobson.json -- that file is printed verbatim by
`hobson config show`. Never logged, in whole or in part.
"""

import json
import os
import time
from collections import namedtuple
from urllib.error import HTTPError, URLError
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

# Three result shapes mirroring Jev's three question types. `None` (not one
# of these) is how every function spells "no opinion" -- local backend, or
# any failure at all.
ChoiceResult = namedtuple("ChoiceResult", ["choice", "probs", "confidence"])
ScoreResult = namedtuple("ScoreResult", ["score", "legend", "probs", "confidence"])
# noul returns no confidence -- see the wire-format note above.
NoulResult = namedtuple("NoulResult", ["probability", "confidence"])


def _find_key():
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
    payload = {"model": decider_cfg["model"], "state": "Hobson's setup is checking this key.",
               "questions": {"answer": {"type": "noul", "instructions": "This is a connection test."}}}
    req = Request(decider_cfg["endpoint"], data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    started = time.monotonic()
    try:
        # Longer than a hook's budget: someone is waiting on this one, and a
        # slow first handshake should not read as a bad key.
        with urlopen(req, timeout=max(8.0, decider_cfg["timeout_ms"] / 1000.0)) as resp:
            body = json.loads(resp.read())
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


def _ask(question, decider_cfg):
    """POST one question to the Decisions API.

    `question` is the body of a single entry in the "questions" dict (e.g.
    {"type": "choice", "options": [...]}) plus a "state" key already mixed
    in by the caller. Returns the parsed per-question answer dict on
    success, or None on any failure -- already logged, never raised.

    Never logs the key. Never logs the state text -- it is whatever the
    caller chose to send, and the log is not the place to keep a copy; only
    the question type, model and cost are logged.
    """
    state = question.pop("state")
    key = _find_key()
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

    payload = {
        "model": decider_cfg["model"],
        "state": state,
        "questions": {"answer": question},
    }
    req = Request(
        decider_cfg["endpoint"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    timeout = decider_cfg["timeout_ms"] / 1000.0

    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (URLError, OSError) as exc:
        log_record.write(f"[decider] request failed ({decider_cfg['model']}, "
                         f"type={question.get('type')}): {type(exc).__name__}")
        return None
    except json.JSONDecodeError:
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


def _probs(answer):
    """Per-option probabilities. Absent on noul, present on choice/score."""
    return dict(answer.get("probabilities") or {})


def choice(state, instructions, criteria, config=None):
    """Ask Jev to pick one key of `criteria` (a {key: description} record).

    Returns a ChoiceResult, or None -- for backend "local" (always), or for
    backend "jev" on any failure or malformed response. Callers must treat
    None as "no opinion" and fall back to their own heuristic.
    """
    decider_cfg = _decider_config(config)
    if decider_cfg["backend"] != "jev":
        return None

    answer = _ask({"state": state, "type": "choice",
                   "instructions": instructions,
                   "criteria": dict(criteria)}, decider_cfg)
    if answer is None:
        return None
    try:
        chosen = answer["choice"]
        confidence = float(answer["confidence"])
    except (KeyError, TypeError, ValueError):
        log_record.write("[decider] malformed choice response")
        return None
    return ChoiceResult(choice=chosen, probs=_probs(answer), confidence=confidence)


def score(state, instructions, criteria, config=None):
    """Ask Jev to place `state` on `criteria` (an ordered list of levels).

    `score` comes back as a fractional position across those levels, with a
    `legend` mapping index -> label. Returns a ScoreResult, or None -- see
    choice() for when.
    """
    decider_cfg = _decider_config(config)
    if decider_cfg["backend"] != "jev":
        return None

    answer = _ask({"state": state, "type": "score",
                   "instructions": instructions,
                   "criteria": list(criteria)}, decider_cfg)
    if answer is None:
        return None
    try:
        position = float(answer["score"])
        confidence = float(answer["confidence"])
    except (KeyError, TypeError, ValueError):
        log_record.write("[decider] malformed score response")
        return None
    return ScoreResult(score=position, legend=dict(answer.get("legend") or {}),
                       probs=_probs(answer), confidence=confidence)


def noul(state, instructions, config=None):
    """Ask Jev a yes/no question: `instructions` is the statement to judge.

    Returns a NoulResult whose .probability is P(true). **confidence is
    always None** -- the API does not return one for this question type, and
    requiring it here is what made an earlier version of this module fall
    back to local on every single call while looking like it worked.
    """
    decider_cfg = _decider_config(config)
    if decider_cfg["backend"] != "jev":
        return None

    answer = _ask({"state": state, "type": "noul",
                   "instructions": instructions}, decider_cfg)
    if answer is None:
        return None
    try:
        probability = float(answer["noul"])
    except (KeyError, TypeError, ValueError):
        log_record.write("[decider] malformed noul response")
        return None
    return NoulResult(probability=probability, confidence=None)
