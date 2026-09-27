"""bark_templates.py — Personality-driven bark phrase templates.

Loads templates from the active personality JSON file. Keeps the same API
surface as before: CATEGORIES, PERMISSION_LEADS, PERMISSION_ACTIONS,
NOTIFICATION_TEMPLATES, GENERIC_PERMISSION_PHRASES, COMMON_TOOLS.

Lazy-loads personality data on first access via module-level _data cache.
"""

import hashlib
import json
import os
from itertools import product


# --------------- personality loader ---------------

_PERSONALITIES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "personalities")
_data = None  # cached personality templates


def _load_personality_templates(personality_name=None):
    """Load templates from the active personality JSON file.

    Falls back to 'hobson' if the requested personality doesn't exist or
    has no templates section.
    """
    if personality_name is None:
        personality_name = _get_personality_name()

    path = os.path.join(_PERSONALITIES_DIR, personality_name, "personality.json")
    if not os.path.isfile(path):
        # Fallback to hobson
        path = os.path.join(_PERSONALITIES_DIR, "hobson", "personality.json")
        if not os.path.isfile(path):
            # Ultimate fallback — return empty structure (should never happen)
            return {}

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    return data.get("templates", {})


def _get_personality_name():
    """The configured personality, as load_config() resolves it: after the
    claudio migration and the "alfred" alias. 'hobson' when unset."""
    from home import load_config
    return load_config().get("personality") or "hobson"


def _ensure_loaded():
    """Lazy-load personality templates on first access."""
    global _data
    if _data is None:
        _data = _load_personality_templates()


def reload_personality(personality_name=None):
    """Force-reload personality templates. Used when personality changes."""
    global _data
    _data = _load_personality_templates(personality_name)


# --------------- lazy proxy properties ---------------
# These module-level names preserve the existing API. They're populated on first
# access via __getattr__ at module level.

def _get_categories():
    _ensure_loaded()
    return _data.get("categories", {})


def _get_permission_leads():
    _ensure_loaded()
    return _data.get("permission_leads", [])


def _get_permission_actions():
    _ensure_loaded()
    return _data.get("permission_actions", [])


def _get_notification_templates():
    _ensure_loaded()
    return _data.get("notification_templates", [])


def _get_generic_permission_phrases():
    _ensure_loaded()
    return _data.get("generic_permission_phrases", [])


def _get_common_tools():
    _ensure_loaded()
    return _data.get("common_tools", [
        "Bash", "WebFetch", "Edit", "WebSearch",
        "AskUserQuestion", "Write", "ExitPlanMode", "Read",
    ])


# Module-level __getattr__ for lazy loading — Python 3.7+
_ATTR_MAP = {
    "CATEGORIES": _get_categories,
    "PERMISSION_LEADS": _get_permission_leads,
    "PERMISSION_ACTIONS": _get_permission_actions,
    "NOTIFICATION_TEMPLATES": _get_notification_templates,
    "GENERIC_PERMISSION_PHRASES": _get_generic_permission_phrases,
    "COMMON_TOOLS": _get_common_tools,
}


def __getattr__(name):
    if name in _ATTR_MAP:
        return _ATTR_MAP[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --------------- tool name normalization ---------------

def normalize_tool_name(tool):
    """Strip MCP server prefixes for cleaner speech and better cache hits.

    mcp__plugin_context-mode_context-mode__ctx_execute -> ctx_execute
    mcp__claude_ai_Slack__slack_send_message -> slack_send_message
    """
    tool = str(tool).strip()
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        if len(parts) >= 3:
            tool = "__".join(parts[2:])
    return tool


# --------------- cache utilities ---------------


def bark_hash(text):
    """Stable SHA-256 prefix for cache key. 16 hex chars = no collisions at this scale."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def all_static_barks():
    """Yield every pre-generatable bark string.

    Returns (category_tag, text) tuples for progress reporting.
    """
    _ensure_loaded()

    notifications = _data.get("notification_templates", [])
    categories = _data.get("categories", {})
    perm_leads = _data.get("permission_leads", [])
    perm_actions = _data.get("permission_actions", [])
    common_tools = _data.get("common_tools", [])
    generic_perms = _data.get("generic_permission_phrases", [])

    # Notification templates
    for t in notifications:
        yield ("notification", t)

    # Category combos: done/broken/question
    for cat_name, cat_data in categories.items():
        for lead, tail in product(cat_data["lead"], cat_data["tail"]):
            yield (cat_name, f"{lead} {tail}")

    # Permission combos for common tools
    for tool in common_tools:
        for lead, action in product(perm_leads, perm_actions):
            yield (f"permission:{tool}", f"{lead} {action.format(tool=tool)}")

    # Generic permission phrases (fallback for tools not in COMMON_TOOLS)
    for phrase in generic_perms:
        yield ("permission:generic", phrase)
