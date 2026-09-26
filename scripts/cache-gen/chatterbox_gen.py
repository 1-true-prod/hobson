#!/usr/bin/env python3
"""chatterbox_gen.py — Pre-generate WAV cache via Chatterbox TTS (voice cloning).

Usage:
    <venv>/bin/python3 chatterbox_gen.py
    <venv>/bin/python3 chatterbox_gen.py --force
    <venv>/bin/python3 chatterbox_gen.py --single "Done. Too easy."
    <venv>/bin/python3 chatterbox_gen.py --device cpu

Requires: chatterbox-tts, torch, torchaudio (in chatterbox venv)
Reference audio: <repo>/models/hobson-reference.wav (or any supported format)
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from bark_templates import all_static_barks
from bark_templates import bark_hash
from engines.base import load_config

CACHE_DIR = os.path.expanduser("~/.claude/voice-cache-chatterbox")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find_reference_audio(personality="hobson"):
    """Find reference audio matching engine lookup order: personality-specific, hobson, generic."""
    search_names = []
    if personality and personality != "hobson":
        search_names.append(f"{personality}-reference")
    search_names.append("hobson-reference")
    search_names.append("alfred-reference")  # its name before 0.3.0
    search_names.append("reference")
    for name in search_names:
        for base_dir in [os.path.join(ROOT, "models"), os.path.expanduser("~/.claude/models")]:
            for ext in ("wav", "mp3", "flac", "ogg", "m4a"):
                p = os.path.join(base_dir, f"{name}.{ext}")
                if os.path.isfile(p):
                    return p
    return os.path.join(ROOT, "models", "hobson-reference.wav")


_config = load_config()
REFERENCE_AUDIO = _find_reference_audio(_config.get("personality", "hobson"))


def cache_path_wav(text):
    return os.path.join(CACHE_DIR, bark_hash(text) + ".wav")


def load_model(device="mps"):
    """Load Chatterbox model. Heavy operation (~5-10s on first call)."""
    import perth
    if perth.PerthImplicitWatermarker is None:
        # resemble-perth C extension failed to load (common on macOS) — stub it out.
        # Watermarking is Resemble AI's audio fingerprinting; not needed for TTS.
        class _NoopWatermarker:
            def apply_watermark(self, wav, sample_rate=None):
                return wav
        perth.PerthImplicitWatermarker = _NoopWatermarker
    from chatterbox.tts import ChatterboxTTS
    return ChatterboxTTS.from_pretrained(device=device)


def _tts_text(text):
    """Replace mid-phrase full stops with commas so TTS doesn't pause like a sentence break."""
    return re.sub(r'\.\s+([A-Za-z])', lambda m: ', ' + m.group(1).lower(), text)


def generate_wav(model, text, reference_audio, output_path, exaggeration=1.0):
    """Generate a single WAV file from text using voice cloning."""
    import torchaudio
    wav = model.generate(_tts_text(text), audio_prompt_path=reference_audio, exaggeration=exaggeration)
    torchaudio.save(output_path, wav, model.sr)


def main():
    parser = argparse.ArgumentParser(
        description="Pre-generate bark WAV cache via Chatterbox TTS"
    )
    parser.add_argument("--force", action="store_true",
                        help="Regenerate all, even if WAV already exists")
    parser.add_argument("--device", default="mps",
                        help="Torch device: mps (Apple Silicon), cuda, cpu (default: mps)")
    parser.add_argument("--single", type=str, default=None,
                        help="Generate a single phrase (for runtime cache-miss backfill)")
    parser.add_argument("--exaggeration", type=float, default=1.0,
                        help="Emotion intensity 0.0-1.0 (default: 1.0, higher = more expressive)")
    args = parser.parse_args()

    if not os.path.isfile(REFERENCE_AUDIO):
        print(f"ERROR: Reference audio not found: {REFERENCE_AUDIO}", file=sys.stderr)
        print("Place a 5-10s audio clip at models/hobson-reference.wav", file=sys.stderr)
        sys.exit(1)

    os.makedirs(CACHE_DIR, exist_ok=True)

    print(f"Loading Chatterbox model (device={args.device})...", file=sys.stderr)
    model = load_model(device=args.device)
    print("Model loaded.", file=sys.stderr)

    # Single-phrase mode (called by engine for cache misses)
    if args.single is not None:
        out = cache_path_wav(args.single)
        if os.path.isfile(out):
            return  # already cached (race condition guard)
        try:
            generate_wav(model, args.single, REFERENCE_AUDIO, out, exaggeration=args.exaggeration)
        except Exception:
            pass  # silent failure for background backfill
        return

    # Batch mode
    barks = list(all_static_barks())
    total = len(barks)

    to_generate = []
    for tag, text in barks:
        out = cache_path_wav(text)
        if not os.path.exists(out) or args.force:
            to_generate.append((tag, text))

    if not to_generate:
        print("Cache is complete. Nothing to generate.")
        return

    print(f"Will generate {len(to_generate)} of {total} barks")
    print(f"Device: {args.device}")
    print(f"Exaggeration: {args.exaggeration}")
    print(f"Reference: {REFERENCE_AUDIO}")
    print()

    generated = 0
    errors = 0
    t0 = time.monotonic()

    for i, (tag, text) in enumerate(to_generate, 1):
        out = cache_path_wav(text)
        try:
            generate_wav(model, text, REFERENCE_AUDIO, out, exaggeration=args.exaggeration)
            generated += 1
        except Exception as e:
            errors += 1
            print(f"  ERROR [{i}/{len(to_generate)}] {tag}: {e}")

        if errors >= 10:
            print("Too many errors, stopping.", file=sys.stderr)
            break

        if generated % 25 == 0 or i == len(to_generate):
            elapsed = time.monotonic() - t0
            rate = generated / elapsed if elapsed > 0 else 0
            remaining = len(to_generate) - i
            eta = remaining / rate if rate > 0 else 0
            print(f"  [{i}/{len(to_generate)}] {tag}: generated {generated}, "
                  f"errors {errors}, {rate:.1f}/s, ETA {eta:.0f}s")

    elapsed = time.monotonic() - t0
    print(f"\nDone. Generated {generated}, errors {errors} "
          f"in {elapsed:.0f}s ({elapsed / 60:.1f} min)")


if __name__ == "__main__":
    main()
