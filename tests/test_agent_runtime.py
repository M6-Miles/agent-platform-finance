from __future__ import annotations

from agent_platform.core.agent_runtime import AgentRuntime
from agent_platform.core.llm_provider import ChatMessage
from agent_platform.core.mock_llm_provider import MockLLMProvider
from agent_platform.core.tools import RegisteredTool, ToolRegistry
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider


def build_runtime() -> AgentRuntime:
    registry = ToolRegistry()
    provider = SampleMarketDataProvider()

    def analyze_with_sample(symbol: str) -> str:
        from agent_platform.finance.analysis import analyze_security
        return analyze_security(symbol, provider=provider).to_markdown()

    registry.register(
        RegisteredTool(
            name="analyze_security",
            description="分析样例证券。",
            handler=analyze_with_sample,
        )
    )
    return AgentRuntime(provider=MockLLMProvider(), tools=registry)


def test_agent_runtime_calls_security_analysis_tool() -> None:
    result = build_runtime().run("请分析 DEMO001")

    assert result.provider == "mock"
    assert len(result.steps) == 1
    assert result.steps[0].name == "analyze_security"
    assert "仅供研究参考" in result.answer


def test_agent_runtime_accepts_conversation_history() -> None:
    history = [
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="你好，请问需要什么帮助？"),
    ]

    result = build_runtime().run("请分析 DEMO002", history=history)

    assert len(result.steps) == 1
    assert "DEMO002" in result.steps[0].output
