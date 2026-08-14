from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ToolDescription:
    name: str
    description: str


class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    def generate(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDescription],
    ) -> ModelReply: ...


class LLMProviderError(RuntimeError):
    """LLM 供应商调用失败的安全基类，不携带响应正文或凭证。"""

    error_type = "provider_error"
    retryable = False

    def __init__(self, message: str = "LLM 服务调用失败") -> None:
        super().__init__(message)


class LLMAuthenticationError(LLMProviderError):
    error_type = "provider_auth_error"


class LLMRateLimitError(LLMProviderError):
    error_type = "provider_rate_limit_error"
    retryable = True


class LLMNetworkError(LLMProviderError):
    error_type = "provider_network_error"
    retryable = True


class LLMServerError(LLMProviderError):
    error_type = "provider_http_5xx"
    retryable = True


class LLMInvalidRequestError(LLMProviderError):
    error_type = "provider_invalid_request"
