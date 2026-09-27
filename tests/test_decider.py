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

    assert decider.probability("a", ["some state"], "pick one", {"a": "first", "b": "second"},
                               config=cfg) is None
    assert fake_decider.calls() == 0


# ── 2. jev backend, no key available -> local result, no request ───────────

def test_jev_backend_no_key_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _jev_config()

    result = decider.probability("a", ["some state"], "pick one", {"a": "first", "b": "second"}, config=cfg)

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

    result = decider.probability("a", ["some state"], "pick one", {"a": "first", "b": "second"}, config=cfg)

    assert result == 0.9
    assert fake_decider.calls() == 1


# ── 3. well-formed 200 -> the label's probability, from a choice question

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

    result = decider.probability("technical", ["customer says nothing loads"], "route it",
                                 {"billing": "b", "technical": "t"}, config=cfg)

    assert result == 0.88
    [request] = fake_decider.sent()
    assert request.payload["questions"]["answer"] == {
        "type": "choice", "instructions": "route it", "criteria": {"billing": "b", "technical": "t"}}
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in json.dumps(request.payload), "the key goes in the header only"


# ── 4. timeout -> local result, no raise ────────────────────────────────────

def test_jev_timeout_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.error(TimeoutError("timed out"))

    result = decider.probability("a", ["state"], "statement", {"a": "A"}, config=cfg)

    assert result is None
    assert "request failed" in _log_text(claude_home)


# ── 5. non-200 -> local result, no raise ────────────────────────────────────

def test_jev_non_200_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.http_error(500)

    result = decider.probability("a", ["state"], "rate", {"a": "A", "b": "B"}, config=cfg)

    assert result is None
    assert "request failed" in _log_text(claude_home)


# ── 6. malformed/unparseable body -> local result, no raise ────────────────

def test_jev_unparseable_body_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.raw_body(b"not json at all")

    result = decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=cfg)

    assert result is None
    assert "not valid JSON" in _log_text(claude_home)


def test_jev_unexpected_shape_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    fake_decider.respond({"unexpected": "shape"})

    result = decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=cfg)

    assert result is None
    assert "missing an 'answers'/'results'" in _log_text(claude_home)


def test_jev_missing_field_falls_back_to_local(claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config()
    # 'probabilities' missing entirely.
    fake_decider.respond({"answers": {"answer": {"type": "choice", "choice": "a"}}})

    result = decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=cfg)

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
    decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=cfg)

    fake_decider.error(TimeoutError("timed out"))
    decider.probability("a", ["state"], "statement", {"a": "A"}, config=cfg)

    fake_decider.http_error(500)
    decider.probability("a", ["state"], "rate", {"a": "A", "b": "B"}, config=cfg)

    fake_decider.raw_body(b"not json")
    decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=cfg)

    fake_decider.respond({"answers": {"answer": {"type": "choice", "choice": "a"}}})  # no probabilities
    decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=cfg)

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=cfg)  # no-key path

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
    decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=_jev_config())
    assert "cost=$0.000013" in _log_text(claude_home)


def test_missing_usage_block_logs_without_a_cost(claude_home, fake_decider, monkeypatch):
    """A response with no usage block must still log, just without a cost."""
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    fake_decider.respond({
        "answers": {"answer": {"type": "choice", "choice": "a",
                               "probabilities": {"a": 1.0}, "confidence": 0.9}},
    })
    decider.probability("a", ["state"], "pick", {"a": "A", "b": "B"}, config=_jev_config())
    text = _log_text(claude_home)
    assert "[decider] asked type=choice" in text
    assert "cost=$" not in text


# ── What may leave: redaction, by kind, at the seam ───────────────────────


@pytest.mark.parametrize("command, secret", [
    ("curl -H 'Authorization: Bearer abc123secretvalue' https://api.example.com/v1", "abc123secretvalue"),
    ("API_KEY=sk-live-0123456789abcdefghij npm run deploy", "0123456789abcdefghij"),
    ("GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123 gh pr create", "ghp_abcdef"),
    ("deploy --token=s3cr3t-value-here --env prod", "s3cr3t-value-here"),
    ("mysql --password hunter2hunter2 -u root", "hunter2hunter2"),
    ("aws s3 ls --profile x AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ("curl -H 'x: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_part' localhost", "eyJhbGci"),
    ("git clone https://user:pass@github.com/org/repo.git", "user:pass"),
    ("ssh deploy@10.0.0.12 uptime", "10.0.0.12"),
    ("scp build.zip jane.doe@corp.example.com:/srv", "jane.doe"),
    ("ping db.internal.acme.io", "acme"),
    ("cat /Users/jdoe/Projects/secret-client/notes.txt", "jdoe"),
    ("cat ~/.ssh/id_rsa", ".ssh"),
    # What the first version let through.
    ("mysql -uroot -phunter2 appdb", "hunter2"),
    ("sshpass -p hunter2 ssh deploy@box", "hunter2"),
    ("docker login -u me -p hunter2 registry.example.com", "hunter2"),
    ("curl -u admin:hunter2 https://api.example.com", "hunter2"),
    ("curl --user 'admin:hunter2' localhost:8080", "hunter2"),
    ("stripe listen --api-key rk_live_abcdefghij0123456789", "rk_live_abcdef"),
    ("export KEY=x; echo rk_live_abcdefghij0123456789", "rk_live_abcdef"),
    ("aws configure set aws_secret_access_key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "K7MDENG"),
    ("scp notes.txt user@myhost:/tmp", "user@myhost"),
])
def test_redaction_removes_what_identifies_or_authenticates(command, secret):
    assert secret not in decider.redact(command)


@pytest.mark.parametrize("command", [
    "mkdir -p build/out", "ssh -p 2222 host", "git push -u origin main", "docker run -p 8080:80 app",
])
def test_a_short_flag_that_is_not_a_password_is_left_alone(command):
    out = decider.redact(command)
    assert "<secret>" not in out


def test_redaction_keeps_what_the_command_does():
    out = decider.redact("rm -rf /Users/jdoe/Dev/app/build && git push --force origin main")
    assert "rm -rf" in out and "build" in out and "git push --force" in out


def test_redaction_keeps_the_dangerous_roots():
    assert decider.redact("rm -rf /") == "rm -rf /"
    assert decider.redact("rm -rf ~") == "rm -rf ~"


def test_heredoc_bodies_and_long_quoted_text_never_leave():
    out = decider.redact("python3 - <<EOF\nprint('customer 42 owes 900')\nEOF\n"
                         "git commit -m \"Fix the refund bug reported by the Acme account team last week\"")
    assert "customer" not in out and "Acme" not in out


def test_the_rest_of_a_heredocs_opening_line_is_still_shown():
    out = decider.redact("cat <<EOF > dump.txt && redis-cli FLUSHALL\ncustomer 42\nEOF")
    assert "FLUSHALL" in out and "customer" not in out


def test_an_edit_line_count_is_not_a_path():
    assert decider.redact("Edit: InvoiceService.kt (+4/-2 lines)") == "Edit: InvoiceService.kt (+4/-2 lines)"


def test_redaction_is_bounded():
    assert len(decider.redact("echo " + "a b " * 1000)) <= 602


def test_every_question_redacts_what_it_sends(jev, claude_home):
    """All three questions, through the real request: nothing a caller hands
    over leaves unredacted. The duplicate check used to send phrases as spoken."""
    import gate
    import risk
    risk.remote_destructive_probability("mysql -uroot -phunter2 -h db.acme.io appdb", jev.config)
    gate.worth_probability("Bash: curl -u admin:hunter2 https://api.acme.io; "
                           "Edit: /Users/jdoe/app/Invoice.kt (+4/-2 lines)", jev.config)
    phrase_gen._p_restates("I pushed the fix to cache.acme.io for jdoe@acme.io.",
                           ["I read /Users/jdoe/Projects/notes.txt."], jev.config)
    states = jev.states()
    assert len(states) == 3
    sent = "\n".join(states)
    for leak in ("hunter2", "acme", "jdoe"):
        assert leak not in sent
    assert "Invoice.kt (+4/-2 lines)" in sent and "notes.txt" in sent


def test_framing_goes_as_written_and_the_layout_is_unchanged(jev, claude_home):
    phrase_gen._p_restates("I fixed the invoice sync.", ["I'm fixing the invoice sync.", "I ran the tests."],
                           jev.config)
    [state] = jev.states()
    assert state == ("Announcements already spoken, most recent last:\n"
                     "- I'm fixing the invoice sync.\n- I ran the tests.\n\n"
                     "Candidate announcement:\n- I fixed the invoice sync.")


def test_session_text_must_say_what_kind_it_is(jev):
    with pytest.raises(TypeError):
        decider.probability("a", [("raw", "text")], "pick", {"a": "A"}, config=jev.config)


@pytest.mark.parametrize("endpoint", ["http://decisions.example.com/x", "ftp://example.com", ""])
def test_the_key_goes_only_to_https(endpoint, claude_home, fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    cfg = _jev_config(endpoint=endpoint)
    assert decider.probability("a", ["state"], "pick", {"a": "A"}, config=cfg) is None
    assert decider.check_key(FAKE_KEY, cfg) == {"ok": False, "reason": "the endpoint is not https"}
    assert fake_decider.calls() == 0
