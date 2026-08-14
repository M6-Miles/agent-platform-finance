"""
连续仓位回测引擎（Fractional-Position Backtest）
================================================
为什么需要新引擎，而不是改 ``backtesting.run_backtest``
--------------------------------------------------
原引擎是 **0/1 二值仓位**：``in_position`` 是布尔量，买入即满仓。
多因子策略需要 **连续仓位**（波动率目标定仓会给出 0.37 这样的仓位），
二值引擎无法表达。同时本轮要求"原 MA 策略必须完整保留作为 baseline"，
所以不动 ``run_backtest`` 一个字节，改为新增一个并行引擎。

Sharpe 公式的同一性是**结构性保证**，不是口头承诺
--------------------------------------------------
本模块 ``from .backtesting import _compute_sharpe`` —— 直接复用原函数对象，
而不是抄一遍公式。跨模块导入下划线名通常不推荐，但此处正是要点：
只要原公式改动，本模块自动跟随；反之本模块**无法**偷偷用另一套公式。
年化天数 ``_TRADING_DAYS_PER_YEAR``、无风险利率 ``_RISK_FREE_RATE``
同理一并导入。任何审计者可以用一行 ``is`` 断言验证（见测试）。

执行时序：t 决策，t+1 开盘执行
-----------------------------
``target_positions[t]`` 是"用截至 t（含 t）的数据算出的目标仓位"。
它最早在 **t+1 开盘**成交。第 i 天（i≥1）按**资金账户**逐步推进：

1. 上一日收盘净值 ``E_prev``，以及上一日**收盘时的实际仓位** ``w_prev``；
2. 拆成股票 ``E_prev*w_prev`` 与现金 ``E_prev*(1-w_prev)``；
3. 隔夜段 ``close[i-1] → open[i]``：只有**股票部分**承担 ``r_open``，
   得到开盘净值 ``E_open = E_prev * (1 + w_prev*r_open)``；
4. 隔夜涨跌会让仓位**漂移**：``w_drift = 股票市值 / E_open``（≠ w_prev）；
5. 开盘调仓到目标 ``w_target = target[i-1]``。由于费用会同时减少净值，
   实际成交额通过资金恒等式求解，而不是简单使用目标权重差乘开盘净值；
6. 按**实际成交额**扣滑点、佣金，卖出方向另扣印花税；
7. 盘中段 ``open[i] → close[i]``：调仓后的目标仓位承担 ``r_close``；
8. 得到收盘净值，并算出**收盘时的实际仓位**（盘中涨跌同样造成漂移），
   作为下一日的 ``w_prev``。

关键：两段收益必须**复合**，不能相加
----------------------------------
本模块 2026-08-10 修复了一处实质错误。原实现为::

    gross_ret = w_prev * r_open + w_new * r_close      # 错误：加法

满仓、无成本时，前收 100 → 开盘 110 → 收盘 121，加法给 0.10+0.10 = 0.20，
而真实日收益是 ``121/100 - 1 = 0.21``。差的正是隔夜与盘中的交乘项
``r_open * r_close``。修复后按净值逐段推进，天然复合。

由此得到一条可机器校验的不变量（见 ``test_full_position_no_cost_compounds``）：

    满仓、无成本、目标仓位不变时，日收益 ≡ close_t / close_{t-1} - 1

同时，换手**必须**按漂移后仓位 ``|w_target - w_drift|`` 计，而不是
``|w_target - w_prev|``：隔夜大涨后即使目标仓位没变，也需要卖出一部分才能
回到目标权重，那笔交易是真实发生的，必须计入成本。

成本模型
-------
与原引擎口径一致：滑点、佣金均为**单边费率**，按开盘时的**实际成交金额**计费。
印花税 ``stamp_duty_pct`` 默认 **0.0**，与原引擎保持完全一致 ——
原引擎不含印花税，若这里默认开启，baseline 与多因子的成本口径就不再可比。
需要时可显式传入（A 股卖出单边 0.05%）。

设开盘净值为 ``E``、开盘股票市值为 ``S``、目标权重为 ``t``。买入费率为
``c_buy``、卖出费率为 ``c_sell``，则实际成交额 ``q`` 为：

``buy:  q = (t*E - S) / (1 + t*c_buy)``

``sell: q = (S - t*E) / (1 - t*c_sell)``

由此可同时保证成交额等于股票市值变化、费用按实际成交额收取，并且调仓后
``stock_after / equity_after_cost == t``，不会产生负现金或隐性杠杆。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# ── 关键：复用原引擎的公式与常量，不重新实现 ──────────────────────────
from .backtesting import (
    _RISK_FREE_RATE,
    _TRADING_DAYS_PER_YEAR,
    _compute_max_drawdown,
    _compute_sharpe,
)

logger = logging.getLogger(__name__)

SHARPE_TARGET = 0.5     # 只是引用既有验收阈值，不在此处重新定义口径


@dataclass(slots=True)
class PositionEpisode:
    """一段连续持仓（w>0）的往返，用于统计交易次数与胜率。"""

    entry_date: str
    exit_date: str
    n_days: int
    peak_weight: float
    equity_growth_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "n_days": self.n_days,
            "peak_weight": round(self.peak_weight, 4),
            "equity_growth_pct": round(self.equity_growth_pct, 4),
        }


@dataclass(slots=True)
class PositionBacktestResult:
    """连续仓位回测结果。字段覆盖要求七的全部报告项。"""

    strategy: str
    symbol: str
    start_date: str
    end_date: str
    data_source: str

    total_return_pct: float
    annualized_return_pct: float
    sharpe_calendar: float
    sharpe_in_position: float
    annualized_volatility_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    total_trades: int

    total_turnover: float            # Σ(实际成交额 / 当日开盘净值)
    annualized_turnover: float
    commission_cost: float           # 货币金额
    slippage_cost: float
    stamp_duty_cost: float
    total_cost: float

    time_in_market_pct: float
    avg_position: float
    max_position: float
    min_position: float
    n_days: int
    initial_capital: float

    equity_curve: list[float] = field(default_factory=list)
    calendar_returns: list[float] = field(default_factory=list)
    episodes: list[PositionEpisode] = field(default_factory=list)
    # 逐日明细：让审计方能**独立重建**净值与成本（要求九第 5 条）
    daily_detail: list[dict[str, Any]] = field(default_factory=list)
    unavailable_reason: str | None = None

    @property
    def meets_sharpe_target(self) -> bool:
        """是否达到既有验收阈值 0.5（用日历口径 = 可实现口径）。"""
        return self.sharpe_calendar >= SHARPE_TARGET

    def verdict(self) -> str:
        """报告用判定串。未达标必须显式输出 BELOW 0.5，不允许美化。"""
        return "PASS" if self.meets_sharpe_target else "BELOW 0.5"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "data_source": self.data_source,
            "n_days": self.n_days,
            "total_return_pct": round(self.total_return_pct, 4),
            "annualized_return_pct": round(self.annualized_return_pct, 4),
            "sharpe_calendar": round(self.sharpe_calendar, 4),
            "sharpe_in_position": round(self.sharpe_in_position, 4),
            "annualized_volatility_pct": round(self.annualized_volatility_pct, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "win_rate_pct": round(self.win_rate_pct, 2),
            "total_trades": self.total_trades,
            "total_turnover": round(self.total_turnover, 4),
            "annualized_turnover": round(self.annualized_turnover, 4),
            "commission_cost": round(self.commission_cost, 2),
            "slippage_cost": round(self.slippage_cost, 2),
            "stamp_duty_cost": round(self.stamp_duty_cost, 2),
            "total_cost": round(self.total_cost, 2),
            "time_in_market_pct": round(self.time_in_market_pct, 2),
            "avg_position": round(self.avg_position, 4),
            "max_position": round(self.max_position, 4),
            "min_position": round(self.min_position, 4),
            "meets_sharpe_target": self.meets_sharpe_target,
            "verdict": self.verdict(),
            "unavailable_reason": self.unavailable_reason,
        }


def _normalize_targets(
    dates: list[str],
    target_positions: "pd.Series | dict[str, float] | list[float]",
) -> list[float]:
    """
    把目标仓位对齐到日期序列，并强制夹到 [0, 1]。

    夹取而非报错：上游可能因浮点误差给出 1.0000000002。但**负仓位与杠杆
    在此被无条件截断** —— 要求五明确禁止融资、负仓位与超过 100% 的仓位。
    缺失日期视为 0（空仓），不做前向填充：没算出信号就是没有仓位。
    """
    if isinstance(target_positions, (pd.Series, dict)):
        items = (
            target_positions.items()
            if isinstance(target_positions, dict)
            else target_positions.items()
        )
        raw = {str(k): float(v) for k, v in items if pd.notna(v)}
        matched = sum(1 for d in dates if d in raw)
        if raw and matched == 0:
            # 一个位置索引的 Series（键 "0","1",...）会在这里一个日期都对不上，
            # 于是"策略全程空仓、收益 0、换手 0"—— 一个看起来正常、实则完全错误
            # 的结果。静默返回零仓位等于伪造出一个假的 baseline，必须炸掉。
            raise ValueError(
                f"target_positions 的键与价格日期完全不匹配（{len(raw)} 个键，0 个命中）。"
                f"示例键={list(raw)[:3]}，示例日期={dates[:3]}。"
                f"若传的是按位置索引的 Series，请改传 list 或改成按日期索引。"
            )
        seq = [raw.get(d, 0.0) for d in dates]
    else:
        seq = [0.0 if pd.isna(v) else float(v) for v in target_positions]
        if len(seq) != len(dates):
            raise ValueError(
                f"target_positions 长度 {len(seq)} 与价格行数 {len(dates)} 不一致"
            )
    out: list[float] = []
    for v in seq:
        if math.isnan(v):
            v = 0.0
        out.append(min(1.0, max(0.0, v)))
    return out


def run_position_backtest(
    symbol: str,
    price_df: pd.DataFrame,
    target_positions: "pd.Series | dict[str, float] | list[float]",
    *,
    strategy: str = "position",
    data_source: str = "",
    initial_capital: float = 1_000_000.0,
    slippage_pct: float = 0.1,
    commission_pct: float = 0.03,
    stamp_duty_pct: float = 0.0,
    risk_free_rate: float = _RISK_FREE_RATE,
) -> PositionBacktestResult:
    """
    连续仓位多头回测。

    参数
    ----
    target_positions
        日期 → 目标仓位（0~1）。语义是"用截至该日收盘的信息决定的仓位"，
        **次日开盘生效**。
    slippage_pct / commission_pct / stamp_duty_pct
        单边费率（百分数）。印花税只在卖出方向计。

    返回
    ----
    :class:`PositionBacktestResult`；数据不足 2 行时返回 ``unavailable_reason``
    而非抛异常（便于 walk-forward 逐折汇总），但**不会**返回伪造的指标。
    """
    df = price_df.copy()
    if "date" not in df.columns or "close" not in df.columns:
        raise ValueError("price_df 必须含 date 与 close 列")
    df["date"] = df["date"].astype(str)
    df = df.sort_values("date", kind="mergesort").reset_index(drop=True)

    dates = [str(d) for d in df["date"].tolist()]
    n = len(dates)
    if n < 2:
        return PositionBacktestResult(
            strategy=strategy, symbol=symbol,
            start_date=dates[0] if dates else "", end_date=dates[-1] if dates else "",
            data_source=data_source,
            total_return_pct=0.0, annualized_return_pct=0.0,
            sharpe_calendar=0.0, sharpe_in_position=0.0,
            annualized_volatility_pct=0.0, max_drawdown_pct=0.0,
            win_rate_pct=0.0, total_trades=0,
            total_turnover=0.0, annualized_turnover=0.0,
            commission_cost=0.0, slippage_cost=0.0, stamp_duty_cost=0.0,
            total_cost=0.0, time_in_market_pct=0.0,
            avg_position=0.0, max_position=0.0, min_position=0.0,
            n_days=n, initial_capital=initial_capital,
            unavailable_reason=f"样本不足：仅 {n} 个交易日，无法计算收益序列",
        )

    close = [float(v) for v in df["close"].tolist()]
    has_open = "open" in df.columns
    open_ = [float(v) for v in df["open"].tolist()] if has_open else list(close)

    targets = _normalize_targets(dates, target_positions)

    slip = slippage_pct / 100.0
    comm = commission_pct / 100.0
    stamp = stamp_duty_pct / 100.0
    if initial_capital <= 0.0:
        raise ValueError("initial_capital 必须大于 0")
    if any(rate < 0.0 for rate in (slip, comm, stamp)):
        raise ValueError("滑点、佣金和印花税费率不得为负")
    if slip + comm + stamp >= 1.0:
        raise ValueError("滑点、佣金和印花税合计费率必须小于 100%")

    equity = initial_capital
    equity_curve: list[float] = [1.0]
    calendar_returns: list[float] = []
    in_position_returns: list[float] = []
    daily_detail: list[dict[str, Any]] = []

    commission_cost = 0.0
    slippage_cost = 0.0
    stamp_cost = 0.0
    total_turnover = 0.0

    weights_held: list[float] = []
    w_prev = 0.0

    episodes: list[PositionEpisode] = []
    ep_start_idx: int | None = None
    ep_start_equity = equity
    ep_peak_w = 0.0

    for i in range(1, n):
        # 关键一行：第 i 天生效的目标仓位来自第 i-1 天 → 信号右移一天。
        w_target = targets[i - 1]

        prev_close = close[i - 1]
        o = open_[i]
        c = close[i]

        # 价格非正时该段收益视为 0，避免除零把净值炸成 inf
        r_open = (o - prev_close) / prev_close if prev_close > 0 else 0.0
        r_close = (c - o) / o if o > 0 else 0.0

        equity_before = equity

        # ── 1. 隔夜：只有旧仓位的股票部分承担 r_open ──────────────────────
        stock_prev = equity_before * w_prev
        cash_prev = equity_before - stock_prev
        stock_at_open = stock_prev * (1.0 + r_open)
        equity_open = stock_at_open + cash_prev

        if equity_open <= 0.0:
            # 净值归零/为负：不再交易，如实记录并终止推进，
            # 不用夹取把它伪装成"还活着"。
            logger.warning("%s 第 %d 日净值不为正（%.4f），停止推进",
                           symbol, i, equity_open)
            equity = 0.0
            calendar_returns.append(-1.0)
            equity_curve.append(0.0)
            weights_held.append(0.0)
            w_prev = 0.0
            break

        # ── 2. 隔夜涨跌导致仓位漂移 ──────────────────────────────────────
        w_drift = stock_at_open / equity_open

        # ── 3. 开盘调仓：按资金恒等式求实际成交额 ────────────────────────
        # 费用会降低调仓后的净值，因此 |目标权重-漂移权重|*E_open 只是
        # 未考虑费用的名义缺口，并不等于实际股票成交额。
        target_weight_gap = abs(w_target - w_drift)
        target_stock_before_cost = w_target * equity_open
        if target_stock_before_cost > stock_at_open:
            trade_side = "buy"
            trade_rate = slip + comm
            trade_notional = (
                (target_stock_before_cost - stock_at_open)
                / (1.0 + w_target * trade_rate)
            )
            sell_notional = 0.0
            stock_after = stock_at_open + trade_notional
        elif target_stock_before_cost < stock_at_open:
            trade_side = "sell"
            trade_rate = slip + comm + stamp
            denominator = 1.0 - w_target * trade_rate
            if denominator <= 0.0:
                raise ValueError("卖出成本导致调仓方程无有效解")
            trade_notional = (
                (stock_at_open - target_stock_before_cost) / denominator
            )
            sell_notional = trade_notional
            stock_after = stock_at_open - trade_notional
        else:
            trade_side = "none"
            trade_notional = 0.0
            sell_notional = 0.0
            stock_after = stock_at_open

        cost_slip_cash = trade_notional * slip
        cost_comm_cash = trade_notional * comm
        cost_stamp_cash = sell_notional * stamp
        cost_cash = cost_slip_cash + cost_comm_cash + cost_stamp_cash

        # ── 4. 按实际成交和费用更新资金账户 ─────────────────────────────
        equity_after_cost = equity_open - cost_cash
        cash_after = equity_after_cost - stock_after
        if equity_after_cost <= 0.0:
            raise ValueError("交易成本导致调仓后净值不为正")
        tolerance = max(1e-8, equity_open * 1e-12)
        if stock_after < -tolerance or cash_after < -tolerance:
            raise RuntimeError("调仓产生负股票资产或负现金")
        stock_after = max(0.0, stock_after)
        cash_after = max(0.0, cash_after)
        w_after_trade = stock_after / equity_after_cost
        if not math.isclose(w_after_trade, w_target, rel_tol=1e-10, abs_tol=1e-12):
            raise RuntimeError("调仓后的实际仓位与目标仓位不一致")

        # ── 5. 盘中：调仓后的目标仓位承担 r_close ────────────────────────
        stock_close = stock_after * (1.0 + r_close)
        equity = equity_after_cost + stock_after * r_close

        net_ret = equity / equity_before - 1.0 if equity_before > 0 else 0.0
        # 无成本反事实收益：用于分离"市场贡献"与"成本拖累"
        gross_equity = equity_open + (equity_open * w_target) * r_close
        gross_ret = gross_equity / equity_before - 1.0 if equity_before > 0 else 0.0

        commission_cost += cost_comm_cash
        slippage_cost += cost_slip_cash
        stamp_cost += cost_stamp_cash
        turnover_w = trade_notional / equity_open
        total_turnover += turnover_w

        calendar_returns.append(net_ret)
        if w_target > 0.0 or w_prev > 0.0:
            in_position_returns.append(net_ret)
        equity_curve.append(equity / initial_capital)
        # ── 6. 盘中涨跌同样造成漂移 → 收盘时的实际仓位 ──────────────────
        # 这是下一日隔夜段的起点。若直接沿用 w_target，隔盘漂移就被抹掉，
        # 换手会被系统性低估。
        # 夹到 [0,1]：满仓时 stock_close/equity 的浮点误差可能给出
        # 1.0000000000000002，让"仓位不得超过 100%"的硬约束在下一轮失效。
        w_close = stock_close / equity if equity > 0 else 0.0
        w_close = min(1.0, max(0.0, w_close))
        weights_held.append(w_close)

        daily_detail.append({
            "i": float(i),
            "trade_side": trade_side,
            "w_prev": w_prev,
            "w_drift": w_drift,
            "w_target": w_target,
            "target_weight_gap": target_weight_gap,
            "w_after_trade": w_after_trade,
            "w_close": w_close,
            "r_open": r_open,
            "r_close": r_close,
            "gross_ret": gross_ret,
            "turnover": turnover_w,
            "trade_notional": trade_notional,
            "sell_notional": sell_notional,
            "stock_at_open": stock_at_open,
            "stock_after": stock_after,
            "cash_after": cash_after,
            "cost_slippage": cost_slip_cash,
            "cost_commission": cost_comm_cash,
            "cost_stamp": cost_stamp_cash,
            "cost_cash": cost_cash,
            "net_ret": net_ret,
            "equity_before": equity_before,
            "equity_open": equity_open,
            "equity_after_cost": equity_after_cost,
            "equity_after": equity,
        })
        w_prev = w_close

        # 持仓段落统计（目标仓位 > 0 的连续区间）
        if w_target > 0.0 and ep_start_idx is None:
            ep_start_idx = i
            ep_start_equity = equity_before
            ep_peak_w = w_target
        elif w_target > 0.0 and ep_start_idx is not None:
            ep_peak_w = max(ep_peak_w, w_target)
        elif w_target <= 0.0 and ep_start_idx is not None:
            episodes.append(PositionEpisode(
                entry_date=dates[ep_start_idx],
                exit_date=dates[i],
                n_days=i - ep_start_idx + 1,
                peak_weight=ep_peak_w,
                equity_growth_pct=(equity / ep_start_equity - 1.0) * 100.0
                if ep_start_equity > 0 else 0.0,
            ))
            ep_start_idx = None
            ep_peak_w = 0.0

    if ep_start_idx is not None:
        episodes.append(PositionEpisode(
            entry_date=dates[ep_start_idx],
            exit_date=dates[-1],
            n_days=n - ep_start_idx,
            peak_weight=ep_peak_w,
            equity_growth_pct=(equity / ep_start_equity - 1.0) * 100.0
            if ep_start_equity > 0 else 0.0,
        ))

    total_return = (equity / initial_capital - 1.0) * 100.0
    annualized = ((equity / initial_capital) ** (_TRADING_DAYS_PER_YEAR / n) - 1.0) * 100.0

    if len(calendar_returns) >= 2:
        m = sum(calendar_returns) / len(calendar_returns)
        var = sum((r - m) ** 2 for r in calendar_returns) / (len(calendar_returns) - 1)
        ann_vol = math.sqrt(var) * math.sqrt(_TRADING_DAYS_PER_YEAR) * 100.0
    else:
        ann_vol = 0.0

    # 同一个 _compute_sharpe，同一个年化方式；调用方无法替换
    sharpe_cal = _compute_sharpe(calendar_returns, risk_free_rate)
    sharpe_pos = _compute_sharpe(in_position_returns, risk_free_rate)
    max_dd = _compute_max_drawdown(equity_curve)

    wins = [e for e in episodes if e.equity_growth_pct > 0]
    win_rate = len(wins) / len(episodes) * 100.0 if episodes else 0.0

    days_in_market = sum(1 for w in weights_held if w > 0.0)
    years = n / _TRADING_DAYS_PER_YEAR

    return PositionBacktestResult(
        strategy=strategy,
        symbol=symbol,
        start_date=dates[0],
        end_date=dates[-1],
        data_source=data_source,
        total_return_pct=total_return,
        annualized_return_pct=annualized,
        sharpe_calendar=sharpe_cal,
        sharpe_in_position=sharpe_pos,
        annualized_volatility_pct=ann_vol,
        max_drawdown_pct=max_dd,
        win_rate_pct=win_rate,
        total_trades=len(episodes),
        total_turnover=total_turnover,
        annualized_turnover=total_turnover / years if years > 0 else 0.0,
        commission_cost=commission_cost,
        slippage_cost=slippage_cost,
        stamp_duty_cost=stamp_cost,
        total_cost=commission_cost + slippage_cost + stamp_cost,
        time_in_market_pct=days_in_market / len(weights_held) * 100.0
        if weights_held else 0.0,
        avg_position=sum(weights_held) / len(weights_held) if weights_held else 0.0,
        max_position=max(weights_held) if weights_held else 0.0,
        min_position=min(weights_held) if weights_held else 0.0,
        n_days=n,
        initial_capital=initial_capital,
        equity_curve=equity_curve,
        calendar_returns=calendar_returns,
        episodes=episodes,
        daily_detail=daily_detail,
    )


def buy_and_hold_benchmark(
    symbol: str,
    price_df: pd.DataFrame,
    *,
    data_source: str = "",
    initial_capital: float = 1_000_000.0,
    slippage_pct: float = 0.1,
    commission_pct: float = 0.03,
    stamp_duty_pct: float = 0.0,
) -> PositionBacktestResult:
    """
    买入持有基准。

    走**同一个引擎、同一段价格、同一套费率** —— 这是要求八"基准必须用相同
    日期区间与相同价格序列"的实现方式：基准与策略共享 ``price_df``，
    不可能对不同区间比较。首日即满仓（含一次建仓成本），此后不再调仓。
    """
    n = len(price_df)
    targets = [1.0] * n
    return run_position_backtest(
        symbol, price_df, targets,
        strategy="buy_and_hold",
        data_source=data_source,
        initial_capital=initial_capital,
        slippage_pct=slippage_pct,
        commission_pct=commission_pct,
        stamp_duty_pct=stamp_duty_pct,
    )
