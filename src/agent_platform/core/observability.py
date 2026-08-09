"""
可观测性面板（Observability Panel）
=====================================
收集 AgentHarness / LangGraph 工作流的运行指标：
  - Token 消耗（输入/输出）
  - 延迟（P50/P95）
  - 失败率与 Guardrail 触发率
  - CircuitBreaker 状态
"""
from __future__ import annotations

import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentCallRecord:
    """单次 Agent 调用记录。"""
    agent_name: str
    task: str
    started_at: float
    duration_s: float
    success: bool
    input_tokens: int
    output_tokens: int
    guardrail_violations: list[str]
    retries: int


@dataclass
class ObservabilityPanel:
    """
    可观测性面板：汇总所有 Agent 调用指标。
    使用方法：
        panel = ObservabilityPanel()
        with panel.record("technical_agent", task="analyze") as ctx:
            result = agent.run(task)
            ctx.set_tokens(input=500, output=200)
    """

    _records: list[AgentCallRecord] = field(default_factory=list)

    def record_call(
        self,
        agent_name: str,
        task: str,
        duration_s: float,
        success: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        guardrail_violations: list[str] | None = None,
        retries: int = 0,
    ) -> None:
        self._records.append(AgentCallRecord(
            agent_name=agent_name,
            task=task,
            started_at=time.time(),
            duration_s=duration_s,
            success=success,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            guardrail_violations=guardrail_violations or [],
            retries=retries,
        ))

    def get_summary(self) -> dict[str, Any]:
        """返回汇总统计字典。"""
        if not self._records:
            return {
                "total_calls": 0,
                "success_rate_pct": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "latency_p50_s": 0.0,
                "latency_p95_s": 0.0,
                "guardrail_violation_count": 0,
                "avg_retries": 0.0,
                "per_agent": {},
            }

        durations = [r.duration_s for r in self._records]
        durations_sorted = sorted(durations)
        n = len(durations_sorted)

        def percentile(data: list[float], p: float) -> float:
            idx = max(0, int(math.ceil(p / 100.0 * n)) - 1)
            return data[min(idx, len(data) - 1)]

        import math
        success_count = sum(1 for r in self._records if r.success)
        total_violations = sum(len(r.guardrail_violations) for r in self._records)

        # Per-agent breakdown
        per_agent: dict[str, Any] = defaultdict(lambda: {"calls": 0, "successes": 0, "total_duration": 0.0, "total_input_tokens": 0, "total_output_tokens": 0})
        for r in self._records:
            per_agent[r.agent_name]["calls"] += 1
            per_agent[r.agent_name]["successes"] += int(r.success)
            per_agent[r.agent_name]["total_duration"] += r.duration_s
            per_agent[r.agent_name]["total_input_tokens"] += r.input_tokens
            per_agent[r.agent_name]["total_output_tokens"] += r.output_tokens

        return {
            "total_calls": len(self._records),
            "success_rate_pct": round(success_count / len(self._records) * 100, 1),
            "total_input_tokens": sum(r.input_tokens for r in self._records),
            "total_output_tokens": sum(r.output_tokens for r in self._records),
            "latency_p50_s": round(percentile(durations_sorted, 50), 3),
            "latency_p95_s": round(percentile(durations_sorted, 95), 3),
            "guardrail_violation_count": total_violations,
            "avg_retries": round(sum(r.retries for r in self._records) / len(self._records), 2),
            "per_agent": {
                name: {
                    "calls": v["calls"],
                    "success_rate_pct": round(v["successes"] / v["calls"] * 100, 1) if v["calls"] > 0 else 0.0,
                    "avg_duration_s": round(v["total_duration"] / v["calls"], 3) if v["calls"] > 0 else 0.0,
                    "total_input_tokens": v["total_input_tokens"],
                    "total_output_tokens": v["total_output_tokens"],
                }
                for name, v in per_agent.items()
            },
        }

    def to_markdown(self) -> str:
        s = self.get_summary()
        lines = [
            "## 可观测性面板",
            f"- 总调用次数：{s['total_calls']}",
            f"- 成功率：{s['success_rate_pct']}%",
            f"- 总输入 Token：{s['total_input_tokens']:,}",
            f"- 总输出 Token：{s['total_output_tokens']:,}",
            f"- 延迟 P50：{s['latency_p50_s']}s  P95：{s['latency_p95_s']}s",
            f"- Guardrail 触发次数：{s['guardrail_violation_count']}",
            f"- 平均重试次数：{s['avg_retries']}",
            "",
            "### 各 Agent 详情",
        ]
        for agent, v in s["per_agent"].items():
            lines.append(
                f"- **{agent}**：{v['calls']} 次，成功率 {v['success_rate_pct']}%，"
                f"均延迟 {v['avg_duration_s']}s，输入 {v['total_input_tokens']} tok"
            )
        return "\n".join(lines)

    def reset(self) -> None:
        self._records.clear()
