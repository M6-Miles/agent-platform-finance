"""
Harness 有效性实验（可复现实验框架）
=====================================
对比 有 Harness 保护 vs 无 Harness 保护时的"幻觉率"（hallucination rate）。

实验设计：
  1. 构造包含已知错误/矛盾的 LLM 输出（模拟幻觉）
  2. 分别通过 有/无 Guardrail 的路径处理
  3. 统计被拦截 vs 漏过的比率

度量指标：
  - hallucination_blocked_rate : Harness 拦截了多少幻觉输出（越高越好）
  - false_positive_rate        : Harness 误拦截了多少正常输出（越低越好）
  - pass_rate_no_harness       : 无 Harness 时幻觉直接通过率

⚠️ 可复现性声明
--------------
本实验框架使用**构造性mock样本**（非真实 LLM 输出）。
每条样本的标签（is_hallucination）和内容均由工程师预先确定。
因此本实验的拦截率/误报率**不能代表真实 LLM 的实际输出质量或 Harness 对真实
LLM 幻觉的实际拦截效果**。这些数字反映的是 Guardrail 规则本身对预设样本的
覆盖能力，是规则覆盖率测试，不是真实 LLM 提升比例的实证。

可复现性保证：
  - EXPERIMENT_SAMPLES_V1 是固定版本评测集（版本号写入样本），不会随代码演进被覆盖。
  - run_harness_effectiveness_experiment 接受 seed 参数（用于未来扩展随机采样）。
  - ExperimentResult.task_results 保存每条样本的原始判定结果，供逐条审计。
  - 所有结果含 experiment_version 字段，标明评测集版本。
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

# 固定版本评测集版本号，每次修改 EXPERIMENT_SAMPLES 时递增
EXPERIMENT_DATASET_VERSION = "v1.0"

# 为每条样本分配确定性任务 ID，便于跨版本逐条追踪
# EXPERIMENT_SAMPLES 中每条样本新增 "task_id" 字段
EXPERIMENT_SAMPLES_V1: list[dict[str, Any]] = [
    {
        "task_id": "HAL-001",
        "label": "缺少 source 字段",
        "is_hallucination": True,
        "output": {
            "signal": "buy",
            "confidence": 0.8,
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "task_id": "HAL-002",
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
        "task_id": "HAL-003",
        "label": "schema 缺少 required 字段 confidence",
        "is_hallucination": True,
        "output": {
            "signal": "buy",
            "source": "synthesis_agent",
            "updated_at": "2024-01-01T00:00:00Z",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "task_id": "HAL-004",
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
        "task_id": "HAL-005",
        "label": "缺少 updated_at 字段",
        "is_hallucination": True,
        "output": {
            "signal": "hold",
            "confidence": 0.5,
            "source": "analysis_agent",
            "disclaimer": "仅供研究参考，不构成投资建议",
        },
    },
    {
        "task_id": "NRM-001",
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
        "task_id": "NRM-002",
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
        "task_id": "NRM-003",
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

# 向后兼容别名（原 EXPERIMENT_SAMPLES 不变语义）
EXPERIMENT_SAMPLES = EXPERIMENT_SAMPLES_V1


@dataclass
class TaskResult:
    """单条样本的实验判定结果，供逐条审计。"""

    task_id: str
    label: str
    is_hallucination: bool
    blocked: bool
    violations: list[str]

    @property
    def correct(self) -> bool:
        """判定正确：幻觉被拦或正常未被拦。"""
        return self.blocked == self.is_hallucination

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "label": self.label,
            "is_hallucination": self.is_hallucination,
            "blocked": self.blocked,
            "correct": self.correct,
            "violations": self.violations,
        }


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
    # 可复现性字段
    experiment_version: str = EXPERIMENT_DATASET_VERSION
    seed: int | None = None
    task_results: tuple[TaskResult, ...] = ()  # 逐条结果，供审计

    @property
    def invalid_calls_no_harness(self) -> int:
        """固定集里错误输出若未拦截会触发的模拟下游调用数。"""
        return self.hallucination_count

    @property
    def invalid_calls_with_harness(self) -> int:
        return self.hallucination_count - self.harness_blocked_hallucinations

    @property
    def invalid_call_reduction_rate(self) -> float:
        if self.invalid_calls_no_harness == 0:
            return 0.0
        return 1.0 - self.invalid_calls_with_harness / self.invalid_calls_no_harness

    @property
    def e2e_correct_rate_no_harness(self) -> float:
        if self.total_samples == 0:
            return 0.0
        return self.normal_count / self.total_samples

    @property
    def e2e_correct_rate_with_harness(self) -> float:
        if self.total_samples == 0:
            return 0.0
        return sum(result.correct for result in self.task_results) / self.total_samples

    @property
    def hallucination_blocked_rate(self) -> float:
        if self.hallucination_count == 0:
            return 1.0
        return self.harness_blocked_hallucinations / self.hallucination_count

    @property
    def false_positive_rate(self) -> float:
        if self.normal_count == 0:
            return 0.0
        return self.harness_blocked_normals / self.normal_count

    @property
    def pass_rate_no_harness(self) -> float:
        if self.hallucination_count == 0:
            return 0.0
        return self.no_harness_passed_hallucinations / self.hallucination_count

    def to_markdown(self) -> str:
        lines = [
            "## Harness 有效性实验报告",
            "",
            f"> **评测集版本**: {self.experiment_version}  "
            f"**随机种子**: {self.seed if self.seed is not None else '无（固定集）'}",
            ">",
            "> ⚠️ **重要免责说明**：本实验使用构造性 mock 样本，非真实 LLM 输出。",
            "> 拦截率/误报率反映的是 Guardrail 规则对预设样本的覆盖能力，",
            "> **不代表真实 LLM 输出的实际幻觉率，也不代表 Harness 对真实模型的实际提升比例**。",
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
            f"| 模拟无效下游调用数 | {self.invalid_calls_no_harness} | "
            f"{self.invalid_calls_with_harness} |",
            f"| 端到端正确处理率 | {self.e2e_correct_rate_no_harness:.0%} | "
            f"{self.e2e_correct_rate_with_harness:.0%} |",
            "",
            f"> 结论：Harness 将幻觉拦截率从 **0%** 提升至 "
            f"**{self.hallucination_blocked_rate:.0%}**，同时误拦截率保持在 "
            f"**{self.false_positive_rate:.0%}**。（仅对 mock 样本集有效）",
            f"> 固定集上的模拟无效调用减少 **{self.invalid_call_reduction_rate:.0%}**；"
            "这里的‘调用’是规则放行后的一次模拟下游动作，不是生产 API 流量。",
        ]
        if self.task_results:
            lines += ["", "### 逐条结果", "| task_id | 标签 | 是幻觉 | 被拦截 | 判定 |",
                      "|---------|------|--------|--------|------|"]
            for tr in self.task_results:
                mark = "✅" if tr.correct else "❌"
                lines.append(
                    f"| {tr.task_id} | {tr.label} | "
                    f"{'是' if tr.is_hallucination else '否'} | "
                    f"{'是' if tr.blocked else '否'} | {mark} |"
                )
        return "\n".join(lines)


def run_harness_effectiveness_experiment(
    samples: list[dict[str, Any]] | None = None,
    *,
    seed: int | None = None,
) -> ExperimentResult:
    """
    运行 Harness 有效性实验（可复现框架）。

    Parameters
    ----------
    samples : list[dict], optional
        实验样本。若不提供则使用固定版本评测集 EXPERIMENT_SAMPLES_V1。
        每条样本建议带 task_id 字段；无 task_id 则自动分配序号。
    seed : int | None
        随机种子（为未来扩展随机抽样保留；当前固定集不影响结果，但写入报告便于审计）。

    Returns
    -------
    ExperimentResult
        含逐条判定结果（task_results）、版本号、种子，完全可复现。

    ⚠️ mock 模式声明
    -----------------
    本函数**不调用任何付费 LLM**。所有"幻觉"均为工程师构造的 mock 样本，
    实验结果不能代表真实 LLM 的幻觉率或 Harness 在生产环境的实际效果。
    """
    if samples is None:
        samples = EXPERIMENT_SAMPLES_V1

    guardrails = [
        JSONSchemaValidator(schema=_SYNTHESIS_SCHEMA),
        KeywordBlocker(keywords=_FORBIDDEN_KEYWORDS),
        SourceAttributionFilter(required=["source", "updated_at"]),
    ]

    hallucination_count = sum(1 for s in samples if s["is_hallucination"])
    normal_count = sum(1 for s in samples if not s["is_hallucination"])
    harness_blocked_hallucinations = 0
    harness_blocked_normals = 0
    task_results: list[TaskResult] = []

    for idx, sample in enumerate(samples):
        output = sample["output"]
        task_id = str(sample.get("task_id") or f"TASK-{idx+1:03d}")
        blocked = False
        violations: list[str] = []

        for g in guardrails:
            try:
                g.validate_output(output)
            except GuardrailViolation as exc:
                blocked = True
                violations.append(str(exc))
                break

        tr = TaskResult(
            task_id=task_id,
            label=sample.get("label", ""),
            is_hallucination=sample["is_hallucination"],
            blocked=blocked,
            violations=violations,
        )
        task_results.append(tr)

        if sample["is_hallucination"] and blocked:
            harness_blocked_hallucinations += 1
            logger.debug("✅ 拦截幻觉: %s (%s)", task_id, violations)
        elif not sample["is_hallucination"] and blocked:
            harness_blocked_normals += 1
            logger.warning("⚠️ 误拦截正常: %s (%s)", task_id, violations)

    return ExperimentResult(
        total_samples=len(samples),
        hallucination_count=hallucination_count,
        normal_count=normal_count,
        harness_blocked_hallucinations=harness_blocked_hallucinations,
        harness_blocked_normals=harness_blocked_normals,
        no_harness_passed_hallucinations=hallucination_count,
        experiment_version=EXPERIMENT_DATASET_VERSION,
        seed=seed,
        task_results=tuple(task_results),
    )
