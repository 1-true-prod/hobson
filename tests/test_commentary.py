"""_handle_commentary: whitelist, cooldown gate, chatty vs LLM, session recording."""

import engines.base as base


def _cfg(verbosity="normal", cooldown=0.0):
    return {
        "engine": "say",
        "personality": "hobson",
        "events": ["commentary"],
        "commentary": {"cooldown": cooldown, "tools": ["Bash", "Edit", "Write", "Agent"],
                       "verbosity": verbosity},
        "ollama": {"model": "m", "url": "http://x"},
    }


def _engine(cfg):
    from engines.say import SayEngine
    return SayEngine(cfg)


def test_commentary_skips_non_whitelisted_tool(claude_home, no_audio):
    eng = _engine(_cfg())
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Read",
             "tool_input": {"file_path": "/x/y.py"}})
    assert not no_audio["say"]


def test_chatty_uses_template_no_ollama(claude_home, no_audio, monkeypatch):
    import phrase_gen
    called = {"n": 0}
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ("done", "x"))
    eng = _engine(_cfg(verbosity="chatty"))
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "/a/base.py"}})
    assert no_audio["say"], "chatty should speak a templated phrase"
    assert called["n"] == 0, "chatty must not call Ollama"


def test_normal_calls_generate_and_speaks(claude_home, no_audio, monkeypatch):
    import phrase_gen
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I am editing the base file."))
    eng = _engine(_cfg(verbosity="normal"))
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "/a/base.py"}})
    assert no_audio["say"] == ["I am editing the base file."]


def test_normal_skip_does_not_speak(claude_home, no_audio, monkeypatch):
    import phrase_gen
    monkeypatch.setattr(phrase_gen, "generate_or_skip", lambda *a, **k: (None, None))
    eng = _engine(_cfg(verbosity="normal"))
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "/a/base.py"}})
    assert no_audio["say"] == []


def test_commentary_cooldown_blocks_second(claude_home, no_audio, monkeypatch):
    """The commentary lock gates the flush/speak path, not the queue.

    Under batching a lone call no longer speaks by itself (see
    tests/test_commentary_batching.py) -- so this sets min_tool_calls=1,
    making every call individually eligible to flush, to isolate what this
    test is actually about: the commentary-cooldown lock blocking a second
    flush that happens too soon after the first, exactly as it did before
    batching existed. It also asserts the deferred batch is *kept*, not
    dropped -- silently losing a locked-out tool call from the queue was the
    original design's very trap.
    """
    import phrase_gen
    import session_state as ss
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I did something useful here."))
    cfg = _cfg(verbosity="normal", cooldown=60.0)
    cfg["commentary"]["min_tool_calls"] = 1
    eng = _engine(cfg)
    evt = {"hook_event_name": "PreToolUse", "tool_name": "Edit",
           "tool_input": {"file_path": "/a/base.py"}}
    eng.run(dict(evt))
    eng.run(dict(evt))
    assert len(no_audio["say"]) == 1, "cooldown should block the second flush"
    state = ss.load_session(base.derive_project_label())
    assert state["pending"], "a locked-out flush must keep its batch queued, not drop it"


def test_commentary_records_session(claude_home, no_audio, monkeypatch):
    import phrase_gen
    import session_state as ss
    monkeypatch.setattr(phrase_gen, "generate_or_skip",
                        lambda *a, **k: ("done", "I am wiring up the form."))
    eng = _engine(_cfg(verbosity="normal"))
    eng.run({"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "/a/base.py"}})
    # derive_project_label is cached; load the session it wrote.
    state = ss.load_session(base.derive_project_label())
    assert state["total_voiced"] == 1
    assert len(state["recent_voiced"]) == 1
    assert state["recent_voiced"][0][0] == "I am wiring up the form."
