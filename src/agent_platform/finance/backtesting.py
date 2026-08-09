"""
回测引擎（Backtesting Engine）
================================
基于历史价格数据对交易信号进行事后验证：
  - 计算 Sharpe 比率、最大回撤、胜率、年化收益、滑点成本
  - 输入：SignalSeries（日期 + signal:buy/sell/hold）
  - 输出：BacktestResult（含详细 equity 曲线）
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_RISK_FREE_RATE = 0.02          # 年化无风险利率（默认 2%）
_TRADING_DAYS_PER_YEAR = 252


@dataclass
class BacktestTrade:
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    signal: str      # buy / sell / hold
    return_pct: float
    profit_loss: float


@dataclass
class BacktestResult:
    symbol: str
    start_date: str
    end_date: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    total_return_pct: float
    annualized_return_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    avg_slippage_pct: float
    trades: list[BacktestTrade]
    equity_curve: list[float]      # 归一化净值序列（初始=1.0）
    source: str = "backtesting"

    # ── 日历口径指标（2026-08-06 新增，与上面的持仓口径并存）──────────────────
    # 背景：sharpe_ratio / annualized_volatility_pct 只统计**持仓日**收益，
    # 却按 sqrt(252) 年化，等于假装全年在市，系统性放大夏普约 1/sqrt(时间在市)。
    # 下面三个字段按**日历交易日**统计（空仓日收益记 0），是可实现口径。
    # 刻意并存而非替换：历史记录与文档引用的都是持仓口径，替换会让旧数字不可追溯。
    # 注：空仓日记 0 表示"空仓期资金收益按 0% 计"（未计入现金的无风险收益），
    #     对策略略偏保守；这是长期只做多策略的常规处理。
    sharpe_calendar: float = 0.0
    annualized_volatility_calendar_pct: float = 0.0
    time_in_market_pct: float = 0.0     # 持仓日 / 交易日

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": round(self.win_rate_pct, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "annualized_return_pct": round(self.annualized_return_pct, 2),
            "annualized_volatility_pct": round(self.annualized_volatility_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "avg_slippage_pct": round(self.avg_slippage_pct, 3),
            "sharpe_calendar": round(self.sharpe_calendar, 3),
            "annualized_volatility_calendar_pct": round(self.annualized_volatility_calendar_pct, 2),
            "time_in_market_pct": round(self.time_in_market_pct, 2),
            "source": self.source,
        }

    def to_markdown(self) -> str:
        # 达标判定用日历口径（可实现口径），不用被时间在市放大的持仓口径
        sharpe_note = "✅ 满足目标" if self.sharpe_calendar >= 0.5 else "⚠️ 低于目标(0.5)"
        return "\n".join([
            f"### 回测结果 — {self.symbol}",
            f"- 回测区间：{self.start_date} 至 {self.end_date}",
            "",
            "**收益指标**",
            f"- 总收益率：{self.total_return_pct:.2f}%",
            f"- 年化收益率：{self.annualized_return_pct:.2f}%",
            f"- 年化波动率：{self.annualized_volatility_pct:.2f}%（持仓日口径）",
            f"- Sharpe 比率（日历口径）：{self.sharpe_calendar:.3f}  {sharpe_note}",
            f"- Sharpe 比率（持仓日口径）：{self.sharpe_ratio:.3f}"
            f"　时间在市 {self.time_in_market_pct:.1f}%",
            f"- 最大回撤：{self.max_drawdown_pct:.2f}%",
            "",
            "**交易统计**",
            f"- 总交易次数：{self.total_trades}",
            f"- 胜率：{self.win_rate_pct:.1f}%（{self.winning_trades}胜/{self.losing_trades}负）",
            f"- 平均滑点：{self.avg_slippage_pct:.3f}%",
        ])


def _compute_sharpe(daily_returns: list[float], risk_free_rate: float = _RISK_FREE_RATE) -> float:
    """计算年化 Sharpe 比率（日度收益率序列）。"""
    if len(daily_returns) < 2:
        return 0.0
    mean_r = sum(daily_returns) / len(daily_returns)
    variance = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    std_r = math.sqrt(variance) if variance > 0 else 0.0
    if std_r == 0:
        return 0.0
    daily_rf = (1 + risk_free_rate) ** (1 / _TRADING_DAYS_PER_YEAR) - 1
    return (mean_r - daily_rf) / std_r * math.sqrt(_TRADING_DAYS_PER_YEAR)


def _compute_max_drawdown(equity_curve: list[float]) -> float:
    """返回最大回撤（百分比，正数）。"""
    max_dd = 0.0
    peak = equity_curve[0] if equity_curve else 1.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def run_backtest(
    symbol: str,
    price_df: pd.DataFrame,
    signals: "pd.Series | dict | list",  # index/key=date str or date, values: "buy"/"sell"/"hold"
    initial_capital: float = 1_000_000.0,
    slippage_pct: float = 0.1,      # 单边滑点 0.1%
    commission_pct: float = 0.03,   # 单边佣金 0.03%
    trailing_stop_pct: float = 0.0, # 追踪止损；0.0=关闭，例如 0.05=持仓峰值回落5%强制平仓
) -> BacktestResult:
    """
    简单多头回测：
    - buy  → 第二天开盘价（含滑点）买入，持有至下一个 sell/hold 信号
    - sell → 第二天开盘价（含滑点）卖出
    - hold → 维持仓位

    signals 支持三种格式：
      - pd.Series / dict: {date | str → "buy"/"sell"/"hold"}
      - list of (date_str, signal) tuples
    """
    # 统一 signals 为 dict[date, str]
    if isinstance(signals, list):
        _sig_dict: dict = {}
        for item in signals:
            k, v = item
            if isinstance(k, str):
                k = date.fromisoformat(k)
            _sig_dict[k] = v
        signals = _sig_dict  # type: ignore[assignment]
    elif hasattr(signals, "to_dict"):  # pd.Series
        signals = {
            (k if hasattr(k, "year") else date.fromisoformat(str(k))): v
            for k, v in signals.items()
        }
    elif isinstance(signals, dict):
        signals = {
            (k if hasattr(k, "year") else date.fromisoformat(str(k))): v
            for k, v in signals.items()
        }

    df = price_df.copy()
    df = df.sort_values("date").reset_index(drop=True)

    trades: list[BacktestTrade] = []
    equity = initial_capital
    equity_curve: list[float] = [1.0]
    daily_returns: list[float] = []
    calendar_returns: list[float] = []   # 每个交易日一项，空仓日记 0.0

    in_position = False
    entry_price = 0.0
    entry_date: date | None = None
    peak_close = 0.0        # 追踪止损：持仓期间的最高收盘价

    slippage_cost = slippage_pct / 100.0
    commission_cost = commission_pct / 100.0

    for i, row in df.iterrows():
        current_date = row["date"] if hasattr(row["date"], "year") else date.fromisoformat(str(row["date"]))
        # 下一条的开盘价（模拟次日执行）；若无 open 列则用 close 替代
        next_row = df.iloc[min(int(i) + 1, len(df) - 1)]
        next_open = float(next_row["open"] if "open" in df.columns else next_row["close"])
        curr_close = float(row["close"])
        signal = str(signals.get(current_date, "hold"))

        # 日收益（无仓时为0）
        # 注意：daily_returns 只收集"持仓日"的收益（用于 sharpe / ann_vol），
        # 而 equity_curve 按"日历交易日"逐日记录——空仓日净值走平。
        # 两者长度不同是有意的：净值曲线必须是完整时间序列，否则回撤与
        # 逐年归因会错位。空仓日追加重复值不会产生新的峰/谷，故 max_dd 不变。
        if i > 0:
            day_ret = 0.0
            if in_position:
                prev_close = float(df.iloc[int(i) - 1]["close"])
                if prev_close > 0:
                    day_ret = (curr_close - prev_close) / prev_close
                    daily_returns.append(day_ret)
                    equity *= (1 + day_ret)
            # 日历口径：空仓日记 0.0（假设资金闲置、名义收益为零，
            # 对多头策略是保守处理）。长度恒等于交易日数-1。
            calendar_returns.append(day_ret)
            equity_curve.append(equity / initial_capital)

        # 追踪止损：更新峰值，如果回落超过阈值则覆盖为卖出信号
        if trailing_stop_pct > 0 and in_position:
            if curr_close > peak_close:
                peak_close = curr_close
            if peak_close > 0 and (peak_close - curr_close) / peak_close >= trailing_stop_pct:
                logger.debug(
                    "追踪止损触发 %s %s: 峰值=%.3f 当前=%.3f 回落=%.2f%%",
                    symbol, current_date, peak_close, curr_close,
                    (peak_close - curr_close) / peak_close * 100,
                )
                signal = "sell"     # 强制平仓

        if signal == "buy" and not in_position:
            entry_price = next_open * (1 + slippage_cost + commission_cost)
            entry_date = current_date
            in_position = True
            peak_close = curr_close     # 建仓时重置峰值基准

        elif signal == "sell" and in_position and entry_date is not None:
            exit_price = next_open * (1 - slippage_cost - commission_cost)
            ret = (exit_price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            pl = (exit_price - entry_price) * (equity / next_open)  # approximate shares
            trades.append(BacktestTrade(
                entry_date=entry_date,
                exit_date=current_date,
                entry_price=entry_price,
                exit_price=exit_price,
                signal=signal,
                return_pct=ret,
                profit_loss=pl,
            ))
            in_position = False
            entry_price = 0.0
            entry_date = None
            peak_close = 0.0    # 平仓后重置

    # 强制平仓（如果还在仓中）
    if in_position and entry_date is not None and len(df) > 0:
        last_price = float(df.iloc[-1]["close"]) * (1 - slippage_cost - commission_cost)
        ret = (last_price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
        trades.append(BacktestTrade(
            entry_date=entry_date,
            exit_date=df.iloc[-1]["date"],
            entry_price=entry_price,
            exit_price=last_price,
            signal="forced_exit",
            return_pct=ret,
            profit_loss=(last_price - entry_price) * 100,
        ))

    # 统计
    winning = [t for t in trades if t.return_pct > 0]
    losing  = [t for t in trades if t.return_pct <= 0]
    win_rate = len(winning) / len(trades) * 100.0 if trades else 0.0
    total_return = (equity / initial_capital - 1.0) * 100.0

    # 年化收益
    n_days = len(df)
    if n_days > 0:
        annualized = ((equity / initial_capital) ** (_TRADING_DAYS_PER_YEAR / n_days) - 1) * 100.0
    else:
        annualized = 0.0

    # 年化波动率
    if daily_returns:
        variance = sum((r - sum(daily_returns)/len(daily_returns))**2 for r in daily_returns) / max(len(daily_returns)-1, 1)
        ann_vol = math.sqrt(variance) * math.sqrt(_TRADING_DAYS_PER_YEAR) * 100.0
    else:
        ann_vol = 0.0

    sharpe = _compute_sharpe(daily_returns)
    max_dd = _compute_max_drawdown(equity_curve)

    # 日历口径指标（与上面的持仓日口径并列，不替换）
    # 为什么需要：上面的 sharpe / ann_vol 只统计持仓日收益，却按 sqrt(252)
    # 年化，等于假装全年在市。当时间在市 f < 100% 时，这会把夏普系统性
    # 放大约 1/sqrt(f) 倍（f=53% 时约 1.37 倍）。
    # 日历口径把空仓日按 0 收益计入，即"空仓时持现金、不赚不亏"，
    # 这才是可实现策略的真实风险调整后收益。
    if calendar_returns:
        cal_mean = sum(calendar_returns) / len(calendar_returns)
        cal_var = (sum((r - cal_mean) ** 2 for r in calendar_returns)
                   / max(len(calendar_returns) - 1, 1))
        ann_vol_cal = math.sqrt(cal_var) * math.sqrt(_TRADING_DAYS_PER_YEAR) * 100.0
    else:
        ann_vol_cal = 0.0

    sharpe_cal = _compute_sharpe(calendar_returns)
    time_in_market = (len(daily_returns) / len(calendar_returns) * 100.0
                      if calendar_returns else 0.0)

    start_str = str(df.iloc[0]["date"]) if len(df) > 0 else ""
    end_str   = str(df.iloc[-1]["date"]) if len(df) > 0 else ""

    return BacktestResult(
        symbol=symbol,
        start_date=start_str,
        end_date=end_str,
        total_trades=len(trades),
        winning_trades=len(winning),
        losing_trades=len(losing),
        win_rate_pct=win_rate,
        total_return_pct=total_return,
        annualized_return_pct=annualized,
        annualized_volatility_pct=ann_vol,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd,
        avg_slippage_pct=slippage_pct,
        trades=trades,
        equity_curve=equity_curve,
        sharpe_calendar=sharpe_cal,
        annualized_volatility_calendar_pct=ann_vol_cal,
        time_in_market_pct=time_in_market,
    )
