"""One voice at a time, across every session.

Hooks are async and every engine played its clip with a detached afplay, so
nothing stopped two clips playing at once: two sessions finishing within a
second talked over each other, and a script reading a spec aloud through the
CLI stacked a new paragraph on the one still playing every few seconds, with
the other sessions' announcements on top.

Now every clip takes a turn first (`take_turn`), and the player is handed the
locked descriptor (`pass_fds`). A flock belongs to the open file, not to the
process that took it, so the lock is held for exactly as long as the player
runs and is let go when it exits, although the hook that started it exited
long before. The next clip, from whichever session, waits for it.

A reading (`hobson say`, read_aloud.py) also takes `reading()` for its whole
length, so a second reading waits for the first rather than alternating with
it passage by passage. Announcements take only the voice, so they are heard
between passages.

A lock that stays busy past its wait is passed over, not waited on for ever:
a clip then plays over whatever holds it, as before this module existed.
"""

import contextlib
import fcntl
import os
import time

import home

# The longest a clip waits for its turn. A passage of `hobson say` is at most
# read_aloud.MAX_CHARS, about 20 seconds of speech, and an announcement about
# five. Short on purpose: Claude Code cancels a hook at its timeout
# (settings-merge.py sets none, so the default applies), and a Stop has already
# spent its model call and render before it waits here.
WAIT_SECONDS = 30.0
POLL_SECONDS = 0.05


def lock_path():
    return home.path("hobson-voice.lock")


def reading_lock_path():
    return home.path("hobson-reading.lock")


def _open(path):
    os.makedirs(home.state_dir(), exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def _wait_for(path, timeout):
    """A descriptor holding the exclusive lock on `path`, or None when it was
    still busy after `timeout` seconds or could not be opened."""
    try:
        fd = _open(path)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(POLL_SECONDS)
        except OSError:
            os.close(fd)
            return None


def take_turn(timeout=WAIT_SECONDS):
    """Wait until nothing is playing, then hold the voice: a descriptor to
    pass to the player, which keeps it until the player exits. The caller
    closes its own copy once the player has started. None when the voice
    stayed busy past `timeout` or the lock could not be had."""
    return _wait_for(lock_path(), timeout)


def busy():
    """Something is playing now. For commentary, which is stale by the time a
    turn would come and is dropped rather than queued."""
    return _busy(lock_path())


def reading_busy():
    """A reading is going."""
    return _busy(reading_lock_path())


def _busy(path):
    try:
        fd = _open(path)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


@contextlib.contextmanager
def reading(timeout):
    """Held for a whole reading. Yields False when another reading was still
    going after `timeout` seconds."""
    fd = _wait_for(reading_lock_path(), timeout)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
