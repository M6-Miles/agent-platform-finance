"""
P-05 非金融 Demo 测试 — 天气分析 Agent
========================================
验证平台可移植性：使用与金融 Agent 完全相同的 Guardrail 机制
"""
from __future__ import annotations

import sys
from pathlib import Path
import pytest

# 确保 examples/weather_analysis 和 src/ 都在路径中
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "examples" / "weather_analysis"))

from weather_agent import WeatherAnalysisAgent, WeatherReport, WEATHER_REPORT_SCHEMA  # noqa: E402
from agent_platform.core.harness import GuardrailViolation  # noqa: E402


# ── 基础数据 ──────────────────────────────────────────────────────────────────

BEIJING_TEMPS = [
    2, 3, 5, 8, 12, 18, 22, 25, 28, 30,
    29, 27, 22, 18, 13, 8, 5, 3, 2, 4,
    6, 9, 14, 19, 23, 26, 28, 29, 27, 24,
]

COOLING_TEMPS = [30, 28, 26, 24, 22, 20, 18, 16, 14, 12]
STABLE_TEMPS  = [20, 21, 19, 20, 21, 20, 19, 21, 20, 20]
WARMING_TEMPS = [10, 12, 14, 16, 18, 20, 22, 24, 26, 28]


# ── 正常路径测试 ──────────────────────────────────────────────────────────────

class TestWeatherAgentBasic:
    def setup_method(self) -> None:
        self.agent = WeatherAnalysisAgent()

    def test_returns_weather_report(self) -> None:
        report = self.agent.analyze("北京", BEIJING_TEMPS)
        assert isinstance(report, WeatherReport)

    def test_report_is_frozen(self) -> None:
        report = self.agent.analyze("北京", BEIJING_TEMPS)
        with pytest.raises((AttributeError, TypeError)):
            report.city = "上海"  # type: ignore[misc]

    def test_city_preserved(self) -> None:
        report = self.agent.analyze("上海", BEIJING_TEMPS)
        assert report.city == "上海"

    def test_period_days_matches_input(self) -> None:
        report = self.agent.analyze("北京", BEIJING_TEMPS)
        assert report.period_days == len(BEIJING_TEMPS)

    def test_avg_temp_reasonable(self) -> None:
        report = self.agent.analyze("北京", BEIJING_TEMPS)
        assert report.min_temp_c <= report.avg_temp_c <= report.max_temp_c

    def test_temp_range(self) -> None:
        report = self.agent.analyze("北京", BEIJING_TEMPS)
        assert abs(report.temp_range_c - (report.max_temp_c - report.min_temp_c)) < 0.01

    def test_volatility_positive(self) -> None:
        report = self.agent.analyze("北京", BEIJING_TEMPS)
        assert report.volatility_c > 0


# ── 趋势判断测试 ──────────────────────────────────────────────────────────────

class TestWeatherTrend:
    def setup_method(self) -> None:
        self.agent = WeatherAnalysisAgent()

    def test_warming_trend(self) -> None:
        report = self.agent.analyze("测试城市", WARMING_TEMPS)
        assert report.trend == "warming"

    def test_cooling_trend(self) -> None:
        report = self.agent.analyze("测试城市", COOLING_TEMPS)
        assert report.trend == "cooling"

    def test_stable_trend(self) -> None:
        report = self.agent.analyze("测试城市", STABLE_TEMPS)
        assert report.trend == "stable"

    def test_trend_in_valid_values(self) -> None:
        for temps in [WARMING_TEMPS, COOLING_TEMPS, STABLE_TEMPS]:
            report = self.agent.analyze("城市", temps)
            assert report.trend in ("warming", "cooling", "stable")


# ── 输出完整性测试 ─────────────────────────────────────────────────────────────

class TestWeatherReportFields:
    def setup_method(self) -> None:
        self.agent = WeatherAnalysisAgent()
        self.report = self.agent.analyze("广州", WARMING_TEMPS, source="测试数据")

    def test_source_preserved(self) -> None:
        assert self.report.source == "测试数据"

    def test_updated_at_set(self) -> None:
        assert self.report.updated_at  # 非空
        assert len(self.report.updated_at) == 10  # ISO date: YYYY-MM-DD

    def test_disclaimer_present(self) -> None:
        assert self.report.disclaimer
        assert len(self.report.disclaimer) > 5

    def test_summary_contains_city(self) -> None:
        assert "广州" in self.report.summary

    def test_summary_contains_avg_temp(self) -> None:
        assert str(self.report.avg_temp_c) in self.report.summary


# ── Guardrail 机制测试（验证平台复用性）─────────────────────────────────────

class TestGuardrailIntegration:
    def setup_method(self) -> None:
        self.agent = WeatherAnalysisAgent()

    def test_keyword_blocker_fires_on_absolute_claims(self) -> None:
        """直接构造包含违禁词的输出，验证 KeywordBlocker 被触发。"""
        from agent_platform.core.harness import KeywordBlocker
        blocker = KeywordBlocker(keywords=["100%准确"])
        with pytest.raises(GuardrailViolation, match="KeywordBlocker"):
            blocker.validate_output({"summary": "这是100%准确的预测", "source": "test"})

    def test_schema_validator_rejects_missing_fields(self) -> None:
        """缺少必填字段时 JSONSchemaValidator 应拒绝。"""
        from agent_platform.core.harness import JSONSchemaValidator
        validator = JSONSchemaValidator(schema=WEATHER_REPORT_SCHEMA)
        with pytest.raises(GuardrailViolation, match="JSONSchemaValidator"):
            validator.validate_output({"city": "北京"})  # 缺少大量必填字段

    def test_source_attribution_filter_raises_on_missing_source(self) -> None:
        """缺少 source 时 SourceAttributionFilter 必须抛出 GuardrailViolation。"""
        from agent_platform.core.harness import SourceAttributionFilter, GuardrailViolation
        filt = SourceAttributionFilter(required=["source", "updated_at"])
        with pytest.raises(GuardrailViolation, match="必要字段"):
            filt.validate_output({"city": "北京", "source": "", "updated_at": ""})

    def test_normal_output_passes_all_guardrails(self) -> None:
        """正常输出应通过所有 Guardrail，不抛异常。"""
        report = self.agent.analyze("哈尔滨", [-10, -8, -5, -2, 0, 3, 6, 8, 10, 12])
        assert report.city == "哈尔滨"


# ── 边界条件测试 ──────────────────────────────────────────────────────────────

class TestEdgeCases:
    def setup_method(self) -> None:
        self.agent = WeatherAnalysisAgent()

    def test_raises_on_single_temp(self) -> None:
        with pytest.raises(ValueError, match="至少需要"):
            self.agent.analyze("北京", [25.0])

    def test_raises_on_empty_temps(self) -> None:
        with pytest.raises((ValueError, Exception)):
            self.agent.analyze("北京", [])

    def test_negative_temps_work(self) -> None:
        """极寒城市（负温）应正常分析。"""
        report = self.agent.analyze("哈尔滨", [-20, -18, -15, -10, -5, 0, 5, 8, 10, 12])
        assert report.min_temp_c < 0
        assert report.avg_temp_c < report.max_temp_c

    def test_tropical_temps_work(self) -> None:
        """热带高温应正常分析。"""
        report = self.agent.analyze("三亚", [28, 29, 30, 31, 32, 33, 32, 31, 30, 29])
        assert report.avg_temp_c >= 28

    def test_two_data_points_minimum(self) -> None:
        """最小2个数据点时应正常运行。"""
        report = self.agent.analyze("测试", [10.0, 20.0])
        assert report.period_days == 2
