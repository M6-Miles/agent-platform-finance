"""
Skill: calculate_indicators
可复用技术指标计算技能 —— 可注入任意 Agent 的上下文。

使用方式：
    from Skill.calculate_indicators import skill_calculate_indicators
    result = skill_calculate_indicators("600519", start="2026-01-01")
"""
from __future__ import annotations

from datetime import date
from typing import Any

from agent_platform.finance.analysis import analyze_security
from agent_platform.finance.market_data_provider import MarketDataProvider


def skill_calculate_indicators(
    symbol: str,
    start: date | str | None = None,
    end: date | str | None = None,
    provider: MarketDataProvider | None = None,
) -> dict[str, Any]:
    """
    计算指定股票的全套技术指标，返回结构化字典。

    返回字段（均由 pandas 代码计算，非 LLM 估算）：
      - latest_close, latest_ma5, latest_ma20
      - latest_ema12, latest_ema26
      - latest_macd, latest_macd_signal
      - latest_rsi
      - latest_bb_upper, latest_bb_lower, latest_bb_position_pct
      - latest_kdj_k, latest_kdj_d, latest_kdj_j
      - latest_atr, latest_cci
      - total_return_pct, annualized_volatility_pct, max_drawdown_pct
    """
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)

    result = analyze_security(symbol=symbol, start=start, end=end, provider=provider)

    return {
        "symbol": result.symbol,
        "name": result.name,
        "market": result.market,
        "source": result.source,
        "updated_at": result.updated_at,
        "start_date": result.start_date,
        "end_date": result.end_date,
        # 价格与趋势
        "latest_close": result.latest_close,
        "latest_ma5": result.latest_ma5,
        "latest_ma20": result.latest_ma20,
        "latest_ema12": result.latest_ema12,
        "latest_ema26": result.latest_ema26,
        # 动量
        "latest_macd": result.latest_macd,
        "latest_macd_signal": result.latest_macd_signal,
        "latest_rsi": result.latest_rsi,
        "latest_kdj_k": result.latest_kdj_k,
        "latest_kdj_d": result.latest_kdj_d,
        "latest_kdj_j": result.latest_kdj_j,
        # 布林带
        "latest_bb_upper": result.latest_bb_upper,
        "latest_bb_lower": result.latest_bb_lower,
        "latest_bb_position_pct": result.latest_bb_position_pct,
        # 波动性
        "latest_atr": result.latest_atr,
        "latest_cci": result.latest_cci,
        # 区间表现
        "total_return_pct": result.total_return_pct,
        "annualized_volatility_pct": result.annualized_volatility_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
    }
