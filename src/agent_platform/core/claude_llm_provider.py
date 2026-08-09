"""Anthropic Claude API 实现 LLMProvider 协议。"""
from __future__ import annotations

from typing import Sequence

from agent_platform.core.llm_provider import (
    ChatMessage,
    ModelReply,
    ToolCall,
    ToolDescription,
)


class ClaudeLLMProvider:
    """通过 Anthropic SDK 调用 Claude 模型。"""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
    ) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return f"claude/{self._model}"

    def generate(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDescription],
    ) -> ModelReply:
        if not messages:
            return ModelReply(text="请先输入一个问题。")

        # 将内部 ChatMessage 转为 Anthropic API 格式
        system_parts = [
            "你是一个专业的证券金融分析助手。",
            "你可以调用 analyze_security 工具来获取行情数据和技术指标。",
            "所有分析仅供参考，不构成投资建议。",
            "请用中文回复，分析要数据支撑，语言简洁专业。",
        ]

        anthropic_msgs: list[dict[str, object]] = []
        for msg in messages:
            role = "assistant" if msg.role == "assistant" else "user"
            anthropic_msgs.append({"role": role, "content": msg.content})

        anthropic_tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "证券代码（如 DEMO001, 600519）",
                        },
                    },
                    "required": ["symbol"],
                },
            }
            for t in tools
        ] if tools else None

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system="\n".join(system_parts),
                messages=anthropic_msgs,  # type: ignore[arg-type]
                tools=anthropic_tools,  # type: ignore[arg-type]
            )
        except Exception as exc:
            error_text = (
                f"Claude API 调用失败：{exc}\n\n"
                "请检查：\n"
                "1. ANTHROPIC_API_KEY 是否正确（https://console.anthropic.com/）\n"
                "2. 网络是否通畅\n"
                "3. API 余额是否充足\n\n"
                "你也可以在侧边栏将 LLM Provider 切换回 'mock' 继续使用离线演示。"
            )
            return ModelReply(text=error_text)

        # 解析 Claude 响应
        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        name=block.name,
                        arguments=dict(block.input) if block.input else {},
                    )
                )

        return ModelReply(
            text="\n".join(text_parts) if text_parts else "正在调用工具分析…",
            tool_calls=tuple(tool_calls),
        )
