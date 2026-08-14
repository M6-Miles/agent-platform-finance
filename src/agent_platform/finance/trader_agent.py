"""
交易信号 Agent（TraderAgent）
==============================
基于 SynthesisResult + MarketRegimeResult 生成最终交易信号：
  信号类型（buy/sell/hold）+ 目标价区间 + 建议仓位
仅供研究参考，不执行真实交易，不连接任何经纪商接口。

Harness：JSONSchemaValidator + SourceAttributionFilter + KeywordBlocker
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

TRADER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "symbol", "signal", "position_pct_suggestion", "take_profit_price",
        "rationale", "source", "updated_at", "disclaimer"
    ],
    "properties": {
        "symbol": {"type": "string"},
        "signal": {"type": "string", "enum": ["buy", "sell", "hold"]},
        "target_price_low": {},
        "target_price_high": {},
        "entry_price": {},
        "stop_loss_price": {},
        "take_profit_price": {},
        "position_pct_suggestion": {"type": "number", "minimum": 0, "maximum": 100},
        "rationale": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}

_MAX_AUTO_POSITION_PCT = 10.0   # 超过此值需人工确认（Rule/no_trade_without_confirmation.md）


class HumanApprovalRequired(Exception):
    """仓位建议超过自动阈值，需人工审批后才能输出。

    Attributes
    ----------
    trader_result : TraderResult | None
        已计算完成的交易建议对象（批准后直接使用，无需重算）。
    """
    def __init__(self, message: str, trader_result=None) -> None:
        super().__init__(message)
        self.trader_result = trader_result


@dataclass(frozen=True, slots=True)
class TraderResult:
    symbol: str
    signal: str                      # buy / sell / hold
    target_price_low: float | None
    target_price_high: float | None
    entry_price: float | None
    stop_loss_price: float | None
    take_profit_price: float | None
    position_pct_suggestion: float   # 0–100（受 _MAX_AUTO_POSITION_PCT 约束）
    rationale: str
    source: str
    updated_at: str
    disclaimer: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "signal": self.signal,
            "target_price_low": self.target_price_low,
            "target_price_high": self.target_price_high,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "position_pct_suggestion": self.position_pct_suggestion,
            "rationale": self.rationale,
            "source": self.source,
            "updated_at": self.updated_at,
            "disclaimer": self.disclaimer,
        }

    def to_markdown(self) -> str:
        signal_emoji = {"buy": "🟢 买入", "sell": "🔴 卖出", "hold": "🟡 持有"}
        tp = (
            f"{self.target_price_low:.2f} – {self.target_price_high:.2f}"
            if self.target_price_low is not None
            else "N/A"
        )
        sl = f"{self.stop_loss_price:.2f}" if self.stop_loss_price is not None else "N/A"
        take_profit = f"{self.take_profit_price:.2f}" if self.take_profit_price is not None else "N/A"
        return "\n".join([
            f"### {self.symbol} 交易建议",
            f"- 信号：{signal_emoji.get(self.signal, self.signal)}",
            f"- 目标价区间：{tp}",
            f"- 止损价：{sl}",
            f"- 止盈价：{take_profit}",
            f"- 建议仓位：{self.position_pct_suggestion:.1f}%",
            f"- 理由：{self.rationale}",
            "",
            f"> ⚠️ {self.disclaimer}",
        ])


def _calc_position(signal: str, confidence: float, regime: str) -> float:
    """计算建议仓位（%）；不内置上限，由调用方决定是否触发 HumanApprovalRequired。"""
    if signal == "sell" or signal == "hold":
        return 0.0
    # buy 信号：置信度 + 大盘状态
    base = confidence * 10.0          # confidence=0.80 → 8%
    if regime == "bull":
        base = base * 1.2             # 牛市加成，可能超过 _MAX_AUTO_POSITION_PCT
    elif regime == "bear":
        base = base * 0.5
    return round(base, 1)


def generate_trade_signal(
    synthesis: dict[str, Any],
    regime: dict[str, Any],
    technical: dict[str, Any] | None = None,
) -> TraderResult:
    """
    生成交易信号。
    synthesis  : SynthesisResult.to_dict()
    regime     : MarketRegimeResult.to_dict()
    technical  : AnalysisResult.to_dict()（可选，用于计算止损）
    """
    from agent_platform.finance.constants import DISCLAIMER

    symbol = synthesis.get("symbol", "")
    signal_raw = synthesis.get("signal", "hold")
    signal = signal_raw if signal_raw in ("buy", "sell", "hold") else "hold"
    confidence = float(synthesis.get("confidence", 0.5))
    regime_str = regime.get("regime", "unknown")

    tgt_low = synthesis.get("target_price_low")
    tgt_high = synthesis.get("target_price_high")

    # 止损价：若有技术数据，利用 ATR（若无则取收盘价 × 0.93）
    stop_loss: float | None = None
    take_profit: float | None = None
    close = None
    if technical:
        close = technical.get("latest_close")
        atr = technical.get("latest_atr")
        if close and atr:
            stop_loss = round(close - 2.0 * atr, 2)
        elif close:
            stop_loss = round(close * 0.93, 2)

    if signal == "buy" and close and stop_loss and stop_loss < close:
        risk_distance = float(close) - stop_loss
        target_candidate = float(tgt_high) if tgt_high is not None else 0.0
        take_profit = round(
            max(target_candidate, float(close) + 1.5 * risk_distance), 2
        )

    position = _calc_position(signal, confidence, regime_str)

    rationale = (
        f"综合研判信号={signal}，置信度={confidence:.0%}，"
        f"大盘状态={regime_str}，建议仓位={position}%"
    )

    # 先完整构建 TraderResult，再检查仓位阈值。
    # 这样 HumanApprovalRequired 可携带完整结果供批准后直接使用，无需重算。
    result = TraderResult(
        symbol=symbol,
        signal=signal,
        target_price_low=tgt_low,
        target_price_high=tgt_high,
        entry_price=float(close) if close else None,
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
        position_pct_suggestion=position,
        rationale=rationale,
        source="trader",
        updated_at=datetime.utcnow().isoformat() + "Z",
        disclaimer=DISCLAIMER,
    )

    # 硬约束：超过 _MAX_AUTO_POSITION_PCT 必须人工审批（携带已计算结果）
    if position > _MAX_AUTO_POSITION_PCT:
        raise HumanApprovalRequired(
            f"建议仓位 {position}% 超过自动阈值 {_MAX_AUTO_POSITION_PCT}%，需人工确认",
            trader_result=result,
        )

    return result
