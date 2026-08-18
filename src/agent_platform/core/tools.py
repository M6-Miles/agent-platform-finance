from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agent_platform.core.llm_provider import ToolDescription


ToolHandler = Callable[..., str]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    description: str
    handler: ToolHandler


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    name: str
    output: str
    is_error: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具已注册：{tool.name}")
        self._tools[tool.name] = tool

    def descriptions(self) -> list[ToolDescription]:
        return [
            ToolDescription(name=tool.name, description=tool.description)
            for tool in self._tools.values()
        ]

    def names(self) -> frozenset[str]:
        """返回已注册工具名，供插件注册器避免依赖内部字典。"""
        return frozenset(self._tools)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecutionResult(
                name=name,
                output=f"未找到工具：{name}",
                is_error=True,
            )

        try:
            output = tool.handler(**arguments)
        except Exception as exc:
            return ToolExecutionResult(
                name=name,
                output=f"工具执行失败：{exc}",
                is_error=True,
            )
        return ToolExecutionResult(name=name, output=str(output))
