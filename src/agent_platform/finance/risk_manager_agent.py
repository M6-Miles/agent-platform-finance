"""
风控 Agent（RiskManagerAgent）
================================
对 TraderResult 进行合规性检查，确保：
  - 单票仓位 ≤ 2% 上限（可配置）
  - 行业集中度 ≤ 30%
  - 跌幅超过 max_drawdown_pct (默认15%) 时发出减仓建议
  - 产出附带 source / updated_at / disclaimer

Harness：JSONSchemaValidator + SourceAttributionFilter + KeywordBlocker
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

RISK_MANAGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "symbol", "approved_position_pct", "risk_flags",
        "final_signal", "source", "updated_at", "disclaimer"
    ],
    "properties": {
        "symbol": {"type": "string"},
        "approved_position_pct": {"type": "number", "minimum": 0, "maximum": 100},
        "risk_flags": {"type": "array"},
        "final_signal": {"type": "string", "enum": ["buy", "sell", "hold", "reduce"]},
        "risk_note": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}

_DEFAULT_MAX_SINGLE_POSITION_PCT = 2.0
_DEFAULT_MAX_INDUSTRY_PCT = 30.0
_DEFAULT_MAX_DRAWDOWN_PCT = 15.0


@dataclass(frozen=True, slots=True)
class RiskManagerResult:
    symbol: str
    approved_position_pct: float   # 风控通过后的实际仓位
    risk_flags: list[str]          # 触发的风险提示列表
    final_signal: str              # buy / sell / hold / reduce
    risk_note: str
    source: str
    updated_at: str
    disclaimer: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "approved_position_pct": self.approved_position_pct,
            "risk_flags": list(self.risk_flags),
            "final_signal": self.final_signal,
            "risk_note": self.risk_note,
            "source": self.source,
            "updated_at": self.updated_at,
            "disclaimer": self.disclaimer,
        }

    def to_markdown(self) -> str:
        sig_map = {
            "buy": "🟢 买入", "sell": "🔴 卖出",
            "hold": "🟡 持有", "reduce": "🟠 减仓"
        }
        flags = "\n".join(f"  - ⚠️ {f}" for f in self.risk_flags) or "  （无风险提示）"
        return "\n".join([
            f"### {self.symbol} 风控结果",
            f"- 批准仓位：{self.approved_position_pct:.1f}%",
            f"- 最终信号：{sig_map.get(self.final_signal, self.final_signal)}",
            "",
            "**风险提示**",
            flags,
            "",
            f"**风控说明**：{self.risk_note}",
            "",
            f"> ⚠️ {self.disclaimer}",
        ])


def assess_risk(
    trader_result: dict[str, Any],
    current_drawdown_pct: float = 0.0,
    current_industry_position_pct: float = 0.0,
    max_single_position_pct: float = _DEFAULT_MAX_SINGLE_POSITION_PCT,
    max_industry_pct: float = _DEFAULT_MAX_INDUSTRY_PCT,
    max_drawdown_pct: float = _DEFAULT_MAX_DRAWDOWN_PCT,
) -> RiskManagerResult:
    """
    对 TraderResult 进行风险管控。

    参数：
      trader_result              : TraderResult.to_dict()
      current_drawdown_pct       : 当前持仓已产生的最大回撤 %（正数）
      current_industry_position_pct : 当前同行业仓位总和 %
      max_single_position_pct    : 单票最大仓位（默认 2%）
      max_industry_pct           : 行业集中度上限（默认 30%）
      max_drawdown_pct           : 触发减仓的最大回撤（默认 15%）
    """
    from agent_platform.finance.constants import DISCLAIMER

    symbol = trader_result.get("symbol", "")
    suggested = float(trader_result.get("position_pct_suggestion", 0.0))
    trader_signal = trader_result.get("signal", "hold")
    risk_flags: list[str] = []
    notes: list[str] = []

    # 1. 单票仓位上限
    approved = suggested
    if suggested > max_single_position_pct:
        approved = max_single_position_pct
        risk_flags.append(
            f"单票建议仓位 {suggested:.1f}% 超过上限 {max_single_position_pct:.1f}%，已截断"
        )

    # 2. 行业集中度
    projected_industry = current_industry_position_pct + approved
    if projected_industry > max_industry_pct:
        excess = projected_industry - max_industry_pct
        approved = max(0.0, approved - excess)
        risk_flags.append(
            f"行业集中度将达 {projected_industry:.1f}%，超过上限 {max_industry_pct:.1f}%，仓位进一步削减"
        )

    # 3. 最大回撤保护
    final_signal = trader_signal
    if current_drawdown_pct >= max_drawdown_pct:
        approved = 0.0
        final_signal = "reduce"
        risk_flags.append(
            f"当前回撤 {current_drawdown_pct:.1f}% ≥ 阈值 {max_drawdown_pct:.1f}%，触发减仓保护"
        )

    if not risk_flags:
        notes.append("所有风控指标通过")
    else:
        notes.append(f"触发 {len(risk_flags)} 项风险提示")

    notes.append(f"最终批准仓位：{approved:.1f}%")

    return RiskManagerResult(
        symbol=symbol,
        approved_position_pct=round(approved, 1),
        risk_flags=risk_flags,
        final_signal=final_signal,
        risk_note="；".join(notes),
        source="risk_manager",
        updated_at=datetime.utcnow().isoformat() + "Z",
        disclaimer=DISCLAIMER,
    )
