"""claudio.py main() entrypoint: stdin dispatch + muted short-circuit."""

import io
import json


def _write_cfg(claude, data):
    (claude / "claudio.json").write_text(json.dumps(data), encoding="utf-8")


def test_main_dispatches_to_engine(claude_home, claudio_entry, monkeypatch):
    _write_cfg(claude_home, {"engine": "say", "muted": False})
    seen = {}

    class FakeEngine:
        def run(self, hook_input):
            seen["hook"] = hook_input

    monkeypatch.setattr(claudio_entry, "load_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(claudio_entry.sys, "stdin",
                        io.StringIO('{"hook_event_name": "Stop"}'))
    claudio_entry.main()
    assert seen["hook"] == {"hook_event_name": "Stop"}


def test_main_muted_short_circuits(claude_home, claudio_entry, monkeypatch):
    _write_cfg(claude_home, {"engine": "say", "muted": True})
    called = {"n": 0}
    monkeypatch.setattr(claudio_entry, "load_engine",
                        lambda cfg: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(claudio_entry.sys, "stdin", io.StringIO("{}"))
    claudio_entry.main()
    assert called["n"] == 0


def test_main_handles_bad_stdin(claude_home, claudio_entry, monkeypatch):
    _write_cfg(claude_home, {"engine": "say", "muted": False})
    seen = {}

    class FakeEngine:
        def run(self, hook_input):
            seen["hook"] = hook_input

    monkeypatch.setattr(claudio_entry, "load_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(claudio_entry.sys, "stdin", io.StringIO("not json at all"))
    claudio_entry.main()
    # Falls back to an empty hook_input rather than crashing.
    assert seen["hook"] == {}
