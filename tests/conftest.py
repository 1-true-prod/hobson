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
    import home
    home._PROJECT_LABEL_CACHE = None
    bark_templates._data = None
    yield
    home._PROJECT_LABEL_CACHE = None
    bark_templates._data = None


@pytest.fixture(autouse=True)
def claude_home(tmp_path, monkeypatch):
    """Redirect ~/.claude state into tmp_path. Returns the .claude dir path.

    Setting $HOME is enough: every Hobson path comes from home.state_dir(),
    which reads it at call time.
    """
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    # Never let a real key on the developer's machine leak into a test run.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    import decider
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

    def fake_say(self, text, kind=None):
        calls["say"].append(text)

    monkeypatch.setattr(base.BaseEngine, "_say_with_volume", fake_say)
    return calls


@pytest.fixture(autouse=True)
def no_face(monkeypatch):
    """Record what would go on Hobson's face instead of showing it.

    autouse for the same reason as no_audio: the real face.show can reach
    presence.ensure_running, whose subprocess is not the one no_audio
    patches, and in a dev checkout build/HobsonPresence.app exists -- a test
    would launch it. tests/test_face.py keeps the real one, taken at import,
    with os.kill and ensure_running stubbed.

    Returns a list of (phrase, kind, audio), with "asked" appended for a
    line shown because you asked (ask.py).
    """
    import face

    shown = []

    def fake_show(config, phrase, kind, audio=None, project=None, now=None, asked=False):
        shown.append((phrase, kind, audio) + (("asked",) if asked else ()))
        return True

    monkeypatch.setattr(face, "show", fake_show)
    monkeypatch.setattr(face, "_failed", False)
    return shown


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
    """Patch urlopen in stop_outcome + phrase_gen and script canned responses.

    Use .generate(word) for /api/generate (classify) and .chat(text) for the
    streamed /api/chat (phrase_gen). .error(exc) makes the next call raise.
    .urls collects the requested URLs for assertions.
    """
    import phrase_gen
    import stop_outcome

    box = {"factory": None, "error": None, "urls": []}

    def fake_urlopen(req, timeout=None):
        box["urls"].append(getattr(req, "full_url", str(req)))
        if box["error"] is not None:
            raise box["error"]
        return box["factory"]()

    monkeypatch.setattr(stop_outcome, "urlopen", fake_urlopen)
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
    response). .respond_with(fn) answers each request with fn(payload).
    .calls is the number of times urlopen was invoked, and .sent() every
    request as it left: its payload, url and headers.
    """
    import decider

    box = {"factory": None, "error": None, "calls": 0, "sent": []}

    def fake_urlopen(req, timeout=None):
        box["calls"] += 1
        payload = json.loads(req.data)
        box["sent"].append(SimpleNamespace(payload=payload, url=req.full_url,
                                           headers=dict(req.header_items())))
        if box["error"] is not None:
            raise box["error"]
        return box["factory"](payload)

    monkeypatch.setattr(decider, "urlopen", fake_urlopen)

    ns = SimpleNamespace()

    def respond(body):
        box["error"] = None
        box["factory"] = lambda payload: _FakeResp(body=json.dumps(body).encode())

    def respond_with(fn):
        box["error"] = None
        box["factory"] = lambda payload: _FakeResp(body=json.dumps(fn(payload)).encode())

    def raw_body(raw_bytes):
        box["error"] = None
        box["factory"] = lambda payload: _FakeResp(body=raw_bytes)

    def error(exc):
        box["error"] = exc

    def http_error(code=500):
        from urllib.error import HTTPError
        box["error"] = HTTPError(
            "https://openrouter.ai/api/alpha/decisions", code, "error", None, None
        )

    ns.respond = respond
    ns.respond_with = respond_with
    ns.raw_body = raw_body
    ns.error = error
    ns.http_error = http_error
    ns.calls = lambda: box["calls"]
    ns.sent = lambda: list(box["sent"])
    return ns


@pytest.fixture
def jev(fake_decider, monkeypatch):
    """The decider on "jev" with a key, answering P = `jev.p` for every label
    of whatever is asked (None: an answer it cannot read, i.e. no opinion).
    `jev.config` selects it; `jev.states()` is each state as it left, which
    is after redaction -- the only place a test can see what was sent."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    ns = SimpleNamespace(p=0.9, config={"decider": {"backend": "jev"}})

    def answer(payload):
        if ns.p is None:
            return {"unexpected": "shape"}
        criteria = payload["questions"]["answer"]["criteria"]
        return {"answers": {"answer": {"type": "choice", "choice": next(iter(criteria)),
                                       "probabilities": {k: ns.p for k in criteria},
                                       "confidence": 1.0}}}

    fake_decider.respond_with(answer)
    ns.states = lambda: [r.payload["state"] for r in fake_decider.sent()]
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
