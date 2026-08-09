"""MockLLMProvider 单元测试。"""
from __future__ import annotations

from agent_platform.core.mock_llm_provider import MockLLMProvider


class TestExtractSymbol:
    @staticmethod
    def _call(question: str) -> str | None:
        return MockLLMProvider._extract_symbol(question)

    def test_demo001(self) -> None:
        assert self._call("分析 DEMO001") == "DEMO001"

    def test_demo002(self) -> None:
        assert self._call("请分析 DEMO002 的行情") == "DEMO002"

    def test_demo_lowercase(self) -> None:
        assert self._call("分析 demo001") == "DEMO001"

    def test_demo_in_middle(self) -> None:
        assert self._call("我想看 DEMO001 怎么样") == "DEMO001"

    def test_a_share_600519(self) -> None:
        assert self._call("分析 600519") == "600519"

    def test_a_share_no_space(self) -> None:
        assert self._call("分析600519行情") == "600519"

    def test_000001(self) -> None:
        assert self._call("000001 怎么样") == "000001"

    def test_seven_digit_returns_none(self) -> None:
        assert self._call("1234567") is None

    def test_no_match_returns_none(self) -> None:
        assert self._call("你好世界") is None

    def test_empty_string(self) -> None:
        assert self._call("") is None

    def test_five_digit_returns_none(self) -> None:
        assert self._call("12345") is None

    def test_demo_wins_over_a_share(self) -> None:
        assert self._call("DEMO003 vs 600519") == "DEMO003"


class TestAsksForAnalysis:
    @staticmethod
    def _call(question: str) -> bool:
        return MockLLMProvider._asks_for_analysis(question)

    def test_keyword_analysis(self) -> None:
        assert self._call("分析") is True

    def test_keyword_macd(self) -> None:
        assert self._call("MACD 金叉") is True

    def test_keyword_rsi(self) -> None:
        assert self._call("RSI 超买") is True

    def test_keyword_kdj(self) -> None:
        assert self._call("kdj 指标") is True

    def test_keyword_bollinger(self) -> None:
        assert self._call("布林带 收窄") is True

    def test_keyword_stock(self) -> None:
        assert self._call("这只股票") is True

    def test_keyword_return(self) -> None:
        assert self._call("回报 率") is True

    def test_hello_not_analysis(self) -> None:
        assert self._call("你好") is False

    def test_empty_string(self) -> None:
        assert self._call("") is False

    def test_english_greeting(self) -> None:
        assert self._call("hello") is False
