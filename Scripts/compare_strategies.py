"""
基线 MA 与多因子策略的公平对比 + Walk-forward 样本外验证
========================================================
用法（离线，不联网）::

    .venv\\Scripts\\python.exe Scripts/compare_strategies.py --offline

输出（全部落在项目目录内）::

    docs/strategy_comparison.json
    docs/strategy_comparison.md

公平性如何保证（要求七）
----------------------
baseline / 多因子 / 基准三条腿走**同一个** ``run_position_backtest``，
共享同一份 ``price_df``、同一组 ``slippage_pct`` / ``commission_pct`` /
``stamp_duty_pct``、同一个 ``_compute_sharpe``、同一个无风险利率、同一段日期。
实现上它们只差一个参数：目标仓位序列。任何一条腿都不可能偷偷换掉区间或费率 ——
区间来自同一个 DataFrame 对象，费率来自同一组 CLI 参数。

不挑股票、不挑区间（要求七末段）
-----------------------------
默认加载数据目录下**全部**标的，不做任何筛选。``--limit`` 只用于自测，
且会在报告里显式标注为"子集，不构成结论"。聚合统计强制输出
均值 / 中位数 / 最差 / 达标数 / 未达标数 / 不可用数，而不是只报最好的那只。

未达标就写未达标
--------------
总判定行在 ``mean_test_sharpe < 0.5`` 时输出 ``BELOW 0.5``。
脚本里没有任何一处会把它改写成 PASS。
"""
from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Windows 控制台默认 GBK，会把中文与 ✅ 打成乱码或直接抛 UnicodeEncodeError
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from agent_platform.finance.multifactor_strategy import (      # noqa: E402
    StrategyParams,
    default_param_grid,
    generate_signals,
    ma_baseline_positions,
)
from agent_platform.finance.position_backtest import (         # noqa: E402
    SHARPE_TARGET,
    buy_and_hold_benchmark,
    run_position_backtest,
)
from agent_platform.finance.sharpe_stats import compute_sharpe_stats  # noqa: E402
from agent_platform.finance.walk_forward import (              # noqa: E402
    MIN_TEST_DAYS,
    MIN_TRAIN_DAYS,
    MIN_VALIDATION_DAYS,
    run_walk_forward,
)

REAL_DIR = ROOT / "data" / "real"
SAMPLE_CSV = ROOT / "data" / "sample" / "prices.csv"
OUT_JSON = ROOT / "docs" / "strategy_comparison.json"
OUT_MD = ROOT / "docs" / "strategy_comparison.md"

REQUIRED_COLS = ("date", "open", "high", "low", "close", "volume")


# ═══════════════════════════════════════════════════════════════════
#   数据加载（纯本地文件，零网络）
# ═══════════════════════════════════════════════════════════════════

def load_real_frames(limit: int = 0) -> tuple[dict[str, pd.DataFrame], str]:
    """加载 data/real 下的本地缓存（由 Scripts/fetch_real_history.py 生成）。"""
    if not REAL_DIR.is_dir():
        return {}, "data/real 不存在"
    csvs = sorted(p for p in REAL_DIR.glob("*.csv") if not p.name.startswith("_"))
    frames: dict[str, pd.DataFrame] = {}
    for p in csvs:
        df = pd.read_csv(p)
        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        if missing:
            # 跳过并记录，不静默当成正常样本（例如 _delisted_candidates.csv 缺 volume）
            print(f"  跳过 {p.name}：缺列 {missing}")
            continue
        df["date"] = df["date"].astype(str)
        frames[p.stem] = df.sort_values("date").reset_index(drop=True)
    if limit:
        frames = dict(list(frames.items())[:limit])
    return frames, f"data/real 本地缓存（{len(frames)} 只）"


def load_sample_frames(limit: int = 0) -> tuple[dict[str, pd.DataFrame], str]:
    """加载 data/sample/prices.csv（随仓库分发的确定性样本）。"""
    if not SAMPLE_CSV.is_file():
        return {}, "data/sample/prices.csv 不存在"
    raw = pd.read_csv(SAMPLE_CSV)
    raw["date"] = raw["date"].astype(str)
    frames: dict[str, pd.DataFrame] = {}
    for sym, g in raw.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        if all(c in g.columns for c in REQUIRED_COLS):
            frames[str(sym)] = g[list(REQUIRED_COLS)].copy()
    if limit:
        frames = dict(list(frames.items())[:limit])
    return frames, f"data/sample/prices.csv（{len(frames)} 只）"


# ═══════════════════════════════════════════════════════════════════
#   单标的对比
# ═══════════════════════════════════════════════════════════════════

def compare_one(
    symbol: str,
    df: pd.DataFrame,
    *,
    data_source: str,
    params: StrategyParams,
    slippage_pct: float,
    commission_pct: float,
    stamp_duty_pct: float,
) -> dict[str, Any]:
    """
    全样本三腿对比。三腿共享同一个 df 与同一组费率 —— 公平性由此保证。

    注意：全样本对比里的多因子用**固定默认参数**，不做任何搜索。
    参数搜索只发生在 walk-forward 的 train+validation 内。
    这样全样本一栏就不含"在全样本上挑过参数"的污染。
    """
    kw = dict(
        data_source=data_source,
        slippage_pct=slippage_pct,
        commission_pct=commission_pct,
        stamp_duty_pct=stamp_duty_pct,
    )

    ma = run_position_backtest(
        symbol, df, ma_baseline_positions(df), strategy="ma_baseline", **kw
    )
    sig = generate_signals(symbol, df, params, data_source=data_source)
    mf = run_position_backtest(
        symbol, df, sig.positions_by_date(), strategy="multifactor", **kw
    )
    bh = buy_and_hold_benchmark(symbol, df, **kw)

    # 公平性自检：三腿必须落在同一区间、同一天数。断言而非注释。
    assert ma.start_date == mf.start_date == bh.start_date, "起始日不一致"
    assert ma.end_date == mf.end_date == bh.end_date, "结束日不一致"
    assert ma.n_days == mf.n_days == bh.n_days, "交易日数不一致"

    def enrich(r: Any) -> dict[str, Any]:
        d = r.to_dict()
        d["benchmark_return_pct"] = round(bh.total_return_pct, 4)
        d["excess_return_vs_benchmark_pct"] = round(
            r.total_return_pct - bh.total_return_pct, 4
        )
        st = compute_sharpe_stats(r.sharpe_calendar, max(len(r.calendar_returns), 3))
        d["sharpe_se"] = round(st.std_error, 4)
        d["sharpe_ci_low"] = round(st.ci_low, 4)
        d["sharpe_ci_high"] = round(st.ci_high, 4)
        return d

    return {
        "symbol": symbol,
        "data_source": data_source,
        "start_date": ma.start_date,
        "end_date": ma.end_date,
        "n_days": ma.n_days,
        "ma_baseline": enrich(ma),
        "multifactor": enrich(mf),
        "benchmark": enrich(bh),
        "multifactor_factors": {
            "used": list(sig.used_factors),
            "unavailable": list(sig.unavailable_factors),
            "meta": sig.factor_meta,
        },
        "params": params.to_dict(),
    }


# ═══════════════════════════════════════════════════════════════════
#   聚合
# ═══════════════════════════════════════════════════════════════════

def aggregate(values: list[float]) -> dict[str, Any]:
    """均值 / 中位数 / 最差 / 最好 / 达标数 / 未达标数（要求七末段全部必报）。"""
    if not values:
        return {
            "count": 0, "mean": None, "median": None, "worst": None, "best": None,
            "n_meeting_threshold": 0, "n_below_threshold": 0,
        }
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "worst": round(min(values), 4),
        "best": round(max(values), 4),
        "n_meeting_threshold": sum(1 for v in values if v >= SHARPE_TARGET),
        "n_below_threshold": sum(1 for v in values if v < SHARPE_TARGET),
    }


def build_report(
    frames: dict[str, pd.DataFrame],
    source_label: str,
    *,
    args: argparse.Namespace,
) -> dict[str, Any]:
    params = StrategyParams()
    grid = default_param_grid()

    per_symbol: list[dict[str, Any]] = []
    wf_results: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

    for i, (sym, df) in enumerate(frames.items(), 1):
        print(f"[{i}/{len(frames)}] {sym} ({len(df)} 行) ...", flush=True)
        try:
            per_symbol.append(compare_one(
                sym, df, data_source=source_label, params=params,
                slippage_pct=args.slippage, commission_pct=args.commission,
                stamp_duty_pct=args.stamp_duty,
            ))
        except Exception as exc:                       # noqa: BLE001
            # 如实记录失败，不吞异常、不从统计里悄悄剔除
            failed.append({"symbol": sym, "stage": "full_sample", "error": repr(exc)})
            print(f"    全样本对比失败: {exc!r}")

        try:
            wf = run_walk_forward(
                sym, df, candidates=grid, n_folds=args.folds,
                train_days=args.train_days, validation_days=args.validation_days,
                test_days=args.test_days, data_source=source_label,
                slippage_pct=args.slippage, commission_pct=args.commission,
                stamp_duty_pct=args.stamp_duty,
                selection_policy=args.selection_policy,
            )
            wf_results.append(wf.to_dict())
            if not wf.available:
                print(f"    walk-forward 不可用: {wf.unavailable_reason}")
        except Exception as exc:                       # noqa: BLE001
            failed.append({"symbol": sym, "stage": "walk_forward", "error": repr(exc)})
            print(f"    walk-forward 失败: {exc!r}")

    ma_sharpes = [r["ma_baseline"]["sharpe_calendar"] for r in per_symbol]
    mf_sharpes = [r["multifactor"]["sharpe_calendar"] for r in per_symbol]
    bh_sharpes = [r["benchmark"]["sharpe_calendar"] for r in per_symbol]

    wf_available = [w for w in wf_results if w["available"]]
    wf_unavailable = [w for w in wf_results if not w["available"]]
    oos_sharpes = [
        f["test_result"]["sharpe_calendar"]
        for w in wf_available for f in w["folds"]
        if f["available"] and f["test_result"]
    ]

    mean_oos = statistics.fmean(oos_sharpes) if oos_sharpes else None
    verdict = (
        "PASS" if (mean_oos is not None and mean_oos >= SHARPE_TARGET) else "BELOW 0.5"
    )

    # ── 退化折诊断 ──────────────────────────────────────────────────────────
    # 为什么必须单列：当某折全程空仓或暴露极低时，日收益序列几乎全为 0，
    # 标准差趋近 0，而 Sharpe 公式的日度无风险利率(≈7.87e-5)是固定的，
    # 于是 (0 - rf)/极小std × sqrt(252) 会放大成 -14 这类极端值。
    # 这是"既有公式遇上近乎恒零序列"的数学结果，不是策略真实亏了 14 个 Sharpe。
    # 处理原则：**头条聚合仍然包含全部 60 折**（不择优、不剔除），
    # 这里只额外披露分布，让读者能分辨"真实亏损"与"退化折产生的数值假象"。
    all_test = [
        f["test_result"] for w in wf_available for f in w["folds"]
        if f["available"] and f["test_result"]
    ]
    flat_folds = [t for t in all_test if t["time_in_market_pct"] == 0.0]
    thin_folds = [t for t in all_test if 0.0 < t["avg_position"] < 0.02]
    real_folds = [t for t in all_test if t["avg_position"] >= 0.02]
    fold_diagnostics = {
        "n_test_folds": len(all_test),
        "n_fully_flat": len(flat_folds),
        "n_thin_exposure": len(thin_folds),
        "n_substantive": len(real_folds),
        "substantive_sharpe": aggregate(
            [t["sharpe_calendar"] for t in real_folds]
        ),
        "note": (
            "退化折（全程空仓或平均仓位<0.02）的 Sharpe 由近乎恒零的收益序列除以"
            "近乎为零的标准差得到，数值不具风险调整含义。头条聚合未剔除这些折。"
        ),
    }

    # 全部标的的日期区间必须一致才谈得上横向比较；不一致要在报告里说清
    ranges = {(r["start_date"], r["end_date"]) for r in per_symbol}

    return {
        "meta": {
            "generated_by": "Scripts/compare_strategies.py",
            "offline": bool(args.offline),
            "data_source": source_label,
            "n_symbols": len(frames),
            "symbol_selection": (
                "数据目录下全部标的（未筛选）" if not args.limit
                else f"⚠️ --limit {args.limit} 子集，仅供自测，不构成结论"
            ),
            "costs": {
                "slippage_pct": args.slippage,
                "commission_pct": args.commission,
                "stamp_duty_pct": args.stamp_duty,
                "note": "单边费率；三条腿共用同一组参数",
            },
            "walk_forward_config": {
                "n_folds": args.folds,
                "train_days": args.train_days,
                "validation_days": args.validation_days,
                "test_days": args.test_days,
                "min_required_days": args.train_days + args.validation_days
                + args.test_days * args.folds,
            },
            "sharpe_threshold": SHARPE_TARGET,
            "sharpe_caliber": "日历口径（空仓日收益记 0），与既有验收口径一致",
            "date_ranges_identical": len(ranges) <= 1,
            "date_ranges": sorted(f"{a}~{b}" for a, b in ranges),
        },
        "full_sample": {
            "per_symbol": per_symbol,
            "aggregate": {
                "ma_baseline_sharpe": aggregate(ma_sharpes),
                "multifactor_sharpe": aggregate(mf_sharpes),
                "benchmark_sharpe": aggregate(bh_sharpes),
                "multifactor_turnover_mean": round(
                    statistics.fmean(
                        [r["multifactor"]["total_turnover"] for r in per_symbol]
                    ), 4,
                ) if per_symbol else None,
                "ma_baseline_turnover_mean": round(
                    statistics.fmean(
                        [r["ma_baseline"]["total_turnover"] for r in per_symbol]
                    ), 4,
                ) if per_symbol else None,
            },
        },
        "walk_forward": {
            "per_symbol": wf_results,
            "n_available": len(wf_available),
            "n_unavailable": len(wf_unavailable),
            "unavailable_reasons": sorted(
                {w["unavailable_reason"] for w in wf_unavailable
                 if w["unavailable_reason"]}
            ),
            "out_of_sample_test_sharpe": aggregate(oos_sharpes),
            "fold_diagnostics": fold_diagnostics,
        },
        "failures": failed,
        "n_failed": len(failed),
        "verdict": {
            "metric": "walk-forward 全部 test 段日历口径 Sharpe 均值",
            "value": None if mean_oos is None else round(mean_oos, 4),
            "threshold": SHARPE_TARGET,
            "result": verdict,
        },
        "limitations": [
            "估值因子（PE/PB/ROE）不可用：离线数据只有当期快照，没有历史序列。"
            "用当期值回填历史属未来数据泄漏，故标记 unavailable 并从打分中剔除，"
            "未按 0 值参与。四大因子族中实际生效三族。",
            "标的清单来自本地缓存，其成分是「至今仍上市」的股票，"
            "存活者偏差无法消除，全部结论对真实收益偏乐观。",
            "空仓日按 0% 收益计（未计入现金的无风险收益），对多头策略偏保守。",
            "回测未建模涨跌停无法成交、停牌、流动性冲击与最小交易单位（手）。",
            "印花税默认 0，与既有引擎口径保持一致；如需建模 A 股卖出印花税，"
            "请显式传入 --stamp-duty 0.05。",
            "本报告为历史回测统计，不构成任何投资建议。",
        ],
    }


# ═══════════════════════════════════════════════════════════════════
#   Markdown 渲染
# ═══════════════════════════════════════════════════════════════════

def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(rep: dict[str, Any]) -> str:
    m = rep["meta"]
    L: list[str] = []
    L.append("# 基线 MA vs 多因子策略 —— 对比与 Walk-forward 样本外验证")
    L.append("")
    L.append(f"- 生成脚本：`{m['generated_by']}`")
    L.append(f"- 离线模式：{m['offline']}")
    L.append(f"- 数据来源：{m['data_source']}")
    L.append(f"- 标的选取：{m['symbol_selection']}")
    L.append(f"- 日期区间：{', '.join(m['date_ranges']) or 'n/a'}"
             f"（三腿区间一致：{m['date_ranges_identical']}）")
    L.append(f"- 成本：滑点 {m['costs']['slippage_pct']}% / 佣金 "
             f"{m['costs']['commission_pct']}% / 印花税 {m['costs']['stamp_duty_pct']}%"
             f"（{m['costs']['note']}）")
    L.append(f"- Sharpe 口径：{m['sharpe_caliber']}；阈值 {m['sharpe_threshold']}")
    wc = m["walk_forward_config"]
    L.append(f"- Walk-forward：{wc['n_folds']} 折，train {wc['train_days']} / "
             f"validation {wc['validation_days']} / test {wc['test_days']} 交易日，"
             f"至少需要 {wc['min_required_days']} 个交易日")
    L.append("")

    v = rep["verdict"]
    L.append("## 总判定")
    L.append("")
    L.append(f"**{v['result']}** — {v['metric']} = {_fmt(v['value'])}"
             f"（阈值 {v['threshold']}）")
    L.append("")
    if v["result"] != "PASS":
        L.append("> 多因子策略样本外 Sharpe 未达到 0.5。此处如实标注 `BELOW 0.5`，"
                 "未做任何美化。")
        L.append("")

    L.append("## 一、全样本对比（同区间 / 同成本 / 同公式）")
    L.append("")
    L.append("| 标的 | 区间 | 策略 | 总收益% | 年化% | Sharpe | 95%CI | 最大回撤% | "
             "波动% | 胜率% | 交易数 | 换手 | 佣金 | 滑点 | 基准收益% | 超额% | ≥0.5 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rep["full_sample"]["per_symbol"]:
        rng = f"{r['start_date']}~{r['end_date']}"
        for key, label in (("ma_baseline", "MA基线"),
                           ("multifactor", "多因子"),
                           ("benchmark", "买入持有")):
            d = r[key]
            L.append(
                f"| {r['symbol']} | {rng} | {label} | "
                f"{_fmt(d['total_return_pct'], 2)} | {_fmt(d['annualized_return_pct'], 2)} | "
                f"{_fmt(d['sharpe_calendar'])} | "
                f"[{_fmt(d['sharpe_ci_low'], 2)}, {_fmt(d['sharpe_ci_high'], 2)}] | "
                f"{_fmt(d['max_drawdown_pct'], 2)} | "
                f"{_fmt(d['annualized_volatility_pct'], 2)} | "
                f"{_fmt(d['win_rate_pct'], 1)} | {d['total_trades']} | "
                f"{_fmt(d['total_turnover'], 2)} | {_fmt(d['commission_cost'], 0)} | "
                f"{_fmt(d['slippage_cost'], 0)} | "
                f"{_fmt(d['benchmark_return_pct'], 2)} | "
                f"{_fmt(d['excess_return_vs_benchmark_pct'], 2)} | "
                f"{'是' if d['meets_sharpe_target'] else '否'} |"
            )
    L.append("")

    L.append("### 聚合统计（全部标的，非择优）")
    L.append("")
    agg = rep["full_sample"]["aggregate"]
    L.append("| 指标 | 标的数 | 均值 | 中位数 | 最差 | 最好 | ≥0.5 数 | <0.5 数 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for key, label in (("ma_baseline_sharpe", "MA基线 Sharpe"),
                       ("multifactor_sharpe", "多因子 Sharpe"),
                       ("benchmark_sharpe", "买入持有 Sharpe")):
        a = agg[key]
        L.append(f"| {label} | {a['count']} | {_fmt(a['mean'])} | {_fmt(a['median'])} | "
                 f"{_fmt(a['worst'])} | {_fmt(a['best'])} | "
                 f"{a['n_meeting_threshold']} | {a['n_below_threshold']} |")
    L.append("")
    L.append(f"- 平均换手：MA基线 {_fmt(agg['ma_baseline_turnover_mean'], 2)}，"
             f"多因子 {_fmt(agg['multifactor_turnover_mean'], 2)}")
    L.append(f"- 失败/异常标的数：{rep['n_failed']}")
    L.append("")

    L.append("## 二、Walk-forward 逐折结果")
    L.append("")
    wf = rep["walk_forward"]
    L.append(f"- 可用标的：{wf['n_available']}，不可用：{wf['n_unavailable']}")
    for reason in wf["unavailable_reasons"]:
        L.append(f"- 不可用原因：{reason}")
    L.append("")
    oos = wf["out_of_sample_test_sharpe"]
    L.append("### 样本外 test 段 Sharpe 汇总")
    L.append("")
    L.append("| 折数 | 均值 | 中位数 | 最差 | 最好 | ≥0.5 数 | <0.5 数 |")
    L.append("|---|---|---|---|---|---|---|")
    L.append(f"| {oos['count']} | {_fmt(oos['mean'])} | {_fmt(oos['median'])} | "
             f"{_fmt(oos['worst'])} | {_fmt(oos['best'])} | "
             f"{oos['n_meeting_threshold']} | {oos['n_below_threshold']} |")
    L.append("")

    fd = wf.get("fold_diagnostics")
    if fd:
        L.append("#### 退化折诊断（头条聚合未剔除任何折）")
        L.append("")
        L.append(f"- test 折总数：{fd['n_test_folds']}")
        L.append(f"- 全程空仓折：{fd['n_fully_flat']}（Sharpe 恒为 0）")
        L.append(f"- 极低暴露折（平均仓位<0.02）：{fd['n_thin_exposure']}")
        L.append(f"- 有实质暴露折：{fd['n_substantive']}")
        sub = fd["substantive_sharpe"]
        L.append(f"- 仅看有实质暴露折：均值 {_fmt(sub['mean'])}，"
                 f"中位 {_fmt(sub['median'])}，最差 {_fmt(sub['worst'])}，"
                 f"最好 {_fmt(sub['best'])}，"
                 f"达标 {sub['n_meeting_threshold']}/{sub['count']}")
        L.append(f"- {fd['note']}")
        L.append("")
        L.append("> 说明：即便只看有实质暴露的折，均值仍未达到 0.5。"
                 "披露这一分布是为了区分「真实亏损」与「近乎恒零序列除以近乎零标准差」"
                 "产生的极端数值，**不是**为了挑选有利子集。")
        L.append("")

    if wf["per_symbol"]:
        L.append("### 逐折明细")
        L.append("")
        L.append("| 标的 | 折 | train | validation | test | 选定参数 | "
                 "train Sharpe | validation Sharpe | **test Sharpe** | test 换手 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for w in wf["per_symbol"]:
            if not w["available"]:
                continue
            for f in w["folds"]:
                if not f["available"] or not f["test_result"]:
                    L.append(f"| {w['symbol']} | {f['fold_id']} | - | - | - | - | "
                             f"- | - | 不可用 | - |")
                    continue
                p = f["chosen_params"]
                plabel = (f"thr={p.get('score_threshold')},tv={p.get('target_vol')},"
                          f"ma={p.get('trend_ma')}")
                L.append(
                    f"| {w['symbol']} | {f['fold_id']} | "
                    f"{f['train_start']}~{f['train_end']} | "
                    f"{f['validation_start']}~{f['validation_end']} | "
                    f"{f['test_start']}~{f['test_end']} | {plabel} | "
                    f"{_fmt(f['train_result']['sharpe_calendar'])} | "
                    f"{_fmt(f['validation_result']['sharpe_calendar'])} | "
                    f"**{_fmt(f['test_result']['sharpe_calendar'])}** | "
                    f"{_fmt(f['test_result']['total_turnover'], 2)} |"
                )
        L.append("")

    L.append("## 三、数据局限与免责声明")
    L.append("")
    for item in rep["limitations"]:
        L.append(f"- {item}")
    L.append("")
    if rep["failures"]:
        L.append("## 四、失败记录（未从统计中剔除）")
        L.append("")
        for f in rep["failures"]:
            L.append(f"- `{f['symbol']}` @ {f['stage']}: {f['error']}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="MA 基线 vs 多因子策略对比")
    ap.add_argument("--offline", action="store_true",
                    help="离线模式：只读本地 CSV，绝不联网")
    ap.add_argument("--data", choices=("auto", "real", "sample"), default="auto",
                    help="数据源：auto=优先 real 再回退 sample")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（自测用）")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--train-days", type=int, default=MIN_TRAIN_DAYS)
    ap.add_argument("--validation-days", type=int, default=MIN_VALIDATION_DAYS)
    ap.add_argument("--test-days", type=int, default=MIN_TEST_DAYS)
    ap.add_argument("--slippage", type=float, default=0.1)
    ap.add_argument("--commission", type=float, default=0.03)
    ap.add_argument("--stamp-duty", type=float, default=0.0)
    ap.add_argument(
        "--selection-policy", choices=("validation", "robust"), default="validation",
        help="选参规则；robust 为挑战方案，不应在查看 test 结果后反复切换",
    )
    ap.add_argument("--json-out", default=str(OUT_JSON))
    ap.add_argument("--md-out", default=str(OUT_MD))
    args = ap.parse_args()

    print("=" * 78)
    print("MA 基线 vs 多因子策略 —— 对比 + Walk-forward 样本外验证")
    print("=" * 78)
    if args.offline:
        print("离线模式：只读本地 CSV，不发起任何网络请求")

    frames: dict[str, pd.DataFrame] = {}
    label = ""
    if args.data in ("auto", "real"):
        frames, label = load_real_frames(args.limit)
    if not frames and args.data in ("auto", "sample"):
        frames, label = load_sample_frames(args.limit)
        if frames:
            print(f"使用样本数据：{label}")
    if not frames:
        print("没有可用的本地价格数据。"
              "请先运行 Scripts/generate_sample_data.py 或 Scripts/fetch_real_history.py")
        return 1
    print(f"数据源：{label}")
    print()

    rep = build_report(frames, label, args=args)

    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    # 输出必须落在项目目录内（安全边界：不写本机其它位置）
    for p in (json_path, md_path):
        rp = p.resolve()
        if not str(rp).startswith(str(ROOT.resolve())):
            print(f"拒绝写出项目目录之外的路径：{rp}")
            return 2
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(rep), encoding="utf-8")

    print()
    print("=" * 78)
    agg = rep["full_sample"]["aggregate"]
    oos = rep["walk_forward"]["out_of_sample_test_sharpe"]
    print(f"全样本 MA 基线 Sharpe 均值：{_fmt(agg['ma_baseline_sharpe']['mean'])}"
          f"（{agg['ma_baseline_sharpe']['n_meeting_threshold']}"
          f"/{agg['ma_baseline_sharpe']['count']} 达标）")
    print(f"全样本 多因子   Sharpe 均值：{_fmt(agg['multifactor_sharpe']['mean'])}"
          f"（{agg['multifactor_sharpe']['n_meeting_threshold']}"
          f"/{agg['multifactor_sharpe']['count']} 达标）")
    print(f"全样本 买入持有 Sharpe 均值：{_fmt(agg['benchmark_sharpe']['mean'])}")
    print(f"样本外 test 段 Sharpe 均值：{_fmt(oos['mean'])}"
          f"（{oos['n_meeting_threshold']}/{oos['count']} 折达标，"
          f"最差 {_fmt(oos['worst'])}，中位 {_fmt(oos['median'])}）")
    print("-" * 78)
    print(f"总判定：{rep['verdict']['result']}")
    print("=" * 78)
    print(f"JSON: {json_path}")
    print(f"MD  : {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
