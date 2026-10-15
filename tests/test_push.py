"""Push to the phone what presence holds while you are away.

The ntfy server is a stand-in on loopback that records each request, so the
whole path is exercised over real HTTP, and the detached child is a real
process (the suite's Popen stub is bypassed for it on purpose).
"""

import io
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import home
import log_record
import presence
import push

REAL_POPEN = subprocess.Popen
TOKEN = "tk_secret0123456789"


class _Ntfy:
    def __init__(self):
        self.requests = []
        self.delay = 0.0
        self.status = 200
        self.redirect_to = None
        self.redirect_status = 307
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"path": self.path, "headers": dict(self.headers.items()),
                                       "body": body.decode("utf-8")})
                time.sleep(outer.delay)
                if outer.redirect_to:
                    self.send_response(outer.redirect_status)
                    self.send_header("Location", outer.redirect_to)
                else:
                    self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hobson-phone"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def wait_for(self, n, seconds=10):
        deadline = time.time() + seconds
        while len(self.requests) < n and time.time() < deadline:
            time.sleep(0.02)
        return self.requests


@pytest.fixture
def ntfy():
    server = _Ntfy()
    yield server
    server.httpd.shutdown()


@pytest.fixture
def configured(claude_home, ntfy):
    """hobson.json with push on at the stand-in, and the token in hobson.env."""
    (claude_home / "hobson.json").write_text(json.dumps({"push": {"backend": "ntfy", "url": ntfy.url}}))
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    return home.load_config()


@pytest.fixture
def spawned(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "_spawn", sent.append)
    return sent


def _log(claude_home):
    return (claude_home / "hobson.log").read_text() if (claude_home / "hobson.log").exists() else ""


def _away(claude_home, state="away"):
    (claude_home / "hobson-presence.json").write_text(json.dumps(
        {"state": state, "source": "lock", "ts": time.time(), "since": time.time(),
         "mode": "auto", "camera": "authorized", "pid": 0, "glance_ts": 0,
         "face": True, "face_only": False, "waves": True}))


# ── What goes over the wire ────────────────────────────────────────────────

def test_the_request_carries_the_phrase_the_project_and_the_token_only(configured, ntfy):
    ok, detail = push.deliver(configured, "webapp", "waiting", "Webapp needs your approval.")
    assert (ok, detail) == (True, "200")
    (request,) = ntfy.requests
    assert request["path"] == "/hobson-phone"
    assert request["body"] == "Webapp needs your approval."
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["Title"] == "webapp"
    assert request["headers"]["Priority"] == "high"
    assert sorted(request["headers"]) == sorted(
        ["Host", "Accept-Encoding", "Authorization", "Content-Type", "Priority", "Title",
         "Content-Length", "User-Agent", "Connection"])


def test_a_project_label_that_is_not_ascii_is_encoded_for_the_header(configured, ntfy):
    push.deliver(configured, "café", "stalled", "Nothing has moved.")
    assert ntfy.requests[0]["headers"]["Title"] == "=?UTF-8?B?Y2Fmw6k=?="
    assert ntfy.requests[0]["headers"]["Priority"] == "default"


@pytest.mark.parametrize("status", [301, 302, 303, 307])
def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(configured, ntfy, status):
    other = _Ntfy()
    ntfy.redirect_to = other.url
    ntfy.redirect_status = status
    ok, detail = push.deliver(configured, "webapp", "waiting", "Hello.")
    assert (ok, detail) == (False, f"HTTP {status}")
    assert other.requests == []
    other.httpd.shutdown()


def test_a_refusal_is_reported_as_a_status_and_never_as_the_url(configured, ntfy):
    ntfy.status = 401
    assert push.deliver(configured, "webapp", "waiting", "Hello.") == (False, "HTTP 401")


def test_an_unreachable_server_is_reported_by_class(claude_home):
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    config = {"push": {"backend": "ntfy", "url": "http://127.0.0.1:9/never"}}
    ok, detail = push.deliver(config, "webapp", "waiting", "Hello.")
    assert ok is False and detail == "URLError"


@pytest.mark.parametrize("url, token, why", [
    ("", True, "no url"),
    ("http://ntfy.example.com/t", True, "the url is not https"),
    ("https://ntfy.example.com/t", False, "no NTFY_TOKEN"),
])
def test_a_config_that_cannot_push_says_why(claude_home, url, token, why):
    if token:
        (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    assert push.unusable(push.settings({"push": {"backend": "ntfy", "url": url}})) == why


def test_the_token_comes_from_the_environment_first(claude_home, monkeypatch):
    (claude_home / "hobson.env").write_text("NTFY_TOKEN=from-file\n")
    monkeypatch.setenv("NTFY_TOKEN", "from-env")
    assert push.find_token() == "from-env"
    monkeypatch.delenv("NTFY_TOKEN")
    assert push.find_token() == "from-file"


# ── Whether to push ────────────────────────────────────────────────────────

def test_nothing_is_pushed_with_the_backend_off(claude_home, spawned):
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    assert push.notify({}, "webapp", "waiting", "Hello.") == "off"
    assert spawned == [] and _log(claude_home) == ""


def test_a_done_is_not_pushed_by_default(configured, spawned):
    assert push.notify(configured, "webapp", "done", "I shipped it.") == "off"
    assert spawned == []


def test_a_wait_is_started_with_a_fixed_sentence_and_never_the_phrase(configured, spawned):
    phrase = "Should I add a note on test [8]'s cancel path?"
    assert push.notify(configured, "webapp", "waiting", phrase, now=1000.5) is None
    assert spawned == [{"project": "webapp", "kind": "waiting",
                        "body": "webapp is waiting on you.", "slot": 1000.5}]
    assert "cancel" not in json.dumps(spawned)


@pytest.mark.parametrize("kind, phrase, body", [
    ("waiting", "Anything at all, in the model's words.", "webapp is waiting on you."),
    ("broken", "I failed to fetch the ticket's Scope.", "webapp stopped on a failure."),
    ("stalled", "Nothing's moved on webapp for 10 minutes.", "Nothing has moved on webapp for 10 minutes."),
    ("stalled", "Nothing's moved on webapp for ten minutes; rm -rf.", "Nothing has moved on webapp."),
    ("stalled", "Nothing's moved on webapp for \u0661\u0660 minutes.", "Nothing has moved on webapp."),
])
def test_the_body_is_fixed_words_and_the_project(kind, phrase, body):
    assert push.body_for("webapp", kind, phrase) == body


def test_a_kind_that_cannot_be_pushed_has_no_sentence():
    with pytest.raises(ValueError):
        push.body_for("webapp", "done", "Finished.")


@pytest.mark.parametrize("kinds", [["waiting", "done"], ["done", "nudge", "commentary", 7]])
def test_a_configured_kind_that_cannot_be_pushed_is_ignored_with_one_line(claude_home, ntfy, spawned, kinds):
    (claude_home / "hobson.json").write_text(json.dumps({"push": {
        "backend": "ntfy", "url": ntfy.url, "kinds": kinds}}))
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    config = home.load_config()
    assert push.notify(config, "webapp", "done", "Finished.") == "off"
    assert _log(claude_home).count("[push] not sent (done): only waiting, broken, stalled can be pushed") == 1
    assert spawned == []
    assert (push.notify(config, "webapp", "waiting", "Hello.") is None) == ("waiting" in kinds)


def test_kinds_that_is_not_a_list_pushes_nothing(claude_home, ntfy, spawned):
    (claude_home / "hobson.json").write_text(json.dumps({"push": {
        "backend": "ntfy", "url": ntfy.url, "kinds": "waiting"}}))
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    assert push.notify(home.load_config(), "webapp", "waiting", "Hello.") == "off"
    assert "[push] not sent (waiting): push.kinds is not a list" in _log(claude_home)
    assert spawned == []


@pytest.mark.parametrize("url", [123, ["https://ntfy.example.com/t"], None, {"a": 1}])
def test_a_url_that_is_not_a_string_is_no_url(claude_home, capsys, url):
    config = {"push": {"backend": "ntfy", "url": url}}
    assert push.deliver(config, "webapp", "waiting", "Hello.") == (False, "no url")
    assert push.status_lines(config)[0] == "Push: ntfy at no url"


def test_a_malformed_push_section_costs_a_log_line_and_raises_nothing(claude_home, spawned):
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    for section in ({"backend": "ntfy", "url": "https://ntfy.example.com/t", "kinds": None},
                    {"backend": "ntfy", "url": "https://ntfy.example.com/t", "per_hour": "six"},
                    "ntfy"):
        assert push.notify({"push": section}, "webapp", "waiting", "Hello.") in ("could not start", "off")
    assert spawned == []


def test_a_config_that_cannot_push_logs_it_and_starts_nothing(claude_home, spawned):
    config = {"push": {"backend": "ntfy", "url": "https://ntfy.example.com/t"}}
    assert push.notify(config, "webapp", "waiting", "Hello.") == "no NTFY_TOKEN"
    assert spawned == []
    assert "[push] not sent (waiting): no NTFY_TOKEN" in _log(claude_home)


def test_a_spawn_that_raises_costs_one_log_line(configured, claude_home, monkeypatch):
    def boom(payload):
        raise OSError("no fork")
    monkeypatch.setattr(push, "_spawn", boom)
    assert push.notify(configured, "webapp", "waiting", "Hello.") == "could not start"
    assert "[push] could not start: OSError" in _log(claude_home)


def _recorded(claude_home):
    return json.loads((claude_home / "hobson-push.json").read_text())


def test_a_spawn_that_fails_gives_its_slot_back(configured, claude_home, monkeypatch):
    def boom(payload):
        raise OSError("no fork")
    monkeypatch.setattr(push, "_spawn", boom)
    for n in range(6):
        assert push.notify(configured, "webapp", "waiting", "Hello.", now=1000 + n) == (
            "could not start" if n == 0 else "collapsed")
    assert _recorded(claude_home) == {"sent": [], "last": {"webapp\x1fwaiting": 1000}}
    sent = []
    monkeypatch.setattr(push, "_spawn", sent.append)
    assert push.notify(configured, "webapp", "waiting", "Hello.", now=1010) == "collapsed"
    assert push.notify(configured, "other", "waiting", "Hello.", now=1010) is None
    assert len(sent) == 1


def test_a_failed_send_gives_its_slot_back(configured, claude_home, ntfy, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    ntfy.status = 500
    for n in range(2):
        assert push.notify(configured, f"project {n}", "waiting", "Hello.") is None
    ntfy.wait_for(2)
    deadline = time.time() + 10
    while time.time() < deadline and (_log(claude_home).count("[push] failed") < 2
                                      or _recorded(claude_home)["sent"]):
        time.sleep(0.05)
    assert set(_recorded(claude_home)["last"]) == {"project 0\x1fwaiting", "project 1\x1fwaiting"}
    assert _recorded(claude_home)["sent"] == []


def test_a_delivered_push_keeps_its_slot(configured, claude_home, ntfy, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    assert push.notify(configured, "webapp", "waiting", "Hello.", now=time.time()) is None
    deadline = time.time() + 10
    while "[push] sent" not in _log(claude_home) and time.time() < deadline:
        time.sleep(0.05)
    assert len(ntfy.wait_for(1)) == 1
    assert "[push] sent (waiting, 200)" in _log(claude_home)
    assert len(_recorded(claude_home)["sent"]) == 1


@pytest.fixture
def inline_child(monkeypatch):
    """The detached child run to completion here, so a count is read after it."""
    def run(payload):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        push._send()
    monkeypatch.setattr(push, "_spawn", run)


def _pushes(config, projects):
    return [push.notify(config, project, "waiting", "Hello.") for project in projects]


@pytest.mark.parametrize("status, requests, held", [(401, 6, 6), (403, 6, 6), (429, 6, 6), (500, 8, 0), (503, 8, 0)])
def test_only_a_server_that_did_not_answer_gives_its_slot_back(configured, claude_home, ntfy, inline_child,
                                                                status, requests, held):
    ntfy.status = status
    results = _pushes(configured, [f"project {n}" for n in range(8)])
    assert len(ntfy.requests) == requests
    assert results.count("over the hourly cap") == 8 - requests
    assert len(_recorded(claude_home)["sent"]) == held


@pytest.mark.parametrize("status, requests", [(401, 1), (429, 1), (500, 1), (200, 1)])
def test_one_project_and_kind_is_asked_once_in_ten_minutes_whatever_the_answer(
        configured, ntfy, inline_child, status, requests):
    ntfy.status = status
    assert _pushes(configured, ["webapp"] * 12).count(None) == 1
    assert len(ntfy.requests) == requests


def test_a_server_that_is_down_gives_every_slot_back_and_is_asked_once_per_project(
        claude_home, inline_child, ntfy):
    (claude_home / "hobson.json").write_text(json.dumps({"push": {
        "backend": "ntfy", "url": "http://127.0.0.1:1/hobson-phone"}}))
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    config = home.load_config()
    assert _pushes(config, [f"project {n}" for n in range(8)] + ["project 0"] * 4).count(None) == 8
    assert _log(claude_home).count("[push] failed (waiting): ") == 8
    assert _recorded(claude_home)["sent"] == []


def test_a_clock_stepped_back_does_not_block_pushes(configured, spawned):
    for n in range(6):
        push.notify(configured, f"project {n}", "waiting", "Hello.", now=900000 + n)
    assert push.notify(configured, "late", "waiting", "Hello.", now=1000) is None
    assert push.notify(configured, "project 0", "waiting", "Hello.", now=1001) is None
    assert len(spawned) == 8


# ── The rate limit ─────────────────────────────────────────────────────────

def test_a_projects_repeated_waits_collapse_into_one_push(configured, spawned):
    results = [push.notify(configured, "webapp", "waiting", f"Wait {n}.", now=1000 + n * 60)
               for n in range(4)]
    assert results == [None, "collapsed", "collapsed", "collapsed"]
    assert len(spawned) == 1


def test_the_same_wait_is_pushed_again_after_the_collapse_window(configured, spawned):
    push.notify(configured, "webapp", "waiting", "First.", now=1000)
    assert push.notify(configured, "webapp", "waiting", "Again.", now=1000 + push.COLLAPSE_SECONDS) is None
    assert len(spawned) == 2


def test_another_project_or_another_kind_is_not_collapsed(configured, spawned):
    push.notify(configured, "webapp", "waiting", "One.", now=1000)
    push.notify(configured, "api", "waiting", "Two.", now=1001)
    push.notify(configured, "webapp", "broken", "Three.", now=1002)
    assert [p["project"] + "/" + p["kind"] for p in spawned] == [
        "webapp/waiting", "api/waiting", "webapp/broken"]


def test_no_more_than_six_pushes_an_hour(configured, spawned):
    results = [push.notify(configured, f"project {n}", "waiting", "Hello.", now=1000 + n)
               for n in range(9)]
    assert results.count(None) == 6
    assert results[6:] == ["over the hourly cap"] * 3
    assert push.notify(configured, "project 20", "waiting", "Hello.", now=1000 + 3601) is None
    assert len(spawned) == 7


def test_the_cap_follows_the_config(claude_home, spawned):
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    config = {"push": {"backend": "ntfy", "url": "https://ntfy.example.com/t", "per_hour": 2}}
    results = [push.notify(config, f"p{n}", "waiting", "Hello.", now=1000 + n) for n in range(4)]
    assert results == [None, None, "over the hourly cap", "over the hourly cap"]


# ── Only when away ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("state, pushed", [("away", 1), ("call", 0), ("company", 0), ("present", 0)])
def test_only_away_pushes(configured, claude_home, spawned, state, pushed):
    _away(claude_home, state)
    config = dict(configured, presence={"enabled": True})
    presence.route(config, "waiting", "Webapp needs you.", project="webapp")
    assert len(spawned) == pushed


def test_a_phrase_away_holds_is_pushed_and_still_on_the_salver(configured, claude_home, spawned):
    _away(claude_home)
    assert presence.route(dict(configured, presence={"enabled": True}),
                          "broken", "The build failed.", project="webapp") is None
    assert [i["phrase"] for i in presence.held()] == ["The build failed."]
    assert [(p["project"], p["kind"], p["body"]) for p in spawned] == [
        ("webapp", "broken", "webapp stopped on a failure.")]


class _Raising:
    @staticmethod
    def notify(*args):
        raise ValueError("bad section")


def test_a_push_that_raises_leaves_route_holding_and_silent(configured, claude_home, monkeypatch):
    _away(claude_home)
    monkeypatch.setattr(push, "notify", _Raising.notify)
    assert presence.route(dict(configured, presence={"enabled": True}),
                          "waiting", "Webapp needs you.", project="webapp") is None
    assert [i["phrase"] for i in presence.held()] == ["Webapp needs you."]
    assert "[push] could not start: ValueError" in _log(claude_home)


@pytest.mark.parametrize("section", [{"backend": "ntfy", "kinds": None}, "ntfy", None, 3])
def test_a_malformed_push_section_leaves_route_as_it_was(claude_home, section):
    _away(claude_home)
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    config = {"presence": {"enabled": True}, "push": section}
    assert presence.route(config, "waiting", "Webapp needs you.", project="webapp") is None
    assert len(presence.held()) == 1


def test_push_is_not_imported_while_it_is_off(configured, claude_home, monkeypatch):
    _away(claude_home)
    monkeypatch.delitem(sys.modules, "push")
    presence.route({"presence": {"enabled": True}}, "waiting", "Hello.", project="webapp")
    assert "push" not in sys.modules
    monkeypatch.setitem(sys.modules, "push", push)


def test_commentary_is_never_pushed(configured, claude_home, spawned):
    _away(claude_home)
    presence.route(dict(configured, presence={"enabled": True}), "commentary", "Editing.", project="webapp")
    assert spawned == []


# ── The detached child ─────────────────────────────────────────────────────

def test_the_hook_returns_before_a_slow_server_answers(configured, claude_home, ntfy, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    ntfy.delay = 2.0
    started = time.monotonic()
    assert push.notify(configured, "webapp", "waiting", "Webapp needs you.") is None
    returned_after = time.monotonic() - started
    assert returned_after < 1.0
    assert ntfy.wait_for(1)[0]["body"] == "webapp is waiting on you."
    deadline = time.time() + 10
    while "[push] sent" not in _log(claude_home) and time.time() < deadline:
        time.sleep(0.05)
    assert "[push] sent (waiting, 200)" in _log(claude_home)
    assert time.monotonic() - started >= 2.0


def test_the_token_never_reaches_the_log_or_a_command_line(configured, claude_home, ntfy, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    ntfy.status = 500
    push.notify(configured, "webapp", "waiting", "Webapp needs you.")
    ntfy.wait_for(1)
    deadline = time.time() + 10
    while "[push] failed" not in _log(claude_home) and time.time() < deadline:
        time.sleep(0.05)
    log = _log(claude_home)
    assert "[push] failed (waiting): HTTP 500" in log
    assert TOKEN not in log and ntfy.url not in log


def test_a_child_with_an_unreadable_payload_logs_one_line(claude_home, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    done = subprocess.run([sys.executable, os.path.join(os.path.dirname(push.__file__), "push.py"), "--send"],
                          input=b"{not json", timeout=20)
    assert done.returncode == 1
    assert "[push] failed: unreadable payload" in _log(claude_home)


def test_the_test_command_sends_here_and_now(configured, ntfy, capsys):
    assert push.main(["--test"]) == 0
    assert ntfy.requests[0]["body"] == "This is a test push from Hobson."
    assert capsys.readouterr().out.startswith("Sent (200)")


def test_the_test_command_with_push_off_says_how_to_turn_it_on(claude_home, capsys):
    assert push.main(["--test"]) == 1
    assert "hobson push on URL" in capsys.readouterr().out


def test_status_shows_the_host_and_never_the_topic_or_the_token(configured, capsys):
    push.main(["--status-text"])
    out = capsys.readouterr().out
    assert "ntfy at 127.0.0.1" in out and "Token: found" in out
    assert "hobson-phone" not in out and TOKEN not in out


def test_a_push_line_is_not_read_as_something_said(claude_home):
    import log_stats
    log_record.write("[push] sent (waiting, 200)")
    assert log_stats.summarize(_log(claude_home).splitlines())["barked"] == 0


# ── The URL ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, why", [
    ("https://ntfy.example.com/t", None),
    ("http://127.0.0.1:8080/t", None),
    ("http://ntfy.example.com/t", "plain http is for this machine only; use https"),
    ("http://[::1", "that is not a valid URL"),
    ("ntfy.example.com/t", "give the topic's full URL, such as https://ntfy.example.com/your-topic"),
    ("", "give the topic's full URL, such as https://ntfy.example.com/your-topic"),
])
def test_push_on_refuses_what_could_never_work(url, why):
    assert push.refusal(url) == why


def test_a_malformed_stored_url_is_reported_and_never_raises(claude_home, capsys):
    (claude_home / "hobson.env").write_text(f"NTFY_TOKEN={TOKEN}\n")
    config = {"push": {"backend": "ntfy", "url": "http://[::1"}}
    assert push.deliver(config, "webapp", "waiting", "Hello.") == (False, "the url is not a valid URL")
    assert push.status_lines(config)[0] == "Push: ntfy at not a valid URL"
