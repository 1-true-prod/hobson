"""The pending queue: batch tool calls, flush at a coarser boundary."""

import time

import session_state as ss


def _state():
    return ss._fresh_state("demo")


def test_pending_starts_empty_and_records():
    st = _state()
    ss.record_pending(st, "Bash", "Run the test suite")
    ss.record_pending(st, "Edit", "phrase_gen.py")
    assert len(st["pending"]) == 2
    assert st["pending"][0]["tool"] == "Bash"


def test_pending_is_capped_and_drops_oldest():
    st = _state()
    for i in range(ss.MAX_PENDING + 4):
        ss.record_pending(st, "Bash", f"cmd {i}")
    assert len(st["pending"]) == ss.MAX_PENDING
    assert st["pending"][-1]["context"] == f"cmd {ss.MAX_PENDING + 3}"


def test_does_not_flush_below_the_call_threshold():
    st = _state()
    st["last_flush_time"] = time.time()
    ss.record_pending(st, "Bash", "one thing")
    assert ss.should_flush(st, min_calls=3, min_seconds=15.0) is False


def test_flushes_once_enough_calls_accumulate():
    st = _state()
    st["last_voiced_time"] = time.time()
    for i in range(3):
        ss.record_pending(st, "Bash", f"cmd {i}")
    assert ss.should_flush(st, min_calls=3, min_seconds=15.0) is True


def test_flushes_on_elapsed_time_with_at_least_one_pending():
    st = _state()
    st["last_flush_time"] = time.time() - 30
    ss.record_pending(st, "Bash", "slow thing")
    assert ss.should_flush(st, min_calls=3, min_seconds=15.0) is True


def test_never_flushes_an_empty_queue():
    """Elapsed time alone must not produce speech — nothing happened."""
    st = _state()
    st["last_voiced_time"] = time.time() - 600
    assert ss.should_flush(st, min_calls=3, min_seconds=15.0) is False


def test_first_ever_event_flushes_promptly():
    """last_voiced_time == 0 means nothing has been said yet; don't stay
    silent through the first three calls of a session."""
    st = _state()
    ss.record_pending(st, "Bash", "first thing")
    assert ss.should_flush(st, min_calls=3, min_seconds=15.0) is True


def test_take_pending_drops_stale_items_and_clears():
    """A remark about work from two minutes ago is a wrong glance, not a
    late one."""
    st = _state()
    ss.record_pending(st, "Bash", "ancient")
    st["pending"][0]["ts"] = time.time() - 300
    ss.record_pending(st, "Edit", "recent")
    items = ss.take_pending(st, stale_seconds=90.0)
    assert [i["context"] for i in items] == ["recent"]
    assert st["pending"] == []


def test_pending_summary_groups_repeats_by_tool():
    items = [
        {"tool": "Edit", "context": "phrase_gen.py", "ts": 0},
        {"tool": "Edit", "context": "base.py", "ts": 0},
        {"tool": "Bash", "context": "Run the test suite", "ts": 0},
    ]
    summary = ss.pending_summary(items)
    assert "2 Edit" in summary
    assert "Run the test suite" in summary


def test_pending_summary_of_nothing_is_none():
    assert ss.pending_summary([]) is None


def test_pending_survives_a_round_trip_through_disk(claude_home):
    st = _state()
    ss.record_pending(st, "Bash", "keep me")
    ss.save_session(st)
    assert ss.load_session("demo")["pending"][0]["context"] == "keep me"


def test_old_sessions_without_pending_are_backfilled(claude_home):
    """load_session backfills from _fresh_state; on-disk sessions predate this."""
    st = _state()
    del st["pending"]
    ss.save_session(st)
    assert ss.load_session("demo")["pending"] == []


# ── Task 4: engine-level batching ───────────────────────────────────────────


def _engine(monkeypatch, spoken):
    from engines.pocket_tts_realtime import PocketTTSRealtimeEngine
    monkeypatch.setattr(
        PocketTTSRealtimeEngine, "speak_dynamic",
        lambda self, phrase, allow_cold_start=False: spoken.append(phrase),
    )
    return PocketTTSRealtimeEngine({
        # "stop" must be enabled too: test_stop_discards_a_pending_batch
        # drives this same engine through a Stop event, and _is_event_enabled()
        # would otherwise short-circuit it before the pending queue is ever
        # touched.
        "engine": "pocket-tts", "events": ["commentary", "stop"], "personality": "alfred",
        "commentary": {"tools": ["bash", "edit", "write"], "verbosity": "normal",
                       "cooldown": 0, "min_tool_calls": 3, "min_seconds": 15.0},
    })


def _prime_bootstrapped_session():
    """Seed session state as already-bootstrapped (something was voiced and
    flushed earlier this session).

    should_flush() deliberately flushes on the very first-ever pending item
    of a brand new session, with last_flush_time == 0
    (test_first_ever_event_flushes_promptly in test_commentary_batching.py's
    session_state coverage) — a session must not stay silent forever just
    because nothing has been attempted yet. The tests below are about the
    steady-state batching gate (min_calls/min_seconds), which only kicks in
    once that one-time bootstrap has already happened, so they prime
    last_flush_time (the field should_flush's bootstrap check reads) first
    to exercise that steady state instead of the bootstrap edge case.
    last_voiced_time is primed too so anything reading it (e.g. the
    near-duplicate guard) also sees an already-bootstrapped session.
    """
    from engines.base import derive_project_label
    project = derive_project_label()
    state = ss.load_session(project)
    state["last_voiced_time"] = time.time()
    state["last_flush_time"] = time.time()
    ss.save_session(state)


def test_early_tool_calls_are_queued_silently(fake_ollama, claude_home, monkeypatch):
    """The first two calls of a batch must not speak — and must not spend an
    Ollama call either."""
    spoken = []
    engine = _engine(monkeypatch, spoken)
    _prime_bootstrapped_session()
    fake_ollama.chat("done | I ran the tests.")

    for i in range(2):
        engine.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_input": {"description": f"step {i}"}})

    assert spoken == []
    assert fake_ollama.urls == [], "queued calls must not hit Ollama"


def test_third_tool_call_flushes_one_utterance(fake_ollama, claude_home, monkeypatch):
    spoken = []
    engine = _engine(monkeypatch, spoken)
    _prime_bootstrapped_session()
    fake_ollama.chat("done | I ran the test suite twice.")

    for i in range(3):
        engine.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_input": {"description": f"run the test suite, pass {i}"}})

    assert len(spoken) == 1, "a batch produces exactly one utterance"


def test_the_batch_summary_reaches_the_model(fake_ollama, claude_home, monkeypatch):
    """Relevance beats timing — the utterance must describe the real work."""
    seen = {}
    import phrase_gen
    real = phrase_gen.generate_or_skip

    def spy(event_type, event_detail, *a, **k):
        seen["detail"] = event_detail
        return real(event_type, event_detail, *a, **k)

    monkeypatch.setattr(phrase_gen, "generate_or_skip", spy)
    monkeypatch.setattr("engines.base.generate_or_skip", spy, raising=False)

    spoken = []
    engine = _engine(monkeypatch, spoken)
    _prime_bootstrapped_session()
    fake_ollama.chat("done | I edited three files.")
    for name in ("a.py", "b.py", "c.py"):
        engine.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                    "tool_input": {"file_path": f"/repo/{name}"}})

    assert "3 Edit" in seen["detail"]


def test_queue_clears_after_a_flush(fake_ollama, claude_home, monkeypatch):
    from engines.base import derive_project_label
    spoken = []
    engine = _engine(monkeypatch, spoken)
    _prime_bootstrapped_session()
    fake_ollama.chat("done | I ran three commands.")
    for i in range(3):
        engine.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_input": {"description": f"step {i}"}})
    assert ss.load_session(derive_project_label())["pending"] == []


# ── Fix A: inline fallback defaults must match DEFAULT_CONFIG ──────────────


def test_flush_gate_fallback_matches_default_config(fake_ollama, claude_home, monkeypatch):
    """_handle_commentary_llm's inline cfg.get(..., default) fallbacks must
    agree with DEFAULT_CONFIG's commentary.min_tool_calls (5) and
    min_seconds (60.0) — not the superseded 3 / 15.0. This only surfaces
    when an engine is built with a commentary dict that omits the keys
    (load_config()'s deep merge normally fills them in)."""
    spoken = []
    engine = _engine(monkeypatch, spoken)
    del engine.config["commentary"]["min_tool_calls"]
    del engine.config["commentary"]["min_seconds"]
    _prime_bootstrapped_session()
    fake_ollama.chat("done | I ran the tests.")

    for i in range(3):
        engine.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_input": {"description": f"step {i}"}})

    assert spoken == [], "3 calls must not flush once the fallback matches DEFAULT_CONFIG's min_tool_calls=5"


# ── Fix B: time the gate off the last flush attempt ─────────────────────────


def test_record_flush_attempt_sets_last_flush_time():
    st = _state()
    assert st["last_flush_time"] == 0.0
    before = time.time()
    ss.record_flush_attempt(st)
    assert st["last_flush_time"] >= before


def test_seconds_since_last_voiced_unaffected_by_a_flush_attempt():
    """record_flush_attempt must not touch last_voiced_time — that field
    drives the near-duplicate guard's decay and genuinely cares about the
    last time something was actually spoken."""
    st = _state()
    st["last_voiced_time"] = time.time() - 120
    ss.record_flush_attempt(st)
    secs = ss.seconds_since_last_voiced(st)
    assert secs is not None and secs >= 120


def test_a_failed_flush_does_not_retrigger_the_gate_immediately(fake_ollama, claude_home, monkeypatch):
    """A flush that speaks nothing (SKIP, or a guard rejection) must not
    leave the elapsed-time gate tripped — otherwise the very next tool call
    flushes again with only one item queued, degrading the batch summary to
    a one-item summary exactly when it should be grouping."""
    import phrase_gen
    from engines.base import derive_project_label

    calls = []

    def fake_generate_or_skip(*a, **k):
        calls.append(1)
        return (None, None)

    monkeypatch.setattr(phrase_gen, "generate_or_skip", fake_generate_or_skip)

    spoken = []
    engine = _engine(monkeypatch, spoken)  # min_tool_calls=3, min_seconds=15.0

    # Prime the session so the elapsed-time branch is already open (both
    # last-voiced and last-flush are stale), but below the call-count
    # threshold — the first call below flushes via the timer, not the count.
    project = derive_project_label()
    state = ss.load_session(project)
    state["last_voiced_time"] = time.time() - 30
    state["last_flush_time"] = time.time() - 30
    ss.save_session(state)

    engine.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"description": "step 1"}})
    assert len(calls) == 1, "the timer-driven flush should have fired once"
    assert spoken == []

    # A second tool call arrives almost immediately after the failed flush.
    # Still only 1 item pending (< min_calls=3), and barely any time has
    # passed since the flush attempt — this must NOT flush again.
    engine.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"description": "step 2"}})

    assert len(calls) == 1, "a failed flush must not leave the timer tripped for the very next call"


# ── Task 5: Stop pre-empts a pending batch ──────────────────────────────────


def test_stop_discards_a_pending_batch(fake_ollama, claude_home, monkeypatch):
    """The task-end summary supersedes queued mid-work chatter."""
    from engines.base import derive_project_label

    spoken = []
    engine = _engine(monkeypatch, spoken)
    _prime_bootstrapped_session()
    fake_ollama.chat("done | I finished the migration.")

    engine.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"description": "step 1"}})
    assert ss.load_session(derive_project_label())["pending"]

    engine.run({"hook_event_name": "Stop", "last_assistant_message": "Done."})
    assert ss.load_session(derive_project_label())["pending"] == []
