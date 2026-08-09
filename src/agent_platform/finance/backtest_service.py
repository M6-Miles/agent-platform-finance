"""
回测服务（Backtest Service）
============================
把"取行情 → 生成 MA5/MA20 信号 → 调用 run_backtest → 裁剪到请求区间"
这条链路从 API 层抽出来，让日期/价格不变量可以被单元测试直接验证。

关键约定
--------
* **不改回测引擎口径。** Sharpe / 波动率 / 回撤全部由
  ``finance/backtesting.py::run_backtest`` 计算，本模块只负责取数、
  造信号、以及把结果裁剪回请求区间，绝不为了"让结果好看"重算指标。
* **预热数据只喂给指标，不进结果。** MA20 需要 20 个交易日，MA5 需要 5 个。
  预热行参与均线计算与交叉判定，但：
    - 信号只在请求区间内生成（第一笔交易不会早于 start）；
    - 净值曲线只覆盖请求区间；
    - ``trading_days`` 用请求区间的实际行数。
* **历史不足时明确报错。** 请求区间 + 可用预热合计不足 MA20 所需交易日时抛
  ``InsufficientHistoryError``，而不是返回一条"看起来能用"的空结果。

信号规则（与页面文案一致的唯一实现）
------------------------------------
金叉：MA5 由 <= MA20 变为 > MA20 → buy
死叉：MA5 由 >= MA20 变为 < MA20 → sell
执行价：由 run_backtest 按"次日开盘价 + 滑点 + 佣金"决定。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from agent_platform.finance.backtesting import BacktestResult, run_backtest
from agent_platform.finance.constants import DISCLAIMER
from agent_platform.finance.data_status import (
    FetchOutcome,
    fetch_price_history,
    normalize_data_mode,
)
from agent_platform.finance.date_window import (
    MIN_TRADING_DAYS_FOR_MA20,
    DateWindow,
    InsufficientHistoryError,
    assert_dates_in_window,
    build_window,
)

MA_FAST = 5
MA_SLOW = 20

# 预热按 MA20 的两倍取，保证请求区间首日的 MA20 已经完全成熟。
WARMUP_TRADING_DAYS = MA_SLOW * 2

SUPPORTED_STRATEGIES = ("ma_crossover",)


class BacktestError(ValueError):
    """回测请求无法完成（端点转换为 HTTP 400）。"""


@dataclass(frozen=True, slots=True)
class BacktestServiceResult:
    symbol: str
    strategy: str
    start_date: str
    end_date: str
    requested_start: str | None
    requested_end: str | None
    trading_days: int
    warmup_rows_used: int
    initial_capital: float
    result: BacktestResult
    equity_curve: list[dict[str, float | str]]
    trades: list[dict[str, float | str]]
    signals: dict[date, str]
    source: str
    updated_at: str
    data_status: str
    fallback_reason: str | None
    disclaimer: str = DISCLAIMER


def build_ma_crossover_signals(frame: pd.DataFrame) -> dict[date, str]:
    """MA5/MA20 金叉死叉信号。

    使用完整 ``frame``（含预热行）计算均线，交叉判定需要前一日的均线值，
    因此预热行的存在直接决定请求区间首日能否产生信号 —— 这正是需要预热的原因。
    均线未成熟（NaN）的行不产生信号。
    """
    data = frame.sort_values("date").reset_index(drop=True)
    closes = data["close"].astype(float)
    ma_fast = closes.rolling(MA_FAST, min_periods=MA_FAST).mean()
    ma_slow = closes.rolling(MA_SLOW, min_periods=MA_SLOW).mean()

    signals: dict[date, str] = {}
    for i in range(1, len(data)):
        pf, ps = ma_fast.iloc[i - 1], ma_slow.iloc[i - 1]
        cf, cs = ma_fast.iloc[i], ma_slow.iloc[i]
        if pd.isna(pf) or pd.isna(ps) or pd.isna(cf) or pd.isna(cs):
            continue
        if pf <= ps and cf > cs:
            signals[data.iloc[i]["date"]] = "buy"
        elif pf >= ps and cf < cs:
            signals[data.iloc[i]["date"]] = "sell"
    return signals


def run_strategy_backtest(
    symbol: str,
    *,
    start: date | str | None = None,
    end: date | str | None = None,
    initial_capital: float = 1_000_000.0,
    data_mode: str = "auto",
    strategy: str = "ma_crossover",
    today: date | None = None,
    fetcher=fetch_price_history,
) -> BacktestServiceResult:
    """执行真实回测，所有交易与净值都限制在请求区间内。"""
    if strategy not in SUPPORTED_STRATEGIES:
        raise BacktestError(
            f"不支持的策略：{strategy!r}。可选值：{', '.join(SUPPORTED_STRATEGIES)}"
        )
    if initial_capital <= 0:
        raise BacktestError(f"初始资金必须为正数，收到 {initial_capital}")

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

    full, warmup_rows = split_warmup_preserving(outcome.frame, window)
    in_window = full[full["_in_window"]].drop(columns=["_in_window"]).reset_index(drop=True)

    if in_window.empty:
        raise BacktestError(f"{symbol} 在请求区间内没有交易数据")

    assert_dates_in_window(in_window["date"], window, label="回测行情")

    total_rows_for_indicators = warmup_rows + len(in_window)
    if total_rows_for_indicators < MIN_TRADING_DAYS_FOR_MA20:
        raise InsufficientHistoryError(
            f"历史数据不足以计算 MA{MA_SLOW}："
            f"请求区间 {len(in_window)} 个交易日 + 预热 {warmup_rows} 个交易日 = "
            f"{total_rows_for_indicators}，至少需要 {MIN_TRADING_DAYS_FOR_MA20}。"
            "请扩大日期区间或改用有更长历史的标的。",
            available=total_rows_for_indicators,
            required=MIN_TRADING_DAYS_FOR_MA20,
        )

    indicator_frame = full.drop(columns=["_in_window"]).reset_index(drop=True)
    all_signals = build_ma_crossover_signals(indicator_frame)

    # 信号裁剪：只保留请求区间内的日期，第一笔交易不可能早于 start
    in_window_dates = set(in_window["date"])
    signals = {d: s for d, s in all_signals.items() if d in in_window_dates}

    result = run_backtest(
        symbol=symbol,
        price_df=in_window,
        signals=signals,
        initial_capital=initial_capital,
    )

    # 净值曲线与 in_window 行一一对应（run_backtest 每个交易日 append 一次）
    dates = [d for d in in_window["date"]]
    curve_len = min(len(result.equity_curve), len(dates))
    equity_curve = [
        {
            "date": dates[i].isoformat(),
            "equity": round(result.equity_curve[i] * initial_capital, 2),
            "nav": round(result.equity_curve[i], 6),
        }
        for i in range(curve_len)
    ]

    date_index = {_as_date(value): index for index, value in enumerate(dates)}

    def _execution_date(signal_date: date, *, forced_exit: bool = False) -> date:
        normalized = _as_date(signal_date)
        if forced_exit:
            return normalized
        index = date_index[normalized]
        return _as_date(dates[min(index + 1, len(dates) - 1)])

    trades = [
        {
            "entry_signal_date": _iso(t.entry_date),
            "exit_signal_date": _iso(t.exit_date),
            "entry_date": _iso(_execution_date(t.entry_date)),
            "exit_date": _iso(
                _execution_date(t.exit_date, forced_exit=t.signal == "forced_exit")
            ),
            "entry_price": round(t.entry_price, 4),
            "exit_price": round(t.exit_price, 4),
            "return_pct": round(t.return_pct, 4),
            "profit_loss": round(t.profit_loss, 2),
            "signal": t.signal,
            "direction": "long",
        }
        for t in result.trades
    ]

    # 防御性断言：任何越界的交易日期都是缺陷
    trade_dates = [
        value
        for trade in trades
        for value in (
            trade["entry_signal_date"],
            trade["exit_signal_date"],
            trade["entry_date"],
            trade["exit_date"],
        )
    ]
    assert_dates_in_window(
        [_as_date(d) for d in trade_dates], window, label="交易日期"
    )

    return BacktestServiceResult(
        symbol=symbol,
        strategy=strategy,
        start_date=_iso(in_window.iloc[0]["date"]),
        end_date=_iso(in_window.iloc[-1]["date"]),
        requested_start=window.start.isoformat() if window.start else None,
        requested_end=window.end.isoformat() if window.end else None,
        trading_days=len(in_window),
        warmup_rows_used=warmup_rows,
        initial_capital=initial_capital,
        result=result,
        equity_curve=equity_curve,
        trades=trades,
        signals=signals,
        source=outcome.source,
        updated_at=outcome.updated_at,
        data_status=outcome.data_status,
        fallback_reason=outcome.fallback_reason,
    )


def split_warmup_preserving(
    frame: pd.DataFrame,
    window: DateWindow,
) -> tuple[pd.DataFrame, int]:
    """返回 (含预热行且带 ``_in_window`` 标记的完整数据, 预热行数)。

    与 ``date_window.split_warmup`` 的区别：这里**保留**预热行，因为均线
    需要它们；``_in_window`` 列让调用方无法误把预热行当作请求区间行返回。
    """
    from agent_platform.finance.date_window import normalize_date_column

    data = (
        normalize_date_column(frame, "date")
        .sort_values("date")
        .reset_index(drop=True)
    )
    if window.end is not None:
        data = data[data["date"] <= window.end].reset_index(drop=True)

    if window.start is None:
        data["_in_window"] = True
        return data, 0

    data["_in_window"] = data["date"] >= window.start
    warmup_rows = int((~data["_in_window"]).sum())
    return data, warmup_rows


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return pd.to_datetime(value).date()


def _iso(value) -> str:
    return _as_date(value).isoformat()
