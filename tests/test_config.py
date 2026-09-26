"""load_config() merge/migration and event-filtering."""

import json

import engines.base as base


def _write_config(claude, data):
    (claude / "claudio.json").write_text(json.dumps(data), encoding="utf-8")


def test_defaults_when_no_file(claude_home):
    cfg = base.load_config()
    assert cfg["engine"] == "say"
    assert cfg["personality"] == "alfred"
    assert cfg["events"] == ["stop", "permission", "notification"]
    # kokoro defaults from DEFAULT_CONFIG. 600s, not 180s: a 180s idle timeout
    # expired during 4.1% of real gaps between utterances, and each expiry cost
    # the next one a fallback to macOS `say`.
    assert cfg["kokoro"]["daemon_idle_timeout"] == 600


def test_shallow_merge_top_level(claude_home):
    _write_config(claude_home, {"engine": "kokoro-realtime", "volume": 7})
    cfg = base.load_config()
    assert cfg["engine"] == "kokoro-realtime"
    assert cfg["volume"] == 7
    # untouched defaults remain
    assert cfg["personality"] == "alfred"


def test_deep_merge_subdict(claude_home):
    _write_config(claude_home, {"ollama": {"model": "custom:1b"}})
    cfg = base.load_config()
    assert cfg["ollama"]["model"] == "custom:1b"
    # sibling key preserved from defaults
    assert cfg["ollama"]["url"] == "http://localhost:11434"


def test_realtime_events_migration(claude_home):
    _write_config(claude_home, {"kokoro": {"realtime_events": ["stop", "commentary"]}})
    cfg = base.load_config()
    assert cfg["events"] == ["stop", "commentary"]
    # deprecated key is stripped from the merged config
    assert "realtime_events" not in cfg["kokoro"]


def test_explicit_events_not_overridden_by_migration(claude_home):
    _write_config(claude_home, {
        "events": ["stop"],
        "kokoro": {"realtime_events": ["stop", "permission", "notification"]},
    })
    cfg = base.load_config()
    assert cfg["events"] == ["stop"]


def test_invalid_json_falls_back_to_defaults(claude_home):
    (claude_home / "claudio.json").write_text("{not json", encoding="utf-8")
    cfg = base.load_config()
    assert cfg["engine"] == "say"


def _engine(config):
    from engines.say import SayEngine
    return SayEngine(config)


def test_event_enabled_matrix(claude_home):
    eng = _engine({"events": ["stop", "permission"], "personality": "alfred"})
    assert eng._is_event_enabled({"hook_event_name": "Stop"}) is True
    assert eng._is_event_enabled({"hook_event_name": "PermissionRequest"}) is True
    assert eng._is_event_enabled({"hook_event_name": "Notification"}) is False
    assert eng._is_event_enabled({"hook_event_name": "PreToolUse"}) is False


def test_event_enabled_unknown_event_passes(claude_home):
    # An event name not in EVENT_MAP is not gated (returns True).
    eng = _engine({"events": [], "personality": "alfred"})
    assert eng._is_event_enabled({"hook_event_name": "SomethingElse"}) is True
