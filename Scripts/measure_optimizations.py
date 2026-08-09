"""
测量已建优化组件的实际效果（regime_aware / sentiment）
=========================================================
目的：验证 synthesis_agent 的 regime_aware 开关与 sentiment 入参
      在回测基准上是否真的改善绩效，而非仅仅"代码按设计工作"。

方法要点
--------
1. 数据集：data/sample/prices.csv 的 TEST001-020（确定性合成，
   与此前追踪止损测量同源，结果可比）。
2. 无前视偏差：MA/EMA/MACD/RSI/BB 均为后视滚动窗口指标，
   在整段序列上一次算完再逐行读取，与逐日 walk-forward 等价。
   信号在 t 日生成、t+1 日开盘执行（由 run_backtest 保证）。
3. 大盘 regime 代理：合成数据无指数，用 20 只等权横截面净值
   构造代理指数，套用 market_regime_agent._determine_regime 的 ±3% 阈值。
   5 日涨跌幅只用到 t 日及之前的数据。
4. 配对统计：同一条价格路径同时跑两个 arm，报告逐股差值的
   均值与标准误（SE = std/sqrt(n)）。配对设计消掉了价格路径本身的
   方差，是检验"开关有没有用"的正确统计量。

用法：
    python Scripts/measure_optimizations.py
    python Scripts/measure_optimizations.py --rebalance 1   # 日频
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from agent_platform.finance.backtesting import run_backtest  # noqa: E402
from agent_platform.finance.indicators import (  # noqa: E402
    add_bollinger_bands,
    add_ema,
    add_macd,
    add_moving_average,
    add_rsi,
)
from agent_platform.finance.synthesis_agent import synthesize  # noqa: E402

PRICES_CSV = ROOT / "data" / "sample" / "prices.csv"
TRADING_DAYS = 252
WARMUP = 30          # 指标预热：MACD(26) + signal(9) 需要约 35 行才稳定
REBALANCE_DEFAULT = 5


# ─── 指标与技术面字典 ─────────────────────────────────────────────────────────

def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """叠加 synthesize() 实际消费的那几个指标（口径对齐 analyze_security）。"""
    out = df.sort_values("date").reset_index(drop=True)
    out = add_moving_average(out, window=5)
    out = add_moving_average(out, window=20)
    out = add_ema(out, window=12)
    out = add_ema(out, window=26)
    out = add_macd(out, fast=12, slow=26, signal=9)
    out = add_rsi(out, period=14)
    out = add_bollinger_bands(out, window=20, num_std=2.0)
    return out


def _tech_dict(row: pd.Series) -> dict:
    """按 analyze_security().to_dict() 的口径组装技术面字段。"""
    bb_u = float(row["bb_upper"])
    bb_l = float(row["bb_lower"])
    close = float(row["close"])
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


def _row_ready(row: pd.Series) -> bool:
    keys = ["ma5", "ma20", "rsi", "macd", "macd_signal", "bb_upper", "bb_lower"]
    return not any(pd.isna(row.get(k)) for k in keys)


# ─── 大盘 regime 代理 ─────────────────────────────────────────────────────────

def build_regime_proxy(
    frames: dict[str, pd.DataFrame],
    mode: str = "absolute",
) -> dict[date, dict]:
    """
    用 20 只等权横截面净值构造代理指数，逐日给出 regime dict。

    mode="absolute"：沿用 market_regime_agent._determine_regime 的 ±3% / 5日阈值。
        注意：等权平均 20 只独立随机游走会把波动摊薄约 sqrt(20)≈4.5 倍，
        该阈值本是为上证综指这类真实指数校准的，套在分散化篮子上几乎永不触发
        （实测 252 个交易日全部落在 consolidation）。

    mode="quantile"：用**扩张窗口**分位数把 5 日涨跌幅切三分，
        使 bull/bear/consolidation 各约占 1/3，让 regime_aware 分支真正被执行。
        扩张窗口 = 每日只用当日及之前已观测到的分布，避免用全期分位数回头切
        造成前视偏差。这是为「测机制是否有效」而放宽的 regime 定义，
        不是对 ±3% 阈值本身的检验 —— 两者结论不可互相替代。
    """
    norm: dict[str, pd.Series] = {}
    for sym, df in frames.items():
        d = df.sort_values("date").reset_index(drop=True)
        s = pd.Series(d["close"].values, index=pd.to_datetime(d["date"]).dt.date)
        norm[sym] = s / s.iloc[0]

    index_series = pd.DataFrame(norm).mean(axis=1).sort_index()

    out: dict[date, dict] = {}
    dates = list(index_series.index)
    hist_changes: list[float] = []   # 扩张窗口：已观测到的 5 日涨跌幅

    _MIN_HIST = 30   # 分位数至少要这么多样本才可信，否则按 consolidation 处理

    for i, d in enumerate(dates):
        if i < 5:
            out[d] = {"regime": "unknown", "regime_note": "预热期，指数数据不足"}
            continue
        prev = float(index_series.iloc[i - 5])
        curr = float(index_series.iloc[i])
        change_5d = (curr - prev) / prev * 100.0 if prev > 0 else 0.0

        if mode == "absolute":
            if change_5d > 3.0:
                regime = "bull"
            elif change_5d < -3.0:
                regime = "bear"
            else:
                regime = "consolidation"
            note = f"代理指数5日涨跌幅 {change_5d:+.2f}%"

        elif mode == "quantile":
            # 只用「当日之前」的历史分布定阈值，当日观测不参与自身分类
            if len(hist_changes) < _MIN_HIST:
                regime = "consolidation"
                note = (f"代理指数5日涨跌幅 {change_5d:+.2f}%"
                        f"（分位数样本不足 n={len(hist_changes)}）")
            else:
                lo = float(pd.Series(hist_changes).quantile(1 / 3))
                hi = float(pd.Series(hist_changes).quantile(2 / 3))
                if change_5d > hi:
                    regime = "bull"
                elif change_5d < lo:
                    regime = "bear"
                else:
                    regime = "consolidation"
                note = (f"代理指数5日涨跌幅 {change_5d:+.2f}%"
                        f"（扩张窗口三分位 {lo:+.2f}/{hi:+.2f}, n={len(hist_changes)}）")
        else:
            raise ValueError(f"未知 regime 代理模式: {mode}")

        out[d] = {"regime": regime, "regime_note": note}
        hist_changes.append(change_5d)   # 分类之后才纳入历史，杜绝自我泄漏

    return out


# ─── 信号生成 ─────────────────────────────────────────────────────────────────

def gen_signals(
    df: pd.DataFrame,
    regime_by_date: dict[date, dict],
    *,
    regime_aware: bool,
    sentiment_score: int | None,
    rebalance: int,
) -> tuple[dict[date, str], dict[str, int]]:
    """逐日（或每 rebalance 日）调用 synthesize() 产出信号。"""
    signals: dict[date, str] = {}
    dist = {"buy": 0, "sell": 0, "hold": 0}

    sentiment = None
    if sentiment_score is not None:
        sentiment = {"score": sentiment_score, "keywords_found": ["测试"]}

    for i, row in df.iterrows():
        idx = int(i)
        if idx < WARMUP or not _row_ready(row):
            continue
        if (idx - WARMUP) % rebalance != 0:
            continue

        d = row["date"] if hasattr(row["date"], "year") else date.fromisoformat(str(row["date"]))
        regime = regime_by_date.get(d, {"regime": "unknown", "regime_note": ""})

        res = synthesize(
            "PROXY",
            _tech_dict(row),
            {},          # 合成数据无基本面
            {},          # 合成数据无行业资金
            regime,
            regime_aware=regime_aware,
            sentiment=sentiment,
        )
        signals[d] = res.signal
        dist[res.signal] = dist.get(res.signal, 0) + 1

    return signals, dist


# ─── 统计 ─────────────────────────────────────────────────────────────────────

def paired_stats(a: list[float], b: list[float]) -> tuple[float, float, float]:
    """返回 (mean_diff, se_diff, t)。b - a 的配对统计。"""
    diffs = [x - y for x, y in zip(b, a)]
    n = len(diffs)
    if n < 2:
        return 0.0, 0.0, 0.0
    m = statistics.fmean(diffs)
    sd = statistics.stdev(diffs)
    se = sd / math.sqrt(n)
    t = m / se if se > 0 else 0.0
    return m, se, t


def run_arm(
    frames: dict[str, pd.DataFrame],
    regime_by_date: dict[date, dict],
    *,
    regime_aware: bool,
    sentiment_score: int | None,
    rebalance: int,
) -> dict:
    sharpes: list[float] = []
    dds: list[float] = []
    trades: list[int] = []
    dist_total = {"buy": 0, "sell": 0, "hold": 0}

    for sym in sorted(frames):
        df = frames[sym]
        sig, dist = gen_signals(
            df, regime_by_date,
            regime_aware=regime_aware,
            sentiment_score=sentiment_score,
            rebalance=rebalance,
        )
        for k, v in dist.items():
            dist_total[k] = dist_total.get(k, 0) + v

        r = run_backtest(sym, df, sig)
        sharpes.append(r.sharpe_ratio)
        dds.append(r.max_drawdown_pct)
        trades.append(r.total_trades)

    return {
        "sharpes": sharpes,
        "dds": dds,
        "mean_sharpe": statistics.fmean(sharpes),
        "n_pass": sum(1 for s in sharpes if s >= 0.5),
        "mean_dd": statistics.fmean(dds),
        "mean_trades": statistics.fmean(trades),
        "dist": dist_total,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebalance", type=int, default=REBALANCE_DEFAULT,
                    help="再平衡间隔（交易日）。5=周频，1=日频")
    ap.add_argument("--regime-mode", choices=("threshold", "quantile"),
                    default="threshold",
                    help="regime 代理口径。threshold=平台原生±3%%（分散化篮子上几乎不触发）；"
                         "quantile=扩张窗口三分位（使 bull/bear 各约占 1/3，用于测机制本身）")
    args = ap.parse_args()

    if not PRICES_CSV.exists():
        print(f"缺少数据文件：{PRICES_CSV}")
        print("请先运行 python Scripts/generate_sample_data.py")
        return 1

    raw = pd.read_csv(PRICES_CSV)
    raw["date"] = pd.to_datetime(raw["date"]).dt.date

    frames: dict[str, pd.DataFrame] = {}
    for sym, g in raw.groupby("symbol"):
        s = str(sym)
        if not s.startswith("TEST"):
            continue
        frames[s] = _enrich(g.copy())

    if not frames:
        print("未找到 TEST* 标的")
        return 1

    n_obs = statistics.fmean(len(d) for d in frames.values())
    se_single = math.sqrt(TRADING_DAYS / n_obs) if n_obs > 0 else float("nan")

    print("=" * 74)
    print("已建优化组件效果测量  —  regime_aware / sentiment")
    print("=" * 74)
    print(f"标的：TEST001-020（{len(frames)} 只，确定性合成）")
    print(f"每股观测：{n_obs:.0f} 交易日   再平衡：每 {args.rebalance} 交易日")
    print(f"单股夏普标准误 ≈ sqrt(252/{n_obs:.0f}) = {se_single:.2f}（IID 下的乐观下界）")
    print("成本：滑点 0.1% + 佣金 0.03%（单边，run_backtest 默认）")
    print()

    regime_by_date = build_regime_proxy(frames, mode=args.regime_mode)
    rc: dict[str, int] = {}
    for v in regime_by_date.values():
        rc[v["regime"]] = rc.get(v["regime"], 0) + 1
    print(f"regime 代理口径：{args.regime_mode}")
    print("代理指数 regime 分布："
          + "  ".join(f"{k}={v}" for k, v in sorted(rc.items())))
    print()

    # ── Arm A / B：regime_aware 开关 ────────────────────────────────────────
    print("-" * 74)
    print("[1] regime_aware 效果（配对比较，同一价格路径）")
    print("-" * 74)

    arm_a = run_arm(frames, regime_by_date, regime_aware=False,
                    sentiment_score=None, rebalance=args.rebalance)
    arm_b = run_arm(frames, regime_by_date, regime_aware=True,
                    sentiment_score=None, rebalance=args.rebalance)

    hdr = f"{'arm':<28}{'均值夏普':>12}{'达标数':>9}{'均值回撤':>11}{'均值交易数':>12}"
    print(hdr)
    print(f"{'A: regime_aware=False（基线）':<28}{arm_a['mean_sharpe']:>+12.3f}"
          f"{arm_a['n_pass']:>6}/20{arm_a['mean_dd']:>10.2f}%{arm_a['mean_trades']:>12.1f}")
    print(f"{'B: regime_aware=True':<28}{arm_b['mean_sharpe']:>+12.3f}"
          f"{arm_b['n_pass']:>6}/20{arm_b['mean_dd']:>10.2f}%{arm_b['mean_trades']:>12.1f}")

    m, se, t = paired_stats(arm_a["sharpes"], arm_b["sharpes"])
    print()
    print(f"配对差值 (B−A)：{m:+.4f}   SE={se:.4f}   t={t:+.2f}")
    verdict = "统计上无法区分于 0" if abs(t) < 2.0 else "显著（|t|>2）"
    print(f"判定：{verdict}")
    print()
    print(f"信号分布 A：{arm_a['dist']}")
    print(f"信号分布 B：{arm_b['dist']}")
    print()

    # ── sentiment 敏感度 ───────────────────────────────────────────────────
    print("-" * 74)
    print("[2] sentiment 杠杆敏感度")
    print("-" * 74)
    print("注：合成数据无真实新闻，无法测'情感是否预测收益'。")
    print("    此处注入常量分值，量化 sentiment 对管线输出的影响幅度。")
    print()

    print(f"{'注入分值':<14}{'均值夏普':>12}{'达标数':>9}{'均值回撤':>11}"
          f"{'buy':>7}{'hold':>7}{'sell':>7}")
    for score in (-10, -5, 0, 5, 10):
        arm = run_arm(frames, regime_by_date, regime_aware=False,
                      sentiment_score=score, rebalance=args.rebalance)
        d = arm["dist"]
        print(f"{score:<+14d}{arm['mean_sharpe']:>+12.3f}{arm['n_pass']:>6}/20"
              f"{arm['mean_dd']:>10.2f}%{d['buy']:>7}{d['hold']:>7}{d['sell']:>7}")

    print()
    print("=" * 74)
    print("提示：单股 SE≈%.2f，逐股均值的配对 SE 见上方 [1]。" % se_single)
    print("     配对 |t|<2 即表示该开关的效果无法与噪声区分。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
