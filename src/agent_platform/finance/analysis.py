from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)

from agent_platform.finance.indicators import (
    add_atr,
    add_bollinger_bands,
    add_cci,
    add_ema,
    add_kdj,
    add_macd,
    add_moving_average,
    add_rsi,
    add_volume_ma,
    annualized_volatility,
    max_drawdown,
    total_return,
)
from agent_platform.finance.market_data_provider import MarketDataProvider
from agent_platform.finance.constants import DISCLAIMER


@dataclass(frozen=True, slots=True)
class SecurityAnalysisResult:
    market: str
    symbol: str
    name: str
    start_date: str
    end_date: str
    source: str
    updated_at: str
    # ── 汇总指标 ─────────────────────────────────────────────────────────
    total_return_pct: float
    annualized_volatility_pct: float
    max_drawdown_pct: float
    # ── 最新值 ────────────────────────────────────────────────────────────
    latest_close: float
    latest_ma5: float
    latest_ma20: float
    latest_rsi: float
    latest_macd: float
    latest_macd_signal: float
    latest_bb_upper: float
    latest_bb_lower: float
    latest_bb_position_pct: float   # 0=下轨,50=中轨,100=上轨
    latest_kdj_k: float
    latest_kdj_d: float
    latest_kdj_j: float
    latest_atr: float
    latest_cci: float
    latest_ema12: float
    latest_ema26: float
    disclaimer: str
    price_history: pd.DataFrame
    data_status: str                     # live / offline_sample / fallback / unavailable
    fallback_reason: str | None          # 降级或不可用时的原因
    latest_volume: float | None = None
    latest_volume_ma5: float | None = None

    def to_markdown(self) -> str:
        bb_pos = f"{self.latest_bb_position_pct:.1f}%"
        rsi_note = (
            "（超买区）" if self.latest_rsi > 70
            else "（超卖区）" if self.latest_rsi < 30
            else "（中性区）"
        )
        macd_bias = "多头" if self.latest_macd > self.latest_macd_signal else "空头"
        kdj_note = (
            "（超买）" if self.latest_kdj_j > 100
            else "（超卖）" if self.latest_kdj_j < 0
            else "（中性）"
        )
        return "\n".join(
            [
                f"### {self.name}（{self.market}:{self.symbol}）行情分析",
                f"- 时间范围：{self.start_date} 至 {self.end_date}",
                f"- 数据来源：{self.source}，更新时间：{self.updated_at}",
                "",
                "**价格与趋势**",
                f"- 最新收盘价：{self.latest_close:.2f}",
                f"- 5 日均线：{self.latest_ma5:.2f}",
                f"- 20 日均线：{self.latest_ma20:.2f}",
                f"- EMA12：{self.latest_ema12:.2f}",
                f"- EMA26：{self.latest_ema26:.2f}",
                f"- 布林带上轨：{self.latest_bb_upper:.2f}",
                f"- 布林带下轨：{self.latest_bb_lower:.2f}",
                f"- 布林带位置：{bb_pos}（0%=下轨，100%=上轨）",
                "",
                "**动量指标**",
                f"- RSI(14)：{self.latest_rsi:.2f}{rsi_note}",
                f"- MACD(12/26/9) DIF：{self.latest_macd:.4f}",
                f"- MACD 信号线 DEA：{self.latest_macd_signal:.4f}",
                f"- 当前 MACD 信号：{macd_bias}",
                f"- KDJ K:{self.latest_kdj_k:.2f} D:{self.latest_kdj_d:.2f} J:{self.latest_kdj_j:.2f}{kdj_note}",
                f"- CCI(20)：{self.latest_cci:.2f}",
                "",
                "**波动性指标**",
                f"- ATR(14)：{self.latest_atr:.2f}（平均真实波幅）",
                f"- 年化波动率：{self.annualized_volatility_pct:.2f}%",
                "",
                "**区间表现**",
                f"- 区间收益率：{self.total_return_pct:.2f}%",
                f"- 最大回撤：{self.max_drawdown_pct:.2f}%",
                "",
                f"> ⚠️ {self.disclaimer}",
            ]
        )

    def to_dict(self) -> dict:
        """将最新指标值序列化为字典，供下游 Agent 消费（不含 price_history DataFrame）。"""
        return {
            "symbol": self.symbol,
            "market": self.market,
            "name": self.name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "source": self.source,
            "updated_at": self.updated_at,
            "latest_close": self.latest_close,
            "latest_ma5": self.latest_ma5,
            "latest_ma20": self.latest_ma20,
            "latest_ema12": self.latest_ema12,
            "latest_ema26": self.latest_ema26,
            "latest_volume": self.latest_volume,
            "latest_volume_ma5": self.latest_volume_ma5,
            "latest_rsi": self.latest_rsi,
            "latest_macd": self.latest_macd,
            "latest_macd_signal": self.latest_macd_signal,
            "latest_bb_upper": self.latest_bb_upper,
            "latest_bb_lower": self.latest_bb_lower,
            "latest_bb_position_pct": self.latest_bb_position_pct,
            "latest_kdj_k": self.latest_kdj_k,
            "latest_kdj_d": self.latest_kdj_d,
            "latest_kdj_j": self.latest_kdj_j,
            "latest_atr": self.latest_atr,
            "latest_cci": self.latest_cci,
            "total_return_pct": self.total_return_pct,
            "annualized_volatility_pct": self.annualized_volatility_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "disclaimer": self.disclaimer,
            "data_status": self.data_status,
            "fallback_reason": self.fallback_reason,
        }


def analyze_security(
    symbol: str,
    start: date | str | None = None,
    end: date | str | None = None,
    provider: MarketDataProvider | None = None,
) -> SecurityAnalysisResult:
    # 兼容字符串日期（ISO 格式 "YYYY-MM-DD"）
    if isinstance(start, str):
        from datetime import datetime as _dt
        start = _dt.fromisoformat(start).date()
    if isinstance(end, str):
        from datetime import datetime as _dt
        end = _dt.fromisoformat(end).date()

    # 修复：不再硬编码 SampleMarketDataProvider，改用 factory 获取配置的 Provider
    data_status = "offline_sample"
    fallback_reason: str | None = None

    if provider is None:
        from agent_platform.finance.provider_factory import create_market_data_provider
        data_provider = create_market_data_provider()
    else:
        data_provider = provider

    # 判断 provider 类型以设置 data_status
    from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
    is_offline_provider = isinstance(data_provider, SampleMarketDataProvider) or bool(
        getattr(data_provider, "offline", False)
    )
    if is_offline_provider:
        data_status = "offline_sample"
        prices = data_provider.get_price_history(symbol=symbol, start=start, end=end)
    else:
        # 真实数据源，尝试获取并标记状态
        try:
            prices = data_provider.get_price_history(symbol=symbol, start=start, end=end)
            data_status = "live"
        except Exception as exc:
            # 降级为样例数据
            logger.warning("[SecurityAnalysis] 真实数据源失败，降级为样例数据: %s", exc)
            from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
            data_provider = SampleMarketDataProvider()
            prices = data_provider.get_price_history(symbol=symbol, start=start, end=end)
            data_status = "fallback"
            fallback_reason = f"真实数据源失败: {type(exc).__name__}"

    # 依次叠加指标列
    prices = add_moving_average(prices, window=5)
    prices = add_moving_average(prices, window=20)
    prices = add_ema(prices, window=12)
    prices = add_ema(prices, window=26)
    prices = add_macd(prices, fast=12, slow=26, signal=9)
    prices = add_rsi(prices, period=14)
    prices = add_bollinger_bands(prices, window=20, num_std=2.0)
    prices = add_volume_ma(prices, window=5)
    prices = add_kdj(prices, k=9, d=3, j_weight=3)
    prices = add_atr(prices, period=14)
    prices = add_cci(prices, period=20)

    first_row = prices.iloc[0]
    latest = prices.iloc[-1]

    bb_upper = float(latest["bb_upper"])
    bb_lower = float(latest["bb_lower"])
    close = float(latest["close"])
    bb_range = bb_upper - bb_lower
    bb_position = (close - bb_lower) / bb_range * 100 if bb_range > 0 else 50.0

    return SecurityAnalysisResult(
        market=str(latest["market"]),
        symbol=str(latest["symbol"]),
        name=str(latest["name"]),
        start_date=str(first_row["date"]),
        end_date=str(latest["date"]),
        source=str(latest["source"]),
        updated_at=str(latest["updated_at"]),
        total_return_pct=total_return(prices) * 100,
        annualized_volatility_pct=annualized_volatility(prices) * 100,
        max_drawdown_pct=max_drawdown(prices) * 100,
        latest_close=close,
        latest_ma5=float(latest["ma5"]),
        latest_ma20=float(latest["ma20"]),
        latest_rsi=float(latest["rsi"]),
        latest_macd=float(latest["macd"]),
        latest_macd_signal=float(latest["macd_signal"]),
        latest_bb_upper=bb_upper,
        latest_bb_lower=bb_lower,
        latest_bb_position_pct=bb_position,
        latest_kdj_k=float(latest["kdj_k"]),
        latest_kdj_d=float(latest["kdj_d"]),
        latest_kdj_j=float(latest["kdj_j"]),
        latest_atr=float(latest["atr"]),
        latest_cci=float(latest["cci"]),
        latest_ema12=float(latest["ema12"]),
        latest_ema26=float(latest["ema26"]),
        latest_volume=float(latest["volume"]),
        latest_volume_ma5=float(latest["volume_ma5"]),
        disclaimer=DISCLAIMER,
        price_history=prices,
        data_status=data_status,
        fallback_reason=fallback_reason,
    )


def analyze_security_as_markdown(symbol: str) -> str:
    return analyze_security(symbol).to_markdown()
