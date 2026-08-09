"""
AgentHarness SDK — Harness Engineering 核心实现
================================================
Agent = Model + Harness

Harness 是所有 Agent 的免疫系统：
  1. Pre-flight  ：执行前校验输入
  2. Run Loop    ：驱动 AgentRuntime
  3. Post-flight ：验证输出合法性
  4. Observability：记录追踪、Token 计量

内置 5 个 Guardrail 插件：
  - JSONSchemaValidator    : 输出结构校验
  - SourceAttributionFilter: 数据来源完整性
  - RateLimiter            : API 调用速率限制
  - KeywordBlocker         : 违规词过滤
  - CrossValidator         : 代码交叉验证（防幻觉）
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 异常体系
# ─────────────────────────────────────────────────────────────────────────────

class GuardrailViolation(Exception):
    """Guardrail 校验失败。"""
    def __init__(self, rule_name: str, detail: str = "") -> None:
        self.rule_name = rule_name
        self.detail = detail
        super().__init__(f"[{rule_name}] {detail}")


class HumanApprovalRequired(GuardrailViolation):
    """需要人工审批才能继续（高风险操作）。"""


class CircuitBreakerOpen(Exception):
    """熔断器打开，拒绝执行。"""


# ─────────────────────────────────────────────────────────────────────────────
# Guardrail 基类
# ─────────────────────────────────────────────────────────────────────────────

class Guardrail(ABC):
    """所有 Guardrail 的抽象基类。"""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    def check_input(self, task: Any) -> None:
        """前置检查。违规时抛出 GuardrailViolation。默认放行。"""

    @abstractmethod
    def validate_output(self, output: Any) -> Any:
        """后置验证。返回（可能修改过的）output；违规时抛出 GuardrailViolation。"""


# ─────────────────────────────────────────────────────────────────────────────
# 1. JSONSchemaValidator
# ─────────────────────────────────────────────────────────────────────────────

class JSONSchemaValidator(Guardrail):
    """确保 Agent 输出符合预定义 JSON Schema。"""

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = schema

    def validate_output(self, output: Any) -> Any:
        if not isinstance(output, dict):
            return output  # 非字典输出跳过（如纯文本）

        errors: list[str] = []
        try:
            import jsonschema
            validator = jsonschema.Draft7Validator(self._schema)
            errors = [e.message for e in validator.iter_errors(output)]
        except ImportError:
            # jsonschema 未安装时退化为必填字段检查
            for f in self._schema.get("required", []):
                if f not in output:
                    errors.append(f"缺少必填字段: {f}")

        if errors:
            raise GuardrailViolation(
                self.name,
                f"Schema 校验失败（{len(errors)} 个错误）: {'; '.join(errors[:3])}",
            )
        return output


# ─────────────────────────────────────────────────────────────────────────────
# 2. SourceAttributionFilter
# ─────────────────────────────────────────────────────────────────────────────

class SourceAttributionFilter(Guardrail):
    """
    过滤掉缺少 source / updated_at 字段的数据引用。
    任何必填溯源字段缺失时抛出 GuardrailViolation，阻断输出流。
    """

    def __init__(self, required: Sequence[str] = ("source", "updated_at")) -> None:
        self._required = list(required)

    def validate_output(self, output: Any) -> Any:
        if not isinstance(output, dict):
            return output

        missing = [f for f in self._required if not output.get(f)]
        if missing:
            raise GuardrailViolation(
                self.name,
                f"输出缺少必要字段: {missing}",
            )
        return output


# ─────────────────────────────────────────────────────────────────────────────
# 3. RateLimiter
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter(Guardrail):
    """限制 Agent 每分钟最多调用 API N 次（滑动窗口）。"""

    def __init__(self, max_calls_per_minute: int = 20) -> None:
        self._max = max_calls_per_minute
        self._window: deque[float] = deque()

    def check_input(self, task: Any) -> None:
        now = time.monotonic()
        # 清除60秒之前的记录
        while self._window and now - self._window[0] > 60.0:
            self._window.popleft()
        if len(self._window) >= self._max:
            wait = 60.0 - (now - self._window[0])
            raise GuardrailViolation(
                self.name,
                f"超过速率限制（{self._max} 次/分钟），请等待 {wait:.1f}s",
            )
        self._window.append(now)

    def validate_output(self, output: Any) -> Any:
        return output


# ─────────────────────────────────────────────────────────────────────────────
# 4. KeywordBlocker
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_BLOCKED_KEYWORDS = [
    "绝对稳赚", "100%收益", "稳赚不赔", "一定涨停", "肯定上涨",
    "必涨", "稳赚", "包赚", "无风险套利", "保证盈利",
    "内幕消息", "坐庄", "庄家", "操盘",
]


class KeywordBlocker(Guardrail):
    """拒绝包含违规词汇的输出，要求 Agent 重新生成。"""

    def __init__(self, keywords: Sequence[str] | None = None) -> None:
        self._keywords = list(keywords or _DEFAULT_BLOCKED_KEYWORDS)

    def validate_output(self, output: Any) -> Any:
        text = output if isinstance(output, str) else str(output)
        for kw in self._keywords:
            if kw in text:
                raise GuardrailViolation(
                    self.name,
                    f"输出包含违规词汇「{kw}」，已阻断",
                )
        return output


# ─────────────────────────────────────────────────────────────────────────────
# 5. CrossValidator
# ─────────────────────────────────────────────────────────────────────────────

class CrossValidator(Guardrail):
    """
    用确定性代码（pandas）交叉验证 Agent 输出中的数值指标。
    防止 LLM 编造技术指标数值（幻觉）。
    """

    def __init__(
        self,
        fields_to_check: Sequence[str] = ("latest_rsi", "latest_macd", "latest_ma5", "latest_ma20"),
        tolerance: float = 0.01,
    ) -> None:
        self._fields = list(fields_to_check)
        self._tolerance = tolerance
        self._ground_truth: dict[str, float] = {}

    def set_ground_truth(self, values: dict[str, float]) -> None:
        """设置代码计算的基准值（由 Harness 在 pre-flight 阶段计算）。"""
        self._ground_truth = values

    def validate_output(self, output: Any) -> Any:
        if not isinstance(output, dict) or not self._ground_truth:
            return output

        mismatches: list[str] = []
        for field_name in self._fields:
            if field_name not in output or field_name not in self._ground_truth:
                continue
            llm_val = float(output[field_name])
            true_val = self._ground_truth[field_name]
            if true_val != 0 and abs(llm_val - true_val) / abs(true_val) > self._tolerance:
                mismatches.append(
                    f"{field_name}: LLM={llm_val:.4f} vs 代码={true_val:.4f}"
                )

        if mismatches:
            raise GuardrailViolation(
                self.name,
                f"指标数值与代码计算偏差超过 {self._tolerance*100:.0f}%: {'; '.join(mismatches)}",
            )
        return output


# ─────────────────────────────────────────────────────────────────────────────
# 熔断器
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CircuitBreaker:
    """连续失败 N 次后打开熔断器，冷却期结束后自动复位。"""

    max_failures: int = 3
    cooldown_s: float = 300.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at > self.cooldown_s:
            self._failures = 0
            self._opened_at = None
            logger.info("[CircuitBreaker] 冷却完毕，熔断器复位")
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.max_failures:
            self._opened_at = time.monotonic()
            logger.error("[CircuitBreaker] 连续失败 %d 次，熔断器打开", self._failures)


# ─────────────────────────────────────────────────────────────────────────────
# 可观测性：TraceRecord
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TraceRecord:
    task: str
    started_at: float
    finished_at: float | None = None
    success: bool = False
    guardrail_violations: list[str] = field(default_factory=list)
    retries: int = 0

    @property
    def duration_s(self) -> float:
        if self.finished_at is None:
            return time.monotonic() - self.started_at
        return self.finished_at - self.started_at


# ─────────────────────────────────────────────────────────────────────────────
# AgentHarness — 主类
# ─────────────────────────────────────────────────────────────────────────────

class AgentHarness:
    """
    所有 Agent 的统一容器和免疫系统。

    用法::

        harness = AgentHarness(
            agent=my_agent,
            guardrails=[
                JSONSchemaValidator(MY_SCHEMA),
                SourceAttributionFilter(),
                RateLimiter(max_calls_per_minute=20),
                KeywordBlocker(),
                CrossValidator(),
            ],
            max_retries=2,
        )
        result = harness.run("分析 600519")
    """

    def __init__(
        self,
        agent: Any,
        guardrails: Sequence[Guardrail] | None = None,
        max_retries: int = 2,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.agent = agent
        self.guardrails: list[Guardrail] = list(guardrails or [])
        self.max_retries = max_retries
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.traces: list[TraceRecord] = []

    def run(self, task: Any) -> Any:
        """
        执行 Harness 完整生命周期：
          Pre-flight → Loop → Post-flight → Observability
        """
        if self.circuit_breaker.is_open():
            raise CircuitBreakerOpen("熔断器打开，请稍后重试")

        trace = TraceRecord(task=str(task), started_at=time.monotonic())
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            trace.retries = attempt
            try:
                # 1. Pre-flight 检查
                for g in self.guardrails:
                    g.check_input(task)

                # 2. 运行 Agent Loop
                result = self._run_agent(task)

                # 3. Post-flight 验证
                for g in self.guardrails:
                    result = g.validate_output(result)

                # 4. 记录成功
                trace.success = True
                trace.finished_at = time.monotonic()
                self.traces.append(trace)
                self.circuit_breaker.record_success()
                logger.info(
                    "[Harness] 成功 | 耗时=%.2fs | 重试=%d | task=%s",
                    trace.duration_s, attempt, str(task)[:60],
                )
                return result

            except GuardrailViolation as exc:
                trace.guardrail_violations.append(str(exc))
                logger.warning("[Harness] Guardrail 违规 (第%d次): %s", attempt + 1, exc)
                last_exc = exc
                if isinstance(exc, HumanApprovalRequired):
                    break  # 人工审批不重试

            except Exception as exc:
                logger.error("[Harness] Agent 执行异常 (第%d次): %s", attempt + 1, exc)
                last_exc = exc

        # 所有重试耗尽
        trace.finished_at = time.monotonic()
        self.traces.append(trace)
        self.circuit_breaker.record_failure()
        raise last_exc or RuntimeError("Harness 执行失败，重试已耗尽")

    def _run_agent(self, task: Any) -> Any:
        """调用底层 Agent。支持 callable 和具有 run() 方法的对象。"""
        if callable(self.agent):
            return self.agent(task)
        if hasattr(self.agent, "run"):
            return self.agent.run(task)
        raise TypeError(f"agent 必须是 callable 或具有 run() 方法，实际类型: {type(self.agent)}")

    def get_stats(self) -> dict[str, Any]:
        """返回可观测统计数据。"""
        total = len(self.traces)
        if total == 0:
            return {"total": 0, "success_rate": 0.0, "avg_duration_s": 0.0}
        success = sum(1 for t in self.traces if t.success)
        avg_dur = sum(t.duration_s for t in self.traces) / total
        total_retries = sum(t.retries for t in self.traces)
        violations: dict[str, int] = {}
        for t in self.traces:
            for v in t.guardrail_violations:
                key = v.split("]")[0].strip("[") if "]" in v else "unknown"
                violations[key] = violations.get(key, 0) + 1
        return {
            "total": total,
            "success": success,
            "success_rate": round(success / total, 3),
            "avg_duration_s": round(avg_dur, 3),
            "total_retries": total_retries,
            "guardrail_violations": violations,
            "circuit_breaker_open": self.circuit_breaker.is_open(),
        }
