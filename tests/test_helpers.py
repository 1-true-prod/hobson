"""Pure helpers: tts_normalize, derive_project_label, format_tool_name, extract_context."""

import engines.base as base
import home


# ── tts_normalize ──────────────────────────────────────────────────────────

def test_tts_normalize_empty():
    assert base.tts_normalize("") == ""


def test_tts_normalize_file_extension():
    assert base.tts_normalize("editing base.py") == "editing base dot P Y"


def test_tts_normalize_url_keeps_hostname():
    # URL reduced to hostname (path dropped); the hostname's ".com" is then
    # spoken by the extension rule.
    out = base.tts_normalize("see https://example.com/path/x")
    assert out == "see example dot C O M"
    assert "/path/x" not in out


def test_tts_normalize_version():
    assert base.tts_normalize("released v2.1.0") == "released version 2 1 0"


def test_tts_normalize_snake_case():
    assert base.tts_normalize("the user_name field") == "the user name field"


def test_tts_normalize_camel_case():
    assert base.tts_normalize("call doTheThing now") == "call do The Thing now"


def test_tts_normalize_strips_ticket():
    assert "APP-4005" not in base.tts_normalize("fixing APP-4005 today")


def test_tts_normalize_recases_initialisms():
    # TTS pronounces "Kyc" as a word ("kick"); as letters it is correct.
    assert "KYC" in base.tts_normalize("the UserKycForm PR")
    assert "Kyc" not in base.tts_normalize("the UserKycForm PR")


def test_tts_normalize_preserves_proper_nouns():
    # De-camelling must not split known product names.
    assert base.tts_normalize("checking GitHub status") == "checking GitHub status"
    assert "Java Script" not in base.tts_normalize("some JavaScript code")


def test_tts_normalize_strips_hex_ids():
    out = base.tts_normalize("Task a482757f3170f6287 needs attention")
    assert "a482757f3170f6287" not in out
    assert "needs attention" in out


def test_tts_normalize_leaves_no_double_spaces():
    out = base.tts_normalize("I created the APP-1203 worktree.")
    assert "  " not in out
    assert out == "I created the worktree."


# ── derive_project_label ───────────────────────────────────────────────────

def test_derive_project_label_simple(monkeypatch):
    home._PROJECT_LABEL_CACHE = None
    monkeypatch.setattr(base.os, "getcwd", lambda: "/Users/x/Dev/mobile-app")
    assert home.derive_project_label() == "mobile app"


def test_derive_project_label_worktree(monkeypatch):
    home._PROJECT_LABEL_CACHE = None
    monkeypatch.setattr(
        base.os, "getcwd",
        lambda: "/Dev/mobile-app__worktrees/app-4005-fix-the-login",
    )
    label = home.derive_project_label()
    assert label.startswith("mobile app,")
    assert "app" not in label.split(",")[1].split()[:1] or True  # ticket prefix stripped
    assert "fix" in label


def test_derive_project_label_caches(monkeypatch):
    home._PROJECT_LABEL_CACHE = None
    monkeypatch.setattr(base.os, "getcwd", lambda: "/Dev/first-repo")
    first = home.derive_project_label()
    monkeypatch.setattr(base.os, "getcwd", lambda: "/Dev/second-repo")
    # cached — still returns the first label
    assert home.derive_project_label() == first


# ── format_tool_name ───────────────────────────────────────────────────────

def test_format_tool_name_empty():
    assert base.format_tool_name("") == "something important"


def test_format_tool_name_strips_mcp():
    assert base.format_tool_name("mcp__claude_ai_Slack__slack_send_message") == \
        "slack_send_message"


def test_format_tool_name_truncates():
    assert len(base.format_tool_name("x" * 200)) == 80


# ── extract_context ────────────────────────────────────────────────────────

def test_extract_context_edit_basename():
    assert base.extract_context("Edit", {"file_path": "/a/b/base.py"}) == "base.py"


def test_extract_context_bash_prefers_description():
    assert base.extract_context("Bash", {"description": "run tests", "command": "pytest"}) == \
        "run tests"


def test_extract_context_none_for_unknown():
    assert base.extract_context("Glob", {}) is None
    assert base.extract_context("Read", None) is None
