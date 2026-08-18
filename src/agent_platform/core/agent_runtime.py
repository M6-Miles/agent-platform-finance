from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from agent_platform.core.llm_provider import ChatMessage, LLMProvider
from agent_platform.core.tools import ToolExecutionResult, ToolRegistry


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    answer: str
    steps: tuple[ToolExecutionResult, ...]
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0


class AgentRuntime:
    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        max_steps: int = 4,
        instruction_context: str = "",
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须大于 0")
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps
        self.instruction_context = instruction_context.strip()

    def run(
        self,
        user_message: str,
        history: Sequence[ChatMessage] = (),
    ) -> AgentRunResult:
        if not user_message.strip():
            raise ValueError("问题不能为空")

        messages = list(history)
        if self.instruction_context:
            messages.insert(0, ChatMessage(role="system", content=self.instruction_context))
        messages.append(ChatMessage(role="user", content=user_message.strip()))
        steps: list[ToolExecutionResult] = []
        input_tokens = 0
        output_tokens = 0

        for _ in range(self.max_steps):
            reply = self.provider.generate(messages, self.tools.descriptions())
            input_tokens += reply.input_tokens
            output_tokens += reply.output_tokens
            if not reply.tool_calls:
                return AgentRunResult(
                    answer=reply.text,
                    steps=tuple(steps),
                    provider=self.provider.name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

            # 将助手这一轮的回复加入对话历史，再追加工具结果，
            # 确保下一轮 generate() 看到完整的 assistant → tool 上下文。
            messages.append(ChatMessage(role="assistant", content=reply.text))

            for call in reply.tool_calls:
                result = self.tools.execute(call.name, call.arguments)
                steps.append(result)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=f"工具 {call.name} 返回：\n{result.output}",
                    )
                )

        return AgentRunResult(
            answer="Agent 达到最大工具调用步数，已安全停止。",
            steps=tuple(steps),
            provider=self.provider.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
