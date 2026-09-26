#!/usr/bin/env python3
"""ab_models.py — dev-only A/B harness for Ollama model selection.

Compares candidate models against the LIVE Ollama server on the two jobs
hobson actually asks of them:

  1. classify  — done/broken/question accuracy over base.EXAMPLES
  2. first-person — do generated Stop phrases natively start with "I "
                    (measured BEFORE phrase_gen's _ensure_first_person repair,
                     so it reflects the model, not the safety net)

This is the harness behind the model-choice notes in CLAUDE.md. Not part of
the pytest suite — it needs a running Ollama with the models pulled.

    ollama pull llama3.2:3b gemma4:e4b qwen3:4b
    python3 scripts/ab_models.py                 # default candidate set
    python3 scripts/ab_models.py gemma4:e4b      # just one
    python3 scripts/ab_models.py --gens 50 a b   # 50 generations each

Classify cases live in scripts/ab_corpus.json (balanced done/broken/question);
edit that file to grow the sample. Falls back to base.EXAMPLES if it's missing.

ponytail: sequential, no concurrency — a few models × tens of calls is a
coffee-break run, not a service. Parallelize if the candidate set grows.
"""
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engines.base as base
from phrase_gen import _build_messages, _chat

# Candidates: current default + the two prior A/B contenders + Gemma 4 E4B.
DEFAULT_MODELS = ["llama3.2:3b", "gemma4:e4b", "qwen3:4b"]

CORPUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab_corpus.json")


def load_corpus():
    """(text, label) list from ab_corpus.json; fall back to base.EXAMPLES."""
    try:
        with open(CORPUS_FILE, encoding="utf-8") as f:
            return [tuple(c) for c in json.load(f)["cases"]]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return list(base.EXAMPLES)


CORPUS = load_corpus()
CATEGORIES = ("done", "broken", "question")
# Generation-pass texts: sample the corpus across all three categories.
GEN_TEXTS = [t for t, _ in CORPUS]


def score_classify(model):
    """Return (correct, total, per_category) over the corpus for one model.

    per_category maps label -> (correct, total), so a model that only fails
    one category (e.g. 'question') is visible instead of hidden in the total.
    """
    per = {c: [0, 0] for c in CATEGORIES}
    correct = 0
    for text, want in CORPUS:
        got = base.classify(text, model=model)
        ok = got == want
        correct += ok
        if want in per:
            per[want][1] += 1
            per[want][0] += ok
    return correct, len(CORPUS), per


def _native_first_person(raw):
    """Does the model's own phrase (post 'category |', pre-repair) start 'I '?"""
    seg = raw.split("|", 1)[1] if "|" in raw else raw
    seg = seg.strip().strip('"\'').strip()
    return seg[:2] == "I " or seg.strip() == "I"


def score_first_person(model, gens):
    """Return (native_first_person, non_empty) over `gens` generations."""
    fp = nonempty = 0
    texts = (GEN_TEXTS * (gens // len(GEN_TEXTS) + 1))[:gens]
    for text in texts:
        messages = _build_messages("Stop", text, "Recent: \"I started the task.\"")
        raw, err = _chat(messages, model, timeout=20, ollama_url=None)
        if err and not raw:
            continue
        nonempty += 1
        fp += _native_first_person(raw)
    return fp, nonempty


def main(argv):
    gens = 50
    if "--gens" in argv:
        i = argv.index("--gens")
        gens = int(argv[i + 1])
        del argv[i:i + 2]
    models = argv or DEFAULT_MODELS

    print(f"A/B: {len(models)} model(s), {len(CORPUS)} classify cases, "
          f"{gens} generations each. Live Ollama.\n")
    hdr = f"{'model':<16} {'classify':>9}"
    hdr += "".join(f"{c:>10}" for c in CATEGORIES)
    hdr += f"{'first-person':>16}"
    print(hdr)
    print("-" * len(hdr))
    for m in models:
        c_ok, c_n, per = score_classify(m)
        fp, ne = score_first_person(m, gens)
        fp_pct = f"{100 * fp / ne:.0f}% ({fp}/{ne})" if ne else "n/a (all failed)"
        row = f"{m:<16} {f'{c_ok}/{c_n}':>9}"
        row += "".join(f"{f'{ok}/{n}':>10}" for ok, n in (per[c] for c in CATEGORIES))
        row += f"{fp_pct:>16}"
        print(row)


if __name__ == "__main__":
    # ponytail: self-check the pure bit (first-person parse) offline, so a
    # broken parser fails loudly without needing a live server.
    assert _native_first_person("done | I fixed the bug.")
    assert _native_first_person('done | "I ran the tests."')
    assert not _native_first_person("done | The build failed.")
    assert not _native_first_person("Finished the task.")
    main(sys.argv[1:])
