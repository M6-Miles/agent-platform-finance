"""
日历口径回测指标测试（2026-08-06 新增）

背景
────
`BacktestResult.sharpe_ratio` / `annualized_volatility_pct` 只统计**持仓日**
收益，却按 sqrt(252) 年化，等于假装全年在市，会系统性放大夏普约
1/sqrt(时间在市)。因此新增三个日历口径字段：

  - `sharpe_calendar`
  - `annualized_volatility_calendar_pct`
  - `time_in_market_pct`

本文件锁定这三个字段的语义。测试策略：**不记录当前输出当作期望值**，
而是用独立推导的闭式值或独立重算序列做对照——记录输出对公式错误不敏感，
公式写错时它会连同错误一起被"锁定"。

关键对照点
  1. 全程持仓 ⇒ 日历口径与持仓口径**逐位相等**（最强的一条）
  2. 全程空仓 ⇒ 时间在市 0%、日历夏普 0
  3. 部分持仓 ⇒ 日历口径被稀释（同号、绝对值更小）
  4. 从 `trades` 独立重建日历收益序列，与引擎结果比对
  5. 空仓日追加重复净值不产生新峰/谷 ⇒ `max_dd` 不变
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from agent_platform.finance.backtesting import (
    run_backtest,
    BacktestResult,
    _compute_sharpe,
    _TRADING_DAYS_PER_YEAR,
)


# ═══════════════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════════════

def _mkdf(prices: list[float], start: date = date(2024, 1, 1)) -> pd.DataFrame:
    """构造价格表。日期用连续自然日即可——引擎只按行序处理，不解析交易日历。"""
    dates = [start + timedelta(days=i) for i in range(len(prices))]
    return pd.DataFrame({"date": dates, "close": [float(p) for p in prices]})


def _zigzag(n: int, base: float = 100.0, drift: float = 0.6) -> list[float]:
    """带噪声的上行价格序列：保证方差 > 0，且均值收益明显为正。

    形状固定（无随机数），因此测试结果可复现。
    """
    prices = [base]
    for i in range(1, n):
        step = drift + (1.5 if i % 3 == 0 else -0.8 if i % 3 == 1 else 0.4)
        prices.append(prices[-1] * (1 + step / 100.0))
    return prices


def _lowdrift(n: int, base: float = 100.0) -> list[float]:
    """近零漂移、正方差的价格序列（形状固定，无随机数）。

    步长按 [+1.0, -1.0, +0.8, -0.8]% 循环，每 4 天算术和恰为 0，
    因此日收益均值 ≈ 0 而标准差 ≈ 0.9%——接近真实日线的量级
    （真实A股日收益 均值/标准差 约 0.01 量级）。

    用途：只有在 μ≈0 时 vol 比值才退化为 sqrt(f)；`_zigzag` 的
    日漂移过大，不适合做该退化检验。
    """
    cycle = [1.0, -1.0, 0.8, -0.8]
    prices = [base]
    for i in range(1, n):
        prices.append(prices[-1] * (1 + cycle[(i - 1) % 4] / 100.0))
    return prices


def _calendar_returns_from_trades(df: pd.DataFrame, result: BacktestResult) -> list[float]:
    """不依赖引擎内部状态，独立重建日历收益序列。

    持仓判定规则（对照引擎实现）：`in_position` 在 entry_date 那次迭代**末尾**
    才置真，而 sell 在 exit_date 那次迭代**末尾**才置假。因此记入收益的日子是
    「entry_date 之后、exit_date 之前及当天」——即左开右闭区间。
    """
    held: set[date] = set()
    dates = [d if hasattr(d, "year") else date.fromisoformat(str(d)) for d in df["date"]]
    idx_of = {d: i for i, d in enumerate(dates)}

    for t in result.trades:
        e = t.entry_date if hasattr(t.entry_date, "year") else date.fromisoformat(str(t.entry_date))
        x = t.exit_date if hasattr(t.exit_date, "year") else date.fromisoformat(str(t.exit_date))
        i_e, i_x = idx_of[e], idx_of[x]
        for k in range(i_e + 1, i_x + 1):     # 左开右闭
            held.add(dates[k])

    out: list[float] = []
    for i in range(1, len(dates)):
        if dates[i] in held:
            prev, curr = df.iloc[i - 1]["close"], df.iloc[i]["close"]
            out.append((float(curr) - float(prev)) / float(prev))
        else:
            out.append(0.0)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 1. 全程持仓：日历口径 ≡ 持仓口径
# ═══════════════════════════════════════════════════════════════════════════

def test_always_in_market_calendar_equals_in_position():
    """第一天买入、从不卖出 ⇒ 两个口径必须逐位相等。

    这是最强的一条对照：若日历序列错位、漏项或多补了 0，等式立刻破。
    """
    prices = _zigzag(60)
    df = _mkdf(prices)
    signals = [(df.iloc[0]["date"].isoformat(), "buy")]

    r = run_backtest("T", df, signals)

    assert r.time_in_market_pct == pytest.approx(100.0, abs=1e-9)
    assert r.sharpe_calendar == pytest.approx(r.sharpe_ratio, abs=1e-12)
    assert r.annualized_volatility_calendar_pct == pytest.approx(
        r.annualized_volatility_pct, abs=1e-12
    )


def test_always_in_market_lengths():
    """净值曲线按日历交易日，长度 == 行数；日历收益长度 == 行数-1。"""
    df = _mkdf(_zigzag(45))
    r = run_backtest("T", df, [(df.iloc[0]["date"].isoformat(), "buy")])

    assert len(r.equity_curve) == len(df)
    assert r.equity_curve[0] == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# 2. 全程空仓
# ═══════════════════════════════════════════════════════════════════════════

def test_never_in_market_zero_time_and_sharpe():
    """无信号 ⇒ 时间在市 0%，日历收益全 0 ⇒ 方差 0 ⇒ 夏普 0。"""
    df = _mkdf(_zigzag(40))
    r = run_backtest("T", df, [])

    assert r.time_in_market_pct == 0.0
    assert r.sharpe_calendar == 0.0
    assert r.annualized_volatility_calendar_pct == 0.0
    # 净值仍是完整时间序列，只是走平
    assert len(r.equity_curve) == len(df)
    assert all(v == 1.0 for v in r.equity_curve)


def test_never_in_market_equity_flat_no_drawdown():
    """空仓下即使价格暴跌，净值不动 ⇒ 回撤 0。"""
    df = _mkdf([100, 90, 80, 70, 60, 50])
    r = run_backtest("T", df, [])
    assert r.max_drawdown_pct == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 3. 部分持仓：稀释效应
# ═══════════════════════════════════════════════════════════════════════════

def test_partial_exposure_dilutes_sharpe():
    """约半程在市 ⇒ 日历夏普与持仓夏普同号，但绝对值更小。

    数学上分子按 f 缩放、分母按约 sqrt(f) 缩放，故夏普按约 sqrt(f) 稀释。
    用明显正漂移的序列，使无风险利率项可忽略、符号稳定。
    """
    prices = _zigzag(61)
    df = _mkdf(prices)
    signals = [
        (df.iloc[1]["date"].isoformat(), "buy"),
        (df.iloc[30]["date"].isoformat(), "sell"),
    ]
    r = run_backtest("T", df, signals)

    assert 0.0 < r.time_in_market_pct < 100.0
    assert r.sharpe_ratio > 0                      # 上行序列，持仓口径为正
    assert r.sharpe_calendar > 0                   # 同号
    assert r.sharpe_calendar < r.sharpe_ratio      # 被稀释
    assert r.annualized_volatility_calendar_pct < r.annualized_volatility_pct


def test_time_in_market_matches_manual_count():
    """时间在市 = 持仓日/交易日，与手工计数一致。

    买入 index 1、卖出 index 30 ⇒ 记入收益的日子是 index 2..30，共 29 天；
    日历收益共 len(df)-1 = 60 天 ⇒ 29/60。
    """
    df = _mkdf(_zigzag(61))
    signals = [
        (df.iloc[1]["date"].isoformat(), "buy"),
        (df.iloc[30]["date"].isoformat(), "sell"),
    ]
    r = run_backtest("T", df, signals)

    assert r.time_in_market_pct == pytest.approx(29 / 60 * 100.0, abs=1e-9)


# ═══════════════════════════════════════════════════════════════════════════
# 4. 独立重算对照
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("buy_i,sell_i", [(1, 15), (3, 40), (0, 55), (10, 11)])
def test_calendar_sharpe_matches_independent_reconstruction(buy_i, sell_i):
    """从 trades 独立重建日历收益序列，夏普与引擎结果一致。"""
    df = _mkdf(_zigzag(61))
    signals = [
        (df.iloc[buy_i]["date"].isoformat(), "buy"),
        (df.iloc[sell_i]["date"].isoformat(), "sell"),
    ]
    r = run_backtest("T", df, signals)

    rebuilt = _calendar_returns_from_trades(df, r)
    assert len(rebuilt) == len(df) - 1
    assert _compute_sharpe(rebuilt) == pytest.approx(r.sharpe_calendar, abs=1e-12)


@pytest.mark.parametrize("buy_i,sell_i", [(1, 15), (3, 40), (10, 11)])
def test_calendar_vol_matches_independent_reconstruction(buy_i, sell_i):
    """年化波动率（日历口径）同样与独立重建序列一致。"""
    df = _mkdf(_zigzag(61))
    signals = [
        (df.iloc[buy_i]["date"].isoformat(), "buy"),
        (df.iloc[sell_i]["date"].isoformat(), "sell"),
    ]
    r = run_backtest("T", df, signals)

    rebuilt = _calendar_returns_from_trades(df, r)
    mean = sum(rebuilt) / len(rebuilt)
    var = sum((x - mean) ** 2 for x in rebuilt) / max(len(rebuilt) - 1, 1)
    expected = math.sqrt(var) * math.sqrt(_TRADING_DAYS_PER_YEAR) * 100.0

    assert r.annualized_volatility_calendar_pct == pytest.approx(expected, abs=1e-9)


def test_vol_identity_exact():
    """两个口径的波动率满足**闭式恒等式**（不是近似）：

        var_cal = f·var_in + f(1-f)·μ_in²          （总体方差口径）

    推导：日历序列有 N 项，其中 K 项等于持仓日收益、N-K 项为 0，f = K/N。
        E[c]  = f·μ_in
        E[c²] = f·E_in[r²] = f(var_in + μ_in²)
        var_cal = E[c²] - E[c]² = f·var_in + f(1-f)·μ_in²

    常见的 "var_cal = f·var_in"（即 vol 比值 = sqrt(f)）只是 μ_in ≈ 0 时的
    特例；漂移明显时会偏离。此处检验精确式，故可用 1e-9 级容差——
    这比 sqrt(f) 近似强得多，能捕捉任何缩放/漏项错误。

    注意引擎报的是**样本**方差（除 n-1），故需做 n/(n-1) 换算。
    """
    df = _mkdf(_zigzag(121))
    signals = [
        (df.iloc[1]["date"].isoformat(), "buy"),
        (df.iloc[60]["date"].isoformat(), "sell"),
    ]
    r = run_backtest("T", df, signals)

    rebuilt = _calendar_returns_from_trades(df, r)
    in_pos = [x for x in rebuilt if x != 0.0]
    N, K = len(rebuilt), len(in_pos)
    f = K / N

    # 独立重建的在市比例应与引擎一致
    assert r.time_in_market_pct == pytest.approx(f * 100.0, abs=1e-9)

    ann = math.sqrt(_TRADING_DAYS_PER_YEAR) * 100.0
    # 由引擎报出的样本波动率反解总体方差
    var_in_pop = (r.annualized_volatility_pct / ann) ** 2 * (K - 1) / K
    mu_in = sum(in_pos) / K

    var_cal_pop_expected = f * var_in_pop + f * (1 - f) * mu_in ** 2
    var_cal_sample_expected = var_cal_pop_expected * N / (N - 1)
    expected_vol = math.sqrt(var_cal_sample_expected) * ann

    assert r.annualized_volatility_calendar_pct == pytest.approx(expected_vol, rel=1e-9)


def test_vol_ratio_tracks_sqrt_time_in_market_when_drift_small():
    """μ_in ≈ 0 时，vol 比值退化为 sqrt(f)——量级检查。

    真实日线数据正处于这个区间（日均收益/日波动 ~0.01 量级），所以这条
    近似在实测中成立；此处用近乎零漂移的序列复现该区间。
    漏乘 sqrt(252) 之类的量级错误会让比值偏离一个数量级，5% 容差足以拦下。
    """
    df = _mkdf(_lowdrift(121))
    signals = [
        (df.iloc[1]["date"].isoformat(), "buy"),
        (df.iloc[60]["date"].isoformat(), "sell"),
    ]
    r = run_backtest("T", df, signals)

    f = r.time_in_market_pct / 100.0
    ratio = r.annualized_volatility_calendar_pct / r.annualized_volatility_pct
    assert ratio == pytest.approx(math.sqrt(f), rel=0.05)


# ═══════════════════════════════════════════════════════════════════════════
# 5. 空仓日不改变最大回撤（日历对齐的核心不变量）
# ═══════════════════════════════════════════════════════════════════════════

def test_flat_days_do_not_change_max_drawdown():
    """平仓后追加交易日 ⇒ 净值走平 ⇒ 不产生新峰/谷 ⇒ max_dd 逐位不变。

    这条不变量是把 `equity_curve` 改为日历对齐时的安全依据。
    """
    prices = _zigzag(41)
    df_short = _mkdf(prices)
    df_long = _mkdf(prices + [prices[-1] * 1.05] * 12)   # 尾部继续涨，但已空仓

    signals = [
        (df_short.iloc[1]["date"].isoformat(), "buy"),
        (df_short.iloc[25]["date"].isoformat(), "sell"),
    ]
    r_short = run_backtest("T", df_short, signals)
    r_long = run_backtest("T", df_long, signals)

    assert r_long.max_drawdown_pct == pytest.approx(r_short.max_drawdown_pct, abs=1e-12)
    assert r_long.sharpe_ratio == pytest.approx(r_short.sharpe_ratio, abs=1e-12)
    # 但时间在市下降了——因为分母变长
    assert r_long.time_in_market_pct < r_short.time_in_market_pct


# ═══════════════════════════════════════════════════════════════════════════
# 6. 序列化与展示
# ═══════════════════════════════════════════════════════════════════════════

def test_to_dict_exposes_calendar_fields():
    df = _mkdf(_zigzag(30))
    r = run_backtest("T", df, [(df.iloc[1]["date"].isoformat(), "buy")])
    d = r.to_dict()

    for k in ("sharpe_calendar", "annualized_volatility_calendar_pct", "time_in_market_pct"):
        assert k in d, f"to_dict 缺少 {k}"
    assert len(d) == 17


def test_calendar_fields_default_to_zero():
    """三个字段带默认值——旧代码用显式 kwargs 构造 BacktestResult 不能被破坏。

    `tests/test_report_exporter.py` 与 `sqlite_store.py` 都是逐字段构造/映射，
    若把新字段设为必填参数，它们会 TypeError。
    """
    r = BacktestResult(
        symbol="X", start_date="2024-01-01", end_date="2024-02-01",
        total_trades=0, winning_trades=0, losing_trades=0, win_rate_pct=0.0,
        total_return_pct=0.0, annualized_return_pct=0.0,
        annualized_volatility_pct=0.0, sharpe_ratio=0.0, max_drawdown_pct=0.0,
        avg_slippage_pct=0.0, trades=[], equity_curve=[1.0],
    )
    assert r.sharpe_calendar == 0.0
    assert r.annualized_volatility_calendar_pct == 0.0
    assert r.time_in_market_pct == 0.0


@pytest.mark.parametrize("cal,inpos,expect_pass", [
    (0.60, 0.10, True),    # 日历达标、持仓不达标 ⇒ 判定应为达标
    (0.10, 2.00, False),   # 持仓远超目标、日历不达标 ⇒ 判定应为不达标
    (0.50, 0.50, True),    # 边界：>= 0.5 视为达标
    (0.499, 3.00, False),
])
def test_markdown_verdict_uses_calendar_not_in_position(cal, inpos, expect_pass):
    """达标判定必须读日历口径。

    第二行参数是关键：持仓口径 2.0 远超目标，但可实现口径只有 0.1，
    此时必须显示"低于目标"。若判定误用持仓口径，这条会失败。
    """
    r = BacktestResult(
        symbol="X", start_date="2024-01-01", end_date="2024-02-01",
        total_trades=1, winning_trades=1, losing_trades=0, win_rate_pct=100.0,
        total_return_pct=1.0, annualized_return_pct=1.0,
        annualized_volatility_pct=10.0, sharpe_ratio=inpos, max_drawdown_pct=1.0,
        avg_slippage_pct=0.0, trades=[], equity_curve=[1.0],
        sharpe_calendar=cal, annualized_volatility_calendar_pct=5.0,
        time_in_market_pct=50.0,
    )
    md = r.to_markdown()

    if expect_pass:
        assert "✅ 满足目标" in md
        assert "⚠️ 低于目标" not in md
    else:
        assert "⚠️ 低于目标(0.5)" in md
        assert "✅ 满足目标" not in md


def test_markdown_shows_both_calibers_and_time_in_market():
    """两个口径都要露出，且标注时间在市——否则读者无法判断放大倍数。"""
    df = _mkdf(_zigzag(40))
    signals = [
        (df.iloc[1]["date"].isoformat(), "buy"),
        (df.iloc[20]["date"].isoformat(), "sell"),
    ]
    md = run_backtest("T", df, signals).to_markdown()

    assert "日历口径" in md
    assert "持仓日口径" in md
    assert "时间在市" in md
