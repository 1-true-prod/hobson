"""classify() against a mocked Ollama /api/generate."""

from urllib.error import URLError

import engines.base as base


def test_classify_done(claude_home, fake_ollama):
    fake_ollama.generate("done")
    assert base.classify("All tests pass.") == "done"
    assert base.classify("x")  # sanity
    assert any("/api/generate" in u for u in fake_ollama.urls)


def test_classify_each_category(claude_home, fake_ollama):
    for word in ("done", "broken", "question"):
        fake_ollama.generate(word)
        assert base.classify("some text") == word


def test_classify_strips_punctuation_and_case(claude_home, fake_ollama):
    fake_ollama.generate("Broken.")
    assert base.classify("It failed") == "broken"


def test_classify_unknown_word_returns_none(claude_home, fake_ollama):
    fake_ollama.generate("banana")
    assert base.classify("???") is None


def test_classify_handles_glued_stop_token(claude_home, fake_ollama):
    # raw mode can return the answer glued to a stop token: "done<end_of_turn>"
    fake_ollama.generate("done<end_of_turn>")
    assert base.classify("All tests pass.") == "done"


def test_classify_network_error_returns_none(claude_home, fake_ollama):
    fake_ollama.error(URLError("connection refused"))
    assert base.classify("anything") is None


def test_classify_respects_model_and_url_args(claude_home, fake_ollama):
    fake_ollama.generate("done")
    base.classify("t", model="custom:1b", ollama_url="http://example:1234")
    assert any(u.startswith("http://example:1234") for u in fake_ollama.urls)
