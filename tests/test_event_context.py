"""_describe_event / extract_context behaviour."""

from engines.base import BaseEngine
from engines.kokoro_realtime import KokoroRealtimeEngine
from engines.pocket_tts_realtime import PocketTTSRealtimeEngine


def test_describe_event_is_defined_once_and_shared():
    """A context fix applied to one engine previously missed the other."""
    assert KokoroRealtimeEngine._describe_event is BaseEngine._describe_event
    assert PocketTTSRealtimeEngine._describe_event is BaseEngine._describe_event


def test_trace_records_the_context_the_model_was_given(fake_ollama, claude_home):
    import phrase_gen
    fake_ollama.chat("done | I pushed the branch to origin.")
    phrase_gen.generate_or_skip(
        "PreToolUse", "Bash: Push branch to origin", session_context="",
        model="llama3.2:3b",
    )
    log_text = (claude_home / "claudio.log").read_text()
    assert "detail=" in log_text
    assert "Push branch to origin" in log_text


def test_trace_truncates_a_huge_context():
    """Observed Bash commands reach 778 chars; the log must stay readable."""
    import phrase_gen
    assert len(phrase_gen._truncate_detail("x" * 500, 120)) <= 123


# ── Task 6: Edit/Write substance ────────────────────────────────────────────

from engines.base import extract_context


def test_edit_context_names_the_changed_symbol():
    ctx = extract_context("Edit", {
        "file_path": "/repo/scripts/phrase_gen.py",
        "old_string": "def _truncate_detail(text):\n    return text\n",
        "new_string": "def _truncate_detail(text, limit=120):\n    return text[:limit]\n",
    })
    assert "phrase_gen.py" in ctx and "_truncate_detail" in ctx


def test_edit_context_falls_back_to_a_line_delta():
    ctx = extract_context("Edit", {
        "file_path": "/repo/config.json",
        "old_string": '"a": 1\n',
        "new_string": '"a": 2\n"b": 3\n"c": 4\n',
    })
    assert "config.json" in ctx and "lines" in ctx


def test_edit_context_never_returns_raw_code():
    ctx = extract_context("Edit", {
        "file_path": "/repo/a.js",
        "old_string": "const x = {a: 1};",
        "new_string": "const y = {b: 2}; foo(y);",
    })
    assert "{" not in ctx and "}" not in ctx and ";" not in ctx


def test_write_context_names_the_defined_symbol():
    ctx = extract_context("Write", {
        "file_path": "/repo/scripts/thing.py",
        "content": "import os\n\n\nclass ThingDoer:\n    pass\n",
    })
    assert "thing.py" in ctx and "ThingDoer" in ctx


def test_edit_context_still_works_with_only_a_path():
    assert extract_context("Edit", {"file_path": "/repo/scripts/x.py"}) == "x.py"


# ── _code_gist: the symbol an edit is about ────────────────────────────────
#
# Measured on 1,137 real Kotlin edits: the old one-modifier regex named a
# symbol for 23% of them, and 18 of those names were the word "class" (from
# every `enum class X`) while 46 were a receiver type instead of the function.

import pytest

from engines.base import _code_gist


@pytest.mark.parametrize("line, name", [
    ("private suspend fun load() {", "load"),
    ("internal data class OrderState(", "OrderState"),
    ("enum class SortKind {", "SortKind"),
    ("fun ThemeConfig.colorFor(key: ColorKey): Color = x", "colorFor"),
    ("internal fun ViewState.Companion.fake() = ViewState()", "fake"),
    ("@Composable fun SettingsScreen() {", "SettingsScreen"),
    ("fun <T> List<T>.second(): T = this[1]", "second"),
    ("func (r *Repo) Save(ctx context.Context) error {", "Save"),
    ("pub(crate) fn parse(input: &str) -> Ast {", "parse"),
    ("export default function App() {", "App"),
    ("die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }", "die"),
])
def test_the_gist_names_the_declared_symbol(line, name):
    assert _code_gist(line, "/repo/f") == name


def test_a_local_val_is_not_the_gist():
    """A val inside a function body is a local, not what the edit is about."""
    assert _code_gist("        val total = items.sumOf { it.price }\n", "/repo/Cart.kt") is None


def test_prose_files_have_no_gist():
    """'let me explain' is a sentence, not a declaration named `me`."""
    assert _code_gist("let me explain the design\n", "/repo/README.md") is None
    ctx = extract_context("Edit", {"file_path": "/repo/README.md", "old_string": "a\n",
                                   "new_string": "let me explain\nthe design\n"})
    assert "me" not in ctx.replace("README.md", "").split()

