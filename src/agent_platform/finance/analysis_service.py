"""
证券分析服务（Analysis Service）
================================
把"取行情 → 预热计算指标 → 裁剪回请求区间 → 汇总指标 + 指标序列"
这条链路从 API 层抽出来，使日期语义与指标语义可被单元测试直接验证。

关键约定
--------
* **预热行只喂给指标，不进结果。** MACD(26/9) 成熟需要约 34 个交易日，
  因此默认向 ``start`` 之前多取 ``WARMUP_TRADING_DAYS`` 个交易日。
  预热行参与指标计算，但：
    - 不作为请求区间的返回行（``series`` 只含 [start, end]）；
    - 不计入 ``trading_days``；
    - 不参与区间收益 / 波动率 / 最大回撤的计算。
* **未成熟的指标点是 None，不是 0。** 指标在其回看窗口填满之前
  数学上没有定义，序列里保持 ``None``，由前端画成断点（缺口）。
  绝不用 0 或任何生成值填充。
* **汇总指标只用请求区间的行。** 区间收益 = 区间末收盘 / 区间首收盘 - 1。
* **历史不足时明确报错**，而不是返回一条"看起来能用"的结果。

指标口径与 ``indicators.py`` 完全一致（直接复用其函数），此处只额外做
"回看窗口未填满 → None"的掩码，因此序列末值与 ``latest_*`` 严格相等。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from agent_platform.finance import indicators as ind
from agent_platform.finance.analysis import SecurityAnalysisResult
from agent_platform.finance.constants import DISCLAIMER
from agent_platform.finance.data_status import (
    FetchOutcome,
    fetch_price_history,
    normalize_data_mode,
)
from agent_platform.finance.date_window import (
    MIN_TRADING_DAYS_FOR_FULL_INDICATORS,
    DateWindow,
    InsufficientHistoryError,
    assert_dates_in_window,
    build_window,
    normalize_date_column,
)

# MACD 信号线成熟需要 26 + 9 - 1 = 34 个交易日，取 40 留出余量。
WARMUP_TRADING_DAYS = 40

# 每个指标列成熟所需的最小行数（含当前行）。行号 < required - 1 的点置 None。
INDICATOR_LOOKBACK: dict[str, int] = {
    "ma5": 5,
    "ma20": 20,
    "ema12": 12,
    "ema26": 26,
    "macd": 26,
    "macd_signal": 34,
    "macd_hist": 34,
    "rsi": 15,
    "bb_upper": 20,
    "bb_middle": 20,
    "bb_lower": 20,
    "kdj_k": 9,
    "kdj_d": 11,
    "kdj_j": 11,
    "atr": 15,
    "cci": 20,
    "volume_ma5": 5,
}

SERIES_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ma5",
    "ma20",
    "macd",
    "macd_signal",
    "macd_hist",
    "rsi",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "bb_upper",
    "bb_middle",
    "bb_lower",
)


class AnalysisError(ValueError):
    """分析请求无法完成（端点转换为 HTTP 400）。"""


@dataclass(frozen=True, slots=True)
class AnalysisWindowResult:
    result: SecurityAnalysisResult
    series: list[dict[str, float | str | None]]
    trading_days: int
    warmup_rows_used: int
    requested_start: str | None
    requested_end: str | None


def add_all_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """按 ``indicators.py`` 的口径叠加全部指标列。"""
    data = frame
    data = ind.add_moving_average(data, window=5)
    data = ind.add_moving_average(data, window=20)
    data = ind.add_ema(data, window=12)
    data = ind.add_ema(data, window=26)
    data = ind.add_macd(data, fast=12, slow=26, signal=9)
    data = ind.add_rsi(data, period=14)
    data = ind.add_bollinger_bands(data, window=20, num_std=2.0)
    data = ind.add_volume_ma(data, window=5)
    data = ind.add_kdj(data, k=9, d=3, j_weight=3)
    data = ind.add_atr(data, period=14)
    data = ind.add_cci(data, period=20)
    return data


def mask_immature_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """把回看窗口未填满的指标点置为 NaN（下游序列化为 None）。

    ``indicators.py`` 用 ``min_periods=1`` 保证下游 Agent 拿到的是数值，
    但对图表而言，"窗口还没填满"的点必须是缺口而不是数值 —— 否则会画出
    一段并不存在的指标走势。掩码只影响前若干行，不改变成熟后的取值。
    """
    data = frame.reset_index(drop=True).copy()
    for column, required in INDICATOR_LOOKBACK.items():
        if column not in data.columns:
            continue
        cutoff = required - 1
        if cutoff <= 0:
            continue
        data.loc[data.index < cutoff, column] = float("nan")
    return data


def _nullable(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return round(float(value), 6)


def analyze_window(
    symbol: str,
    *,
    start: date | str | None = None,
    end: date | str | None = None,
    data_mode: str = "auto",
    today: date | None = None,
    fetcher=fetch_price_history,
) -> AnalysisWindowResult:
    """在校验过的日期区间内做证券分析，返回汇总指标 + 可绘图的指标序列。"""
    mode = normalize_data_mode(data_mode)
    window: DateWindow = build_window(
        start, end, warmup_trading_days=WARMUP_TRADING_DAYS, today=today
    )

    outcome: FetchOutcome = fetcher(
        symbol,
        data_mode=mode,
        start=window.fetch_start,
        end=window.end,
    )

    full = normalize_date_column(outcome.frame).sort_values("date").reset_index(drop=True)
    if window.end is not None:
        full = full[full["date"] <= window.end].reset_index(drop=True)
    if full.empty:
        raise AnalysisError(f"{symbol} 在请求区间内没有行情数据")

    if window.start is not None:
        in_window_mask = full["date"] >= window.start
    else:
        in_window_mask = pd.Series(True, index=full.index)

    warmup_rows = int((~in_window_mask).sum())
    window_rows = int(in_window_mask.sum())
    if window_rows == 0:
        raise AnalysisError(
            f"{symbol} 在请求区间 "
            f"[{window.start}, {window.end}] 内没有交易日数据"
        )

    total_rows = len(full)
    if total_rows < MIN_TRADING_DAYS_FOR_FULL_INDICATORS:
        raise InsufficientHistoryError(
            f"历史数据不足以计算完整指标：请求区间 {window_rows} 个交易日 + "
            f"预热 {warmup_rows} 个交易日 = {total_rows}，"
            f"至少需要 {MIN_TRADING_DAYS_FOR_FULL_INDICATORS}（MACD 信号线周期）。"
            "请扩大日期区间或改用有更长历史的标的。",
            available=total_rows,
            required=MIN_TRADING_DAYS_FOR_FULL_INDICATORS,
        )

    enriched = add_all_indicators(full)
    masked = mask_immature_indicators(enriched)

    in_window = masked[in_window_mask.to_numpy()].reset_index(drop=True)
    assert_dates_in_window(in_window["date"], window, label=f"{symbol} 分析行情")

    # 汇总指标只看请求区间的行（预热行不得影响区间收益/回撤/波动率）
    latest_raw = enriched.iloc[-1]
    latest = in_window.iloc[-1]
    first_row = in_window.iloc[0]

    bb_upper = float(latest_raw["bb_upper"])
    bb_lower = float(latest_raw["bb_lower"])
    close = float(latest["close"])
    bb_range = bb_upper - bb_lower
    bb_position = (close - bb_lower) / bb_range * 100 if bb_range > 0 else 50.0

    result = SecurityAnalysisResult(
        market=str(latest["market"]),
        symbol=str(latest["symbol"]),
        name=str(latest["name"]),
        start_date=str(first_row["date"]),
        end_date=str(latest["date"]),
        source=outcome.source,
        updated_at=outcome.updated_at,
        total_return_pct=ind.total_return(in_window) * 100,
        annualized_volatility_pct=ind.annualized_volatility(in_window) * 100,
        max_drawdown_pct=ind.max_drawdown(in_window) * 100,
        latest_close=close,
        latest_ma5=float(latest_raw["ma5"]),
        latest_ma20=float(latest_raw["ma20"]),
        latest_rsi=float(latest_raw["rsi"]),
        latest_macd=float(latest_raw["macd"]),
        latest_macd_signal=float(latest_raw["macd_signal"]),
        latest_bb_upper=bb_upper,
        latest_bb_lower=bb_lower,
        latest_bb_position_pct=bb_position,
        latest_kdj_k=float(latest_raw["kdj_k"]),
        latest_kdj_d=float(latest_raw["kdj_d"]),
        latest_kdj_j=float(latest_raw["kdj_j"]),
        latest_atr=float(latest_raw["atr"]),
        latest_cci=float(latest_raw["cci"]),
        latest_ema12=float(latest_raw["ema12"]),
        latest_ema26=float(latest_raw["ema26"]),
        latest_volume=float(latest_raw["volume"]),
        latest_volume_ma5=float(latest_raw["volume_ma5"]),
        disclaimer=DISCLAIMER,
        price_history=in_window,
        data_status=outcome.data_status,
        fallback_reason=outcome.fallback_reason,
    )

    series: list[dict[str, float | str | None]] = []
    for _, row in in_window.iterrows():
        point: dict[str, float | str | None] = {"date": row["date"].isoformat()}
        for column in SERIES_COLUMNS:
            point[column] = _nullable(row.get(column))
        series.append(point)

    return AnalysisWindowResult(
        result=result,
        series=series,
        trading_days=window_rows,
        warmup_rows_used=warmup_rows,
        requested_start=window.start.isoformat() if window.start else None,
        requested_end=window.end.isoformat() if window.end else None,
    )
