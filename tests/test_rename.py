#!/usr/bin/env python3
"""Tests to verify the claude-bark → claudio rename didn't break anything.

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
    """Verify all modules reference the new claudio.json config path."""

    def test_base_config_file(self):
        from engines.base import CONFIG_FILE
        self.assertIn("claudio.json", CONFIG_FILE)
        self.assertNotIn("claude-bark", CONFIG_FILE)

    def test_base_old_config_file(self):
        from engines.base import OLD_CONFIG_FILE
        self.assertIn("claude-bark.json", OLD_CONFIG_FILE)

    def test_lock_file(self):
        from engines.base import BARK_LOCK_FILE
        self.assertIn("claudio.lock", BARK_LOCK_FILE)
        self.assertNotIn("voice-bark", BARK_LOCK_FILE)

    def test_commentary_lock_file(self):
        from engines.base import COMMENTARY_LOCK_FILE
        self.assertIn("claudio-commentary.lock", COMMENTARY_LOCK_FILE)
        self.assertNotIn("voice-bark", COMMENTARY_LOCK_FILE)

    def test_log_file(self):
        from engines.base import LOG_FILE
        self.assertIn("claudio.log", LOG_FILE)
        self.assertNotIn("voice-bark", LOG_FILE)


# ── Config migration ─────────────────────────────────────────────────

class TestConfigMigration(unittest.TestCase):
    """Verify load_config() auto-migrates old claude-bark.json."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.new_config = os.path.join(self.tmpdir, "claudio.json")
        self.old_config = os.path.join(self.tmpdir, "claude-bark.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_migration_copies_old_to_new(self):
        """If claudio.json missing but claude-bark.json exists, copy it."""
        import engines.base as base

        # Write old config
        old_data = {"engine": "kokoro-realtime", "personality": "pirate"}
        with open(self.old_config, "w") as f:
            json.dump(old_data, f)

        # Patch paths
        orig_config = base.CONFIG_FILE
        orig_old = base.OLD_CONFIG_FILE
        try:
            base.CONFIG_FILE = self.new_config
            base.OLD_CONFIG_FILE = self.old_config

            config = base.load_config()
            self.assertEqual(config["engine"], "kokoro-realtime")
            self.assertEqual(config["personality"], "pirate")
            # New file should have been created
            self.assertTrue(os.path.isfile(self.new_config))
        finally:
            base.CONFIG_FILE = orig_config
            base.OLD_CONFIG_FILE = orig_old

    def test_no_migration_if_new_exists(self):
        """If claudio.json exists, don't touch old config."""
        import engines.base as base

        new_data = {"engine": "say", "personality": "alfred"}
        old_data = {"engine": "chatterbox", "personality": "pirate"}
        with open(self.new_config, "w") as f:
            json.dump(new_data, f)
        with open(self.old_config, "w") as f:
            json.dump(old_data, f)

        orig_config = base.CONFIG_FILE
        orig_old = base.OLD_CONFIG_FILE
        try:
            base.CONFIG_FILE = self.new_config
            base.OLD_CONFIG_FILE = self.old_config

            config = base.load_config()
            # Should use new config, not old
            self.assertEqual(config["engine"], "say")
        finally:
            base.CONFIG_FILE = orig_config
            base.OLD_CONFIG_FILE = orig_old

    def test_defaults_if_neither_exists(self):
        """If no config exists, return defaults without error."""
        import engines.base as base

        orig_config = base.CONFIG_FILE
        orig_old = base.OLD_CONFIG_FILE
        try:
            base.CONFIG_FILE = os.path.join(self.tmpdir, "nonexistent.json")
            base.OLD_CONFIG_FILE = os.path.join(self.tmpdir, "also-nonexistent.json")

            config = base.load_config()
            self.assertEqual(config["engine"], "say")
            self.assertEqual(config["personality"], "alfred")
        finally:
            base.CONFIG_FILE = orig_config
            base.OLD_CONFIG_FILE = orig_old


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

    def test_new_claudio_hook(self):
        entry = {"hooks": [{"type": "command", "command": "python3 /path/scripts/claudio.py"}]}
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
    """Verify the engine registry in claudio.py is intact."""

    def test_engines_dict(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "claudio_entry",
            os.path.join(REPO_ROOT, "scripts", "claudio.py"),
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
        path = os.path.join(REPO_ROOT, "scripts", "personalities", "alfred", "personality.json")
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
        bark_templates.reload_personality("alfred")
        cats = bark_templates.CATEGORIES
        self.assertIn("done", cats)
        self.assertIn("broken", cats)
        self.assertIn("question", cats)
        self.assertIsInstance(bark_templates.PERMISSION_LEADS, list)
        self.assertIsInstance(bark_templates.NOTIFICATION_TEMPLATES, list)


# ── hooks.json ────────────────────────────────────────────────────────

class TestHooksJson(unittest.TestCase):
    """Verify hooks.json references claudio.py."""

    def test_hooks_json_commands(self):
        path = os.path.join(REPO_ROOT, "hooks", "hooks.json")
        with open(path) as f:
            data = json.load(f)

        self.assertIn("claudio", data["description"])
        for event, entries in data["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    self.assertIn("claudio.py", hook["command"],
                                  f"Hook for {event} still references old name")
                    self.assertNotIn("voice-bark", hook["command"])


# ── No stale references in source ────────────────────────────────────

class TestNoStaleReferences(unittest.TestCase):
    """Verify no accidental old name references in non-migration code."""

    def _read_file(self, relpath):
        with open(os.path.join(REPO_ROOT, relpath), encoding="utf-8") as f:
            return f.read()

    def test_claudio_py_no_old_refs(self):
        content = self._read_file("scripts/claudio.py")
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
