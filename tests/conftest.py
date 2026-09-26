"""Shared pytest fixtures for the hobson suite.

All tests run offline and silent: external side-effects (Ollama HTTP, afplay/say
subprocesses) are mocked, and all ~/.claude state is redirected into a tmp dir.
"""

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
for _p in (SCRIPTS, os.path.join(SCRIPTS, "engines")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_hyphenated(name, relpath):
    """Import a module whose filename isn't a valid identifier (e.g. settings-merge.py)."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Process-global caches leak across tests — reset around each one."""
    import bark_templates
    import engines.base as base
    base._PROJECT_LABEL_CACHE = None
    bark_templates._data = None
    yield
    base._PROJECT_LABEL_CACHE = None
    bark_templates._data = None


@pytest.fixture(autouse=True)
def claude_home(tmp_path, monkeypatch):
    """Redirect ~/.claude state into tmp_path.

    Patches the import-time path constants in base/session_state AND $HOME (for
    the call-time os.path.expanduser in bark_templates / kokoro / chatterbox).
    Returns the .claude dir path.
    """
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    # Never let a real key on the developer's machine leak into a test run.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    import engines.base as base
    import session_state
    import nudge
    import decider

    monkeypatch.setattr(base, "CONFIG_FILE", str(claude / "hobson.json"))
    monkeypatch.setattr(base, "BARK_LOCK_FILE", str(claude / "hobson.lock"))
    monkeypatch.setattr(base, "COMMENTARY_LOCK_FILE", str(claude / "hobson-commentary.lock"))
    monkeypatch.setattr(base, "LOG_FILE", str(claude / "hobson.log"))
    monkeypatch.setattr(session_state, "SESSIONS_DIR", str(claude / "hobson-sessions"))
    # nudge.py's LOCK_DIR is an import-time constant (and the per-project
    # activity tokens live in it too, via activity_path), evaluated
    # against the real $HOME the moment any test module first does
    # `from nudge import ...` (which happens at collection time, before this
    # fixture's setenv runs) — patch them explicitly so no test can touch
    # the developer's real ~/.claude via a real lock file or activity token.
    monkeypatch.setattr(nudge, "LOCK_DIR", str(claude))
    # decider.py's ENV_FILE is the same kind of import-time constant — patch
    # it so a test can never read or write the developer's real
    # ~/.claude/hobson.env (which is where a real OPENROUTER_API_KEY lives).
    monkeypatch.setattr(decider, "ENV_FILE", str(claude / "hobson.env"))
    # The "no key" warning is once-per-process; reset it so each test sees a
    # fresh process rather than inheriting a previous test's warning state.
    monkeypatch.setattr(decider, "_warned_no_key", False)
    return claude


@pytest.fixture(autouse=True)
def no_audio(monkeypatch):
    """Record audio side-effects instead of invoking afplay/say/nudge/watchdog.

    Returns a dict with 'popen' (list of arg-lists) and 'say' (list of texts).

    autouse — not opt-in — because engines.base.subprocess.Popen is also the
    call site for _maybe_start_nudge/_maybe_start_watchdog: any test that
    drives a real commentary flush without requesting this fixture would
    otherwise spawn a genuine detached nudge.py/--watchdog process. That
    happened for real during this series (test_commentary_batching.py's
    tests construct engines without requesting no_audio) before this was
    made autouse — a hard rule violation this fixture must make structurally
    impossible, not just documented against.
    """
    import engines.base as base

    calls = {"popen": [], "say": []}

    class FakePopen:
        def __init__(self, args, *a, **k):
            calls["popen"].append(args)

        def wait(self, *a, **k):
            return 0

    monkeypatch.setattr(base.subprocess, "Popen", FakePopen)

    def fake_say(self, text):
        calls["say"].append(text)

    monkeypatch.setattr(base.BaseEngine, "_say_with_volume", fake_say)
    return calls


class _FakeResp:
    """Minimal urlopen response: supports context-manager, read(), iteration."""

    def __init__(self, body=b"", lines=None):
        self._body = body
        self._lines = lines or []

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_ollama(monkeypatch):
    """Patch urlopen in base + phrase_gen and script canned responses.

    Use .generate(word) for /api/generate (classify) and .chat(text) for the
    streamed /api/chat (phrase_gen). .error(exc) makes the next call raise.
    .urls collects the requested URLs for assertions.
    """
    import engines.base as base
    import phrase_gen

    box = {"factory": None, "error": None, "urls": []}

    def fake_urlopen(req, timeout=None):
        box["urls"].append(getattr(req, "full_url", str(req)))
        if box["error"] is not None:
            raise box["error"]
        return box["factory"]()

    monkeypatch.setattr(base, "urlopen", fake_urlopen)
    monkeypatch.setattr(phrase_gen, "urlopen", fake_urlopen)

    ns = SimpleNamespace(urls=box["urls"])

    def generate(word):
        box["error"] = None
        box["factory"] = lambda: _FakeResp(body=json.dumps({"response": word}).encode())

    def chat(text):
        lines = [
            json.dumps({"message": {"content": text}, "done": False}).encode(),
            json.dumps({"message": {"content": ""}, "done": True}).encode(),
        ]
        box["error"] = None
        box["factory"] = lambda: _FakeResp(lines=lines)

    def error(exc):
        box["error"] = exc

    ns.generate = generate
    ns.chat = chat
    ns.error = error
    return ns


@pytest.fixture
def fake_decider(monkeypatch):
    """Patch urlopen in decider.py and script canned responses.

    Use .respond(body_dict) for a 200 with a JSON body, .raw_body(bytes) for
    a 200 with an arbitrary (e.g. unparseable) body, .error(exc) to make the
    next call raise directly, and .http_error(code) for a non-200 via a real
    urllib.error.HTTPError (what urlopen actually raises for a non-2xx
    response). .calls is the number of times urlopen was invoked.
    """
    import decider

    box = {"factory": None, "error": None, "calls": 0}

    def fake_urlopen(req, timeout=None):
        box["calls"] += 1
        if box["error"] is not None:
            raise box["error"]
        return box["factory"]()

    monkeypatch.setattr(decider, "urlopen", fake_urlopen)

    ns = SimpleNamespace()

    def respond(body):
        box["error"] = None
        box["factory"] = lambda: _FakeResp(body=json.dumps(body).encode())

    def raw_body(raw_bytes):
        box["error"] = None
        box["factory"] = lambda: _FakeResp(body=raw_bytes)

    def error(exc):
        box["error"] = exc

    def http_error(code=500):
        from urllib.error import HTTPError
        box["error"] = HTTPError(
            "https://openrouter.ai/api/alpha/decisions", code, "error", None, None
        )

    ns.respond = respond
    ns.raw_body = raw_body
    ns.error = error
    ns.http_error = http_error
    ns.calls = lambda: box["calls"]
    return ns


@pytest.fixture
def settings_merge(tmp_path, monkeypatch):
    """The hyphenated settings-merge.py module, with SETTINGS_FILE in tmp."""
    mod = _load_hyphenated("settings_merge", "settings-merge.py")
    monkeypatch.setattr(mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    return mod


@pytest.fixture
def hobson_entry():
    """The hobson.py entrypoint module (importing it does not run main())."""
    return _load_hyphenated("hobson_entry", "hobson.py")
