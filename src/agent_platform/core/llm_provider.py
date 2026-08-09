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
