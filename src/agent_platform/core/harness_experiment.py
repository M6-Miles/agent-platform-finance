"""
Harness 有效性实验
==================
对比 有 Harness 保护 vs 无 Harness 保护时的"幻觉率"（hallucination rate）。

实验设计：
  1. 构造包含已知错误/矛盾的 LLM 输出（模拟幻觉）
  2. 分别通过 有/无 Guardrail 的路径处理
  3. 统计被拦截 vs 漏过的比率

度量指标：
  - hallucination_blocked_rate : Harness 拦截了多少幻觉输出（越高越好）
  - false_positive_rate        : Harness 误拦截了多少正常输出（越低越好）
  - pass_rate_no_harness       : 无 Harness 时幻觉直接通过率
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent_platform.core.harness import (
    GuardrailViolation,
    JSONSchemaValidator,
    KeywordBlocker,
    SourceAttributionFilter,
)

logger = logging.getLogger(__name__)

# ── 实验数据集：已知幻觉 & 正常输出 ─────────────────────────────────────────

# 每条样本：{"output": {...}, "is_hallucination": bool, "label": str}
EXPERIMENT_SAMPLES: list[dict[str, Any]] = [
    # ──── 幻觉样本（应该被拦截）────────────────────────────────────────────
    {
        "label": "缺少 source 字段",
        "is_hallucination": True,
        "output": {
            "signal": "buy",
            "confidence": 0.8,
            # "source" 字段故意缺失
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "label": "包含违禁词 '绝对稳赚'",
        "is_hallucination": True,
        "output": {
            "signal": "buy",
            "confidence": 0.9,
            "summary": "该股绝对稳赚，100%收益",
            "source": "analysis_agent",
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "label": "schema 缺少 required 字段 confidence",
        "is_hallucination": True,
        "output": {
            "signal": "buy",
            # confidence 缺失
            "source": "synthesis_agent",
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "label": "违禁词 '稳赚不赔'",
        "is_hallucination": True,
        "output": {
            "signal": "sell",
            "confidence": 0.3,
            "notes": "稳赚不赔策略已启动",
            "source": "trader_agent",
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "label": "缺少 updated_at 字段",
        "is_hallucination": True,
        "output": {
            "signal": "hold",
            "confidence": 0.5,
            "source": "analysis_agent",
            # updated_at 缺失
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    # ──── 正常样本（不应被拦截）────────────────────────────────────────────
    {
        "label": "正常 buy 信号",
        "is_hallucination": False,
        "output": {
            "signal": "buy",
            "confidence": 0.75,
            "source": "synthesis_agent",
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "label": "正常 sell 信号",
        "is_hallucination": False,
        "output": {
            "signal": "sell",
            "confidence": 0.35,
            "source": "synthesis_agent",
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "label": "正常 hold 信号",
        "is_hallucination": False,
        "output": {
            "signal": "hold",
            "confidence": 0.5,
            "source": "synthesis_agent",
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
]

# Schema（只验证最重要的必填字段）
_SYNTHESIS_SCHEMA = {
    "type": "object",
    "required": ["signal", "confidence", "source", "updated_at"],
    "properties": {
        "signal": {"type": "string", "enum": ["buy", "sell", "hold"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
    },
}

_FORBIDDEN_KEYWORDS = ["绝对稳赚", "100%收益", "稳赚不赔", "零风险", "必涨", "必赢"]


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    total_samples: int
    hallucination_count: int
    normal_count: int
    # Harness ON
    harness_blocked_hallucinations: int
    harness_blocked_normals: int
    # Harness OFF（无保护，全部通过）
    no_harness_passed_hallucinations: int  # 等于 hallucination_count

    @property
    def hallucination_blocked_rate(self) -> float:
        """Harness 拦截幻觉的比率（召回率，越高越好）。"""
        if self.hallucination_count == 0:
            return 1.0
        return self.harness_blocked_hallucinations / self.hallucination_count

    @property
    def false_positive_rate(self) -> float:
        """Harness 误拦截正常输出的比率（假正例率，越低越好）。"""
        if self.normal_count == 0:
            return 0.0
        return self.harness_blocked_normals / self.normal_count

    @property
    def pass_rate_no_harness(self) -> float:
        """无 Harness 时幻觉直接通过率（= 1.0，全漏）。"""
        if self.hallucination_count == 0:
            return 0.0
        return self.no_harness_passed_hallucinations / self.hallucination_count

    def to_markdown(self) -> str:
        return "\n".join([
            "## Harness 有效性实验报告",
            "",
            "| 指标 | 无 Harness | 有 Harness |",
            "|------|-----------|-----------|",
            f"| 样本总数 | {self.total_samples} | {self.total_samples} |",
            f"| 幻觉样本数 | {self.hallucination_count} | {self.hallucination_count} |",
            f"| 幻觉被拦截 | 0 | {self.harness_blocked_hallucinations} |",
            f"| 幻觉通过率 | {self.pass_rate_no_harness:.0%} ❌ | "
            f"{1-self.hallucination_blocked_rate:.0%} {'✅' if self.hallucination_blocked_rate >= 0.8 else '⚠️'} |",
            f"| 幻觉拦截率 | 0% | {self.hallucination_blocked_rate:.0%} |",
            f"| 正常误拦截率 | 0% | {self.false_positive_rate:.0%} |",
            "",
            f"> 结论：Harness 将幻觉拦截率从 **0%** 提升至 "
            f"**{self.hallucination_blocked_rate:.0%}**，同时误拦截率保持在 "
            f"**{self.false_positive_rate:.0%}**。",
        ])


def run_harness_effectiveness_experiment(
    samples: list[dict[str, Any]] | None = None,
) -> ExperimentResult:
    """
    运行 Harness 有效性实验。

    Parameters
    ----------
    samples : list[dict], optional
        实验样本。若不提供则使用内置 EXPERIMENT_SAMPLES。

    Returns
    -------
    ExperimentResult
    """
    if samples is None:
        samples = EXPERIMENT_SAMPLES

    # 构建 Guardrail 列表（直接使用 validate_output，不需要 agent）
    guardrails = [
        JSONSchemaValidator(schema=_SYNTHESIS_SCHEMA),
        KeywordBlocker(keywords=_FORBIDDEN_KEYWORDS),
        SourceAttributionFilter(required=["source", "updated_at"]),
    ]

    hallucination_count = sum(1 for s in samples if s["is_hallucination"])
    normal_count = sum(1 for s in samples if not s["is_hallucination"])
    harness_blocked_hallucinations = 0
    harness_blocked_normals = 0

    for sample in samples:
        output = sample["output"]
        blocked = False
        violations: list[str] = []

        for g in guardrails:
            try:
                g.validate_output(output)
            except GuardrailViolation as exc:
                blocked = True
                violations.append(str(exc))
                break  # 一个 Guardrail 拦截即停止

        if sample["is_hallucination"] and blocked:
            harness_blocked_hallucinations += 1
            logger.debug("✅ 拦截幻觉: %s (%s)", sample["label"], violations)
        elif not sample["is_hallucination"] and blocked:
            harness_blocked_normals += 1
            logger.warning("⚠️ 误拦截正常: %s (%s)", sample["label"], violations)

    return ExperimentResult(
        total_samples=len(samples),
        hallucination_count=hallucination_count,
        normal_count=normal_count,
        harness_blocked_hallucinations=harness_blocked_hallucinations,
        harness_blocked_normals=harness_blocked_normals,
        no_harness_passed_hallucinations=hallucination_count,  # 全部通过
    )
