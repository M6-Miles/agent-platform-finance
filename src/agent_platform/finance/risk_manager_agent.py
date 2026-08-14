"""
风控 Agent（RiskManagerAgent）
================================
对 TraderResult 进行合规性检查，确保：
  - 止损触发时，单笔交易对账户造成的最大亏损 ≤ 2%（可配置）
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
        "final_signal", "estimated_loss_pct", "risk_budget_pct",
        "take_profit_price", "risk_reward_ratio",
        "source", "updated_at", "disclaimer"
    ],
    "properties": {
        "symbol": {"type": "string"},
        "approved_position_pct": {"type": "number", "minimum": 0, "maximum": 100},
        "entry_price": {"type": ["number", "null"]},
        "stop_loss_price": {"type": ["number", "null"]},
        "take_profit_price": {"type": ["number", "null"]},
        "risk_reward_ratio": {"type": ["number", "null"], "minimum": 0},
        "stop_distance_pct": {"type": ["number", "null"], "minimum": 0},
        "estimated_loss_pct": {"type": "number", "minimum": 0},
        "risk_budget_pct": {"type": "number", "minimum": 0},
        "risk_flags": {"type": "array"},
        "final_signal": {"type": "string", "enum": ["buy", "sell", "hold", "reduce"]},
        "risk_note": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}

_DEFAULT_MAX_LOSS_PCT = 2.0
_DEFAULT_MAX_INDUSTRY_PCT = 30.0
_DEFAULT_MAX_DRAWDOWN_PCT = 15.0


@dataclass(frozen=True, slots=True)
class RiskManagerResult:
    symbol: str
    approved_position_pct: float   # 风控通过后的实际仓位
    entry_price: float | None
    stop_loss_price: float | None
    take_profit_price: float | None
    risk_reward_ratio: float | None
    stop_distance_pct: float | None
    estimated_loss_pct: float
    risk_budget_pct: float
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
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "risk_reward_ratio": self.risk_reward_ratio,
            "stop_distance_pct": self.stop_distance_pct,
            "estimated_loss_pct": self.estimated_loss_pct,
            "risk_budget_pct": self.risk_budget_pct,
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
            f"- 单笔账户风险：{self.estimated_loss_pct:.2f}% / 上限 {self.risk_budget_pct:.2f}%",
            f"- 风险收益比：{self.risk_reward_ratio:.2f}:1" if self.risk_reward_ratio is not None else "- 风险收益比：N/A",
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
    max_loss_pct: float = _DEFAULT_MAX_LOSS_PCT,
    max_industry_pct: float = _DEFAULT_MAX_INDUSTRY_PCT,
    max_drawdown_pct: float = _DEFAULT_MAX_DRAWDOWN_PCT,
    max_single_position_pct: float | None = None,
) -> RiskManagerResult:
    """
    对 TraderResult 进行风险管控。

    参数：
      trader_result              : TraderResult.to_dict()
      current_drawdown_pct       : 当前持仓已产生的最大回撤 %（正数）
      current_industry_position_pct : 当前同行业仓位总和 %
      max_loss_pct               : 单笔止损触发时允许损失的账户权益比例（默认 2%）
      max_industry_pct           : 行业集中度上限（默认 30%）
      max_drawdown_pct           : 触发减仓的最大回撤（默认 15%）
      max_single_position_pct    : 可选额外仓位上限；不用于代替单笔亏损预算
    """
    from agent_platform.finance.constants import DISCLAIMER

    symbol = trader_result.get("symbol", "")
    suggested = float(trader_result.get("position_pct_suggestion", 0.0))
    trader_signal = trader_result.get("signal", "hold")
    risk_flags: list[str] = []
    notes: list[str] = []

    if max_loss_pct <= 0:
        raise ValueError("max_loss_pct 必须大于 0")

    # 1. 单笔亏损预算：账户损失 = 仓位比例 × 止损距离比例。
    approved = suggested
    entry_price = trader_result.get("entry_price")
    stop_loss_price = trader_result.get("stop_loss_price")
    take_profit_price = trader_result.get("take_profit_price")
    stop_distance_pct: float | None = None
    risk_reward_ratio: float | None = None
    if trader_signal == "buy" and suggested > 0:
        try:
            entry_price = float(entry_price)
            stop_loss_price = float(stop_loss_price)
        except (TypeError, ValueError):
            entry_price = stop_loss_price = None

        try:
            take_profit_price = float(take_profit_price)
        except (TypeError, ValueError):
            take_profit_price = None

        if not entry_price or entry_price <= 0 or stop_loss_price is None or not 0 < stop_loss_price < entry_price:
            approved = 0.0
            risk_flags.append("买入建议缺少有效参考价或止损价，无法计算单笔亏损，禁止自动批准仓位")
        else:
            stop_distance_pct = (entry_price - stop_loss_price) / entry_price * 100.0
            risk_limited_position = max_loss_pct / stop_distance_pct * 100.0
            if approved > risk_limited_position:
                approved = risk_limited_position
                risk_flags.append(
                    f"止损距离 {stop_distance_pct:.2f}% 下建议仓位将超过单笔亏损上限 "
                    f"{max_loss_pct:.2f}%，仓位已降至 {approved:.2f}%"
                )

            if take_profit_price is None or take_profit_price <= entry_price:
                approved = 0.0
                risk_flags.append("买入建议缺少有效止盈价，禁止自动批准仓位")
            else:
                risk_reward_ratio = (
                    (take_profit_price - entry_price) / (entry_price - stop_loss_price)
                )
                if risk_reward_ratio < 1.5:
                    approved = 0.0
                    risk_flags.append(
                        f"风险收益比 {risk_reward_ratio:.2f}:1 低于最低要求 1.50:1，禁止自动批准仓位"
                    )

    if max_single_position_pct is not None and approved > max_single_position_pct:
        approved = max_single_position_pct
        risk_flags.append(f"仓位超过额外单票上限 {max_single_position_pct:.1f}%，已截断")

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
    estimated_loss_pct = (
        approved * stop_distance_pct / 100.0 if stop_distance_pct is not None else 0.0
    )
    notes.append(f"止损触发时预计账户损失：{estimated_loss_pct:.2f}%")

    return RiskManagerResult(
        symbol=symbol,
        approved_position_pct=round(approved, 1),
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        risk_reward_ratio=(round(risk_reward_ratio, 4) if risk_reward_ratio is not None else None),
        stop_distance_pct=round(stop_distance_pct, 4) if stop_distance_pct is not None else None,
        estimated_loss_pct=round(estimated_loss_pct, 4),
        risk_budget_pct=round(max_loss_pct, 4),
        risk_flags=risk_flags,
        final_signal=final_signal,
        risk_note="；".join(notes),
        source="risk_manager",
        updated_at=datetime.utcnow().isoformat() + "Z",
        disclaimer=DISCLAIMER,
    )
