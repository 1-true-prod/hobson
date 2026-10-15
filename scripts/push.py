#!/usr/bin/env python3
"""push.py -- send what presence holds while you are away to your phone.

A wait that sits on the salver (presence.hold) is unseen until you are back,
which can be an hour. With `push.backend` "ntfy", presence.route calls
notify() for a held phrase of a push-worthy kind, only when you are away: not
on a call, not with company, where a phone buzzing would say what the voice
was kept from saying.

The second place data leaves the Mac (the first is the decider). One POST to
the topic's URL carries a fixed sentence per kind as the body ("webapp is
waiting on you.") and the project label as the title, and an access token as
a bearer token. Never the spoken phrase, which on the daemon engines is model
output written from the session's last message. The token is NTFY_TOKEN from the environment or ~/.claude/hobson.env,
never hobson.json (`hobson config show` prints that), and never logged. It
goes only to https, or to plain http on loopback, and a redirect is refused:
urllib would forward the header to wherever it points.

The hook never waits for the network. notify() decides (config, a rate
limit) and starts this script in a session of its own (start_new_session, so
it outlives the hook) with the payload on its standard input; the child makes
the request, writes one `[push]` line, and exits. Every failure is that one
line and costs nothing else.

Rate limit, kept in state_path(): one push per project and kind within
COLLAPSE_SECONDS, which folds a session's repeated waits into one buzz, and at
most `push.per_hour` pushes in any hour. A slot is given back when no server
answered (the child could not start, the connection failed or timed out) or
it answered with a 5xx, so an outage while away does not use up the hour.

    python3 scripts/push.py --test         send one now, here, and say what happened
    python3 scripts/push.py --check-url URL  why `hobson push on` refuses it, or nothing
    python3 scripts/push.py --status-text
"""

import argparse
import base64
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home  # noqa: E402
import kinds  # noqa: E402
import log_record  # noqa: E402

COLLAPSE_SECONDS = 600
HOUR = 3600
TIMEOUT_SECONDS = 10
TOKEN_NAME = "NTFY_TOKEN"
PUSHABLE = ("waiting", "broken", "stalled")


def state_path():
    return home.path("hobson-push.json")


def body_for(project, kind, phrase=""):
    """The sentence that goes out: fixed words and the project label, nothing
    from the session. A stall's minutes are read from the watchdog's own
    sentence, digits only."""
    name = project or "A session"
    if kind == "waiting":
        return f"{name} is waiting on you."
    if kind == "broken":
        return f"{name} stopped on a failure."
    if kind == "stalled":
        minutes = re.search(r"for ([0-9]{1,3}) minutes", phrase or "")
        return (f"Nothing has moved on {name} for {minutes.group(1)} minutes." if minutes
                else f"Nothing has moved on {name}.")
    raise ValueError(f"{kind} is not pushed")


def refusal(url):
    """Why `hobson push on` will not take this URL, or None."""
    if not home.is_valid_url(url):
        return "that is not a valid URL"
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "give the topic's full URL, such as https://ntfy.example.com/your-topic"
    if not home.https_or_loopback(url):
        return "plain http is for this machine only; use https"
    return None


def find_token():
    return os.environ.get(TOKEN_NAME) or home.read_env(TOKEN_NAME)


def settings(config):
    """The push section with a url that is a string (else none) and `kinds`
    cut to what can be pushed: a list, of PUSHABLE kinds. What was cut is
    in "ignored" ("kinds" itself when it is not a list)."""
    cfg = dict(home.with_defaults(config or {})["push"])
    if not isinstance(cfg["url"], str):
        cfg["url"] = ""
    configured = cfg["kinds"]
    if not isinstance(configured, list):
        cfg["kinds"], cfg["ignored"] = [], ["kinds"]
    else:
        cfg["kinds"] = [k for k in configured if k in PUSHABLE]
        cfg["ignored"] = [k for k in configured if k not in PUSHABLE]
    return cfg


def unusable(cfg):
    """Why this config cannot push, or None."""
    if not cfg["url"]:
        return "no url"
    if not home.is_valid_url(cfg["url"]):
        return "the url is not a valid URL"
    if not home.https_or_loopback(cfg["url"]):
        return "the url is not https"
    if not find_token():
        return f"no {TOKEN_NAME}"
    return None


@contextlib.contextmanager
def _state():
    """The rate-limit record under its lock, saved once on a clean exit:
    {"sent": [ts, ...], "last": {"project\\x1fkind": ts}}."""
    os.makedirs(home.state_dir(), exist_ok=True)
    with open(home.path("hobson-push.lock"), "a+", encoding="utf-8") as lock:
        deadline = time.monotonic() + 3.0
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break  # go ahead without it rather than hang a hook
                time.sleep(0.01)
        try:
            with open(state_path(), encoding="utf-8") as f:
                record = json.load(f)
            sent = [float(t) for t in record.get("sent") or []]
            last = {k: float(t) for k, t in (record.get("last") or {}).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            sent, last = [], {}
        yield sent, last
        home.write_json_atomic(state_path(), {"sent": sent, "last": last})


def claim(project, kind, per_hour, now):
    """Take a slot to push this phrase: None if taken (and recorded), else
    why not: "collapsed" or "over the hourly cap"."""
    key = f"{project}\x1f{kind}"
    with _state() as (sent, last):
        sent[:] = [t for t in sent if 0 <= now - t < HOUR]
        for k in [k for k, t in last.items() if not 0 <= now - t < COLLAPSE_SECONDS]:
            del last[k]
        if key in last:
            return "collapsed"
        if len(sent) >= per_hour:
            return "over the hourly cap"
        sent.append(now)
        last[key] = now
    return None


def release(slot):
    """Give back the hourly slot `claim` took at `slot`, for a push that no
    server answered. `last` stays: the collapse still bounds the retries."""
    with _state() as (sent, last):
        if slot in sent:
            sent.remove(slot)


def answered(detail):
    """Whether a failure's detail is a server answering under 500 (a redirect
    or a 4xx), which keeps its slot, rather than a request that got nowhere or
    a server in trouble."""
    m = re.fullmatch(r"HTTP ([0-9]{3})", detail)
    return bool(m) and int(m.group(1)) < 500


def notify(config, project, kind, phrase, now=None):
    """Push a held phrase of `kind` to the phone in a detached process. Only a
    fixed sentence goes out (body_for). Returns None when it was started, else
    why it was not. Never raises, whatever the push section holds."""
    project = project or ""
    slot = None
    try:
        cfg = settings(config)
        if cfg["backend"] != "ntfy":
            return "off"
        if kind not in cfg["kinds"]:
            if "kinds" in cfg["ignored"]:
                log_record.write(f"[push] not sent ({kind}): push.kinds is not a list")
            elif kind in cfg["ignored"]:
                log_record.write(f"[push] not sent ({kind}): only {', '.join(PUSHABLE)} can be pushed")
            return "off"
        why = unusable(cfg)
        if not why:
            slot = time.time() if now is None else now
            why = claim(project, kind, cfg["per_hour"], slot)
            if why:
                slot = None
        if why:
            log_record.write(f"[push] not sent ({kind}): {why}")
            return why
        _spawn({"project": project, "kind": kind, "body": body_for(project, kind, phrase),
                "slot": slot})
    except Exception as e:
        if slot is not None:
            with contextlib.suppress(Exception):
                release(slot)
        with contextlib.suppress(Exception):
            log_record.write(f"[push] could not start: {type(e).__name__}")
        return "could not start"
    return None


def _spawn(payload):
    import subprocess  # spawners only
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--send"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    child.stdin.write(json.dumps(payload).encode("utf-8"))
    child.stdin.close()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _header_text(text):
    """A header value: as is when ASCII, else RFC 2047, which ntfy decodes."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def deliver(config, project, kind, body):
    """The request itself, here and now. (True, "200") or (False, why); why
    is a status or an exception's class, never its text, which could carry
    the URL. Never raises."""
    cfg = settings(config)
    why = unusable(cfg)
    if why:
        return False, why
    headers = {"Authorization": f"Bearer {find_token()}",
               "Content-Type": "text/plain; charset=utf-8",
               "Priority": "high" if kinds.of(kind).urgency <= 1 else "default"}
    if project:
        headers["Title"] = _header_text(project)
    req = Request(cfg["url"], data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with build_opener(_NoRedirect).open(req, timeout=TIMEOUT_SECONDS) as resp:
            return True, str(resp.status)
    except HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__


def _send():
    """--send: the detached child. Payload on stdin, one log line."""
    try:
        payload = json.load(sys.stdin)
        project, kind, body, slot = payload["project"], payload["kind"], payload["body"], payload["slot"]
    except (ValueError, KeyError, TypeError):
        log_record.write("[push] failed: unreadable payload")
        return 1
    ok, detail = deliver(home.load_config(), project, kind, body)
    log_record.write(f"[push] sent ({kind}, {detail})" if ok else f"[push] failed ({kind}): {detail}")
    if not ok and not answered(detail):
        with contextlib.suppress(Exception):
            release(slot)
    return 0 if ok else 1


def _test():
    config = home.load_config()
    if settings(config)["backend"] != "ntfy":
        print("Push is off: hobson push on URL")
        return 1
    ok, detail = deliver(config, "hobson", "waiting", "This is a test push from Hobson.")
    log_record.write(f"[push] test {'sent (' + detail + ')' if ok else 'failed: ' + detail}")
    print(f"Sent ({detail}). It should reach the phone now." if ok else f"Not sent: {detail}")
    return 0 if ok else 1


def status_lines(config=None):
    cfg = settings(home.load_config() if config is None else config)
    if cfg["backend"] != "ntfy":
        return ["Push: off"]
    host = (urlsplit(cfg["url"]).hostname or "no url") if home.is_valid_url(cfg["url"]) else "not a valid URL"
    token = "found" if find_token() else f"missing ({TOKEN_NAME} in the environment or hobson.env)"
    return [f"Push: ntfy at {host}", f"Token: {token}",
            f"Kinds: {', '.join(cfg['kinds']) or 'none'}, only while you are away",
            f"Limit: {cfg['per_hour']} an hour, one per project and kind in {COLLAPSE_SECONDS // 60} minutes"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--send", action="store_true", help="the detached child: payload on stdin")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--status-text", action="store_true")
    parser.add_argument("--check-url", metavar="URL", help="print why URL is refused; exit 1 if it is")
    args = parser.parse_args(argv)
    if args.send:
        return _send()
    if args.check_url is not None:
        why = refusal(args.check_url)
        if why:
            print(why)
        return 1 if why else 0
    if args.test:
        return _test()
    print("\n".join(status_lines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
