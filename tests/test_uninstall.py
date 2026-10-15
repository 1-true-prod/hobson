"""uninstall.sh keeps its own list of what Hobson writes under ~/.claude. It
drifted: hobson-coaching.lock and an unreadable config kept aside outlived
an uninstall. These tests hold the list to the names the scripts write."""

import glob
import os
import re

import hobson
import home
from engines.daemon import DaemonEngine

UNINSTALL = open(os.path.join(home.ROOT, "uninstall.sh"), encoding="utf-8").read()

# Written elsewhere, or not files we leave behind: hobson-say-* is a temp
# file in $TMPDIR, the reference clip ships in models/, hobson.py is code.
NOT_STATE = {"hobson-say-", "hobson-reference", "hobson-reference.wav", "hobson.py"}


def _names_written():
    """Every literal state-file name in scripts/: "hobson-…" or "hobson.…",
    an f-string cut where its first placeholder starts."""
    names = set()
    for source in glob.glob(os.path.join(home.ROOT, "scripts", "**", "*.py"), recursive=True):
        text = open(source, encoding="utf-8").read()
        names.update(re.findall(r"""["'](hobson[-.][A-Za-z_.*-]*)""", text))
    return names - NOT_STATE


def test_every_state_file_the_scripts_write_is_removed():
    missing = sorted(n for n in _names_written() if n not in UNINSTALL)
    assert not missing, f"uninstall.sh does not remove {missing}"


def test_every_project_file_kind_is_removed():
    kinds = set()
    for source in glob.glob(os.path.join(home.ROOT, "scripts", "*.py")):
        text = open(source, encoding="utf-8").read()
        kinds.update(re.findall(r"""project_file\(["']([a-z]+)["']""", text))
    kinds.update(("nudge", "watchdog"))  # nudge.lock_path(kind, …)
    assert kinds >= {"alive", "activity"}
    for kind in kinds:
        assert f'"$CLAUDE_DIR"/hobson-{kind}-' in UNINSTALL, kind


def test_every_copy_of_the_config_kept_aside_is_removed():
    """home.py keeps an unreadable config aside, `hobson config reset` backs
    it up, install.sh copies an invalid one: each as hobson.json.<suffix>."""
    suffixes = set()
    for name, pattern in (("scripts/home.py", r'\{target\}(\.[a-z]+)'),
                          ("hobson", r'\$CONFIG_FILE(\.[a-z]+)'),
                          ("install.sh", r'path \+ "(\.[a-z]+)"')):
        found = re.findall(pattern, open(os.path.join(home.ROOT, name), encoding="utf-8").read())
        assert found, name
        suffixes.update(found)
    # write_atomic's temp file, for every state file it writes.
    assert ".tmp" in suffixes and '"$CLAUDE_DIR"/hobson*.tmp-*' in UNINSTALL
    for suffix in suffixes - {".tmp"}:
        assert f"hobson.json{suffix}" in UNINSTALL, suffix


def test_every_daemons_files_are_removed():
    for name in hobson.ENGINES:
        cls = hobson.engine_class(name)
        if not issubclass(cls, DaemonEngine):
            continue
        stem = os.path.splitext(cls.spec.script)[0]
        for suffix in (".pid", ".log", ".token"):
            assert stem + suffix in UNINSTALL or f"$name-daemon{suffix}" in UNINSTALL, stem + suffix
        assert f'"$CLAUDE_DIR"/{cls.spec.playback}*.wav' in UNINSTALL, cls.spec.playback
        # Stopped on the port the config gives, read through home.py.
        section = cls.spec.section
        assert f"{section}.daemon_port" in UNINSTALL, section
