"""
多股对比服务（Comparison Service）
==================================
把"多只标的对齐到共同交易日后算指标"这件事从 API 层抽出来，
使数学口径可被单元测试直接验证，而不必经过 HTTP。

口径约定（与项目其它模块保持一致）
----------------------------------
* 归一化收益：以**共同交易日的首日**为基准，(close / close_0 - 1) * 100。
* 年化波动率：日收益样本标准差（ddof=0，与 ``indicators.annualized_volatility``
  一致）× sqrt(252) × 100。
* Sharpe：与 ``backtesting._compute_sharpe`` 同一口径 —— 日超额收益均值
  除以日收益标准差（ddof=1）再乘 sqrt(252)，无风险利率 2% 年化按几何日频折算。
* 最大回撤：收盘价相对历史峰值的最大跌幅（负值，与 ``indicators.max_drawdown``
  同符号约定）。
* 相关系数矩阵：由**共同交易日的日收益**算 Pearson 相关，强制对称、
  对角为 1、并夹紧到 [-1, 1]。
* 胜率：本服务**不提供**买入持有的"胜率" —— 没有交易就没有胜负。
  上涨交易日占比另行以 ``up_day_ratio_pct`` 明确命名给出。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd

from agent_platform.finance.backtesting import (
    _TRADING_DAYS_PER_YEAR,
    _compute_sharpe,
)
from agent_platform.finance.constants import DISCLAIMER
from agent_platform.finance.data_status import (
    STATUS_UNAVAILABLE,
    FetchOutcome,
    combine_statuses,
    fetch_price_history,
    normalize_data_mode,
)
from agent_platform.finance.date_window import (
    DateWindow,
    assert_dates_in_window,
    build_window,
    split_warmup,
)

MIN_COMMON_TRADING_DAYS = 2


class ComparisonError(ValueError):
    """对比请求无法完成（端点转换为 HTTP 400）。"""


@dataclass(frozen=True, slots=True)
class SymbolMetrics:
    symbol: str
    name: str
    trading_days: int
    latest_close: float
    total_return_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    up_day_ratio_pct: float
    normalized_returns: list[float]
    source: str
    updated_at: str
    data_status: str
    fallback_reason: str | None


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    symbols: list[str]
    dates: list[str]
    trading_days: int
    stocks: list[SymbolMetrics]
    correlation_matrix: dict[str, dict[str, float]]
    failed_symbols: dict[str, str]
    source: str
    updated_at: str
    data_status: str
    fallback_reason: str | None
    disclaimer: str = DISCLAIMER


def _max_drawdown_pct(closes: pd.Series) -> float:
    running_max = closes.cummax()
    drawdown = closes / running_max - 1.0
    return float(drawdown.min()) * 100.0


def _annualized_volatility_pct(returns: pd.Series) -> float:
    if len(returns) < 2:
        return 0.0
    return float(returns.std(ddof=0) * math.sqrt(_TRADING_DAYS_PER_YEAR)) * 100.0


def correlation_matrix_from_returns(
    returns_by_symbol: dict[str, pd.Series],
    symbols: list[str],
) -> dict[str, dict[str, float]]:
    """由真实日收益构造对称、对角为 1、取值在 [-1, 1] 的相关矩阵。

    单一标的或零方差序列的相关系数在数学上未定义，此处置 0.0（非对角），
    对角始终 1.0，绝不返回随机数。
    """
    frame = pd.DataFrame({s: returns_by_symbol[s] for s in symbols}).dropna()
    raw = frame.corr(method="pearson")

    matrix: dict[str, dict[str, float]] = {}
    for row in symbols:
        matrix[row] = {}
        for col in symbols:
            if row == col:
                matrix[row][col] = 1.0
                continue
            try:
                value = float(raw.loc[row, col])
            except (KeyError, TypeError, ValueError):
                value = 0.0
            if not math.isfinite(value):
                value = 0.0
            matrix[row][col] = value

    # 强制对称：取上下三角均值后夹紧，消除浮点不对称
    for i, row in enumerate(symbols):
        for col in symbols[i + 1 :]:
            averaged = (matrix[row][col] + matrix[col][row]) / 2.0
            clamped = max(-1.0, min(1.0, averaged))
            rounded = round(clamped, 6)
            matrix[row][col] = rounded
            matrix[col][row] = rounded
    return matrix


def compare_symbols(
    symbols: list[str],
    *,
    start: date | str | None = None,
    end: date | str | None = None,
    data_mode: str = "auto",
    today: date | None = None,
    fetcher=fetch_price_history,
) -> ComparisonResult:
    """获取每个标的的行情、对齐共同交易日、计算真实指标与相关矩阵。

    单个标的失败不会污染其它标的：失败原因记入 ``failed_symbols``，
    剩余标的继续计算。全部失败时抛 ``ComparisonError``。
    """
    mode = normalize_data_mode(data_mode)
    normalized = [s.strip().upper() for s in symbols if s and s.strip()]
    unique_symbols = list(dict.fromkeys(normalized))
    if len(unique_symbols) < 2:
        raise ComparisonError("多股对比至少需要 2 个不同的证券代码")

    window: DateWindow = build_window(start, end, warmup_trading_days=0, today=today)

    frames: dict[str, pd.DataFrame] = {}
    outcomes: dict[str, FetchOutcome] = {}
    failed: dict[str, str] = {}

    for symbol in unique_symbols:
        try:
            outcome = fetcher(
                symbol,
                data_mode=mode,
                start=window.fetch_start,
                end=window.end,
            )
            in_window, _ = split_warmup(outcome.frame, window)
            if in_window.empty:
                raise ComparisonError(f"{symbol} 在请求区间内没有交易数据")
            assert_dates_in_window(in_window["date"], window, label=f"{symbol} 行情")
            frames[symbol] = in_window
            outcomes[symbol] = outcome
        except Exception as exc:  # noqa: BLE001 - 单标的失败必须隔离
            failed[symbol] = f"{type(exc).__name__}: {exc}"

    if len(frames) < 2:
        detail = "；".join(f"{k} → {v}" for k, v in failed.items()) or "无可用标的"
        raise ComparisonError(f"可用标的不足 2 个，无法对比。失败详情：{detail}")

    ok_symbols = [s for s in unique_symbols if s in frames]

    # ── 对齐共同交易日 ────────────────────────────────────────────────────
    common: set[date] | None = None
    for symbol in ok_symbols:
        dates = set(frames[symbol]["date"])
        common = dates if common is None else (common & dates)
    common_dates = sorted(common or set())
    if len(common_dates) < MIN_COMMON_TRADING_DAYS:
        raise ComparisonError(
            f"共同交易日不足（{len(common_dates)} 天，至少需要 "
            f"{MIN_COMMON_TRADING_DAYS} 天），无法计算收益与相关性"
        )
    assert_dates_in_window(common_dates, window, label="共同交易日")

    aligned: dict[str, pd.DataFrame] = {}
    returns_by_symbol: dict[str, pd.Series] = {}
    for symbol in ok_symbols:
        frame = frames[symbol]
        subset = (
            frame[frame["date"].isin(common_dates)]
            .sort_values("date")
            .reset_index(drop=True)
        )
        aligned[symbol] = subset
        series = subset["close"].astype(float)
        series.index = pd.Index(subset["date"], name="date")
        returns_by_symbol[symbol] = series.pct_change().dropna()

    stocks: list[SymbolMetrics] = []
    for symbol in ok_symbols:
        subset = aligned[symbol]
        closes = subset["close"].astype(float).reset_index(drop=True)
        returns = returns_by_symbol[symbol]
        base = float(closes.iloc[0])
        normalized_returns = [
            round((float(c) / base - 1.0) * 100.0, 4) for c in closes
        ] if base else [0.0 for _ in closes]

        # Sharpe 复用 backtesting._compute_sharpe，保证与回测页同一口径
        # （日超额收益均值 / 日标准差 × sqrt(252)，无风险利率 2% 几何折算）。
        sharpe = _compute_sharpe(list(returns.astype(float)))
        up_days = int((returns > 0).sum())
        name_series = subset["name"] if "name" in subset.columns else None

        outcome = outcomes[symbol]
        stocks.append(
            SymbolMetrics(
                symbol=symbol,
                name=str(name_series.iloc[-1]) if name_series is not None else symbol,
                trading_days=len(subset),
                latest_close=round(float(closes.iloc[-1]), 4),
                total_return_pct=round((float(closes.iloc[-1]) / base - 1.0) * 100.0, 4)
                if base
                else 0.0,
                annualized_volatility_pct=round(_annualized_volatility_pct(returns), 4),
                sharpe_ratio=round(sharpe, 4),
                max_drawdown_pct=round(_max_drawdown_pct(closes), 4),
                up_day_ratio_pct=round(up_days / len(returns) * 100.0, 2)
                if len(returns)
                else 0.0,
                normalized_returns=normalized_returns,
                source=outcome.source,
                updated_at=outcome.updated_at,
                data_status=outcome.data_status,
                fallback_reason=outcome.fallback_reason,
            )
        )

    matrix = correlation_matrix_from_returns(returns_by_symbol, ok_symbols)

    statuses = [outcomes[s].data_status for s in ok_symbols]
    aggregate_status = combine_statuses(statuses) if statuses else STATUS_UNAVAILABLE
    reasons = [
        f"{s}: {outcomes[s].fallback_reason}"
        for s in ok_symbols
        if outcomes[s].fallback_reason
    ]
    reasons += [f"{s}: {msg}" for s, msg in failed.items()]

    sources = sorted({outcomes[s].source for s in ok_symbols})
    updated_ats = sorted({outcomes[s].updated_at for s in ok_symbols})

    return ComparisonResult(
        symbols=ok_symbols,
        dates=[d.isoformat() for d in common_dates],
        trading_days=len(common_dates),
        stocks=stocks,
        correlation_matrix=matrix,
        failed_symbols=failed,
        source=" | ".join(sources),
        updated_at=updated_ats[-1] if updated_ats else "",
        data_status=aggregate_status,
        fallback_reason=" | ".join(reasons) if reasons else None,
    )
