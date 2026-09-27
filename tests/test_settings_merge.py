"""settings-merge.py: merge/remove/check, idempotency, atomic + quoted writes."""

import json
import os

import pytest


def _read(sm):
    with open(sm.SETTINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


def test_merge_adds_hooks_and_no_permission(settings_merge):
    sm = settings_merge
    sm.merge_hooks("/install/dir")
    data = _read(sm)
    assert set(data["hooks"]) == {
        "PermissionRequest", "Stop", "Notification", "PreToolUse", "UserPromptSubmit",
    }
    # Hooks run outside the permission system; granting Claude `say:*`
    # (which can write files with -o) bought nothing.
    assert "permissions" not in data


def test_merge_quotes_install_path(settings_merge):
    sm = settings_merge
    sm.merge_hooks("/install dir/with space")
    data = _read(sm)
    cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd == '"python3" "/install dir/with space/scripts/hobson.py"'


def test_merge_pins_the_interpreter_it_is_given(settings_merge):
    sm = settings_merge
    sm.merge_hooks("/d", python="/opt/homebrew/bin/python3")
    cmd = _read(sm)["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd == '"/opt/homebrew/bin/python3" "/d/scripts/hobson.py"'


def test_merge_is_idempotent(settings_merge):
    sm = settings_merge
    sm.merge_hooks("/d")
    sm.merge_hooks("/d")
    data = _read(sm)
    # exactly one of our hooks per event, no duplicates
    assert len(data["hooks"]["Stop"]) == 1


def test_merge_preserves_foreign_hooks(settings_merge):
    sm = settings_merge
    foreign = {"hooks": [{"type": "command", "command": "echo other"}]}
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"hooks": {"Stop": [foreign]}}, f)
    sm.merge_hooks("/d")
    data = _read(sm)
    cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "echo other" in cmds
    assert any("hobson.py" in c for c in cmds)


def test_remove_strips_ours_keeps_foreign(settings_merge):
    sm = settings_merge
    foreign = {"hooks": [{"type": "command", "command": "echo other"}]}
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"hooks": {"Stop": [foreign]}}, f)
    sm.merge_hooks("/d")
    sm.remove_hooks()
    data = _read(sm)
    cmds = [h["command"] for e in data["hooks"].get("Stop", []) for h in e["hooks"]]
    assert "echo other" in cmds
    assert not any("hobson.py" in c for c in cmds)


def test_remove_strips_legacy_claude_bark(settings_merge):
    sm = settings_merge
    legacy = {"hooks": [{"type": "command", "command": "python3 ~/claude-bark/bark.py"}]}
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"hooks": {"Stop": [legacy]}}, f)
    sm.remove_hooks()
    data = _read(sm)
    assert "Stop" not in data.get("hooks", {})  # emptied array is deleted


def test_rerun_leaves_a_current_file_untouched(settings_merge):
    """Re-running the installer is the update path: no rewrite, no backup."""
    sm = settings_merge
    sm.merge_hooks("/d")
    parent = os.path.dirname(sm.SETTINGS_FILE)
    mtime = os.stat(sm.SETTINGS_FILE).st_mtime_ns
    sm.merge_hooks("/d")
    assert os.stat(sm.SETTINGS_FILE).st_mtime_ns == mtime
    assert [f for f in os.listdir(parent) if ".backup-" in f] == []


def test_merge_backs_up_a_file_it_changes(settings_merge):
    sm = settings_merge
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"model": "opus"}, f)
    sm.merge_hooks("/d")
    parent = os.path.dirname(sm.SETTINGS_FILE)
    backups = [f for f in os.listdir(parent) if ".backup-" in f]
    assert len(backups) == 1
    assert _read(sm)["model"] == "opus"


def test_merge_writes_through_a_symlinked_settings_file(settings_merge, tmp_path):
    """Dotfile managers keep settings.json as a link; it must stay one."""
    sm = settings_merge
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    os.symlink(real, sm.SETTINGS_FILE)

    sm.merge_hooks("/d")

    assert os.path.islink(sm.SETTINGS_FILE)
    data = json.loads(real.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    assert "Stop" in data["hooks"]


def test_merge_keeps_the_file_mode(settings_merge):
    sm = settings_merge
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)
    os.chmod(sm.SETTINGS_FILE, 0o600)
    sm.merge_hooks("/d")
    assert os.stat(sm.SETTINGS_FILE).st_mode & 0o777 == 0o600


def test_settings_file_follows_claude_config_dir(tmp_path, monkeypatch):
    from conftest import _load_hyphenated
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    mod = _load_hyphenated("settings_merge_ccd", "settings-merge.py")
    assert mod.SETTINGS_FILE == str(tmp_path / "elsewhere" / "settings.json")


def test_merge_drops_the_say_permission_old_installs_added(settings_merge):
    sm = settings_merge
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"permissions": {"allow": ["Bash(say:*)", "Bash(ls:*)"]}}, f)
    sm.merge_hooks("/d")
    assert _read(sm)["permissions"]["allow"] == ["Bash(ls:*)"]


def test_remove_drops_the_say_permission_old_installs_added(settings_merge):
    sm = settings_merge
    sm.merge_hooks("/d")
    settings = _read(sm)
    settings["permissions"] = {"allow": ["Bash(say:*)", "Bash(ls:*)"]}
    sm.save_settings(settings)
    sm.remove_hooks()
    assert _read(sm)["permissions"]["allow"] == ["Bash(ls:*)"]


# ── Someone called Hobson ────────────────────────────────────────────────
#
# Hooks were claimed by the substring "hobson" anywhere in the command, so a
# user whose home is /Users/hobson lost their own hooks: install replaced
# them in hobson's five events, uninstall deleted them from every event.

def _theirs(cmd):
    return {"hooks": [{"type": "command", "command": cmd}]}


def test_a_hook_under_a_home_named_hobson_is_not_ours(settings_merge):
    sm = settings_merge
    assert sm._is_our_hook(_theirs("/Users/hobson/bin/guard.sh")) is False
    assert sm._is_our_hook(_theirs('"/usr/bin/python3" "/Users/hobson/.local/share/hobson/scripts/hobson.py"')) is True


def test_install_and_uninstall_leave_a_hobson_named_users_hooks(settings_merge):
    sm = settings_merge
    guard = _theirs("/Users/hobson/bin/guard.sh")
    start = _theirs("/Users/hobson/bin/on-start.sh")
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"hooks": {"PreToolUse": [guard], "SessionStart": [start]}}, f)

    sm.merge_hooks("/d")
    assert guard in _read(sm)["hooks"]["PreToolUse"]
    sm.remove_hooks()
    assert _read(sm)["hooks"] == {"PreToolUse": [guard], "SessionStart": [start]}


def _shared_entry(sm):
    """A foreign hook added to hobson's own matcher group, as a hand edit or
    another tool appending to an existing group would leave it."""
    sm.merge_hooks("/d")
    data = _read(sm)
    data["hooks"]["Notification"][0]["hooks"].append({"type": "command", "command": "/opt/other/notify.sh"})
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data["hooks"]["Notification"][0]["matcher"]


def _commands(entries):
    return [h["command"] for e in entries for h in e["hooks"]]


def test_uninstall_keeps_a_foreign_hook_in_hobsons_group(settings_merge):
    sm = settings_merge
    matcher = _shared_entry(sm)
    sm.remove_hooks()
    [entry] = _read(sm)["hooks"]["Notification"]
    assert entry == {"matcher": matcher, "hooks": [{"type": "command", "command": "/opt/other/notify.sh"}]}


def test_reinstall_keeps_a_foreign_hook_in_hobsons_group(settings_merge):
    sm = settings_merge
    matcher = _shared_entry(sm)
    sm.merge_hooks("/d")
    entries = _read(sm)["hooks"]["Notification"]
    assert "/opt/other/notify.sh" in _commands(entries)
    assert sum("hobson.py" in c for c in _commands(entries)) == 1
    assert all(e["matcher"] == matcher for e in entries)
    before = _read(sm)
    sm.merge_hooks("/d")
    assert _read(sm) == before, "and a second run changes nothing"


# ── Shapes hobson never wrote ────────────────────────────────────────────

@pytest.mark.parametrize("settings", [
    {"hooks": None},
    {"hooks": {"Stop": None}},
    {"permissions": None},
    {"permissions": {"allow": None}},
])
def test_null_sections_do_not_crash_merge_check_or_remove(settings_merge, settings):
    sm = settings_merge
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f)
    sm.check_hooks()
    sm.merge_hooks("/d")
    assert sm.check_hooks() is True
    sm.remove_hooks()
    assert not sm._is_our_hook(_read(sm).get("hooks", {}).get("Stop", [{}])[0])


def test_settings_that_are_not_an_object_exit_cleanly(settings_merge):
    sm = settings_merge
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump([1, 2], f)
    with pytest.raises(SystemExit):
        sm.merge_hooks("/d")


def test_check_names_an_interpreter_that_is_gone(settings_merge, capsys):
    """Hooks whose Python was uninstalled fail inside Claude Code silently."""
    sm = settings_merge
    sm.merge_hooks("/d", python="/nonexistent/bin/python3")
    assert sm.check_hooks() is False
    assert "interpreter missing: /nonexistent/bin/python3" in capsys.readouterr().out


def test_atomic_save_leaves_no_tmp(settings_merge):
    sm = settings_merge
    sm.merge_hooks("/d")
    parent = os.path.dirname(sm.SETTINGS_FILE)
    leftovers = [f for f in os.listdir(parent) if ".tmp-" in f]
    assert leftovers == []


def test_load_settings_invalid_json_exits(settings_merge):
    sm = settings_merge
    with open(sm.SETTINGS_FILE, "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(SystemExit):
        sm.load_settings()


def test_is_our_hook(settings_merge):
    sm = settings_merge
    assert sm._is_our_hook({"hooks": [{"command": "python3 /x/scripts/hobson.py"}]}) is True
    assert sm._is_our_hook({"hooks": [{"command": "python3 ~/claude-bark/b.py"}]}) is True
    assert sm._is_our_hook({"hooks": [{"command": "echo unrelated"}]}) is False
    assert sm._is_our_hook({}) is False


def test_check_hooks_reports_state(settings_merge, capsys):
    sm = settings_merge
    assert sm.check_hooks() is False
    sm.merge_hooks("/d")
    assert sm.check_hooks() is True


# ── Nudges never fired: their trigger was filtered out upstream ───────────
#
# nudge.py spawns on idle_prompt / agent_needs_input notifications, but the
# installer registered the Notification hook with matcher "permission_prompt"
# (unchanged since the initial commit), so Claude Code never delivered those
# notifications to hobson at all. Across the whole log: 0 of either type,
# 0 nudges ever. Two lists had drifted apart, so pin them together.

def _notification_matcher_types(entries):
    matcher = entries[0].get("matcher") or ""
    return {t.strip() for t in matcher.replace(",", "|").split("|") if t.strip()}


def test_installer_notification_matcher_admits_every_nudge_trigger(settings_merge):
    from nudge import WAITING_TYPES
    admitted = _notification_matcher_types(settings_merge.HOOKS_TO_INJECT["Notification"])
    missing = set(WAITING_TYPES) - admitted
    assert not missing, f"Notification matcher filters out nudge triggers: {missing}"


def test_plugin_hooks_json_notification_matcher_admits_every_nudge_trigger():
    """hooks/hooks.json (plugin mode) must agree with the standalone installer."""
    from nudge import WAITING_TYPES
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "hooks", "hooks.json"), encoding="utf-8") as f:
        data = json.load(f)
    hooks = data.get("hooks", data)
    admitted = _notification_matcher_types(hooks["Notification"])
    missing = set(WAITING_TYPES) - admitted
    assert not missing, f"hooks.json Notification matcher filters out: {missing}"


def test_installer_still_admits_permission_prompts(settings_merge):
    """Widening the matcher must not drop what hobson already speaks on."""
    admitted = _notification_matcher_types(settings_merge.HOOKS_TO_INJECT["Notification"])
    assert "permission_prompt" in admitted


# ── --check passed a partial install ──────────────────────────────────────
#
# check_hooks returned True if it found ANY hobson hook, and doctor trusts
# its exit code. This machine had four of five hooks -- UserPromptSubmit,
# which writes the activity token that cancels a nudge the moment the user
# types, was never installed -- and doctor reported "Hooks installed".

def test_check_fails_when_a_hook_is_missing(settings_merge, capsys):
    sm = settings_merge
    sm.merge_hooks("/d")
    settings = _read(sm)
    del settings["hooks"]["UserPromptSubmit"]
    sm.save_settings(settings)

    assert sm.check_hooks() is False
    assert "UserPromptSubmit" in capsys.readouterr().out


def test_check_fails_when_an_installed_hook_is_stale(settings_merge, capsys):
    """An old install's narrower Notification matcher must be reported."""
    sm = settings_merge
    sm.merge_hooks("/d")
    settings = _read(sm)
    for entry in settings["hooks"]["Notification"]:
        if sm._is_our_hook(entry):
            entry["matcher"] = "permission_prompt"
    sm.save_settings(settings)

    assert sm.check_hooks() is False
    assert "Notification" in capsys.readouterr().out


def test_check_passes_on_a_complete_current_install(settings_merge):
    sm = settings_merge
    sm.merge_hooks("/d")
    assert sm.check_hooks() is True


def test_remerge_repairs_a_stale_partial_install(settings_merge):
    """Re-running the installer is the documented fix; it must actually fix."""
    sm = settings_merge
    sm.merge_hooks("/d")
    settings = _read(sm)
    del settings["hooks"]["UserPromptSubmit"]
    for entry in settings["hooks"]["Notification"]:
        if sm._is_our_hook(entry):
            entry["matcher"] = "permission_prompt"
    sm.save_settings(settings)
    assert sm.check_hooks() is False

    sm.merge_hooks("/d")
    assert sm.check_hooks() is True


def test_a_command_merely_named_hobson_is_not_ours(settings_merge):
    """Hobson is a surname: another tool's command, or a home, may carry it."""
    sm = settings_merge
    assert sm._is_our_hook({"hooks": [{"command": "hobson hook PreToolUse"}]}) is False
    assert sm._is_our_hook({"hooks": [{"command": "/Users/hobson/bin/play.py"}]}) is False
