"""tests/test_harness.py — AgentHarness SDK 单元测试"""
from __future__ import annotations

import pytest

from agent_platform.core.harness import (
    AgentHarness,
    CircuitBreaker,
    CrossValidator,
    GuardrailViolation,
    JSONSchemaValidator,
    KeywordBlocker,
    RateLimiter,
    SourceAttributionFilter,
)


# ─── 测试用 Schema ────────────────────────────────────────────────────────────
SIMPLE_SCHEMA = {
    "type": "object",
    "required": ["symbol", "source", "updated_at"],
    "properties": {
        "symbol": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "value": {"type": "number"},
    },
}

GOOD_OUTPUT = {"symbol": "600519", "source": "akshare", "updated_at": "2026-07-31T00:00:00Z", "value": 1800.0}
BAD_OUTPUT = {"symbol": "600519"}   # 缺少 source / updated_at


# ─── JSONSchemaValidator ─────────────────────────────────────────────────────

class TestJSONSchemaValidator:
    def test_valid_output_passes(self) -> None:
        v = JSONSchemaValidator(SIMPLE_SCHEMA)
        assert v.validate_output(GOOD_OUTPUT) == GOOD_OUTPUT

    def test_missing_fields_raises(self) -> None:
        v = JSONSchemaValidator(SIMPLE_SCHEMA)
        with pytest.raises(GuardrailViolation, match="Schema 校验失败"):
            v.validate_output(BAD_OUTPUT)

    def test_non_dict_skipped(self) -> None:
        v = JSONSchemaValidator(SIMPLE_SCHEMA)
        assert v.validate_output("纯文本") == "纯文本"

    def test_check_input_is_noop(self) -> None:
        v = JSONSchemaValidator(SIMPLE_SCHEMA)
        v.check_input("任意输入")   # 不应抛出


# ─── SourceAttributionFilter ─────────────────────────────────────────────────

class TestSourceAttributionFilter:
    def test_complete_output_passes(self) -> None:
        f = SourceAttributionFilter()
        out = f.validate_output(GOOD_OUTPUT)
        assert "_source_warning" not in out

    def test_missing_source_raises(self) -> None:
        f = SourceAttributionFilter()
        with pytest.raises(GuardrailViolation, match="必要字段"):
            f.validate_output({"symbol": "600519", "updated_at": "2026-07-31"})

    def test_non_dict_skipped(self) -> None:
        f = SourceAttributionFilter()
        assert f.validate_output("文本") == "文本"


# ─── RateLimiter ──────────────────────────────────────────────────────────────

class TestRateLimiter:
    def test_within_limit_passes(self) -> None:
        rl = RateLimiter(max_calls_per_minute=5)
        for _ in range(5):
            rl.check_input("task")   # 不应抛出

    def test_exceeds_limit_raises(self) -> None:
        rl = RateLimiter(max_calls_per_minute=3)
        for _ in range(3):
            rl.check_input("task")
        with pytest.raises(GuardrailViolation, match="超过速率限制"):
            rl.check_input("task")

    def test_validate_output_is_passthrough(self) -> None:
        rl = RateLimiter()
        assert rl.validate_output(GOOD_OUTPUT) is GOOD_OUTPUT


# ─── KeywordBlocker ──────────────────────────────────────────────────────────

class TestKeywordBlocker:
    def test_clean_text_passes(self) -> None:
        kb = KeywordBlocker()
        assert kb.validate_output("这只股票可能有上涨空间，注意风险") == "这只股票可能有上涨空间，注意风险"

    def test_blocked_keyword_raises(self) -> None:
        kb = KeywordBlocker()
        with pytest.raises(GuardrailViolation, match="违规词汇"):
            kb.validate_output("买入，绝对稳赚！")

    def test_custom_keywords(self) -> None:
        kb = KeywordBlocker(["禁词A", "禁词B"])
        with pytest.raises(GuardrailViolation):
            kb.validate_output("包含禁词A的文本")
        assert kb.validate_output("干净文本") == "干净文本"


# ─── CrossValidator ──────────────────────────────────────────────────────────

class TestCrossValidator:
    def test_within_tolerance_passes(self) -> None:
        cv = CrossValidator(fields_to_check=["latest_rsi"], tolerance=0.01)
        cv.set_ground_truth({"latest_rsi": 65.0})
        out = {"latest_rsi": 65.3}   # 偏差 0.46% < 1%
        assert cv.validate_output(out) == out

    def test_exceeds_tolerance_raises(self) -> None:
        cv = CrossValidator(fields_to_check=["latest_rsi"], tolerance=0.01)
        cv.set_ground_truth({"latest_rsi": 65.0})
        with pytest.raises(GuardrailViolation, match="偏差超过"):
            cv.validate_output({"latest_rsi": 70.0})   # 偏差 7.7%

    def test_no_ground_truth_skips(self) -> None:
        cv = CrossValidator()
        out = {"latest_rsi": 50.0}
        assert cv.validate_output(out) is out

    def test_non_dict_skips(self) -> None:
        cv = CrossValidator()
        cv.set_ground_truth({"latest_rsi": 50.0})
        assert cv.validate_output("文本") == "文本"


# ─── CircuitBreaker ──────────────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_open_after_max_failures(self) -> None:
        cb = CircuitBreaker(max_failures=2, cooldown_s=999)
        assert not cb.is_open()
        cb.record_failure()
        assert not cb.is_open()
        cb.record_failure()
        assert cb.is_open()

    def test_success_resets_counter(self) -> None:
        cb = CircuitBreaker(max_failures=3, cooldown_s=999)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()   # 重置后只失败1次，不应打开
        assert not cb.is_open()


# ─── AgentHarness 集成测试 ────────────────────────────────────────────────────

class TestAgentHarness:
    def _make_harness(self, agent, guardrails=None, max_retries=0):
        return AgentHarness(agent=agent, guardrails=guardrails or [], max_retries=max_retries)

    def test_successful_run(self) -> None:
        harness = self._make_harness(lambda task: GOOD_OUTPUT)
        result = harness.run("分析600519")
        assert result == GOOD_OUTPUT
        assert harness.get_stats()["success_rate"] == 1.0

    def test_guardrail_violation_raises(self) -> None:
        harness = self._make_harness(
            lambda task: BAD_OUTPUT,
            guardrails=[JSONSchemaValidator(SIMPLE_SCHEMA)],
        )
        with pytest.raises(GuardrailViolation):
            harness.run("任务")

    def test_retry_on_violation(self) -> None:
        """第一次违规，第二次成功。"""
        calls = {"n": 0}

        def flaky_agent(task):
            calls["n"] += 1
            return BAD_OUTPUT if calls["n"] == 1 else GOOD_OUTPUT

        harness = self._make_harness(
            flaky_agent,
            guardrails=[JSONSchemaValidator(SIMPLE_SCHEMA)],
            max_retries=1,
        )
        result = harness.run("任务")
        assert result == GOOD_OUTPUT
        assert calls["n"] == 2

    def test_get_stats_after_runs(self) -> None:
        harness = self._make_harness(lambda task: GOOD_OUTPUT)
        harness.run("1")
        harness.run("2")
        stats = harness.get_stats()
        assert stats["total"] == 2
        assert stats["success"] == 2

    def test_agent_with_run_method(self) -> None:
        class MyAgent:
            def run(self, task):
                return GOOD_OUTPUT

        harness = self._make_harness(MyAgent())
        assert harness.run("x") == GOOD_OUTPUT

    def test_invalid_agent_raises_type_error(self) -> None:
        harness = self._make_harness("not_callable")
        with pytest.raises(TypeError):
            harness.run("x")
