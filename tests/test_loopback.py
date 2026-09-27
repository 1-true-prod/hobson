"""loopback.py and tts_daemon.py: the guard every local server runs, over real HTTP.

The TTS daemons used to accept /shutdown and /generate from anything that
could reach 127.0.0.1, which is any web page: a text/plain POST needs no
CORS preflight. Pocket TTS also fetched whatever URL a request named as its
voice. These drive the daemon runner with a stand-in backend, so no model
is loaded; the wizard's side is in test_setup_wizard.py.
"""

import http.client
import json
import os
import stat
import threading
import time

import pytest

import loopback
import tts_daemon

_WAV = b"RIFF" + b"\0" * 200
_CUSTOM = "/Users/me/voices/my-voice.wav"  # a startup voice the user configured


@pytest.fixture
def daemon(claude_home):
    calls = []

    def synthesize(text, voice, params):
        calls.append((text, voice))
        return _WAV

    tts = tts_daemon.Daemon("/x/scripts/pocket-tts-daemon.py",
                            tts_daemon.Backend(synthesize=synthesize, voices=frozenset({"alba", "charles"})),
                            _CUSTOM, idle_timeout=600)
    tts.bind(0)
    thread = threading.Thread(target=tts.serve, kwargs={"poll_interval": 0.005}, daemon=True)
    thread.start()
    tts.calls = calls
    tts.port = tts.server.server_address[1]
    yield tts
    tts.stop()
    thread.join(2)


def _send(tts, method, path, body=None, token=True, host=None, origin=None,
          ctype="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", tts.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{tts.port}", "Content-Type": ctype}
    if token:
        headers[loopback.TOKEN_HEADER] = tts.server.token if token is True else token
    if origin:
        headers["Origin"] = origin
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    result = resp.status, resp.read()
    conn.close()
    return result


def _alive(tts):
    return _send(tts, "GET", "/health", token=False)[0] == 200


def test_a_page_elsewhere_cannot_shut_a_daemon_down(daemon):
    """What any web page could send: a simple cross-origin POST, no token."""
    status, _ = _send(daemon, "POST", "/shutdown", body=b"", token=False,
                      origin="https://evil.example", ctype="text/plain")
    assert status == 403
    assert _alive(daemon)


def test_nor_make_it_speak(daemon):
    status, _ = _send(daemon, "POST", "/generate", body=json.dumps({"text": "hello"}).encode(),
                      token=False, origin="https://evil.example", ctype="text/plain")
    assert status == 403
    assert daemon.calls == []


def test_another_origin_is_refused_even_with_the_token(daemon):
    assert _send(daemon, "POST", "/generate", {"text": "hi"}, origin="https://evil.example")[0] == 403
    assert _send(daemon, "POST", "/generate", {"text": "hi"}, origin="null")[0] == 403
    assert daemon.calls == []


def test_a_dns_rebinding_page_is_refused(daemon):
    assert _send(daemon, "GET", "/health", token=False, host=f"evil.example:{daemon.port}")[0] == 403
    assert _send(daemon, "POST", "/generate", {"text": "hi"}, host=f"evil.example:{daemon.port}")[0] == 403


def test_generate_needs_the_token(daemon):
    assert _send(daemon, "POST", "/generate", {"text": "hi"}, token=False)[0] == 403
    assert _send(daemon, "POST", "/generate", {"text": "hi"}, token="wrong")[0] == 403
    assert daemon.calls == []
    assert _send(daemon, "POST", "/generate", {"text": "I pushed the branch."}) == (200, _WAV)
    assert daemon.calls == [("I pushed the branch.", _CUSTOM)]


def test_the_engine_and_the_cli_are_not_browsers(daemon):
    """No Origin at all (urllib, curl), or this server's own: both pass."""
    assert _send(daemon, "POST", "/generate", {"text": "a"})[0] == 200
    assert _send(daemon, "POST", "/generate", {"text": "b"}, origin=f"http://127.0.0.1:{daemon.port}")[0] == 200
    assert _send(daemon, "POST", "/generate", {"text": "c"}, host=f"localhost:{daemon.port}")[0] == 200


@pytest.mark.parametrize("voice", [
    "https://evil.example/voice.safetensors",   # fetched by Pocket TTS before any other check
    "hf://someone/repo/voice.safetensors",
    "/etc/hosts.safetensors",                   # loaded from any path
    "../../voices/x.wav", "marius.safetensors", "", 42, None, ["alba"],
])
def test_a_voice_outside_the_catalogue_is_refused(daemon, voice):
    assert _send(daemon, "POST", "/generate", {"text": "hi", "voice": voice})[0] == 400
    assert daemon.calls == []


@pytest.mark.parametrize("voice", ["alba", "charles", _CUSTOM])
def test_a_catalogue_voice_or_the_startup_one_is_spoken(daemon, voice):
    assert _send(daemon, "POST", "/generate", {"text": "hi", "voice": voice})[0] == 200
    assert daemon.calls == [("hi", voice)]


def test_a_body_over_the_cap_is_refused(daemon):
    assert _send(daemon, "POST", "/generate", {"text": "x" * 20000})[0] == 400
    assert daemon.calls == []


def test_health_needs_no_token_and_says_who_it_is(daemon):
    status, body = _send(daemon, "GET", "/health", token=False)
    assert status == 200 and json.loads(body)["pid"] == os.getpid()


def test_shutdown_with_the_token_stops_it_and_removes_its_files(daemon, claude_home):
    token_file = claude_home / "pocket-tts-daemon.token"
    pid_file = claude_home / "pocket-tts-daemon.pid"
    assert token_file.read_text() == daemon.server.token
    assert pid_file.read_text() == str(os.getpid())
    assert _send(daemon, "POST", "/shutdown")[0] == 200
    # The reply goes before the handler removes the files.
    for _ in range(100):
        if not token_file.exists() and not pid_file.exists():
            break
        time.sleep(0.02)
    assert not token_file.exists() and not pid_file.exists()


def test_the_token_file_is_readable_by_its_owner_only(daemon, claude_home):
    mode = stat.S_IMODE(os.stat(claude_home / "pocket-tts-daemon.token").st_mode)
    assert mode == 0o600


def test_a_daemon_that_loses_the_port_leaves_the_winners_files_alone(daemon, claude_home):
    """Two hooks can cold-start a daemon at once. The loser must not write
    its token over the winner's, or every client is refused until idle-out."""
    loser = tts_daemon.Daemon("/x/scripts/pocket-tts-daemon.py", daemon.backend, "charles", 600)
    with pytest.raises(OSError):
        loser.bind(daemon.port)
    assert (claude_home / "pocket-tts-daemon.token").read_text() == daemon.server.token
    assert _send(daemon, "POST", "/generate", {"text": "still me"})[0] == 200


def test_files_already_replaced_by_another_daemon_are_not_removed(daemon, claude_home):
    token_file = claude_home / "pocket-tts-daemon.token"
    token_file.write_text("a newer daemon's token")
    daemon.stop()
    assert token_file.read_text() == "a newer daemon's token"


def test_issue_token_never_leaves_a_readable_copy(tmp_path):
    path = tmp_path / "x.token"
    stale = tmp_path / f"x.token.{os.getpid()}.tmp"
    stale.write_text("stale")
    os.chmod(stale, 0o644)
    token = loopback.issue_token(str(path))
    assert path.read_text() == token and len(token) >= 40
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not stale.exists()
    assert loopback.issue_token(str(path)) != token


@pytest.mark.parametrize("length", ["-5", "abc", "999999999"])
def test_a_lying_content_length_is_a_400(daemon, length):
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    conn.putrequest("POST", "/generate", skip_host=True)
    conn.putheader("Host", f"127.0.0.1:{daemon.port}")
    conn.putheader(loopback.TOKEN_HEADER, daemon.server.token)
    conn.putheader("Content-Length", length)
    conn.endheaders()
    assert conn.getresponse().status == 400
    assert daemon.calls == []


def test_a_request_with_no_host_is_refused(daemon):
    """HTTP/1.0 may leave Host out; a guard that needs one must not pass None."""
    import socket
    with socket.create_connection(("127.0.0.1", daemon.port), timeout=5) as sock:
        sock.sendall(b"GET /health HTTP/1.0\r\n\r\n")
        assert sock.recv(64).startswith(b"HTTP/1.0 403")


def test_a_body_that_never_comes_does_not_hold_the_thread(daemon, monkeypatch):
    import socket
    monkeypatch.setattr(tts_daemon._Handler, "timeout", 0.2)
    with socket.create_connection(("127.0.0.1", daemon.port), timeout=5) as sock:
        sock.sendall((f"POST /generate HTTP/1.1\r\nHost: 127.0.0.1:{daemon.port}\r\n"
                      f"{loopback.TOKEN_HEADER}: {daemon.server.token}\r\n"
                      "Content-Length: 100\r\n\r\n{\"te").encode())
        assert sock.recv(64) == b"", "the server closes the connection instead of waiting forever"
    assert _alive(daemon)
