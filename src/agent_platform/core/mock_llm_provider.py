from __future__ import annotations

import re
from typing import Sequence

from agent_platform.core.llm_provider import (
    ChatMessage,
    ModelReply,
    ToolCall,
    ToolDescription,
)


class MockLLMProvider:
    """离线、可预测的演示模型，不调用任何外部 API。"""

    @property
    def name(self) -> str:
        return "mock"

    def generate(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDescription],
    ) -> ModelReply:
        if not messages:
            return ModelReply(text="请先输入一个问题。")

        latest = messages[-1]
        if latest.role == "tool":
            return ModelReply(
                text=(
                    "我已根据工具返回的数据完成分析。\n\n"
                    f"{latest.content}\n\n"
                    "以上分析结果仅供研究参考，不构成投资建议。"
                )
            )

        latest_user = next(
            (message for message in reversed(messages) if message.role == "user"),
            latest,
        )
        question = latest_user.content.strip()
        available_tools = {tool.name for tool in tools}
        if "analyze_security" in available_tools and self._asks_for_analysis(question):
            symbol = self._extract_symbol(question) or "TEST001"
            return ModelReply(
                text="我先调用证券分析工具读取行情数据并计算技术指标。",
                tool_calls=(ToolCall("analyze_security", {"symbol": symbol}),),
            )

        return ModelReply(
            text=(
                "这是本地 Mock Agent 的离线回复。"
                "你可以输入「分析 TEST001」「分析 600519」「MACD 金叉」等指令；"
                "Agent 会调用证券分析工具读取行情并计算技术指标。"
            )
        )

    @staticmethod
    def _asks_for_analysis(question: str) -> bool:
        keywords = (
            "分析", "行情", "均线", "收益", "波动", "demo",
            "macd", "rsi", "kdj", "布林带", "指标", "股票",
            "对比", "回报", "回撤", "涨跌",
        )
        return any(keyword in question.lower() for keyword in keywords)

    @staticmethod
    def _extract_symbol(question: str) -> str | None:
        # 优先匹配样例代码 TEST001-TEST020 / DEMO001 等，再匹配 6 位数字 A 股代码
        # 注意：用 (?<!\d) / (?!\d) 而非 \b，避免中文字符紧接数字时边界失效
        sample = re.search(r"(?:TEST|DEMO)\d{3}", question.upper())
        if sample:
            return sample.group(0)
        real = re.search(r"(?<!\d)(\d{6})(?!\d)", question)
        if real:
            return real.group(1)
        return None
