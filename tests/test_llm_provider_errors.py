from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from agent_platform.core.claude_llm_provider import ClaudeLLMProvider
from agent_platform.core.deepseek_llm_provider import DeepSeekLLMProvider
from agent_platform.core.llm_provider import (
    ChatMessage,
    LLMAuthenticationError,
    LLMInvalidRequestError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
)


class _HTTPClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, *_args, **_kwargs):
        raise self.exc


def _status_error(status: int, secret: str = "sk-test-secret") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(status, request=request, text=secret)
    return httpx.HTTPStatusError("unsafe " + secret, request=request, response=response)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, LLMAuthenticationError),
        (429, LLMRateLimitError),
        (503, LLMServerError),
        (422, LLMInvalidRequestError),
    ],
)
def test_deepseek_maps_http_errors(monkeypatch, caplog, status, expected) -> None:
    secret = "sk-test-secret"
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: _HTTPClient(_status_error(status, secret)))
    provider = DeepSeekLLMProvider(secret)

    with pytest.raises(expected) as caught:
        provider.generate([ChatMessage("user", "test")], [])

    assert secret not in str(caught.value)
    assert secret not in caplog.text


def test_deepseek_maps_timeout(monkeypatch) -> None:
    request = httpx.Request("POST", "https://example.invalid")
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: _HTTPClient(httpx.ReadTimeout("timeout", request=request)),
    )
    provider = DeepSeekLLMProvider("secret")
    with pytest.raises(LLMNetworkError):
        provider.generate([ChatMessage("user", "test")], [])


class _ClaudeMessages:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def create(self, **_kwargs):
        raise self.exc


def _claude_provider(exc: Exception) -> ClaudeLLMProvider:
    provider = object.__new__(ClaudeLLMProvider)
    provider._client = SimpleNamespace(messages=_ClaudeMessages(exc))
    provider._model = "test-model"
    provider._max_tokens = 10
    return provider


def test_claude_maps_authentication_error() -> None:
    import anthropic

    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(401, request=request)
    exc = anthropic.AuthenticationError("unsafe", response=response, body=None)
    with pytest.raises(LLMAuthenticationError):
        _claude_provider(exc).generate([ChatMessage("user", "test")], [])


def test_claude_maps_rate_limit_error() -> None:
    import anthropic

    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(429, request=request)
    exc = anthropic.RateLimitError("unsafe", response=response, body=None)
    with pytest.raises(LLMRateLimitError):
        _claude_provider(exc).generate([ChatMessage("user", "test")], [])


def test_claude_maps_network_error() -> None:
    import anthropic

    request = httpx.Request("POST", "https://example.invalid")
    exc = anthropic.APIConnectionError(message="unsafe", request=request)
    with pytest.raises(LLMNetworkError):
        _claude_provider(exc).generate([ChatMessage("user", "test")], [])


def test_claude_maps_server_error() -> None:
    import anthropic

    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(503, request=request)
    exc = anthropic.InternalServerError("unsafe", response=response, body=None)
    with pytest.raises(LLMServerError):
        _claude_provider(exc).generate([ChatMessage("user", "test")], [])
