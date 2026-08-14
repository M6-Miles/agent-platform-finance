#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收验证脚本（三项交付物）

用法：
  python Scripts/validate_deliverables.py               # 默认 offline 模式
  python Scripts/validate_deliverables.py --offline     # 纯离线，使用 SampleMarketDataProvider
  python Scripts/validate_deliverables.py --online      # 在线模式，调用 AkShare/Tushare 真实数据
  python Scripts/validate_deliverables.py --online --tushare-token <TOKEN>

A 验收（端到端流程）默认使用 LangGraph 作为主编排引擎。
offline 模式跳过真实联网请求，使用固定 SampleMarketDataProvider。
online 模式调用 AkShare（无需 Token）或 Tushare（需设置 TUSHARE_TOKEN 环境变量）。
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import date
from pathlib import Path

# 强制 UTF-8 输出（兼容 Windows GBK 终端）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pandas as pd

# ── 路径设置 ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "Scripts"))

from agent_platform.finance.backtesting import run_backtest
from agent_platform.core.harness_experiment import run_harness_effectiveness_experiment

DOCS_DIR = ROOT / "docs"
DOCS_DIR.mkdir(exist_ok=True)
REPORT_PATH = DOCS_DIR / "deliverables_report.md"

DISCLAIMER = "⚠️  仅供研究参考，不构成投资建议。"

# ── 样例标的集 ────────────────────────────────────────────────────────────────
from generate_sample_data import (  # noqa: E402
    TEST_UNIVERSE,
    write_dataset,
)

# 兼容原有解包形式 (symbol, name, base, vol)
STOCK_UNIVERSE = [(s, n, b, v) for s, n, b, v, _drift in TEST_UNIVERSE]

# 联网验收必须使用交易所真实代码。TEST001-TEST020 只属于离线确定性数据集，
# 将它们发给 AkShare 会把“样例代码不存在”误报成在线链路故障。
ONLINE_STOCK_UNIVERSE = [
    "600519", "000001", "601318", "600036", "000333",
    "000858", "300750", "002594", "601166", "600276",
    "000651", "601398", "601857", "600900", "002415",
    "300059", "601088", "600309", "000725", "601012",
]


def _ensure_sample_data() -> None:
    """确保样例数据存在（委托给确定性生成器）。"""
    write_dataset(force=False)


# ═══════════════════════════════════════════════════════════════════════════════
# A. 端到端流程（≥20只股票）
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# A. 端到端流程（≥20只股票）
# ═══════════════════════════════════════════════════════════════════════════════

def classify_preflight_results(results: list[dict]) -> dict[str, int]:
    """
    五路状态分类的纯函数（供单测）。

    分类规则：
    - execute: preflight == "execute"
    - manual_review: preflight == "manual_review" 或 status 包含"审批"
    - block: preflight == "block"
    - no_trade: preflight == "no_trade"
    - error: preflight == "error"

    五类互斥（按优先级），且总和 = len(results)。
    """
    n_execute = 0
    n_review = 0
    n_block = 0
    n_no_trade = 0
    n_error = 0

    for r in results:
        pf = r.get("preflight")
        if pf == "execute":
            n_execute += 1
        elif pf == "manual_review" or ("审批" in r.get("status", "") and not pf):
            n_review += 1
        elif pf == "block":
            n_block += 1
        elif pf == "error":
            n_error += 1
        elif pf == "no_trade":
            n_no_trade += 1
        else:
            # 未知状态归入 error（防御性）
            n_error += 1

    return {
        "execute": n_execute,
        "manual_review": n_review,
        "block": n_block,
        "no_trade": n_no_trade,
        "error": n_error,
    }


def classify_graph_state(state: dict) -> str:
    """把一次 LangGraph 返回状态归一化为五路验收结果。

    ``graph.invoke`` 在 ``interrupt()`` 处暂停时，节点尚未返回，因此
    ``final_action`` 仍可能为空，状态也可能保留为 ``trading``。唯一可靠的
    暂停标志是 LangGraph 返回的 ``__interrupt__``。过去把这种状态默认为
    ``no_trade``，会把人工审批路径错误计入低置信度退出。
    """
    action = state.get("final_action")
    if action in {"execute", "manual_review", "block", "no_trade", "error"}:
        return action
    if state.get("__interrupt__"):
        return "manual_review"
    if state.get("har_required"):
        return "manual_review"
    status = str(state.get("status") or "")
    if status == "no_trade":
        return "no_trade"
    if status == "error" or state.get("errors"):
        return "error"
    # final_action 为空且没有中断/明确终态，说明工作流停在未知状态。
    # 防御性归为 error，不能再静默美化为 no_trade。
    return "error"


def _print_e2e_summary(results: list[dict]) -> None:
    """打印五路计数汇总（execute/manual_review/block/no_trade/error）。"""
    counts = classify_preflight_results(results)
    print(f"\n  结果汇总 ({len(results)}/20):")
    print(f"    ✅ execute       : {counts['execute']}")
    print(f"    ⚠️  manual_review : {counts['manual_review']}")
    print(f"    ⛔ block          : {counts['block']}")
    print(f"    ⚪ no_trade       : {counts['no_trade']}  （正常完成但不交易，非错误）")
    print(f"    ❌ error          : {counts['error']}  （真正异常）")
    print("  （仅 execute 计为完全通过风控，引擎：LangGraph）")


def run_e2e_batch_langgraph() -> list[dict]:
    """【主路径】使用 LangGraph 编排完整投研流程，覆盖 ≥20 只股票。

    LangGraph 是生产入口；每只股票通过 run_securities_analysis() 调用已编译图。
    online 模式下节点内部会尝试 AkShare；失败时自动降级到 sample 数据。
    """
    from agent_platform.finance.securities_graph import (
        build_securities_graph,
        run_securities_analysis,
    )

    print("\n" + "="*64)
    print("  验收 A：端到端投研流程（≥20只股票）— LangGraph 引擎")
    print("="*64)

    graph = build_securities_graph()
    results = []
    symbols = ONLINE_STOCK_UNIVERSE

    for i, symbol in enumerate(symbols, 1):
        t0 = time.time()
        try:
            state = run_securities_analysis(symbol=symbol, graph=graph)
            elapsed = time.time() - t0

            action = classify_graph_state(state)
            status_val = state.get("status", "unknown")

            if action == "execute":
                status = "✅ execute"
            elif action == "manual_review":
                status = "⚠️ manual_review"
            elif action == "block":
                status = "⛔ block"
            elif status_val == "error":
                status = "❌ error"
                action = "error"
            else:
                # no_trade / 低置信度
                status = "⚪ no_trade"
                action = "no_trade"

            syn = state.get("synthesis") or {}
            tech = state.get("technical_analysis") or {}
            errors = state.get("errors") or []
            results.append({
                "symbol": symbol,
                "signal": syn.get("signal", "—"),
                "confidence": syn.get("confidence", 0.0),
                "total_return_pct": tech.get("total_return_pct", 0.0),
                "elapsed_s": round(elapsed, 2),
                "status": status,
                "preflight": action,
                "errors": errors,
            })
            print(f"  [{i:02d}/20] {symbol}  信号={syn.get('signal','—'):<5}  "
                  f"置信度={syn.get('confidence', 0.0):.0%}  "
                  f"preflight={action}  "
                  f"耗时={elapsed:.2f}s")
        except Exception as e:
            elapsed = time.time() - t0
            results.append({
                "symbol": symbol, "status": f"❌ {e}",
                "preflight": "error", "elapsed_s": round(elapsed, 2),
            })
            print(f"  [{i:02d}/20] {symbol}  ❌ {e}")

    _print_e2e_summary(results)
    return results


def run_e2e_batch_offline() -> list[dict]:
    """【离线路径】通过 LangGraph（data_mode="offline"）运行，零网络调用。

    与在线路径使用完全相同的 graph.invoke 路径，仅通过 data_mode 注入区分。
    节点内部检测 data_mode=="offline" 时会传 force_offline=True 给各 Agent，
    跳过 AkShare / Tushare 调用，直接使用 SampleMarketDataProvider 样例数据。
    """
    from agent_platform.finance.securities_graph import (
        build_securities_graph,
        run_securities_analysis,
    )

    print("\n" + "="*64)
    print("  验收 A：端到端投研流程（≥20只股票）— offline 模式（LangGraph）")
    print("="*64)

    graph = build_securities_graph()
    results = []
    symbols = [s[0] for s in STOCK_UNIVERSE]

    for i, symbol in enumerate(symbols, 1):
        t0 = time.time()
        try:
            # 两条路径唯一差异：data_mode="offline" 注入
            state = run_securities_analysis(
                symbol=symbol,
                graph=graph,
                data_mode="offline",
            )
            elapsed = time.time() - t0

            action = classify_graph_state(state)
            status_val = state.get("status", "unknown")

            if action == "execute":
                status = "✅ execute"
            elif action == "manual_review":
                status = "⚠️ manual_review"
            elif action == "block":
                status = "⛔ block"
            elif status_val == "error":
                status = "❌ error"
                action = "error"
            else:
                status = "⚪ no_trade"
                action = "no_trade"

            syn = state.get("synthesis") or {}
            tech = state.get("technical_analysis") or {}
            results.append({
                "symbol": symbol,
                "signal": syn.get("signal", "—"),
                "confidence": syn.get("confidence", 0.0),
                "total_return_pct": tech.get("total_return_pct", 0.0),
                "elapsed_s": round(elapsed, 2),
                "status": status,
                "preflight": action,
            })
            print(f"  [{i:02d}/20] {symbol}  信号={syn.get('signal', '—'):<5}  "
                  f"置信度={syn.get('confidence', 0.0):.0%}  "
                  f"收益={tech.get('total_return_pct', 0.0):+.1f}%  "
                  f"preflight={action}  "
                  f"耗时={elapsed:.2f}s")
        except Exception as e:
            results.append({"symbol": symbol, "status": f"❌ {e}", "preflight": "error"})
            print(f"  [{i:02d}/20] {symbol}  ❌ {e}")

    _print_e2e_summary(results)
    return results


# 两条路径均走 LangGraph；区别仅是 data_mode
def run_e2e_batch(online: bool = False) -> list[dict]:
    """端到端批量验证入口。

    online=True  → run_e2e_batch_langgraph()，data_mode="auto"（AkShare 优先）
    online=False → run_e2e_batch_offline()，data_mode="offline"（零网络）

    两者均通过 run_securities_analysis(graph=g, data_mode=...) 调用 LangGraph。
    """
    if online:
        return run_e2e_batch_langgraph()
    return run_e2e_batch_offline()


# ═══════════════════════════════════════════════════════════════════════════════
# B. 回测验证（Sharpe ≥ 0.5）
# ═══════════════════════════════════════════════════════════════════════════════

def _ma_crossover_signals(price_df: pd.DataFrame) -> list[tuple[str, str]]:
    """MA5/MA20 金叉死叉信号生成。"""
    df = price_df.copy()
    df["ma5"]  = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df = df.dropna().reset_index(drop=True)

    signals: list[tuple[str, str]] = []
    prev_above = df["ma5"].iloc[0] > df["ma20"].iloc[0]
    for _, row in df.iterrows():
        above = row["ma5"] > row["ma20"]
        if above and not prev_above:
            signals.append((str(row["date"]), "buy"))
        elif not above and prev_above:
            signals.append((str(row["date"]), "sell"))
        prev_above = above
    return signals


def run_backtest_batch() -> list[dict]:
    print("\n" + "="*64)
    print("  验收 B：回测 Sharpe ≥ 0.5 验证")
    print("="*64)

    from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
    provider = SampleMarketDataProvider()
    results = []

    for symbol, name, _, _ in STOCK_UNIVERSE:
        try:
            price_df = provider.get_price_history(symbol)
            price_df["date"] = price_df["date"].astype(str)
            signals = _ma_crossover_signals(price_df)
            bt = run_backtest(symbol, price_df, signals, initial_capital=1_000_000)
            sharpe = bt.sharpe_ratio or 0.0
            flag = "✅" if sharpe >= 0.5 else "⚠️ "
            results.append({
                "symbol": symbol, "name": name,
                "total_return_pct": bt.total_return_pct,
                "sharpe_ratio": sharpe,
                "max_drawdown_pct": bt.max_drawdown_pct,
                "win_rate_pct": bt.win_rate_pct,
                "total_trades": bt.total_trades,
                "flag": flag,
            })
            print(f"  {flag} {symbol}  Sharpe={sharpe:+.2f}  "
                  f"收益={bt.total_return_pct:+.1f}%  "
                  f"回撤={bt.max_drawdown_pct:.1f}%  "
                  f"胜率={bt.win_rate_pct:.1f}%  "
                  f"交易={bt.total_trades}次")
        except Exception as e:
            results.append({"symbol": symbol, "name": name, "flag": "❌", "error": str(e)})
            print(f"  ❌ {symbol}: {e}")

    passed = [r for r in results if r.get("sharpe_ratio", -99) >= 0.5]
    avg_sharpe = (sum(r.get("sharpe_ratio", 0) for r in results if "sharpe_ratio" in r)
                  / max(1, len([r for r in results if "sharpe_ratio" in r])))
    print(f"\n  结果: {len(passed)}/20 只 Sharpe ≥ 0.5  |  平均 Sharpe = {avg_sharpe:.2f}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# C. Harness 有效性对照实验
# ═══════════════════════════════════════════════════════════════════════════════

def run_harness_exp() -> dict:
    print("\n" + "="*64)
    print("  验收 C：Harness 有效性对照实验")
    print("="*64)

    exp = run_harness_effectiveness_experiment()

    print(f"  幻觉拦截率   (Harness ON)  : {exp.hallucination_blocked_rate:.1%}")
    print(f"  误报率       (正常被拦截)  : {exp.false_positive_rate:.1%}")
    print(f"  无保护通过率 (Harness OFF) : {exp.pass_rate_no_harness:.1%}")
    print()

    hallucination_reduction = exp.hallucination_blocked_rate

    print(f"  ▶ Harness 使幻觉输出通过率从 {exp.pass_rate_no_harness:.1%} 下降至"
          f" {1-exp.hallucination_blocked_rate:.1%}，"
          f"降低 {hallucination_reduction:.1%}")
    print(f"  ▶ 误报率 {exp.false_positive_rate:.1%} —— 正常输出几乎不受影响")
    print(f"  ▶ 模拟无效下游调用减少 {exp.invalid_call_reduction_rate:.1%} "
          f"({exp.invalid_calls_no_harness} → {exp.invalid_calls_with_harness})")
    print(f"  ▶ 端到端正确处理率 {exp.e2e_correct_rate_no_harness:.1%} → "
          f"{exp.e2e_correct_rate_with_harness:.1%}")

    return {
        "hallucination_blocked_rate": exp.hallucination_blocked_rate,
        "false_positive_rate": exp.false_positive_rate,
        "pass_rate_no_harness": exp.pass_rate_no_harness,
        "invalid_calls_no_harness": exp.invalid_calls_no_harness,
        "invalid_calls_with_harness": exp.invalid_calls_with_harness,
        "invalid_call_reduction_rate": exp.invalid_call_reduction_rate,
        "e2e_correct_rate_no_harness": exp.e2e_correct_rate_no_harness,
        "e2e_correct_rate_with_harness": exp.e2e_correct_rate_with_harness,
        "markdown": exp.to_markdown(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 生成 Markdown 报告
# ═══════════════════════════════════════════════════════════════════════════════

def write_report(e2e: list[dict], bt: list[dict], harness: dict) -> None:
    n_execute  = sum(1 for r in e2e if r.get("preflight") == "execute")
    n_review   = sum(1 for r in e2e if r.get("preflight") in ("manual_review", "需人工审批")
                     or "审批" in r.get("status", ""))
    n_block    = sum(1 for r in e2e if r.get("preflight") == "block")
    n_no_trade = sum(1 for r in e2e if r.get("preflight") == "no_trade")
    n_error    = sum(1 for r in e2e if r.get("preflight") == "error")
    n_completed = len(e2e) - n_error
    bt_ok  = [r for r in bt if r.get("sharpe_ratio", -99) >= 0.5]
    avg_sharpe = (sum(r.get("sharpe_ratio", 0) for r in bt if "sharpe_ratio" in r)
                  / max(1, len([r for r in bt if "sharpe_ratio" in r])))

    lines = [
        "# 交付物验证报告",
        "",
        f"> 生成时间: {date.today()}  |  {DISCLAIMER}",
        "",
        "## 验收 A：端到端投研流程 ≥20只股票",
        "",
        (f"**工作流完成: {n_completed}/20；其中 {n_execute} 只通过执行前检查（execute）"
         f"  |  {n_review} 只待人工复核  |  {n_block} 只已阻断"
         f"  |  {n_no_trade} 只不交易（no_trade）  |  {n_error} 只真正异常**  "
         f"{'✅ 20只均完成且无异常' if n_completed >= 20 else '⚠️ 未完成20只无异常验收'}"),
        "",
        "| 股票代码 | 信号 | 置信度 | 年化收益 | 耗时(s) | Preflight | 状态 |",
        "|---------|------|--------|---------|---------|-----------|------|",
    ]
    for r in e2e:
        pf = r.get("preflight", "—")
        if r["status"].startswith("✅") or r["status"].startswith("⚠️") or r["status"].startswith("⛔"):
            lines.append(
                f"| {r['symbol']} | {r.get('signal','—')} | "
                f"{r.get('confidence', 0):.0%} | "
                f"{r.get('total_return_pct', 0):+.1f}% | "
                f"{r.get('elapsed_s', 0):.2f} | {pf} | {r['status']} |"
            )
        else:
            lines.append(f"| {r['symbol']} | — | — | — | — | {pf} | {r['status']} |")

    lines += [
        "",
        "## 验收 B：回测 Sharpe ≥ 0.5",
        "",
        f"**结果: {len(bt_ok)}/20 只 Sharpe ≥ 0.5  |  平均 Sharpe = {avg_sharpe:.2f}**  "
        f"{'✅ 通过' if avg_sharpe >= 0.5 else '⚠️ 低于基线'}",
        "",
        "| 股票 | Sharpe | 总收益 | 最大回撤 | 胜率 | 交易次数 |",
        "|------|--------|--------|---------|------|---------|",
    ]
    for r in bt:
        if "sharpe_ratio" in r:
            lines.append(
                f"| {r['symbol']} | {r['sharpe_ratio']:+.2f} | "
                f"{r['total_return_pct']:+.1f}% | "
                f"{r['max_drawdown_pct']:.1f}% | "
                f"{r['win_rate_pct']:.1f}% | {r['total_trades']} |"
            )

    lines += [
        "",
        "## 验收 C：Harness 有效性对照实验",
        "",
        "| 指标 | 数值 | 含义 |",
        "|------|------|------|",
        f"| 幻觉拦截率 | **{harness['hallucination_blocked_rate']:.1%}** | Harness 开启时，幻觉输出被拦截的比例（越高越好）|",
        f"| 误报率 | **{harness['false_positive_rate']:.1%}** | 正常输出被误拦截的比例（越低越好）|",
        f"| 无保护通过率 | **{harness['pass_rate_no_harness']:.1%}** | 不使用 Harness 时幻觉直接通过的比例 |",
        f"| 模拟无效调用减少 | **{harness['invalid_call_reduction_rate']:.1%}** | 固定评测集上的模拟下游动作，不代表生产 API 流量 |",
        f"| 端到端正确处理率 | **{harness['e2e_correct_rate_no_harness']:.1%} → {harness['e2e_correct_rate_with_harness']:.1%}** | 固定评测集正确放行/拦截比例 |",
        "",
        f"**结论：** Harness 使幻觉输出通过率从 "
        f"{harness['pass_rate_no_harness']:.1%} 降至 "
        f"{1-harness['hallucination_blocked_rate']:.1%}，"
        f"降幅 **{harness['hallucination_blocked_rate']:.1%}**，"
        f"误报率仅 {harness['false_positive_rate']:.1%}。",
        "",
        "### 实验详细数据",
        "",
        harness["markdown"],
        "",
        "---",
        f"*{DISCLAIMER}*",
    ]

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📄 完整报告已写入: {REPORT_PATH}")


# ═══════════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="验证三项交付物（A：端到端 ≥20只 / B：Sharpe ≥ 0.5 / C：Harness 有效性）"
    )
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument(
        "--offline", action="store_true", default=False,
        help="离线模式：使用 SampleMarketDataProvider，不访问公网（默认）",
    )
    mode_grp.add_argument(
        "--online", action="store_true", default=False,
        help="在线模式：调用 AkShare/Tushare 真实行情，需要网络",
    )
    parser.add_argument(
        "--tushare-token", default=None,
        help="Tushare API Token（也可通过环境变量 TUSHARE_TOKEN 传入）",
    )
    args = parser.parse_args()

    online = args.online   # --offline 或什么都不传 → offline
    if args.tushare_token:
        os.environ["TUSHARE_TOKEN"] = args.tushare_token

    mode_label = "🌐 online（AkShare/Tushare + LangGraph）" if online else "📦 offline（SampleDataProvider）"
    print(f"\n🚀 开始验证三项交付物... 模式：{mode_label}\n")
    _ensure_sample_data()

    e2e_results  = run_e2e_batch(online=online)
    bt_results   = run_backtest_batch()
    harness_data = run_harness_exp()

    write_report(e2e_results, bt_results, harness_data)

    # 汇总（严格：只有 preflight=execute 才算完全通过）
    n_execute  = sum(1 for r in e2e_results if r.get("preflight") == "execute")
    n_review   = sum(1 for r in e2e_results if r.get("preflight") in ("manual_review",)
                     or "审批" in r.get("status", ""))
    n_block    = sum(1 for r in e2e_results if r.get("preflight") == "block")
    n_no_trade = sum(1 for r in e2e_results if r.get("preflight") == "no_trade")
    n_error    = sum(1 for r in e2e_results if r.get("preflight") == "error")
    bt_vals = [r.get("sharpe_ratio", -99) for r in bt_results if "sharpe_ratio" in r]
    avg_sharpe = sum(bt_vals) / max(1, len(bt_vals))
    hr = harness_data["hallucination_blocked_rate"]

    print("\n" + "="*64)
    print("  三项交付物验证汇总")
    print("="*64)
    print(f"  模式：{mode_label}")
    print(f"  编排引擎：{'LangGraph 1.x（online AkShare/Tushare）' if online else 'LangGraph 1.x（offline SampleMarketDataProvider）'}")
    print("  A. 端到端 ≥20只")
    completed = len(e2e_results) - n_error
    print(f"     工作流完成       : {completed}/20  {'✅ PASS' if completed >= 20 else '⚠️  PARTIAL'}")
    print(f"     ✅ execute       : {n_execute}/20  （执行决策分布，不是工作流完成率）")
    print(f"     ⚠️  manual_review : {n_review}")
    print(f"     ⛔ block          : {n_block}")
    print(f"     ⚪ no_trade       : {n_no_trade}  （正常完成但不交易，非错误）")
    print(f"     ❌ error          : {n_error}  （真正异常）")
    print(f"  B. 平均 Sharpe     : {avg_sharpe:.2f}  {'✅ PASS' if avg_sharpe >= 0.5 else '⚠️  BELOW 0.5'}")
    print(f"  C. 幻觉拦截率      : {hr:.1%}  {'✅ PASS' if hr >= 0.6 else '⚠️  CHECK'}")
    print("="*64)
    print(f"\n  {DISCLAIMER}")


if __name__ == "__main__":
    main()

