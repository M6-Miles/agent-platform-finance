"""
E-01 十年窗口复测（真实 A 股，含标准误与置信区间）
====================================================
目的：把回测窗口从 1 年拉到 10 年，使 E-01「夏普 > 0.5」第一次成为
      可证伪命题。1 年日频数据的夏普 SE≈1.00，实测 -0.440 的 95% CI
      约 [-2.4, +1.5]，把 +0.5 完整覆盖 —— 无法区分"策略不行"与"样本太短"。
      10 年窗口把 SE 降到约 0.32。

方法（与平台生产口径一致，不另造一套）：
  - 数据：Scripts/fetch_real_history.py 缓存的 20 只大盘股前复权日线
  - 指标：analysis.py 相同参数（MA5/MA20/MACD(12,26,9)/RSI14/BB(20,2)）
  - 信号：synthesis_agent.synthesize() 真实管线，周频（每 5 交易日）再平衡
  - 大盘：真实上证指数 + 平台 _determine_regime() 的 ±3% 阈值
  - 回测：backtesting.run_backtest()，滑点 0.1% + 佣金 0.03% 单边
  - 统计：sharpe_stats 给出 SE / 95%CI / 对 0.5 的假设检验

诚实性约束：所有样本、口径和失败结果必须在输出报告中如实记录。
  - 不调参、不挑区间、不挑标的。标的清单在 fetch_real_history.py 中按
    "大盘股 + 2016-08 前上市"事前规则固定，与收益无关。
  - 存活者偏差已知且不可消除（20 只均为至今仍上市的公司），已在输出中标注。
  - 无论结果正负，如实记录。

用法：
    python Scripts/measure_real_10y.py
    python Scripts/measure_real_10y.py --rebalance 1     # 日频
    python Scripts/measure_real_10y.py --quick 5         # 只跑前 5 只（自测）
"""
from __future__ import annotations

import argparse
import io
import statistics
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_platform.finance.backtesting import run_backtest              # noqa: E402
from agent_platform.finance.indicators import (                          # noqa: E402
    add_bollinger_bands, add_macd, add_moving_average, add_rsi,
)
from agent_platform.finance.market_regime_agent import _determine_regime  # noqa: E402
from agent_platform.finance.backtesting import _compute_sharpe           # noqa: E402
from agent_platform.finance.sharpe_stats import (                        # noqa: E402
    SharpeStats, compute_sharpe_stats, paired_diff_stats,
)
from agent_platform.finance.synthesis_agent import synthesize            # noqa: E402

REAL_DIR = ROOT / "data" / "real"
INDEX_CSV = REAL_DIR / "_index_sh000001.csv"
REBALANCE_DEFAULT = 5
TRADING_DAYS = 252


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """叠加与 analyze_security 完全相同参数的指标列（全为后视滚动，无前视偏差）。"""
    d = df.sort_values("date").reset_index(drop=True)
    d = add_moving_average(d, window=5)
    d = add_moving_average(d, window=20)
    d = add_macd(d, fast=12, slow=26, signal=9)
    d = add_rsi(d, period=14)
    d = add_bollinger_bands(d, window=20, num_std=2.0)
    return d


def _tech_dict(row: pd.Series) -> dict:
    """从单行构造 technical dict，字段与 SecurityAnalysisResult.to_dict() 对齐。"""
    close = float(row["close"])
    bb_u, bb_l = float(row["bb_upper"]), float(row["bb_lower"])
    rng = bb_u - bb_l
    bb_pos = (close - bb_l) / rng * 100.0 if rng > 0 else 50.0
    return {
        "latest_close": close,
        "latest_ma5": float(row["ma5"]),
        "latest_ma20": float(row["ma20"]),
        "latest_rsi": float(row["rsi"]),
        "latest_macd": float(row["macd"]),
        "latest_macd_signal": float(row["macd_signal"]),
        "latest_bb_position_pct": bb_pos,
    }


def build_regime_series() -> dict[date, dict]:
    """真实上证指数 → 逐日 regime dict，阈值用平台 _determine_regime。"""
    df = pd.read_csv(INDEX_CSV)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("date").reset_index(drop=True)

    out: dict[date, dict] = {}
    for i in range(len(df)):
        d = df.iloc[i]["date"]
        if i < 5:
            out[d] = {"regime": "unknown", "regime_note": "预热期"}
            continue
        prev = float(df.iloc[i - 5]["close"])
        curr = float(df.iloc[i]["close"])
        c5 = (curr - prev) / prev * 100.0 if prev > 0 else 0.0
        r, _risk, note = _determine_regime(c5, None)
        out[d] = {"regime": r, "regime_note": note}
    return out


def gen_signals(
    df: pd.DataFrame,
    regime_by_date: dict[date, dict],
    *,
    regime_aware: bool,
    rebalance: int,
) -> dict[date, str]:
    """走 synthesize() 真实管线逐日产出信号；非再平衡日一律 hold。"""
    sigs: dict[date, str] = {}
    warmup = 26  # MACD slow 需要的最短预热
    for i in range(len(df)):
        row = df.iloc[i]
        d = row["date"]
        if i < warmup or i % rebalance != 0:
            sigs[d] = "hold"
            continue
        if pd.isna(row.get("ma20")) or pd.isna(row.get("macd_signal")):
            sigs[d] = "hold"
            continue
        regime = regime_by_date.get(d, {"regime": "unknown", "regime_note": ""})
        r = synthesize(
            str(row.get("symbol", "NA")),
            _tech_dict(row), {}, {}, regime,
            regime_aware=regime_aware,
        )
        sigs[d] = r.signal
    return sigs


def stats_from_returns(rets: list[float], *, threshold: float = 0.5) -> SharpeStats:
    """
    从日收益序列构造 SharpeStats。

    点估计刻意复用 backtesting._compute_sharpe，确保本脚本报告的夏普
    与 run_backtest 报告的完全一致（避免两套实现漂移）。
    """
    if len(rets) < 3:
        return compute_sharpe_stats(0.0, len(rets), threshold=threshold)
    return compute_sharpe_stats(_compute_sharpe(rets), len(rets), threshold=threshold)


def buy_and_hold_sharpe(df: pd.DataFrame) -> float:
    """买入持有基准夏普（同一序列，无成本）。"""
    c = df["close"].astype(float).reset_index(drop=True)
    rets = [(c.iloc[i] - c.iloc[i - 1]) / c.iloc[i - 1]
            for i in range(1, len(c)) if c.iloc[i - 1] > 0]
    return stats_from_returns(rets).sharpe


def run_arm(
    frames: dict[str, pd.DataFrame],
    regime_by_date: dict[date, dict],
    *,
    regime_aware: bool,
    trailing_stop: float,
    rebalance: int,
) -> dict:
    sharpes: list[float] = []
    sharpes_cal: list[float] = []
    tims: list[float] = []
    dds: list[float] = []
    trades: list[float] = []
    per_symbol: dict[str, float] = {}
    per_symbol_cal: dict[str, float] = {}
    per_symbol_tim: dict[str, float] = {}
    ret_by_date: dict[str, dict[date, float]] = {}

    for sym, df in frames.items():
        sigs = gen_signals(df, regime_by_date,
                           regime_aware=regime_aware, rebalance=rebalance)
        res = run_backtest(sym, df, sigs, trailing_stop_pct=trailing_stop)
        sharpes.append(res.sharpe_ratio)
        sharpes_cal.append(res.sharpe_calendar)
        tims.append(res.time_in_market_pct)
        dds.append(res.max_drawdown_pct)
        trades.append(float(res.total_trades))
        per_symbol[sym] = res.sharpe_ratio
        per_symbol_cal[sym] = res.sharpe_calendar
        per_symbol_tim[sym] = res.time_in_market_pct
        eq = res.equity_curve
        dts = list(df["date"])
        # 按**日期**记账，不按行序。见下方等权组合处的说明。
        series: dict[date, float] = {}
        for i in range(1, min(len(eq), len(dts))):
            if eq[i - 1] > 0:
                series[dts[i]] = (eq[i] - eq[i - 1]) / eq[i - 1]
        ret_by_date[sym] = series

    # 等权组合：**按日期对齐**逐日取各股收益均值。
    #
    # 2026-08-06 修正：此处原先按行序对齐（第 t 项对第 t 项）。20 只标的的交易
    # 日历并不相同——9 只因停牌而更短，最多缺 74 日——按行序平均会把不同日期
    # 的收益混在一起。实测该错位使组合夏普从 +0.593 虚高到 +0.811（差 +0.219），
    # 即此前报告的 +0.811 大部分是对齐错误的产物，而非策略表现。
    # 现改为按日期取交集/并集对齐：某日只对当日有数据的标的取均值。
    all_dates = sorted({d for s in ret_by_date.values() for d in s})
    port: list[float] = []
    port_dates: list[date] = []
    for d in all_dates:
        vals = [s[d] for s in ret_by_date.values() if d in s]
        if vals:
            port_dates.append(d)
            port.append(statistics.fmean(vals))

    # 注：portfolio_returns 由 equity_curve 逐日差分得到，而 equity_curve 是
    # 按日历交易日记录的（空仓日走平 ⇒ 收益 0），因此**组合序列本身已是日历口径**。
    # 逐股 sharpes 才有两个口径之分。
    return {
        "sharpes": sharpes,
        "sharpes_cal": sharpes_cal,
        "per_symbol": per_symbol,
        "per_symbol_cal": per_symbol_cal,
        "per_symbol_tim": per_symbol_tim,
        "mean_sharpe": statistics.fmean(sharpes) if sharpes else 0.0,
        "mean_sharpe_cal": statistics.fmean(sharpes_cal) if sharpes_cal else 0.0,
        "mean_tim": statistics.fmean(tims) if tims else 0.0,
        "n_pass": sum(1 for s in sharpes if s >= 0.5),
        "n_pass_cal": sum(1 for s in sharpes_cal if s >= 0.5),
        "mean_dd": statistics.fmean(dds) if dds else 0.0,
        "mean_trades": statistics.fmean(trades) if trades else 0.0,
        "portfolio_returns": port,
        "portfolio_dates": port_dates,     # 与 portfolio_returns 逐位对应
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebalance", type=int, default=REBALANCE_DEFAULT)
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

    n_obs = statistics.fmean(len(d) for d in frames.values())
    se_ref = (compute_sharpe_stats(0.0, int(n_obs)).std_error
              if n_obs > 2 else float("nan"))

    print("=" * 78)
    print("E-01 十年窗口复测 —— 真实 A 股，含标准误与置信区间")
    print("=" * 78)
    print(f"标的：{len(frames)} 只大盘股（事前规则选取，与收益无关）")
    print(f"每股观测：{n_obs:.0f} 交易日（约 {n_obs/TRADING_DAYS:.1f} 年）")
    print(f"再平衡：每 {args.rebalance} 交易日   成本：滑点 0.1% + 佣金 0.03% 单边")
    print(f"单股夏普 SE ≈ {se_ref:.3f}（对比 1 年窗口的 1.00）")
    print()
    print("⚠️ 存活者偏差：20 只均为至今仍上市的公司，退市/暴雷标的被结构性排除，")
    print("   故本结果对真实可实现收益偏乐观。此偏差不可通过调参消除。")
    print()

    regime_by_date = build_regime_series()
    rc: dict[str, int] = {}
    for v in regime_by_date.values():
        rc[v["regime"]] = rc.get(v["regime"], 0) + 1
    tot = sum(rc.values())
    print("真实指数 regime 分布：" + "  ".join(
        f"{k}={v}({v/tot*100:.1f}%)" for k, v in sorted(rc.items(), key=lambda x: -x[1])))
    print()

    # ── 主命题：E-01 ────────────────────────────────────────────────────────
    print("-" * 78)
    print("[1] E-01 主命题：MA/synthesize 管线夏普是否 > 0.5")
    print("-" * 78)

    base = run_arm(frames, regime_by_date, regime_aware=False,
                   trailing_stop=0.0, rebalance=args.rebalance)

    # 两个口径并列。日历口径是**可实现口径**（空仓日收益记 0），达标判定以它为准；
    # 持仓口径只统计持仓日却按 sqrt(252) 年化，等于假装全年在市，会放大约
    # 1/sqrt(时间在市) 倍，仅为与历史记录对照而保留。
    # 各自的 CI 用各自的 n_obs：持仓口径样本量更小（只有持仓日），SE 更大。
    print(f"{'标的':<10}{'日历夏普':>10}{'95% CI(日历)':>22}"
          f"{'持仓夏普':>10}{'在市%':>8}   判定")
    for sym, sh_cal in sorted(base["per_symbol_cal"].items(), key=lambda x: -x[1]):
        n_cal = len(frames[sym]) - 1
        st = compute_sharpe_stats(sh_cal, n_cal, threshold=0.5)
        tim = base["per_symbol_tim"][sym]
        mark = "—" if st.threshold_in_ci else ("✅" if st.sharpe > 0.5 else "❌")
        print(f"{sym:<10}{sh_cal:>+10.3f}   "
              f"[{st.ci_low:>+7.3f}, {st.ci_high:>+7.3f}]"
              f"{base['per_symbol'][sym]:>+10.3f}{tim:>8.1f}   {mark}")

    print()
    print(f"逐股均值夏普（日历口径）：{base['mean_sharpe_cal']:+.3f}   "
          f"达标数(点估计≥0.5)：{base['n_pass_cal']}/{len(frames)}")
    print(f"逐股均值夏普（持仓口径）：{base['mean_sharpe']:+.3f}   "
          f"达标数：{base['n_pass']}/{len(frames)}   "
          f"均值时间在市 {base['mean_tim']:.1f}%")
    print(f"均值最大回撤：{base['mean_dd']:.2f}%   均值交易数：{base['mean_trades']:.1f}")
    print()

    pstat = stats_from_returns(base["portfolio_returns"], threshold=0.5)
    print("等权组合（20 只逐日等权，最能代表可实现结果）：")
    print("  " + pstat.to_line())
    print(f"  n={pstat.n_obs} 日   SE={pstat.std_error:.3f}   判定：{pstat.verdict}")
    bh = statistics.fmean(buy_and_hold_sharpe(d) for d in frames.values())
    print(f"  买入持有基准（无成本）逐股均值夏普：{bh:+.3f}")
    print()

    # ── 逐年稳定性 ─────────────────────────────────────────────────────────
    print("-" * 78)
    print("[2] 逐年稳定性（检查结论是否由个别年份主导）")
    print("-" * 78)
    pr = base["portfolio_returns"]
    # 直接用 run_arm 返回的真实日期，不再从各股日期并集里重建对齐。
    # 原先的 dates_sorted[1:len(pr)+1] 是猜测式对齐：它假定组合序列恰好等于
    # 并集去掉首日，一旦某日无任何标的有数据就会整体错位，逐年归因随之偏移。
    by_year: dict[int, list[float]] = {}
    for d, r in zip(base["portfolio_dates"], pr):
        by_year.setdefault(d.year, []).append(r)
    print(f"{'年份':<8}{'交易日':>7}{'组合夏普':>11}{'SE':>8}{'95% CI':>22}")
    for y in sorted(by_year):
        rr = by_year[y]
        if len(rr) < 30:
            continue
        s = stats_from_returns(rr)
        print(f"{y:<8}{s.n_obs:>7}{s.sharpe:>+11.3f}{s.std_error:>8.3f}"
              f"   [{s.ci_low:>+7.3f}, {s.ci_high:>+7.3f}]")
    print()

    # ── 已建组件在十年窗口上的配对复测 ──────────────────────────────────────
    print("-" * 78)
    print("[3] 已建组件十年窗口配对复测")
    print("-" * 78)

    arm_reg = run_arm(frames, regime_by_date, regime_aware=True,
                      trailing_stop=0.0, rebalance=args.rebalance)
    m, se, t = paired_diff_stats(base["sharpes"], arm_reg["sharpes"])
    print(f"regime_aware=True  均值夏普 {arm_reg['mean_sharpe']:+.3f}  "
          f"回撤 {arm_reg['mean_dd']:.2f}%")
    print(f"  配对差值 {m:+.4f}  SE={se:.4f}  t={t:+.2f}  "
          f"→ {'显著' if abs(t) >= 2 else '无法区分于 0'}")
    print()

    for ts in (0.05, 0.10):
        arm_ts = run_arm(frames, regime_by_date, regime_aware=False,
                         trailing_stop=ts, rebalance=args.rebalance)
        m2, se2, t2 = paired_diff_stats(base["sharpes"], arm_ts["sharpes"])
        print(f"trailing_stop={ts:.2f}  均值夏普 {arm_ts['mean_sharpe']:+.3f}  "
              f"回撤 {arm_ts['mean_dd']:.2f}%（基线 {base['mean_dd']:.2f}%）")
        print(f"  配对差值 {m2:+.4f}  SE={se2:.4f}  t={t2:+.2f}  "
              f"→ {'显著' if abs(t2) >= 2 else '无法区分于 0'}")
    print()
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
