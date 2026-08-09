"""
E-01 滚动窗口 + 样本外验证
==========================
目的：回答"+0.811 是不是真的"，而不是"能不能把它做得更高"。

为什么需要这一步
────────────────
十年复测给出等权组合夏普 +0.811（SE 0.323，95%CI [+0.179, +1.443]）。
但 [2] 逐年表显示：只有 2017(+5.024)、2019(+2.136)、2020(+2.109) 的 CI 排除 0，
2018(-1.324) 与 2026(-1.463) 为负。全期点估计可能只是**少数年份的集中暴露**，
而不是稳定的边际。分辨这两者不能靠提高全期均值——那只会更过拟合——只能靠：

  [1] 滚动窗口：3 年窗口、步进 1 年。若边际稳定，各窗口 CI 下界应**持续为正**；
      若只是少数年份主导，则多数窗口会覆盖 0。
  [2] 样本外：严格切分 训练段(2016-08~2021-12) / 留出段(2022-01~2026-08)，
      留出段完全不参与任何判断。留出段夏普是否存活，是最有力的一条证据。
  [3] 逐年正负比例与最大连续回撤年数——衡量"能不能拿得住"。

口径
────
一律用**日历口径**（空仓日收益记 0）。持仓口径按 sqrt(252) 年化却只统计持仓日，
会把夏普系统性放大约 1/sqrt(时间在市)≈1.43 倍，不是可实现口径。

方法上的诚实说明
────────────────
  - 收益序列取自"全期连续运行一次"的 equity_curve 再按日期切片，而不是在每个
    窗口内重启回测。前者衡量"策略连续运行时该子区间赚了多少"，是实际口径；
    后者会在每个窗口边界人为清仓，反而失真。
  - 指标参数（MA5/MA20、MACD 12/26/9、RSI14、BB(20,2)）是平台自始固定的教科书
    默认值，**未在本数据上拟合**；trailing_stop 与 regime_aware 虽在全期测过，
    但基线臂并未启用。故留出段接近真正的样本外。
  - 仍然不是干净的样本外：20 只标的清单（大盘股 + 2016-08 前上市）本身用到了
    "至今仍上市"这一信息，存活者偏差不可消除。留出段结论同样对真实收益偏乐观。
  - 不因结果不好而调参、换区间、换标的。

用法：
    python Scripts/validate_rolling_oos.py
    python Scripts/validate_rolling_oos.py --window 3 --step 1
    python Scripts/validate_rolling_oos.py --split 2022-01-01
    python Scripts/validate_rolling_oos.py --quick 5
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date
from pathlib import Path

import pandas as pd

# 注意：**不要**在这里再包一层 sys.stdout。下面 import 的 measure_real_10y 在
# 模块级已经执行 sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)。
# 若此处先包一层，那一层会被丢弃，而它被 GC 时会关闭底层 buffer，
# 导致后续 print 抛 "ValueError: I/O operation on closed file."
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "Scripts"))

from agent_platform.finance.backtesting import run_backtest              # noqa: E402

# 复用十年复测脚本的数据/信号构造，确保两份报告口径一致（不另造一套管线）
from measure_real_10y import (                                           # noqa: E402
    _enrich, build_regime_series, gen_signals, stats_from_returns,
)

REAL_DIR = ROOT / "data" / "real"
INDEX_CSV = REAL_DIR / "_index_sh000001.csv"
TRADING_DAYS = 252
REBALANCE_DEFAULT = 5


# ═══════════════════════════════════════════════════════════════════════════
# 收益序列构造
# ═══════════════════════════════════════════════════════════════════════════

def per_symbol_calendar_returns(
    frames: dict[str, pd.DataFrame],
    regime_by_date: dict[date, dict],
    *,
    rebalance: int,
) -> tuple[dict[str, dict[date, float]], dict[str, float]]:
    """全期各跑一次回测，返回 {symbol: {date: 日历口径日收益}} 与逐股日历夏普。

    equity_curve 现在是按日历交易日记录的（空仓日走平），因此
    eq[i]/eq[i-1]-1 就是第 i 个交易日的日历口径收益，可直接按日期切片。
    """
    by_symbol: dict[str, dict[date, float]] = {}
    sharpe_full: dict[str, float] = {}

    for sym, df in frames.items():
        sigs = gen_signals(df, regime_by_date, regime_aware=False, rebalance=rebalance)
        res = run_backtest(sym, df, sigs)
        eq = res.equity_curve
        dates = list(df["date"])
        assert len(eq) == len(dates), f"{sym}: equity_curve 未按日历对齐 {len(eq)}!={len(dates)}"

        series: dict[date, float] = {}
        for i in range(1, len(eq)):
            if eq[i - 1] > 0:
                series[dates[i]] = (eq[i] - eq[i - 1]) / eq[i - 1]
        by_symbol[sym] = series
        sharpe_full[sym] = res.sharpe_calendar

    return by_symbol, sharpe_full


def equal_weight_portfolio(
    by_symbol: dict[str, dict[date, float]],
) -> list[tuple[date, float]]:
    """按日期对齐取等权均值（而非按行序对齐，避免各股交易日不同导致错位）。"""
    all_dates = sorted({d for s in by_symbol.values() for d in s})
    out: list[tuple[date, float]] = []
    for d in all_dates:
        vals = [s[d] for s in by_symbol.values() if d in s]
        if vals:
            out.append((d, statistics.fmean(vals)))
    return out


def slice_returns(
    series: list[tuple[date, float]], lo: date, hi: date
) -> list[float]:
    """左闭右开 [lo, hi)。"""
    return [r for d, r in series if lo <= d < hi]


# ═══════════════════════════════════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════════════════════════════════

def report_rolling(
    port: list[tuple[date, float]], *, window_years: int, step_years: int
) -> dict:
    """滚动窗口：CI 下界是否持续为正。"""
    print("-" * 78)
    print(f"[1] 滚动窗口稳定性（{window_years} 年窗口，步进 {step_years} 年，日历口径）")
    print("-" * 78)

    first, last = port[0][0], port[-1][0]
    print(f"数据区间：{first} ~ {last}")
    print()
    print(f"{'窗口':<26}{'交易日':>7}{'组合夏普':>11}{'SE':>7}{'95% CI':>22}  判定")

    rows: list[tuple[str, float, float, float, bool, bool]] = []
    y = first.year
    while True:
        lo = date(y, first.month, first.day) if y == first.year else date(y, 1, 1)
        hi = date(y + window_years, 1, 1) if y != first.year else date(
            first.year + window_years, first.month, first.day)
        if lo >= last:
            break
        rets = slice_returns(port, lo, hi)
        if len(rets) < TRADING_DAYS:          # 不足 1 年的尾窗不评
            break
        st = stats_from_returns(rets, threshold=0.5)
        pos = st.ci_low > 0
        above = st.ci_low > 0.5
        mark = "CI>0.5" if above else ("CI>0" if pos else "覆盖 0")
        # 标签用**实际覆盖区间**而非名义窗口端点：末尾几个窗口会被数据末日截断，
        # 若仍按名义端点标注（如 "~2028-01-01"）会让只含 384 日的窗口看起来是 3 年。
        eff_hi = min(hi, last)
        truncated = hi > last
        label = f"{lo} ~ {eff_hi}" + ("*" if truncated else "")
        print(f"{label:<26}{st.n_obs:>7}{st.sharpe:>+11.3f}{st.std_error:>7.3f}"
              f"   [{st.ci_low:>+7.3f}, {st.ci_high:>+7.3f}]  {mark}")
        rows.append((label, st.sharpe, st.ci_low, st.ci_high, pos, above))
        y += step_years

    n = len(rows)
    n_pos = sum(1 for r in rows if r[4])
    n_above = sum(1 for r in rows if r[5])
    n_neg_point = sum(1 for r in rows if r[1] < 0)
    n_trunc = sum(1 for r in rows if r[0].endswith("*"))
    print()
    if n_trunc:
        print(f"* 标记的 {n_trunc} 个窗口被数据末日截断，不足 {window_years} 年，"
              f"SE 更大（置信区间更宽），解读时应打折。")
    print(f"窗口数 {n}：CI 下界 > 0 的 {n_pos}/{n}；CI 下界 > 0.5 的 {n_above}/{n}；"
          f"点估计为负的 {n_neg_point}/{n}")
    if n:
        print(f"窗口夏普：均值 {statistics.fmean(r[1] for r in rows):+.3f}   "
              f"最小 {min(r[1] for r in rows):+.3f}   最大 {max(r[1] for r in rows):+.3f}")
    print()
    if n_pos == n and n:
        print("解读：所有窗口 CI 下界均为正 ⇒ 正夏普在子区间上是持续的。")
    elif n_pos == 0:
        print("解读：没有任何窗口能把 0 排除 ⇒ 全期点估计不足以支撑'存在稳定边际'。")
    else:
        print(f"解读：仅 {n_pos}/{n} 个窗口能把 0 排除 ⇒ 全期点估计主要由部分区间贡献，")
        print("      不构成'稳定边际'的证据。这比夏普数值差一点更值得警惕。")
    print()
    return {"n": n, "n_pos": n_pos, "n_above": n_above, "rows": rows}


def report_oos(
    port: list[tuple[date, float]],
    by_symbol: dict[str, dict[date, float]],
    *,
    split: date,
) -> dict:
    """严格样本外切分。"""
    print("-" * 78)
    print(f"[2] 样本外验证（切分点 {split}，留出段不参与任何判断）")
    print("-" * 78)

    first, last = port[0][0], port[-1][0]
    tr = slice_returns(port, first, split)
    ho = slice_returns(port, split, date(last.year + 1, 1, 1))

    if len(tr) < TRADING_DAYS or len(ho) < TRADING_DAYS:
        print(f"切分后样本不足（训练 {len(tr)} 日 / 留出 {len(ho)} 日），跳过。")
        return {}

    s_tr = stats_from_returns(tr, threshold=0.5)
    s_ho = stats_from_returns(ho, threshold=0.5)

    print(f"{'段':<10}{'区间':<26}{'交易日':>7}{'组合夏普':>11}{'SE':>7}{'95% CI':>22}")
    print(f"{'训练':<10}{f'{first} ~ {split}':<26}{s_tr.n_obs:>7}{s_tr.sharpe:>+11.3f}"
          f"{s_tr.std_error:>7.3f}   [{s_tr.ci_low:>+7.3f}, {s_tr.ci_high:>+7.3f}]")
    print(f"{'留出':<10}{f'{split} ~ {last}':<26}{s_ho.n_obs:>7}{s_ho.sharpe:>+11.3f}"
          f"{s_ho.std_error:>7.3f}   [{s_ho.ci_low:>+7.3f}, {s_ho.ci_high:>+7.3f}]")
    print()
    print(f"训练段判定：{s_tr.verdict}")
    print(f"留出段判定：{s_ho.verdict}")
    print(f"衰减：留出 − 训练 = {s_ho.sharpe - s_tr.sharpe:+.3f}")
    print()

    # 逐股留出段
    ho_sharpes: list[float] = []
    for sym, series in by_symbol.items():
        rr = [r for d, r in sorted(series.items()) if d >= split]
        if len(rr) >= TRADING_DAYS:
            ho_sharpes.append(stats_from_returns(rr).sharpe)
    if ho_sharpes:
        n_pass = sum(1 for s in ho_sharpes if s >= 0.5)
        print(f"留出段逐股夏普：均值 {statistics.fmean(ho_sharpes):+.3f}   "
              f"点估计≥0.5 的 {n_pass}/{len(ho_sharpes)}   "
              f"为负的 {sum(1 for s in ho_sharpes if s < 0)}/{len(ho_sharpes)}")
    print()

    if s_ho.sharpe <= 0:
        print("解读：留出段夏普不为正 ⇒ 全期 +0.811 未能样本外存活，E-01 不成立。")
    elif s_ho.ci_low > 0.5:
        print("解读：留出段 CI 下界 > 0.5 ⇒ 样本外显著达标（最强证据）。")
    elif s_ho.ci_low > 0:
        print("解读：留出段显著为正但无法区分于 0.5 ⇒ 有正边际，但达标未被证明。")
    else:
        print("解读：留出段点估计为正但 CI 覆盖 0 ⇒ 无法排除'留出段表现来自运气'。")
    print()
    return {"train": s_tr, "holdout": s_ho, "holdout_per_symbol": ho_sharpes}


def report_persistence(port: list[tuple[date, float]]) -> None:
    """逐年正负与最长连续负年——衡量可持有性。"""
    print("-" * 78)
    print("[3] 逐年正负与最长连续负年（日历口径）")
    print("-" * 78)

    by_year: dict[int, list[float]] = {}
    for d, r in port:
        by_year.setdefault(d.year, []).append(r)

    years = sorted(by_year)
    signs: list[tuple[int, float, bool]] = []
    print(f"{'年份':<8}{'交易日':>7}{'组合夏普':>11}{'年内累计%':>11}")
    for y in years:
        rr = by_year[y]
        if len(rr) < 30:
            continue
        s = stats_from_returns(rr)
        cum = 1.0
        for r in rr:
            cum *= (1 + r)
        print(f"{y:<8}{len(rr):>7}{s.sharpe:>+11.3f}{(cum - 1) * 100:>+11.2f}")
        signs.append((y, s.sharpe, s.sharpe > 0))

    n_pos = sum(1 for _, _, p in signs if p)
    worst = cur = 0
    for _, _, p in signs:
        cur = 0 if p else cur + 1
        worst = max(worst, cur)
    print()
    print(f"正夏普年份：{n_pos}/{len(signs)}   最长连续负夏普年数：{worst}")
    if worst >= 2:
        print(f"解读：存在连续 {worst} 年负夏普的区间 ⇒ 实际持有需承受多年不赚钱，")
        print("      即便全期点估计为正，也难以在真实约束下坚持执行。")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebalance", type=int, default=REBALANCE_DEFAULT)
    ap.add_argument("--window", type=int, default=3, help="滚动窗口年数")
    ap.add_argument("--step", type=int, default=1, help="步进年数")
    ap.add_argument("--split", type=str, default="2022-01-01", help="样本外切分点")
    ap.add_argument("--quick", type=int, default=0, help="只跑前 N 只（自测用）")
    args = ap.parse_args()

    if not INDEX_CSV.exists():
        print(f"缺少指数缓存：{INDEX_CSV}")
        return 1
    csvs = sorted(p for p in REAL_DIR.glob("*.csv") if not p.name.startswith("_"))
    if not csvs:
        print("缺少个股缓存，请先运行 Scripts/fetch_real_history.py")
        return 1
    if args.quick:
        csvs = csvs[: args.quick]

    frames: dict[str, pd.DataFrame] = {}
    for p in csvs:
        df = pd.read_csv(p)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        frames[p.stem] = _enrich(df)

    print("=" * 78)
    print("E-01 滚动窗口 + 样本外验证（日历口径）")
    print("=" * 78)
    print(f"标的：{len(frames)} 只（事前规则选取）   再平衡：每 {args.rebalance} 交易日")
    print("成本：滑点 0.1% + 佣金 0.03% 单边   基线臂：regime_aware=False, 无追踪止损")
    print()
    print("⚠️ 存活者偏差仍在：20 只均为至今仍上市的公司，留出段结论同样偏乐观。")
    print("⚠️ 口径提示：全部为日历口径（空仓日记 0）。持仓口径会把夏普放大约")
    print("   1/sqrt(时间在市) 倍，不可实现。")
    print()

    regime_by_date = build_regime_series()
    by_symbol, sharpe_full = per_symbol_calendar_returns(
        frames, regime_by_date, rebalance=args.rebalance)
    port = equal_weight_portfolio(by_symbol)

    full = stats_from_returns([r for _, r in port], threshold=0.5)
    print(f"全期等权组合（对照）：{full.to_line()}")
    print(f"  逐股日历夏普均值：{statistics.fmean(sharpe_full.values()):+.3f}")
    print()

    report_rolling(port, window_years=args.window, step_years=args.step)
    report_oos(port, by_symbol, split=date.fromisoformat(args.split))
    report_persistence(port)

    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
