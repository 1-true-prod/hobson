"""The wizard's voice: every line it says, and every voice it offers, has a
shipped clip (setup/ui/voice/, rendered by scripts/cache-gen/setup_voice_gen.py)."""

import json
import os
import re

import home
import setup_wizard as sw

UI = os.path.join(home.ROOT, "setup", "ui")


def load(name):
    with open(os.path.join(UI, name), encoding="utf-8") as f:
        return json.load(f)


def lines():
    return {k: v for k, v in load("lines.json").items() if not k.startswith("_")}


def test_every_line_the_wizard_uses_exists():
    with open(os.path.join(UI, "wizard.js"), encoding="utf-8") as f:
        source = f.read()
    keys = set(re.findall(r'L\("([\w.]+)"\)', source))
    keys |= {f"hero.{kind}.{part}" for kind in ("setup", "update") for part in ("night", "morning", "afternoon", "evening")}
    assert keys - set(lines()) == set()


def test_every_line_has_a_clip_on_disk():
    manifest = load("voice/manifest.json")
    missing = [text for key, text in lines().items() if key != "audition.voice" and text not in manifest["lines"]]
    assert missing == [], "re-render: python3 scripts/cache-gen/setup_voice_gen.py"
    for path in manifest["lines"].values():
        assert os.path.isfile(os.path.join(UI, path))


def test_every_offered_voice_can_be_heard():
    auditions = load("voice/manifest.json")["auditions"]
    line = lines()["audition.voice"]
    for engine, voices in sw.VOICES.items():
        for v in voices:
            assert os.path.isfile(os.path.join(UI, auditions[engine][v["id"]][line])), (engine, v["id"])
        default = voices[0]["id"]
        for p in sw.personalities():
            assert p["sample"] in auditions[engine][default], (engine, p["id"])


def test_the_first_voice_of_each_engine_is_its_default():
    assert sw.VOICES["kokoro-realtime"][0]["id"] == home.DEFAULT_CONFIG["kokoro"]["voice"]
    assert sw.VOICES["pocket-tts"][0]["id"] == home.DEFAULT_CONFIG["pocket_tts"]["voice"]


def test_there_is_a_choice_of_female_voices():
    for engine, voices in sw.VOICES.items():
        assert sum(v["sex"] == "female" for v in voices) >= 2, engine


def test_no_orphan_clips():
    manifest = load("voice/manifest.json")
    listed = set(manifest["lines"].values())
    for by_voice in manifest["auditions"].values():
        for clips in by_voice.values():
            listed |= set(clips.values())
    on_disk = {f"voice/{n}" for n in os.listdir(os.path.join(UI, "voice")) if n.endswith(".m4a")}
    assert on_disk == listed


def test_every_clip_is_servable():
    manifest = load("voice/manifest.json")
    for path in manifest["lines"].values():
        assert sw.CLIP.match(path)
