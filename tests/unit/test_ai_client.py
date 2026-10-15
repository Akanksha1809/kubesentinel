import json
import socket
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from kubesentinel.ai.client import AiClientError, OllamaClient


def _response(body: dict) -> BytesIO:
    buf = BytesIO(json.dumps(body).encode("utf-8"))
    buf.__enter__ = lambda self: self  # support `with urlopen(...) as response`
    buf.__exit__ = lambda self, *a: None
    return buf


def test_generate_returns_stripped_response_text():
    client = OllamaClient("http://localhost:11434", "qwen2.5-coder:3b")
    with patch("urllib.request.urlopen", return_value=_response({"response": "  hello  "})):
        text = client.generate("summarize this")
    assert text == "hello"


def test_generate_sends_expected_request_body():
    client = OllamaClient("http://localhost:11434", "qwen2.5-coder:3b", timeout_seconds=5.0)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _response({"response": "ok"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.generate("my prompt")

    assert captured["url"] == "http://localhost:11434/api/generate"
    assert captured["body"] == {"model": "qwen2.5-coder:3b", "prompt": "my prompt", "stream": False}
    assert captured["timeout"] == 5.0


def test_generate_raises_ai_client_error_on_connection_failure():
    client = OllamaClient("http://localhost:11434", "qwen2.5-coder:3b")
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        with pytest.raises(AiClientError):
            client.generate("prompt")


def test_generate_raises_ai_client_error_on_timeout():
    client = OllamaClient("http://localhost:11434", "qwen2.5-coder:3b")
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        with pytest.raises(AiClientError):
            client.generate("prompt")


def test_generate_raises_ai_client_error_on_missing_response_field():
    client = OllamaClient("http://localhost:11434", "qwen2.5-coder:3b")
    with patch("urllib.request.urlopen", return_value=_response({"done": True})):
        with pytest.raises(AiClientError):
            client.generate("prompt")


def test_generate_raises_ai_client_error_on_malformed_json():
    buf = BytesIO(b"not json")
    buf.__enter__ = lambda self: self
    buf.__exit__ = lambda self, *a: None
    client = OllamaClient("http://localhost:11434", "qwen2.5-coder:3b")
    with patch("urllib.request.urlopen", return_value=buf):
        with pytest.raises(AiClientError):
            client.generate("prompt")
