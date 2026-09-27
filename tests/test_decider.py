"""decider.py: local-by-default seam, jev backend, and fail-closed behaviour.

The suite stays offline: fake_decider (tests/conftest.py) patches
decider.urlopen so nothing here ever makes a real request.
"""

import json

import pytest

import decider
import home

# A synthetic, obviously-fake token -- not a real secret. Used only to prove
# it never reaches a log line (test_key_never_appears_in_logs below).
FAKE_KEY = "sk-or-FAKE-TEST-TOKEN-0000000000000000"


def _jev_config(**overrides):
    cfg = dict(home.DEFAULT_CONFIG)
    cfg["decider"] = {**cfg["decider"], "backend": "jev", **overrides}
    return cfg


def _log_text(claude_home):
    log_path = claude_home / "hobson.log"
    return log_path.read_text(encoding="utf-8") if log_path.exists() else ""


# ── 1. default config -> local, no HTTP ─────────────────────────────────────

def test_default_backend_is_local_and_makes_no_request(claude_home, fake_decider):
    cfg = home.load_config()
    assert cfg["decider"]["backend"] == "local"

    assert decider.choice("some state", "pick one", {"a": "first", "b": "second"}, config=cfg) is None
    assert decider.score("some state", "rate it", ["Calm", "Angry"], config=cfg) is None
    assert decider.noul("some state", "this is urgent", config=cfg) is None
    assert fake_decider.calls() == 0


# ── 2. jev backend, no key available -> local result, no request ───────────

def test_jev_backend_no_key_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _jev_config()

    result = decider.choice("some state", "pick one", {"a": "first", "b": "second"}, config=cfg)

    assert result is None
    assert fake_decider.calls() == 0
    assert "no OPENROUTER_API_KEY found" in _log_text(claude_home)


def test_key_falls_back_to_hobson_env_file(claude_home, fake_decider, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (claude_home / "hobson.env").write_text(f"OPENROUTER_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    cfg = _jev_config()
    fake_decider.respond({
        "answers": {"answer": {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.9}}
    })

    result = decider.choice("some state", "pick one", {"a": "first", "b": "second"}, config=cfg)

    assert result is not None
    assert result.choice == "a"
    assert fake_decider.calls() == 1


# ── 3. well-formed 200 -> parses answer/probs/confidence for all three shapes

def test_jev_choice_parses_well_formed_response(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.respond({
        "answers": {
            "answer": {
                "type": "choice", "choice": "technical",
                "probabilities": {"billing": 0.12, "technical": 0.88},
                "confidence": 0.81,
            }
        }
    })

    result = decider.choice("customer says nothing loads", "route it", {"billing": "b", "technical": "t"}, config=cfg)

    assert result == decider.ChoiceResult(
        choice="technical", probs={"billing": 0.12, "technical": 0.88}, confidence=0.81
    )


def test_jev_score_parses_well_formed_response(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.respond({
        "answers": {
            "answer": {
                "type": "score", "score": 1.0,
                "probabilities": {"Calm": 0.2, "Concerned": 0.7, "Angry": 0.1},
                "confidence": 0.65,
            }
        }
    })

    result = decider.score("customer message", "rate tone", ["Calm", "Concerned", "Angry"], config=cfg)

    assert result.score == 1.0
    assert result.probs == {"Calm": 0.2, "Concerned": 0.7, "Angry": 0.1}
    assert result.confidence == 0.65


def test_jev_noul_parses_well_formed_response(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.respond({
        "answers": {"answer": {"type": "noul", "noul": 0.73}}
    })

    result = decider.noul("customer message", "this is urgent", config=cfg)

    assert result == decider.NoulResult(probability=0.73, confidence=None)


# ── 4. timeout -> local result, no raise ────────────────────────────────────

def test_jev_timeout_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.error(TimeoutError("timed out"))

    result = decider.noul("state", "statement", config=cfg)

    assert result is None
    assert "request failed" in _log_text(claude_home)


# ── 5. non-200 -> local result, no raise ────────────────────────────────────

def test_jev_non_200_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.http_error(500)

    result = decider.score("state", "rate", ["a", "b"], config=cfg)

    assert result is None
    assert "request failed" in _log_text(claude_home)


# ── 6. malformed/unparseable body -> local result, no raise ────────────────

def test_jev_unparseable_body_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.raw_body(b"not json at all")

    result = decider.choice("state", "pick", {"a": "A", "b": "B"}, config=cfg)

    assert result is None
    assert "not valid JSON" in _log_text(claude_home)


def test_jev_unexpected_shape_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.respond({"unexpected": "shape"})

    result = decider.choice("state", "pick", {"a": "A", "b": "B"}, config=cfg)

    assert result is None
    assert "missing an 'answers'/'results'" in _log_text(claude_home)


def test_jev_missing_field_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    # 'confidence' missing entirely.
    fake_decider.respond({"answers": {"answer": {"type": "choice", "choice": "a"}}})

    result = decider.choice("state", "pick", {"a": "A", "b": "B"}, config=cfg)

    assert result is None
    assert "malformed choice response" in _log_text(claude_home)


# ── 7. the key never appears in any logged line ─────────────────────────────

def test_key_never_appears_in_logs(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()

    # Run one of each failure path plus a success, so every logging call site
    # in decider.py fires at least once.
    fake_decider.respond({
        "answers": {"answer": {"type": "choice", "choice": "a",
                               "probabilities": {}, "confidence": 0.5}}
    })
    decider.choice("state", "pick", {"a": "A", "b": "B"}, config=cfg)

    fake_decider.error(TimeoutError("timed out"))
    decider.noul("state", "statement", config=cfg)

    fake_decider.http_error(500)
    decider.score("state", "rate", ["a", "b"], config=cfg)

    fake_decider.raw_body(b"not json")
    decider.choice("state", "pick", {"a": "A", "b": "B"}, config=cfg)

    fake_decider.respond({"answers": {"answer": {"type": "choice", "choice": "a"}}})  # missing confidence
    decider.choice("state", "pick", {"a": "A", "b": "B"}, config=cfg)

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    decider.choice("state", "pick", {"a": "A", "b": "B"}, config=cfg)  # no-key path

    log_text = _log_text(claude_home)
    assert log_text  # sanity: something was actually logged
    assert FAKE_KEY not in log_text
    # Guard against a partial leak too (e.g. an f-string slicing bug).
    assert FAKE_KEY[:12] not in log_text


# ── 8. load_config() merges the decider block without clobbering overrides ─

def test_load_config_merges_decider_block(claude_home):
    (claude_home / "hobson.json").write_text(
        json.dumps({"decider": {"backend": "jev"}}), encoding="utf-8"
    )

    cfg = home.load_config()

    assert cfg["decider"]["backend"] == "jev"
    # untouched defaults survive the deep-merge
    assert cfg["decider"]["model"] == "typesafe/jev-1.13"
    assert cfg["decider"]["timeout_ms"] == 4000
    assert cfg["decider"]["endpoint"] == "https://openrouter.ai/api/alpha/decisions"


def test_default_config_has_local_decider_backend(claude_home):
    cfg = home.load_config()
    assert cfg["decider"]["backend"] == "local"


def test_decider_config_does_not_mutate_default_config_shared_dict():
    # load_config() does `dict(DEFAULT_CONFIG)` -- a shallow copy that shares
    # sub-dict objects with the module constant. decider._decider_config()
    # must never write into that shared dict.
    before = dict(home.DEFAULT_CONFIG["decider"])
    cfg = home.load_config()
    decider._decider_config(cfg)
    assert home.DEFAULT_CONFIG["decider"] == before


# ── The first real call site: overturning a near-duplicate ────────────────
#
# Jaccard cannot see tense, so "I fixed the invoice sync" reads as a duplicate
# of "I'm fixing the invoice sync" and is silenced -- the completion of a thing
# you just announced starting. The decider is asked only when the local
# guard has already decided to suppress, and can only overturn that.

import phrase_gen  # noqa: E402

_PRIOR = [["I'm fixing the invoice sync.", 0.0]]
_CANDIDATE = "I fixed the invoice sync."


def _jev_cfg(**over):
    cfg = {"backend": "jev", "model": "m", "timeout_ms": 100,
           "endpoint": "http://127.0.0.1:9/x", "dedup_restates_max": 0.5}
    cfg.update(over)
    return {"decider": cfg}


def test_local_backend_leaves_the_rejection_standing(claude_home, fake_decider):
    """Default config must behave exactly as it did before the seam existed."""
    assert phrase_gen._guard_reject_reason(
        "Stop", _CANDIDATE, _PRIOR) == "near-duplicate of a recent phrase"
    assert fake_decider.calls() == 0


def test_confident_yes_overturns_the_rejection(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_decider.respond({"answers": {"answer": {"type": "choice", "choice": "progress", "probabilities": {"restates": 0.02, "progress": 0.97}, "confidence": 0.9}}})
    assert phrase_gen._decider_rescues_duplicate(
        _CANDIDATE, _PRIOR, config=_jev_cfg()) is True


def test_weak_signal_leaves_the_rejection_standing(claude_home, fake_decider, monkeypatch):
    """P(restates) at or above the ceiling is not a yes -- rejection stands."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_decider.respond({"answers": {"answer": {"type": "choice", "choice": "restates", "probabilities": {"restates": 0.94, "progress": 0.05}, "confidence": 0.99}}})
    assert phrase_gen._decider_rescues_duplicate(
        _CANDIDATE, _PRIOR, config=_jev_cfg()) is False


def test_failure_leaves_the_rejection_standing(claude_home, fake_decider, monkeypatch):
    """Every failure path must preserve today's behaviour, not release speech."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    for arm in (lambda: fake_decider.error(OSError("boom")),
                lambda: fake_decider.http_error(500),
                lambda: fake_decider.raw_body(b"not json"),
                lambda: fake_decider.respond({"answers": {"answer": {}}})):
        arm()
        assert phrase_gen._decider_rescues_duplicate(
            _CANDIDATE, _PRIOR, config=_jev_cfg()) is False


def test_no_priors_asks_nothing(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert phrase_gen._decider_rescues_duplicate(
        _CANDIDATE, [], config=_jev_cfg()) is False
    assert fake_decider.calls() == 0


def test_decider_can_never_cause_a_rejection(claude_home, fake_decider, monkeypatch):
    """A phrase the local guard allows is never shown to the decider."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_decider.respond({"answers": {"answer": {"type": "choice", "choice": "unrelated", "probabilities": {"restates": 0.0}, "confidence": 1.0}}})
    assert phrase_gen._guard_reject_reason(
        "Stop", "I deployed the release to staging.",
        [["I'm reviewing an unrelated pull request.", 0.0]]) is None
    assert fake_decider.calls() == 0


def test_uncertain_argmax_still_speaks_when_restates_is_unlikely(
        claude_home, fake_decider, monkeypatch):
    """The case an argmax rule got wrong.

    A real logged completion ("Review of sealed args ... finished") comes
    back argmax=progress at only 0.35 confidence. Keying on the argmax plus
    a confidence floor silenced it; P(restates)=0.44 reads it correctly as
    more likely new than repeated.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    fake_decider.respond({"answers": {"answer": {
        "type": "choice", "choice": "progress",
        "probabilities": {"restates": 0.44, "progress": 0.48, "unrelated": 0.08},
        "confidence": 0.35}}})
    assert phrase_gen._decider_rescues_duplicate(
        _CANDIDATE, _PRIOR, config=_jev_cfg()) is True


def test_missing_probabilities_upholds_the_rejection(
        claude_home, fake_decider, monkeypatch):
    """A response we did not understand must not release speech."""
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    fake_decider.respond({"answers": {"answer": {
        "type": "choice", "choice": "progress", "confidence": 0.9}}})
    assert phrase_gen._decider_rescues_duplicate(
        _CANDIDATE, _PRIOR, config=_jev_cfg()) is False


def test_response_cost_is_logged_for_spend_tracking(claude_home, fake_decider, monkeypatch):
    """The API's own cost figure goes in the log line.

    `hobson monitor` recovers spend to date by summing these, so the log is
    the ledger -- there is no separate tally file that could drift out of
    step with what actually ran.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    fake_decider.respond({
        "answers": {"answer": {"type": "choice", "choice": "a",
                               "probabilities": {"a": 1.0}, "confidence": 0.9}},
        "usage": {"input_tokens": 319, "output_tokens": 20, "cost": 1.3398e-05},
    })
    decider.choice("state", "pick", {"a": "A", "b": "B"}, config=_jev_config())
    assert "cost=$0.000013" in _log_text(claude_home)


def test_missing_usage_block_logs_without_a_cost(claude_home, fake_decider, monkeypatch):
    """A response with no usage block must still log, just without a cost."""
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    fake_decider.respond({
        "answers": {"answer": {"type": "choice", "choice": "a",
                               "probabilities": {"a": 1.0}, "confidence": 0.9}},
    })
    decider.choice("state", "pick", {"a": "A", "b": "B"}, config=_jev_config())
    text = _log_text(claude_home)
    assert "[decider] asked type=choice" in text
    assert "cost=$" not in text
