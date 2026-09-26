"""Guards that keep generated phrases speakable and honest."""
import time

import pytest

import phrase_gen


def test_parse_response_never_speaks_the_separator():
    # Real logged failure: an invalid category fell through and the pipe
    # was spoken aloud.
    raw = "awaiting user input | Need to confirm merge and login progress."
    result = phrase_gen._parse_response(raw)
    assert result is not None
    category, phrase = result
    assert "|" not in phrase
    assert "awaiting user input" not in phrase
    # _clean_phrase's pre-existing _ensure_first_person repair prepends "I "
    # here because "need" is in _LEAD_VERBS -- correct given the persona's
    # first-person rule, and unrelated to the pipe-leak this test targets.
    assert phrase == "I need to confirm merge and login progress."


def test_parse_response_invalid_category_falls_back_to_done():
    result = phrase_gen._parse_response("I action_required | Task needs attention here.")
    assert result is not None
    category, phrase = result
    assert category == "done"
    assert "|" not in phrase


def test_parse_response_valid_category_still_works():
    result = phrase_gen._parse_response("broken | I hit a merge conflict.")
    assert result == ("broken", "I hit a merge conflict.")


def test_word_budget_constants_agree_with_prompt():
    # The prompt text and the gate must state the same numbers.
    assert phrase_gen.MIN_WORDS == 4
    assert phrase_gen.MAX_WORDS == 12
    messages = phrase_gen._build_messages("Stop", "detail", "Recent: none")
    prompt_text = " ".join(m["content"] for m in messages if m["role"] == "user")
    assert "4-12 words" in prompt_text


def test_clean_phrase_accepts_four_words():
    # Real logged phrase, previously good, must not be newly rejected.
    assert phrase_gen._clean_phrase("I checked card files.") == "I checked card files."


def test_clean_phrase_rejects_thirteen_words():
    long_phrase = "I " + " ".join(["word"] * 12) + "."
    assert phrase_gen._clean_phrase(long_phrase) is None


def test_clean_phrase_normalizes_identifiers():
    out = phrase_gen._clean_phrase("done | I found ColumnWithDividers today.")
    assert out is not None
    assert "ColumnWithDividers" not in out
    assert "Column With Dividers" in out


def test_clean_phrase_strips_ticket_ids():
    out = phrase_gen._clean_phrase("I implemented APP-1203 login handoff work.")
    assert out is not None
    assert "APP-1203" not in out


@pytest.mark.parametrize("phrase", [
    "I inspected the SSH state.",
    "I checked fingerprint keys.",
    "I verified the granite license.",
    "I committed the migration.",
])
def test_looks_past_tense_flags_completed_claims(phrase):
    assert phrase_gen.looks_past_tense(phrase) is True


@pytest.mark.parametrize("phrase", [
    "I'm checking the PR base branch.",
    "I'm researching new candidates.",
    "I need your permission.",
    "I await your decision.",
])
def test_looks_past_tense_allows_present_tense(phrase):
    assert phrase_gen.looks_past_tense(phrase) is False


def test_is_near_duplicate_catches_exact_repeat():
    recent = ["I'm navigating to Account Settings."]
    assert phrase_gen.is_near_duplicate("I'm navigating to Account Settings.", recent) is True


def test_is_near_duplicate_catches_reworded_repeat():
    # Real logged pair.
    recent = ["I'm searching for balance changes."]
    assert phrase_gen.is_near_duplicate("I'm searching for balance cell changes.", recent) is True


def test_is_near_duplicate_allows_distinct_phrases():
    recent = ["I'm navigating to Account Settings."]
    assert phrase_gen.is_near_duplicate("I hit a merge conflict.", recent) is False


def test_is_near_duplicate_empty_recent():
    assert phrase_gen.is_near_duplicate("I merged the branch.", []) is False


# ── is_near_duplicate: per-match decay (unit level) ────────────────────────

def test_is_near_duplicate_decay_skips_old_match():
    now = 1_000_000.0
    old = [("I'm navigating to Account Settings.", now - phrase_gen.DUPE_DECAY_SECONDS - 1)]
    assert phrase_gen.is_near_duplicate(
        "I'm navigating to Account Settings.", old,
        decay_seconds=phrase_gen.DUPE_DECAY_SECONDS, now=now) is False


def test_is_near_duplicate_decay_keeps_recent_match():
    now = 1_000_000.0
    recent = [("I'm navigating to Account Settings.", now - 5)]
    assert phrase_gen.is_near_duplicate(
        "I'm navigating to Account Settings.", recent,
        decay_seconds=phrase_gen.DUPE_DECAY_SECONDS, now=now) is True


def test_is_near_duplicate_zero_timestamp_is_unknown_not_infinitely_old():
    # Trap: ts=0.0 means "unknown", not "infinitely old". Unknown must stay
    # conservative and still dedupe -- getting this backwards is exactly
    # what would make hobson chatty for one session after upgrade.
    now = 1_000_000.0
    unknown = [("I'm navigating to Account Settings.", 0.0)]
    assert phrase_gen.is_near_duplicate(
        "I'm navigating to Account Settings.", unknown,
        decay_seconds=phrase_gen.DUPE_DECAY_SECONDS, now=now) is True


def test_is_near_duplicate_mixed_bare_and_pair_entries():
    # A list mixing legacy bare strings with [phrase, ts] pairs behaves
    # sanely: each entry is judged on its own shape.
    now = 1_000_000.0
    decayed_pair = ("I hit a merge conflict.", now - phrase_gen.DUPE_DECAY_SECONDS - 1)
    mixed = [decayed_pair, "I'm navigating to Account Settings."]
    # The pair is stale and drops out, but the bare string has unknown age
    # and still catches the duplicate.
    assert phrase_gen.is_near_duplicate(
        "I'm navigating to Account Settings.", mixed,
        decay_seconds=phrase_gen.DUPE_DECAY_SECONDS, now=now) is True


def test_is_near_duplicate_bare_strings_unaffected_by_decay():
    # Backward compat: scripts/log_analyse.py:107 calls this with a plain
    # list of bare strings and no decay_seconds -- the only place the
    # project's duplicate-rate metric comes from. Must behave exactly as
    # before this task.
    recent = ["I'm navigating to Account Settings."]
    assert phrase_gen.is_near_duplicate("I'm navigating to Account Settings.", recent) is True


def test_pretooluse_past_tense_never_speaks_a_false_claim(monkeypatch):
    # Superseded the original "is_rejected" assertion. Rejecting produced
    # silence 77% of the time, so past tense is now repaired instead. What
    # must still hold is the actual requirement: the spoken phrase never
    # claims completed work. See
    # test_pretooluse_past_tense_is_repaired_not_dropped for the repair path.
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I inspected the SSH state.", None))
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: check ssh", "No announcements yet.")
    assert phrase is None or not phrase_gen.looks_past_tense(phrase)


def test_pretooluse_present_tense_passes(monkeypatch):
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I'm checking the SSH state.", None))
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: check ssh", "No announcements yet.")
    assert phrase == "I'm checking the SSH state."


def test_stop_regenerates_once_on_duplicate(monkeypatch):
    calls = []

    def fake_chat(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return "done | I verified the granite license.", None
        return "done | I merged the preview clean.", None

    monkeypatch.setattr(phrase_gen, "_chat", fake_chat)
    category, phrase = phrase_gen.generate_or_skip(
        "Stop", "finished", "Recent: none",
        recent_voiced=["I verified the granite license."])
    assert len(calls) == 2, "Stop should regenerate once on rejection"
    assert phrase == "I merged the preview clean."


def test_pretooluse_does_not_regenerate(monkeypatch):
    calls = []

    def fake_chat(*a, **k):
        calls.append(1)
        return "done | I verified the granite license.", None

    monkeypatch.setattr(phrase_gen, "_chat", fake_chat)
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: verify the granite license", "Recent: none",
        recent_voiced=["I verified the granite license."])
    assert len(calls) == 1, "PreToolUse must not pay for a retry"
    assert phrase is None


def test_prompt_states_the_missing_rules():
    messages = phrase_gen._build_messages(
        "PreToolUse", "Bash: x", 'Recent: "I merged the branch."')
    text = " ".join(m["content"] for m in messages if m["role"] == "user").lower()
    assert "has not happened yet" in text or "not yet run" in text
    assert "do not repeat" in text
    assert "describe" in text


def test_log_line_includes_project(claude_home, monkeypatch):
    import engines.base as base
    monkeypatch.setattr(base, "derive_project_label", lambda: "hobson")
    base.log("hello")
    text = (claude_home / "hobson.log").read_text()
    assert "[hobson]" in text


def test_plain_verbs_swaps_concentrated_slop():
    assert phrase_gen._plain_verbs("I completed the migration.") == "I finished the migration."
    assert phrase_gen._plain_verbs("I verified the license.") == "I checked the license."


def test_plain_verbs_preserves_case_and_other_words():
    assert phrase_gen._plain_verbs("Completed the sweep.") == "Finished the sweep."
    assert phrase_gen._plain_verbs("I merged the branch.") == "I merged the branch."


def test_plain_verbs_does_not_match_inside_words():
    # "completion" must not become "finishedion".
    assert phrase_gen._plain_verbs("I hit a completion error.") == "I hit a completion error."


# ── Tense repair ───────────────────────────────────────────────────────────
# Rejecting a past-tense PreToolUse phrase converted the defect into silence:
# 77% of generations were being dropped, 31 of them commentary the user would
# otherwise have heard. Repairing beats discarding. Every verb below was taken
# from real guard rejections in hobson.log, not invented.

@pytest.mark.parametrize("verb,want", [
    ("checked", "checking"),      # 22 real occurrences
    ("inspected", "inspecting"),  # 11
    ("found", "finding"),         # 9 — irregular
    ("verified", "verifying"),    # 6 — -ied
    ("ran", "running"),           # 5 — irregular
    ("hit", "hitting"),           # 4 — irregular
    ("committed", "committing"),  # doubled consonant survives
    ("staged", "staging"),        # silent-e elision
    ("wrote", "writing"),         # irregular
    ("created", "creating"),
    ("set", "setting"),           # irregular, no -ed
])
def test_progressive_form_covers_real_rejected_verbs(verb, want):
    assert phrase_gen._progressive_form(verb) == want


def test_progressive_form_returns_none_for_unknown():
    assert phrase_gen._progressive_form("permission") is None


def test_to_present_progressive_rewrites_the_subject():
    assert (phrase_gen.to_present_progressive("I inspected the SSH state.")
            == "I'm inspecting the SSH state.")


def test_to_present_progressive_handles_have_contraction():
    assert (phrase_gen.to_present_progressive("I've confirmed the changes are live.")
            == "I'm confirming the changes are live.")


def test_to_present_progressive_leaves_present_tense_alone():
    assert phrase_gen.to_present_progressive("I'm checking the branch.") is None


def test_pretooluse_past_tense_is_repaired_not_dropped(monkeypatch):
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I inspected the SSH state.", None))
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: check ssh", "No announcements yet.")
    assert phrase == "I'm inspecting the SSH state."


def test_repaired_phrase_no_longer_trips_the_tense_guard():
    repaired = phrase_gen.to_present_progressive("I checked the granite license.")
    assert phrase_gen.looks_past_tense(repaired) is False


def test_unrepairable_past_tense_still_rejected(monkeypatch):
    # When repair declines, the guard must still drop the phrase rather than
    # speak a false claim. Forced here, because in practice the repair rules
    # cover everything looks_past_tense flags -- any -ed word plus the
    # irregular map -- so this fallback is deliberately hard to reach.
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I inspected the SSH state.", None))
    monkeypatch.setattr(phrase_gen, "_progressive_form", lambda verb: None)
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: inspect the SSH state", "No announcements yet.")
    assert phrase is None


# ── Compound-tense repair ──────────────────────────────────────────────────
# Repairing only the leading verb produced broken grammar on compound
# sentences: 3 of 21 real repaired phrases read "I'm checking X and updated
# Y". All cases below are from hobson.log.

@pytest.mark.parametrize("phrase,want", [
    ("I'm compiling and ran Android unit tests.",
     "I'm compiling and running Android unit tests."),
    ("I'm checking tests and updated log format.",
     "I'm checking tests and updating log format."),
    ("I'm checking the test suite and cleaned up pollution.",
     "I'm checking the test suite and cleaning up pollution."),
])
def test_repair_tense_fixes_verbs_after_and(phrase, want):
    assert phrase_gen.repair_tense(phrase) == want


def test_repair_tense_fixes_lead_and_trailing_together():
    assert (phrase_gen.repair_tense("I verified tests and updated log format.")
            == "I'm verifying tests and updating log format.")


def test_repair_tense_leaves_participle_adjectives_alone():
    # "polluted" modifies "tests" -- it is not a verb and must not be touched.
    assert phrase_gen.repair_tense("I'm finding polluted tests in hobson.") is None


def test_repair_tense_returns_none_when_nothing_to_do():
    assert phrase_gen.repair_tense("I'm checking the branch.") is None


# ── PermissionRequest is also a pre-action event ───────────────────────────
# It fires before permission is granted, so it has the same before-the-fact
# semantics as PreToolUse. "I asked for a user question." was being spoken
# past-tense because the guard was scoped to PreToolUse only.

def test_permission_request_past_tense_is_repaired(monkeypatch):
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I asked for a user question.", None))
    category, phrase = phrase_gen.generate_or_skip(
        "PermissionRequest", "Tool: Bash", "No announcements yet.")
    assert phrase == "I'm asking for a user question."


def test_stop_keeps_past_tense():
    # Stop fires *after* the work, so past tense is correct there and must
    # survive untouched.
    assert phrase_gen._repair_phrase("Stop", "I merged the branch.")[0] == \
        "I merged the branch."


# ── Irregular past-tense detection ─────────────────────────────────────────
# "I read Android build logs." was spoken twice on PreToolUse as a false
# completed claim: "read" ends in "ad", not "ed", and was absent from
# _LEAD_VERBS. Detection after "I" can use a wider vocabulary than
# sentence-initial detection, because after "I" the word can only be a verb.

@pytest.mark.parametrize("phrase,want", [
    ("I read Android build logs.", "I'm reading Android build logs."),
    ("I built the release APK.", "I'm building the release APK."),
    ("I sent the review request.", "I'm sending the review request."),
    ("I took a screenshot of it.", "I'm taking a screenshot of it."),
    ("I left the branch alone.", "I'm leaving the branch alone."),
])
def test_irregular_past_tense_detected_and_repaired(phrase, want):
    assert phrase_gen.looks_past_tense(phrase) is True
    assert phrase_gen.repair_tense(phrase) == want


def test_sentence_initial_detection_stays_conservative():
    # "Left" at position 0 may be a direction, not a verb -- prepending "I"
    # there would produce "I left pane is broken."
    assert phrase_gen._ensure_first_person("Left pane is broken.") == \
        "Left pane is broken."


# ── Duplicate-guard time decay ─────────────────────────────────────────────
# Near-duplicates were 15 of 18 rejections -- the main remaining source of
# silence. Repetition only grates back-to-back: the same phrase two minutes
# later is not what made "I'm navigating to Account Settings" three times annoying.

def test_duplicate_rejected_when_recent(monkeypatch):
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I'm checking the branch.", None))
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: check the branch", "Recent: none",
        recent_voiced=[("I'm checking the branch.", time.time() - 5)])
    assert phrase is None


def test_duplicate_allowed_after_decay_window(monkeypatch):
    # The 51% case: decay is now keyed to the *matching* entry's own
    # timestamp, not the time since any phrase was voiced.
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I'm checking the branch.", None))
    old_ts = time.time() - (phrase_gen.DUPE_DECAY_SECONDS + 1)
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: check the branch", "Recent: none",
        recent_voiced=[("I'm checking the branch.", old_ts)])
    assert phrase == "I'm checking the branch."


def test_duplicate_still_checked_when_elapsed_unknown(monkeypatch):
    # Bare string = unknown per-match age (legacy shape / backward compat).
    # Decay is keyed to the matching entry's own timestamp, so a stale
    # *global* seconds_since_last_voiced must not let an unknown-age match
    # through -- stay conservative and dedupe.
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I'm checking the branch.", None))
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: check the branch", "Recent: none",
        recent_voiced=["I'm checking the branch."],
        seconds_since_last_voiced=phrase_gen.DUPE_DECAY_SECONDS + 1)
    assert phrase is None


def test_duplicate_global_elapsed_no_longer_gates_a_fresh_match(monkeypatch):
    # Regression guard for the bug this task fixes: seconds_since_last_voiced
    # is time since *any* phrase, not the matching one, so a stale global
    # value must not let a genuinely recent match through.
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I'm checking the branch.", None))
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: check the branch", "Recent: none",
        recent_voiced=[("I'm checking the branch.", time.time() - 5)],
        seconds_since_last_voiced=phrase_gen.DUPE_DECAY_SECONDS + 1)
    assert phrase is None


# ── T1: guard rejections are not logged as parse failures ─────────────────
# A guard rejection (near-duplicate, unrepaired past tense) is the system
# working as designed -- not a failure to parse the model's output. Before
# this, both cases left last_raw holding the raw model reply, so
# describe_generation_failure() read it as UNUSABLE ("could not parse a
# phrase from ..."), which is false and double-counted in hobson stats.

def test_rejected_generation_sets_last_raw_marker(monkeypatch):
    monkeypatch.setattr(phrase_gen, "_chat",
                        lambda *a, **k: ("done | I'm checking the branch.", None))
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: check the branch", "Recent: none",
        recent_voiced=["I'm checking the branch."],
        seconds_since_last_voiced=5)
    assert phrase is None
    assert phrase_gen.last_raw == "(rejected: near-duplicate of a recent phrase)"


def test_stop_retry_reject_keeps_last_rejection_marker(monkeypatch):
    # Stop retries once on rejection (attempts = 2). If both attempts are
    # rejected, the loop falls through to `return None, None` -- last_raw
    # must still carry the *second* attempt's rejection reason, not a stale
    # value from the first attempt or the raw model text.
    calls = []

    def fake_chat(*a, **k):
        calls.append(1)
        return "done | I verified the granite license.", None

    monkeypatch.setattr(phrase_gen, "_chat", fake_chat)
    category, phrase = phrase_gen.generate_or_skip(
        "Stop", "finished", "Recent: none",
        recent_voiced=["I verified the granite license."])
    assert len(calls) == 2
    assert phrase is None
    assert phrase_gen.last_raw == "(rejected: near-duplicate of a recent phrase)"


def test_describe_generation_failure_rejected():
    from engines.base import describe_generation_failure
    verdict, detail = describe_generation_failure(
        "(rejected: near-duplicate of a recent phrase)")
    assert verdict == "REJECTED"
    assert "near-duplicate of a recent phrase" in detail


def test_describe_generation_failure_skip_error_unusable_unchanged():
    # Regression: the pre-existing verdicts must classify exactly as before
    # -- T1 only adds a new REJECTED branch, it does not touch these.
    from engines.base import describe_generation_failure

    verdict, detail = describe_generation_failure("SKIP")
    assert verdict == "SKIP"
    assert detail == "model chose not to speak"

    verdict, detail = describe_generation_failure(
        "(error: request timed out after 8s)")
    assert verdict == "ERROR"
    assert "timed out" in detail

    verdict, detail = describe_generation_failure(
        "(error: URLError connection refused)")
    assert verdict == "ERROR"
    assert "unreachable" in detail

    verdict, detail = describe_generation_failure("(error: boom)")
    assert verdict == "ERROR"
    assert "ollama call failed" in detail

    verdict, detail = describe_generation_failure("")
    assert verdict == "ERROR"
    assert detail == "ollama returned nothing"

    verdict, detail = describe_generation_failure("garbled | nonsense")
    assert verdict == "UNUSABLE"
    assert "could not parse a phrase" in detail


# ── The decider may also block a reworded repeat, in commentary only ───────
#
# Scored over 148 real spoken phrases that had another said in the previous
# two minutes and passed the local guard: every commentary phrase at
# P(restates) >= 0.75 was a genuine repeat ("I'm creating run dirs." after
# "I'm creating run dirs and froze grading criteria."), the next one down
# (0.56) an arguable re-run. Stop is exempt: one of its two scores >= 0.8
# was "I confirmed ..." after "I'm confirming ..." -- a completion.

import time as _time  # noqa: E402

import phrase_gen as _pg  # noqa: E402


def _fresh(*phrases, age=10):
    return [[p, _time.time() - age] for p in phrases]


def _p(monkeypatch, value, seen=None):
    def fake(phrase, priors, config):
        if seen is not None:
            seen.append(list(priors))
        return value
    monkeypatch.setattr(_pg, "_p_restates", fake)


def test_commentary_that_restates_a_recent_phrase_is_blocked(claude_home, monkeypatch):
    _p(monkeypatch, 0.96)
    reason = _pg._guard_reject_reason("PreToolUse", "I'm creating run dirs.",
                                      _fresh("I'm creating run dirs and froze grading criteria."))
    assert reason and "restates" in reason


def test_commentary_below_the_threshold_is_spoken(claude_home, monkeypatch):
    _p(monkeypatch, 0.56)
    assert _pg._guard_reject_reason("PreToolUse", "I'm running the serialization tests.",
                                    _fresh("I've run the serialization tests.")) is None


@pytest.mark.parametrize("event", ["Stop", "PermissionRequest", "Notification"])
def test_only_commentary_can_be_blocked(event, claude_home, monkeypatch):
    _p(monkeypatch, 0.99)
    # Present tense, so the past-tense guard on pre-action events stays out of it.
    assert _pg._guard_reject_reason(event, "I need you to check the guest system memory.",
                                    _fresh("I'm confirming the guest system is using 12GB.")) is None


def test_no_opinion_speaks(claude_home, monkeypatch):
    _p(monkeypatch, None)
    assert _pg._guard_reject_reason("PreToolUse", "I'm fixing the views.",
                                    _fresh("I'm reviewing the view tasks.")) is None


def test_only_recent_phrases_with_a_known_age_are_compared(claude_home, monkeypatch):
    """Blocking silences, so an unknown-age prior (ts 0.0) must not count."""
    seen = []
    _p(monkeypatch, 0.99, seen)
    recent = _fresh("said a minute ago", age=60) + _fresh("said ten minutes ago", age=600) \
        + [["legacy entry", 0.0]]
    _pg._guard_reject_reason("PreToolUse", "I'm doing something new.", recent)
    assert seen == [["said a minute ago"]]


def test_nothing_recent_means_no_question_asked(claude_home, monkeypatch):
    seen = []
    _p(monkeypatch, 0.99, seen)
    assert _pg._guard_reject_reason("PreToolUse", "I'm starting.", _fresh("old", age=600)) is None
    assert seen == []


def test_commentary_worded_as_a_question_is_not_spoken():
    """"Is the weekend really coming?" narrated a Bash call."""
    assert phrase_gen._guard_reject_reason(
        "PreToolUse", "Should the build quest guide be shown?", []) is not None
    assert phrase_gen._guard_reject_reason(
        "PermissionRequest", "Should I push the branch to origin?", []) is None
    assert phrase_gen._guard_reject_reason(
        "Stop", "Should I fix that and push the six commits?", []) is None


# ── Commentary must be about its batch ─────────────────────────────────────
#
# Blind-labelled, 130 of 399 spoken commentary phrases did not describe the
# batch they were spoken for: they reworded a recent phrase, spoke the branch
# name as the action, or invented a failure for work that had not run yet.


@pytest.mark.parametrize("detail,phrase", [
    # Copied from the recent "Should I commit the updated CLAUDE.md?".
    ("4 Bash (Build blind labelling set of 380 Stops)", "I'm committing the updated CLAUDE dot M D."),
    # The branch name, spoken as the action.
    ("2 Bash (cd /Users/dev/Dev/mobile-app__w)", "I'm widening the search filters."),
    ("5 Bash (Push and confirm PR state)", "I'm hitting a build error in the main project."),
])
def test_commentary_about_something_else_is_not_spoken(detail, phrase):
    assert phrase_gen._guard_reject_reason(
        "PreToolUse", phrase, [], event_detail=detail) == "not about this batch"


@pytest.mark.parametrize("detail,phrase", [
    ("3 Bash (Resume pending merge commit on APP-1201)", "I'm hitting a bash error with the merge commit."),
    ("2 Bash (Rebuild APK with decode shim)", "Rebuilding APK failed due to decode shim issues."),
])
def test_commentary_may_not_invent_a_failure(detail, phrase):
    """The batch has not run; nothing in it has failed yet."""
    assert phrase_gen._guard_reject_reason(
        "PreToolUse", phrase, [], event_detail=detail) == "a failure the batch does not show"


@pytest.mark.parametrize("detail,phrase", [
    ("Edit: InvoiceService.kt (+6/-4 lines)", "I'm editing the Invoice Service."),
    ("3 Bash (Run the installer test suite)", "I'm running the installer tests."),
    ("Write: SPEC.md", "I'm writing the specification."),
    ("Write: reference_emulator_webcam_stuck_capture.md", "I'm writing the stuck webcam capture report."),
    ("Bash: Re-run the failing InvoiceSync test", "I'm re-running the failing invoice sync test."),
    ("Edit: InvoiceService.kt (+12/-2 lines)", "I'm adding error handling to the invoice service."),
])
def test_commentary_about_its_batch_is_spoken(detail, phrase):
    assert phrase_gen._guard_reject_reason("PreToolUse", phrase, [], event_detail=detail) is None


def test_only_commentary_is_held_to_its_batch():
    assert phrase_gen._guard_reject_reason(
        "Stop", "I hit a build error in photo upload.", [],
        event_detail="Last message: Build failed with a type error.") is None


def test_an_ungrounded_batch_phrase_is_never_spoken(fake_ollama, claude_home):
    fake_ollama.chat("done | I'm committing the updated CLAUDE.md.")
    category, phrase = phrase_gen.generate_or_skip(
        "PreToolUse", "4 Bash (Build blind labelling set of 380 Stops)", "Recent: none",
        verbosity="decided")
    assert phrase is None
