#!/usr/bin/env python3
"""setup_voice_gen.py — record the setup wizard's voices, once, for shipping.

The wizard ships clips, not TTS models, and plays only what
setup/ui/voice/manifest.json lists (a line with no clip goes to macOS `say`):

  lines       every line in setup/ui/lines.json, said by the narrator,
              Charles (Pocket TTS, VCTK p254)
  auditions   for each voice the wizard offers (setup_wizard.VOICES), the
              "audition.voice" line; and each personality's sample line in
              each engine's default voice. So a voice is heard before its
              engine is downloaded.

Kokoro-82M is Apache-2.0; Pocket TTS and the VCTK voices are CC BY 4.0,
credited in setup/ui/voice/LICENSES.md. Nothing else may be rendered here:
a voice cloned from someone's recording is not ours to ship.

A dev tool. Each engine renders in its own venv (no daemons: their pid files
belong to the running Hobson), as the engine itself would speak it: Kokoro in
en-us at the default speed, Pocket TTS peak-normalised to 0.95.

    python3 scripts/cache-gen/setup_voice_gen.py [--venvs DIR]

A clip is named by the hash of what made it (engine, voice, setting, text),
so one already rendered is kept and only new lines cost anything; clips
nothing uses any more are deleted. tests/test_setup_voice.py fails when a
line or a voice has no clip. Needs afconvert (macOS) for AAC.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import home  # noqa: E402
import setup_wizard  # noqa: E402
from bark_templates import bark_hash  # noqa: E402

UI = os.path.join(home.ROOT, "setup", "ui")
VOICE_DIR = os.path.join(UI, "voice")
MANIFEST = os.path.join(VOICE_DIR, "manifest.json")
BITRATE = "48000"  # mono speech: about 6 KB a second

# The narrator, and the temperature he is sampled at: lower than the
# engine's 0.7 default, so a line comes out steady on the one take shipped.
NARRATOR = ("pocket-tts", "charles", 0.4)
AUDITION_LINE = "audition.voice"


def engine_param(engine):
    """How the engine speaks by default: Kokoro's speed, Pocket's temperature."""
    if engine == "kokoro-realtime":
        return home.DEFAULT_CONFIG["kokoro"]["speed"]
    return home.DEFAULT_CONFIG["pocket_tts"]["temp"]


KOKORO_WORKER = r"""
import json, os, sys
import soundfile as sf
from kokoro_onnx import Kokoro
models = sys.argv[1]
tts = Kokoro(os.path.join(models, "kokoro-v1.0.int8.onnx"), os.path.join(models, "voices-v1.0.bin"))
for job in json.load(sys.stdin):
    # en-us whatever the voice: what kokoro-daemon.py asks for.
    samples, rate = tts.create(job["text"], voice=job["voice"], speed=job["param"], lang="en-us")
    sf.write(job["out"], samples, rate)
    print(job["out"], flush=True)
"""

POCKET_WORKER = r"""
import json, sys
import numpy as np
import scipy.io.wavfile as wav
from pocket_tts import TTSModel
models = {}
for job in json.load(sys.stdin):
    temp = job["param"]
    if temp not in models:
        models[temp] = TTSModel.load_model(language="english", temp=temp)
    model = models[temp]
    audio = model.generate_audio(model.get_state_for_audio_prompt(job["voice"]), job["text"]).numpy()
    peak = abs(audio).max()
    if peak > 1e-6:
        audio = audio * (0.95 / peak)  # as pocket-tts-daemon.py does
    wav.write(job["out"], model.sample_rate, audio)
    print(job["out"], flush=True)
"""


def clip_name(engine, voice, param, text):
    return f"{bark_hash(f'{engine}|{voice}|{param}|{text}')}.m4a"


def wanted_clips():
    """[(engine, voice, param, text)] for everything the manifest will list."""
    with open(os.path.join(UI, "lines.json"), encoding="utf-8") as f:
        lines = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    jobs = [(NARRATOR[0], NARRATOR[1], NARRATOR[2], text) for key, text in lines.items() if key != AUDITION_LINE]
    for engine, voices in setup_wizard.VOICES.items():
        param = engine_param(engine)
        for v in voices:
            jobs.append((engine, v["id"], param, lines[AUDITION_LINE]))
        for p in setup_wizard.personalities():
            jobs.append((engine, voices[0]["id"], param, p["sample"]))
    return jobs, lines


def render(engine, jobs, venvs):
    """Render (voice, param, text, out_path) jobs for one engine, as WAVs."""
    if not jobs:
        return
    venv = "kokoro" if engine == "kokoro-realtime" else "pocket-tts"
    python = os.path.join(venvs, venv, "bin", "python3")
    if not os.path.exists(python):
        sys.exit(f"No {venv} venv at {python}: pass --venvs, or run `hobson setup {venv}`")
    argv = [python, "-c", KOKORO_WORKER if engine == "kokoro-realtime" else POCKET_WORKER]
    if engine == "kokoro-realtime":
        models = next((d for d in (os.path.join(home.ROOT, "models"), home.path("models"))
                       if os.path.exists(os.path.join(d, "kokoro-v1.0.int8.onnx"))), None)
        if not models:
            sys.exit("No Kokoro model: run `hobson setup kokoro` first")
        argv.append(models)
    payload = [{"voice": v, "param": p, "text": t, "out": out} for v, p, t, out in jobs]
    subprocess.run(argv, input=json.dumps(payload), text=True, check=True,
                   stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--venvs", default=os.path.join(home.ROOT, "venvs"),
                        help="where the kokoro and pocket-tts venvs are (a worktree has none of its own)")
    args = parser.parse_args()

    jobs, lines = wanted_clips()
    os.makedirs(VOICE_DIR, exist_ok=True)
    manifest = {"_comment": "Written by scripts/cache-gen/setup_voice_gen.py; licences in LICENSES.md.",
                "narrator": {"engine": NARRATOR[0], "voice": NARRATOR[1], "temp": NARRATOR[2]},
                "lines": {}, "auditions": {}}
    todo = {}
    with tempfile.TemporaryDirectory() as tmp:
        for engine, voice, param, text in jobs:
            name = clip_name(engine, voice, param, text)
            path = f"voice/{name}"
            if (engine, voice, param) == NARRATOR and text in lines.values() and text != lines[AUDITION_LINE]:
                manifest["lines"][text] = path
            else:
                manifest["auditions"].setdefault(engine, {}).setdefault(voice, {})[text] = path
            if not os.path.exists(os.path.join(VOICE_DIR, name)):
                todo.setdefault(engine, []).append((voice, param, text, os.path.join(tmp, name + ".wav")))
        for engine, engine_jobs in todo.items():
            print(f"{engine}: rendering {len(engine_jobs)} clip(s)")
            render(engine, engine_jobs, args.venvs)
            for voice, _, text, wav in engine_jobs:
                out = os.path.join(VOICE_DIR, os.path.basename(wav)[:-len(".wav")])
                subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", BITRATE, wav, out], check=True)
                print(f"  {voice:<12} {text}")

    wanted = {clip_name(*j) for j in jobs}
    for name in os.listdir(VOICE_DIR):
        if name.endswith(".m4a") and name not in wanted:
            os.remove(os.path.join(VOICE_DIR, name))
            print(f"  removed {name}")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    size = sum(os.path.getsize(os.path.join(VOICE_DIR, n)) for n in wanted)
    print(f"{len(manifest['lines'])} lines, {len(wanted) - len(manifest['lines'])} auditions: {size // 1024} KB")


if __name__ == "__main__":
    main()
