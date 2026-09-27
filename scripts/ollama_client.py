"""ollama_client.py — shared Ollama HTTP plumbing.

Single source of truth for the Ollama endpoint defaults and request
construction, so the classify (stop_outcome.py, /api/generate, non-streaming) and
phrase-generation (phrase_gen.py, /api/chat, streaming) call sites can't
drift apart on URL handling, headers, or constants. Callers own their own
timeout and response parsing (streaming vs not).
"""

import json
from urllib.request import Request

DEFAULT_OLLAMA_MODEL = "llama3.2:3b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"


def build_request(path, payload, ollama_url=None):
    """Build a POST Request to `<ollama_url><path>` with a JSON body."""
    url = (ollama_url or DEFAULT_OLLAMA_URL).rstrip("/") + path
    body = json.dumps(payload).encode()
    return Request(url, data=body, headers={"Content-Type": "application/json"})
