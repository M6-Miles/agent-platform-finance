"""
Evaluator Agent
================
独立质量评分器：对 SynthesisResult / TraderResult 的输出进行无参考评分，
检测可能的幻觉、逻辑矛盾、数据缺失。
输出：EvaluationResult（含维度分数 + 总分）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

EVALUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["subject", "overall_score", "dimensions", "source", "updated_at"],
    "properties": {
        "subject": {"type": "string"},
        "overall_score": {"type": "number", "minimum": 0, "maximum": 100},
        "dimensions": {"type": "object"},
        "issues": {"type": "array"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
    },
}


@dataclass(frozen=True, slots=True)
class EvaluationDimension:
    name: str
    score: float          # 0–100
    weight: float         # 权重
    notes: str


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    subject: str          # 被评估的 Agent 名称
    overall_score: float  # 加权总分 0–100
    dimensions: list[EvaluationDimension]
    issues: list[str]     # 发现的问题列表
    source: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "overall_score": self.overall_score,
            "dimensions": {
                d.name: {"score": d.score, "weight": d.weight, "notes": d.notes}
                for d in self.dimensions
            },
            "issues": list(self.issues),
            "source": self.source,
            "updated_at": self.updated_at,
        }

    def to_markdown(self) -> str:
        score_icon = "✅" if self.overall_score >= 80 else ("⚠️" if self.overall_score >= 60 else "❌")
        dim_lines = [
            f"  - {d.name}（权重{d.weight:.0%}）：{d.score:.0f}/100  {d.notes}"
            for d in self.dimensions
        ]
        issue_lines = [f"  - ⚠️ {i}" for i in self.issues] or ["  （无问题）"]
        return "\n".join([
            f"### Evaluator 评分 — {self.subject}",
            f"- 综合评分：{self.overall_score:.0f}/100  {score_icon}",
            "",
            "**维度评分**",
            *dim_lines,
            "",
            "**发现的问题**",
            *issue_lines,
        ])


# ── 评分规则 ─────────────────────────────────────────────────────────────────

def _score_data_completeness(output: dict[str, Any]) -> tuple[float, list[str]]:
    """数据完整性：source / updated_at / disclaimer 必须存在。"""
    issues: list[str] = []
    score = 100.0
    for field in ["source", "updated_at", "disclaimer"]:
        if not output.get(field):
            issues.append(f"缺少必填字段 {field}")
            score -= 30.0
    return max(0.0, score), issues


def _score_logical_consistency(output: dict[str, Any], subject: str) -> tuple[float, list[str]]:
    """逻辑一致性检查（Synthesis / Trader）。"""
    issues: list[str] = []
    score = 100.0

    if subject == "synthesis":
        conf = output.get("confidence", -1)
        signal = output.get("signal", "hold")
        if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
            issues.append(f"confidence={conf} 超出 [0,1] 范围")
            score -= 40.0
        if signal not in {"buy", "sell", "hold"}:
            issues.append(f"signal={signal!r} 不在允许范围")
            score -= 40.0

    elif subject == "trader":
        pos = output.get("position_pct_suggestion", -1)
        sig = output.get("signal", "hold")
        if not (0 <= pos <= 100):
            issues.append(f"position_pct_suggestion={pos} 超出 [0,100]")
            score -= 40.0
        if sig == "buy" and pos <= 0:
            issues.append("signal=buy 但仓位建议为 0%，逻辑可疑")
            score -= 20.0

    elif subject == "risk_manager":
        approved = output.get("approved_position_pct", -1)
        estimated_loss = output.get("estimated_loss_pct", -1)
        risk_budget = output.get("risk_budget_pct", -1)
        if not isinstance(approved, (int, float)) or not (0 <= approved <= 100):
            issues.append(f"approved_position_pct={approved} 超出 [0,100]")
            score -= 40.0
        if not isinstance(estimated_loss, (int, float)) or estimated_loss < 0:
            issues.append(f"estimated_loss_pct={estimated_loss} 无效")
            score -= 30.0
        if (
            isinstance(estimated_loss, (int, float))
            and isinstance(risk_budget, (int, float))
            and estimated_loss > risk_budget + 1e-9
        ):
            issues.append(
                f"预计账户损失 {estimated_loss:.2f}% 超过风险预算 {risk_budget:.2f}%"
            )
            score -= 40.0

    return max(0.0, score), issues


def _score_no_forbidden_claims(output: dict[str, Any]) -> tuple[float, list[str]]:
    """违禁词检查。"""
    FORBIDDEN = ["绝对稳赚", "100%收益", "稳赚不赔", "零风险", "必涨", "必赢", "稳定盈利"]
    text = str(output)
    issues: list[str] = []
    score = 100.0
    for kw in FORBIDDEN:
        if kw in text:
            issues.append(f"包含违禁词: {kw}")
            score -= 50.0
    return max(0.0, score), issues


def evaluate(
    subject: str,      # "synthesis" / "trader" / "risk_manager"
    output: dict[str, Any],
) -> EvaluationResult:
    """对 Agent 输出进行独立质量评分。"""
    updated_at = datetime.utcnow().isoformat() + "Z"
    all_issues: list[str] = []

    completeness_score, ci = _score_data_completeness(output)
    all_issues.extend(ci)

    logic_score, li = _score_logical_consistency(output, subject)
    all_issues.extend(li)

    forbidden_score, fi = _score_no_forbidden_claims(output)
    all_issues.extend(fi)

    dimensions = [
        EvaluationDimension("数据完整性", completeness_score, 0.4, "检查 source/updated_at/disclaimer"),
        EvaluationDimension("逻辑一致性", logic_score, 0.4, "检查 signal/confidence 的内部一致"),
        EvaluationDimension("违禁词", forbidden_score, 0.2, "检查风险提示与合规词汇"),
    ]

    overall = sum(d.score * d.weight for d in dimensions)

    return EvaluationResult(
        subject=subject,
        overall_score=round(overall, 1),
        dimensions=dimensions,
        issues=all_issues,
        source="evaluator_agent",
        updated_at=updated_at,
    )
