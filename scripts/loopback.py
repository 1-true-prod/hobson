"""loopback.py — the guard every Hobson HTTP server on 127.0.0.1 runs.

Three servers listen on this machine: the setup wizard and the two TTS
daemons. Binding to 127.0.0.1 keeps out the network, not web pages: any page
in any browser can send requests to localhost. What keeps those out is here,
once, on every method and route:

  - Host must name this server (127.0.0.1:PORT or localhost:PORT). A DNS-
    rebinding page reaches us under its own name, and could read replies.
  - Origin, when a browser sends one (it does on every POST), must be this
    server. A cross-origin POST with a text/plain body needs no preflight: it
    used to reach the daemons' /shutdown and /generate from any page.
  - A token, in X-Hobson-Token and compared in constant time, on every route
    that acts, for anything else on the machine. The wizard's comes in its
    window's URL; a daemon's is in a mode-600 file only that daemon writes
    (issue_token).
  - A cap on the body, and on how long a request may stall.

The guards used to live inside the wizard alone. The daemons, near-copies of
each other, had none, so a fix reached one server of three.

Standard library only, Python 3.9: the daemons import this from their own
venvs.
"""

import hmac
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN_HEADER = "X-Hobson-Token"


class Server(ThreadingHTTPServer):
    """A server on 127.0.0.1 only. `token` is what routes that act require;
    None refuses them all."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, handler, token=None):
        super().__init__(("127.0.0.1", port), handler)
        self.token = token


class GuardedHandler(BaseHTTPRequestHandler):
    """Every do_* method starts with `if not self.guard(token=...): return`."""

    max_body = 64 * 1024
    # Seconds a socket read or write may block: a client that promises a
    # body and never sends it would otherwise hold its thread forever.
    timeout = 10

    def log_message(self, *args):
        pass  # a URL can carry a token: nothing about requests is logged

    def guard(self, token):
        """True when this request may go on; otherwise a 403 has been sent.
        `token`: whether this route needs the server's token."""
        port = self.server.server_address[1]
        names = (f"127.0.0.1:{port}", f"localhost:{port}")
        origin = self.headers.get("Origin")
        ok = (self.headers.get("Host") in names
              and (origin is None or origin in tuple("http://" + n for n in names))
              and (not token or self._token_ok()))
        if not ok:
            self.send_json(403, {"error": "forbidden"})
        return ok

    def _token_ok(self):
        expected = self.server.token
        given = self.headers.get(TOKEN_HEADER) or ""
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

    def read_json(self):
        """The request body, a JSON object, as a dict. ValueError when it is
        over max_body, not JSON, or not an object."""
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= self.max_body:
            raise ValueError("body too large")
        data = json.loads(self.rfile.read(length) if length else b"{}")
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data

    def send_json(self, status, body):
        self.send_body(status, json.dumps(body).encode("utf-8"), "application/json")

    def send_body(self, status, data, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def issue_token(path):
    """A fresh token, written to `path` at mode 600 from its first byte
    (never readable by anyone else, even briefly), and returned."""
    token = secrets.token_urlsafe(32)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, path)
    return token


def read_token(path):
    """The token in `path`, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None
