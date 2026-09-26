"""bark_templates lazy loading, normalize_tool_name, bark_hash invariant, presets."""

import json
import os

import bark_templates


def test_lazy_categories_for_alfred(claude_home, monkeypatch):
    monkeypatch.setattr(bark_templates, "_get_personality_name", lambda: "alfred")
    bark_templates._data = None
    cats = bark_templates.CATEGORIES
    assert {"done", "broken", "question"} <= set(cats)
    assert "lead" in cats["done"] and "tail" in cats["done"]


def test_missing_personality_falls_back(claude_home, monkeypatch):
    monkeypatch.setattr(bark_templates, "_get_personality_name", lambda: "does-not-exist")
    bark_templates._data = None
    # Falls back to alfred templates rather than crashing.
    assert bark_templates.CATEGORIES != {}


def test_unknown_attr_raises():
    try:
        bark_templates.NOT_A_REAL_ATTR
    except AttributeError:
        return
    raise AssertionError("expected AttributeError")


def test_normalize_tool_name_strips_mcp_prefix():
    assert bark_templates.normalize_tool_name(
        "mcp__plugin_context-mode_context-mode__ctx_execute") == "ctx_execute"
    assert bark_templates.normalize_tool_name(
        "mcp__claude_ai_Slack__slack_send_message") == "slack_send_message"
    assert bark_templates.normalize_tool_name("Bash") == "Bash"


def test_all_static_barks_yields_tuples(claude_home, monkeypatch):
    monkeypatch.setattr(bark_templates, "_get_personality_name", lambda: "alfred")
    bark_templates._data = None
    items = list(bark_templates.all_static_barks())
    assert items
    assert all(isinstance(t, tuple) and len(t) == 2 for t in items)


# ── bark_hash invariant across the three definitions ───────────────────────

def test_bark_hash_invariant():
    import engines.base as base
    text = "Very good sir, the task is complete."
    assert bark_templates.bark_hash(text) == base.bark_hash(text)

    # cache-gen imports bark_hash from bark_templates — confirm same value.
    import importlib.util
    cg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "cache-gen", "chatterbox_gen.py",
    )
    src = open(cg_path, encoding="utf-8").read()
    assert "from bark_templates import" in src and "bark_hash" in src


# ── presets.json validation ────────────────────────────────────────────────

def _load_presets():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "presets.json",
    )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_presets_reference_valid_engines(claudio_entry):
    presets = _load_presets()
    valid_engines = set(claudio_entry.ENGINES)
    for name, preset in presets.items():
        engine = preset.get("config", {}).get("engine")
        assert engine in valid_engines, f"{name}: bad engine {engine!r}"


def test_presets_reference_existing_personalities():
    presets = _load_presets()
    pdir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "personalities",
    )
    for name, preset in presets.items():
        personality = preset.get("config", {}).get("personality")
        assert os.path.isdir(os.path.join(pdir, personality)), \
            f"{name}: missing personality {personality!r}"


def test_presets_reference_valid_events():
    presets = _load_presets()
    valid = {"stop", "permission", "notification", "commentary"}
    for name, preset in presets.items():
        for ev in preset.get("config", {}).get("events", []):
            assert ev in valid, f"{name}: bad event {ev!r}"
