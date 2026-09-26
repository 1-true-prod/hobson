#!/usr/bin/env python3
"""The renames (claude-bark → claudio → Hobson) broke nothing, and lost nothing.

Run: python3 -m pytest tests/ -v
  or: python3 tests/test_rename.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

# Add scripts/ to path so we can import modules
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))


# ── Config paths ──────────────────────────────────────────────────────

class TestConfigPaths(unittest.TestCase):
    """Verify all modules reference the new hobson.json config path."""

    def test_base_config_file(self):
        from engines.base import CONFIG_FILE
        self.assertIn("hobson.json", CONFIG_FILE)
        self.assertNotIn("claude-bark", CONFIG_FILE)

    def test_lock_file(self):
        from engines.base import BARK_LOCK_FILE
        self.assertIn("hobson.lock", BARK_LOCK_FILE)
        self.assertNotIn("voice-bark", BARK_LOCK_FILE)

    def test_commentary_lock_file(self):
        from engines.base import COMMENTARY_LOCK_FILE
        self.assertIn("hobson-commentary.lock", COMMENTARY_LOCK_FILE)
        self.assertNotIn("voice-bark", COMMENTARY_LOCK_FILE)

    def test_log_file(self):
        from engines.base import LOG_FILE
        self.assertIn("hobson.log", LOG_FILE)
        self.assertNotIn("voice-bark", LOG_FILE)


# ── Migration from claudio (and claude-bark) ─────────────────────────
#
# Hobson was claudio until 0.3.0. Everything a user had under the old name
# must come across the first time it is needed, and nothing under the new
# name may ever be overwritten.

def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_migration_moves_config_key_log_and_sessions(claude_home):
    import engines.base as base
    _write(claude_home / "claudio.json", {"engine": "kokoro-realtime", "volume": 7})
    (claude_home / "claudio.env").write_text("OPENROUTER_API_KEY=sk-test\n")
    os.chmod(claude_home / "claudio.env", 0o600)
    (claude_home / "claudio.log").write_text("[2026-09-26 10:00:00] [x] spoke\n")
    (claude_home / "claudio-sessions").mkdir()
    (claude_home / "claudio-sessions" / "abc.json").write_text("{}")

    moved = base.migrate_legacy_state()

    assert len(moved) == 4
    assert json.load(open(claude_home / "hobson.json"))["volume"] == 7
    assert (claude_home / "hobson.env").read_text() == "OPENROUTER_API_KEY=sk-test\n"
    assert os.stat(claude_home / "hobson.env").st_mode & 0o777 == 0o600
    assert "spoke" in (claude_home / "hobson.log").read_text()
    assert (claude_home / "hobson-sessions" / "abc.json").exists()
    assert not any(p.name.startswith("claudio") for p in claude_home.iterdir())


def test_migration_never_overwrites_hobson_state(claude_home):
    import engines.base as base
    _write(claude_home / "hobson.json", {"engine": "say"})
    _write(claude_home / "claudio.json", {"engine": "chatterbox"})
    assert base.migrate_legacy_state() == []
    assert json.load(open(claude_home / "hobson.json"))["engine"] == "say"
    assert (claude_home / "claudio.json").exists()  # left for the user


def test_migration_is_idempotent(claude_home):
    import engines.base as base
    _write(claude_home / "claudio.json", {"engine": "say"})
    assert base.migrate_legacy_state()
    assert base.migrate_legacy_state() == []


def test_claude_bark_config_still_migrates(claude_home):
    import engines.base as base
    _write(claude_home / "claude-bark.json", {"engine": "kokoro-realtime", "personality": "pirate"})
    config = base.load_config()
    assert config["engine"] == "kokoro-realtime"
    assert config["personality"] == "pirate"
    assert (claude_home / "hobson.json").exists()


def test_claudio_config_wins_over_claude_bark(claude_home):
    import engines.base as base
    _write(claude_home / "claudio.json", {"engine": "say"})
    _write(claude_home / "claude-bark.json", {"engine": "chatterbox"})
    base.migrate_legacy_state()
    assert json.load(open(claude_home / "hobson.json"))["engine"] == "say"


def test_the_alfred_personality_becomes_hobson(claude_home):
    """Alfred was the default persona; the migrated config names Hobson."""
    import engines.base as base
    _write(claude_home / "claudio.json", {"personality": "alfred", "volume": 4})
    base.migrate_legacy_state()
    on_disk = json.load(open(claude_home / "hobson.json"))
    assert on_disk == {"personality": "hobson", "volume": 4}


def test_load_config_reads_alfred_as_hobson(claude_home):
    import engines.base as base
    _write(claude_home / "hobson.json", {"personality": "alfred"})
    assert base.load_config()["personality"] == "hobson"


def test_defaults_when_there_is_no_config_at_all(claude_home):
    import engines.base as base
    config = base.load_config()
    assert config["engine"] == "say"
    assert config["personality"] == "hobson"


def test_old_entrypoint_runs_hobson(claude_home, monkeypatch):
    """Hooks installed as claudio run scripts/claudio.py until rewritten."""
    import io
    import runpy
    event = {"hook_event_name": "UserPromptSubmit", "cwd": str(claude_home)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    runpy.run_path(os.path.join(REPO_ROOT, "scripts", "claudio.py"), run_name="__main__")
    # The prompt path writes the activity token -- proof hobson.py ran.
    assert any(p.name.startswith("hobson-activity-") for p in claude_home.iterdir())


# ── bark_hash consistency ─────────────────────────────────────────────

class TestBarkHash(unittest.TestCase):
    """Verify bark_hash is identical in both modules and produces stable output."""

    def test_known_hash(self):
        from engines.base import bark_hash as hash1
        from bark_templates import bark_hash as hash2

        text = "Very good, sir. The task is complete."
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

        self.assertEqual(hash1(text), expected)
        self.assertEqual(hash2(text), expected)

    def test_consistency(self):
        from engines.base import bark_hash as hash1
        from bark_templates import bark_hash as hash2

        for text in ["hello", "", "emoji: 🎉", "a" * 1000]:
            self.assertEqual(hash1(text), hash2(text),
                             f"Hash mismatch for {text!r}")

    def test_length(self):
        from engines.base import bark_hash
        self.assertEqual(len(bark_hash("test")), 16)

    def test_hex_chars_only(self):
        from engines.base import bark_hash
        import re
        self.assertRegex(bark_hash("test"), r'^[0-9a-f]{16}$')


# ── normalize_tool_name ───────────────────────────────────────────────

class TestNormalizeToolName(unittest.TestCase):
    """Verify MCP prefix stripping works correctly."""

    def test_plain_name(self):
        from bark_templates import normalize_tool_name
        self.assertEqual(normalize_tool_name("Bash"), "Bash")

    def test_mcp_plugin_prefix(self):
        from bark_templates import normalize_tool_name
        result = normalize_tool_name("mcp__plugin_context-mode_context-mode__ctx_execute")
        self.assertEqual(result, "ctx_execute")

    def test_mcp_service_prefix(self):
        from bark_templates import normalize_tool_name
        result = normalize_tool_name("mcp__claude_ai_Slack__slack_send_message")
        self.assertEqual(result, "slack_send_message")

    def test_non_mcp_passthrough(self):
        from bark_templates import normalize_tool_name
        self.assertEqual(normalize_tool_name("Edit"), "Edit")
        self.assertEqual(normalize_tool_name("Read"), "Read")

    def test_empty_and_whitespace(self):
        from bark_templates import normalize_tool_name
        self.assertEqual(normalize_tool_name(""), "")
        self.assertEqual(normalize_tool_name("  Bash  "), "Bash")


# ── extract_context ───────────────────────────────────────────────────

class TestExtractContext(unittest.TestCase):
    """Verify context extraction for commentary phrases."""

    def test_edit_tool(self):
        from engines.base import extract_context
        result = extract_context("Edit", {"file_path": "/path/to/foo.py"})
        self.assertEqual(result, "foo.py")

    def test_bash_tool_description(self):
        from engines.base import extract_context
        result = extract_context("Bash", {"description": "Run tests", "command": "pytest"})
        self.assertEqual(result, "Run tests")

    def test_bash_tool_command_fallback(self):
        from engines.base import extract_context
        result = extract_context("Bash", {"command": "pytest -v tests/"})
        self.assertEqual(result, "pytest -v tests/")

    def test_agent_tool(self):
        from engines.base import extract_context
        result = extract_context("Agent", {"description": "Search codebase"})
        self.assertEqual(result, "Search codebase")

    def test_grep_tool(self):
        from engines.base import extract_context
        result = extract_context("Grep", {"pattern": "TODO"})
        self.assertEqual(result, "TODO")

    def test_none_for_empty(self):
        from engines.base import extract_context
        self.assertIsNone(extract_context("Edit", {}))
        self.assertIsNone(extract_context("Edit", None))

    def test_unknown_tool(self):
        from engines.base import extract_context
        self.assertIsNone(extract_context("UnknownTool", {"foo": "bar"}))


# ── snippet ───────────────────────────────────────────────────────────

class TestSnippet(unittest.TestCase):
    """Verify text snippet extraction."""

    def test_empty(self):
        from engines.base import snippet
        self.assertEqual(snippet(""), "")
        self.assertEqual(snippet("   "), "")

    def test_short_text(self):
        from engines.base import snippet
        text = "First. Second. Third."
        result = snippet(text)
        self.assertIn("First", result)

    def test_long_text_truncation(self):
        from engines.base import snippet
        sentences = [f"Sentence {i}." for i in range(20)]
        text = " ".join(sentences)
        result = snippet(text)
        # Should contain first 2 and last 3 sentences
        self.assertIn("Sentence 0.", result)
        self.assertIn("Sentence 1.", result)
        self.assertIn("[...]", result)
        self.assertIn("Sentence 19.", result)


# ── Hook identification (settings-merge) ──────────────────────────────

class TestIsOurHook(unittest.TestCase):
    """Verify hook detection finds both new and legacy hooks."""

    def _is_our_hook(self, entry):
        # settings-merge.py has a hyphen so we need importlib
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "settings_merge",
            os.path.join(REPO_ROOT, "scripts", "settings-merge.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._is_our_hook(entry)

    def test_new_hobson_hook(self):
        entry = {"hooks": [{"type": "command", "command": "python3 /path/scripts/hobson.py"}]}
        self.assertTrue(self._is_our_hook(entry))

    def test_legacy_claude_bark_hook(self):
        entry = {"hooks": [{"type": "command", "command": "python3 /path/scripts/claude-bark.py"}]}
        self.assertTrue(self._is_our_hook(entry))

    def test_legacy_voice_bark_hook(self):
        entry = {"hooks": [{"type": "command", "command": "python3 /path/scripts/voice-bark.py"}]}
        self.assertTrue(self._is_our_hook(entry))

    def test_unrelated_hook(self):
        entry = {"hooks": [{"type": "command", "command": "python3 /path/other-tool.py"}]}
        self.assertFalse(self._is_our_hook(entry))

    def test_empty_hooks(self):
        entry = {"hooks": []}
        self.assertFalse(self._is_our_hook(entry))

    def test_missing_command(self):
        entry = {"hooks": [{"type": "command"}]}
        self.assertFalse(self._is_our_hook(entry))


# ── phrase_gen parsing ────────────────────────────────────────────────

class TestPhraseGenParsing(unittest.TestCase):
    """Verify phrase parsing and cleaning logic."""

    # _clean_phrase only prepends "I " to subject-less verb phrases; a phrase
    # with its own subject ("Very good sir, ...") is left untouched.
    def test_clean_phrase_normal(self):
        from phrase_gen import _clean_phrase
        result = _clean_phrase("Very good sir, the task is complete.")
        self.assertEqual(result, "Very good sir, the task is complete.")

    def test_clean_phrase_strips_quotes(self):
        from phrase_gen import _clean_phrase
        result = _clean_phrase('"Very good sir, the task is complete."')
        self.assertEqual(result, "Very good sir, the task is complete.")

    def test_clean_phrase_strips_asterisks(self):
        from phrase_gen import _clean_phrase
        result = _clean_phrase("**Very good sir, the task is complete.**")
        self.assertEqual(result, "Very good sir, the task is complete.")

    def test_clean_phrase_rejects_too_short(self):
        from phrase_gen import _clean_phrase
        self.assertIsNone(_clean_phrase("Too short."))

    def test_clean_phrase_rejects_too_long(self):
        from phrase_gen import _clean_phrase
        long_text = " ".join(["word"] * 25) + "."
        self.assertIsNone(_clean_phrase(long_text))

    def test_parse_response_with_category(self):
        from phrase_gen import _parse_response
        result = _parse_response("done | Very good sir, the task is complete.")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "done")
        self.assertIn("complete", result[1])

    def test_parse_response_bare_phrase(self):
        from phrase_gen import _parse_response
        result = _parse_response("Very good sir, the task is complete.")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "done")  # defaults to done

    def test_parse_response_invalid_category(self):
        from phrase_gen import _parse_response
        result = _parse_response("invalid | Very good sir, the task is complete.")
        # Should still parse as bare phrase with default "done"
        self.assertIsNotNone(result)

    def test_parse_response_all_categories(self):
        from phrase_gen import _parse_response
        for cat in ("done", "broken", "question"):
            result = _parse_response(f"{cat} | Five or more words in this phrase here.")
            self.assertIsNotNone(result, f"Failed for category {cat}")
            self.assertEqual(result[0], cat)


# ── Entrypoint engine registry ────────────────────────────────────────

class TestEngineRegistry(unittest.TestCase):
    """Verify the engine registry in hobson.py is intact."""

    def test_engines_dict(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hobson_entry",
            os.path.join(REPO_ROOT, "scripts", "hobson.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        self.assertIn("say", mod.ENGINES)
        self.assertIn("kokoro-realtime", mod.ENGINES)
        self.assertIn("chatterbox", mod.ENGINES)
        self.assertIn("pocket-tts", mod.ENGINES)
        self.assertEqual(len(mod.ENGINES), 4)


# ── Event map ─────────────────────────────────────────────────────────

class TestEventMap(unittest.TestCase):
    """Verify BaseEngine EVENT_MAP is correct."""

    def test_event_map_keys(self):
        from engines.base import BaseEngine
        expected = {"Stop": "stop", "PermissionRequest": "permission",
                    "Notification": "notification", "PreToolUse": "commentary"}
        self.assertEqual(BaseEngine.EVENT_MAP, expected)


# ── Personality loading ───────────────────────────────────────────────

class TestPersonalityLoading(unittest.TestCase):
    """Verify personality templates load correctly."""

    def test_alfred_personality_exists(self):
        path = os.path.join(REPO_ROOT, "scripts", "personalities", "hobson", "personality.json")
        self.assertTrue(os.path.isfile(path))
        with open(path) as f:
            data = json.load(f)
        self.assertIn("templates", data)
        self.assertIn("categories", data["templates"])

    def test_all_personalities_valid_json(self):
        pdir = os.path.join(REPO_ROOT, "scripts", "personalities")
        for name in os.listdir(pdir):
            pjson = os.path.join(pdir, name, "personality.json")
            if os.path.isfile(pjson):
                with open(pjson) as f:
                    data = json.load(f)
                self.assertIn("name", data, f"Personality {name} missing 'name'")

    def test_bark_templates_lazy_load(self):
        """Verify template attributes are accessible."""
        import bark_templates
        bark_templates.reload_personality("hobson")
        cats = bark_templates.CATEGORIES
        self.assertIn("done", cats)
        self.assertIn("broken", cats)
        self.assertIn("question", cats)
        self.assertIsInstance(bark_templates.PERMISSION_LEADS, list)
        self.assertIsInstance(bark_templates.NOTIFICATION_TEMPLATES, list)


# ── hooks.json ────────────────────────────────────────────────────────

class TestHooksJson(unittest.TestCase):
    """Verify hooks.json references hobson.py."""

    def test_hooks_json_commands(self):
        path = os.path.join(REPO_ROOT, "hooks", "hooks.json")
        with open(path) as f:
            data = json.load(f)

        self.assertIn("hobson", data["description"])
        for event, entries in data["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    self.assertIn("hobson.py", hook["command"],
                                  f"Hook for {event} still references old name")
                    self.assertNotIn("voice-bark", hook["command"])


# ── No stale references in source ────────────────────────────────────

class TestNoStaleReferences(unittest.TestCase):
    """Verify no accidental old name references in non-migration code."""

    def _read_file(self, relpath):
        with open(os.path.join(REPO_ROOT, relpath), encoding="utf-8") as f:
            return f.read()

    def test_hobson_py_no_old_refs(self):
        content = self._read_file("scripts/hobson.py")
        self.assertNotIn("voice-bark", content)
        self.assertNotIn("claude-bark", content)

    def test_hooks_json_no_old_refs(self):
        content = self._read_file("hooks/hooks.json")
        self.assertNotIn("voice-bark", content)
        self.assertNotIn("claude-bark", content)

    def test_init_no_old_refs(self):
        content = self._read_file("scripts/engines/__init__.py")
        self.assertNotIn("claude-bark", content)


if __name__ == "__main__":
    unittest.main()
