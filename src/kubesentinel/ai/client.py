"""AI backend client. The only implementation is a local Ollama server --
no API key, no cloud account, no per-request cost. `AiClient` is a Protocol
so `summarizer.py` depends on "something that can generate text from a
prompt," never on Ollama specifically -- a future cloud backend would
implement the same Protocol without summarizer.py changing at all.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Protocol

logger = logging.getLogger(__name__)


class AiClientError(Exception):
    """Raised on any failure talking to the AI backend (unreachable, timed
    out, non-200, malformed response). Always caught by the caller -- an AI
    summary is advisory, and its unavailability must never crash a request
    for something that already exists (the incident itself)."""


class AiClient(Protocol):
    def generate(self, prompt: str) -> str: ...


class OllamaClient:
    """Talks to a local Ollama server's `/api/generate` endpoint.

    Uses the standard library's `urllib.request` rather than adding an HTTP
    dependency for one local call -- this is a single POST with a hard
    timeout, not a client library's worth of surface area.
    """

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    def generate(self, prompt: str) -> str:
        body = json.dumps({"model": self._model, "prompt": prompt, "stream": False}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise AiClientError(f"could not reach Ollama at {self._base_url}: {exc}") from exc
        except TimeoutError as exc:
            raise AiClientError(f"Ollama request timed out after {self._timeout}s") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AiClientError(f"Ollama returned non-JSON response: {raw[:200]!r}") from exc

        text = parsed.get("response")
        if not isinstance(text, str) or not text.strip():
            raise AiClientError(f"Ollama response had no usable 'response' text: {parsed}")
        return text.strip()
