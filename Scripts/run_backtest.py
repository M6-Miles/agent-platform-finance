#!/usr/bin/env python
"""
Scripts/run_backtest.py
========================
回测执行脚本 — 对指定标的运行完整投研 + 回测流程，输出回测报告。

用法
----
    python Scripts/run_backtest.py                     # 使用内置样例数据 DEMO001
    python Scripts/run_backtest.py --symbol 000001     # 指定股票（需联网 + AkShare）
    python Scripts/run_backtest.py --symbol DEMO002 --capital 500000
    python Scripts/run_backtest.py --list              # 列出可用样例标的

⚠️  仅供研究参考，不构成投资建议。
"""
from __future__ import annotations

import argparse
import io
import sys
from datetime import date, timedelta
from pathlib import Path

# 强制 UTF-8 输出（兼容 Windows GBK 终端，否则打印 ⚠️/📥 等字符会抛 UnicodeEncodeError）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 将 src/ 加入模块搜索路径（脚本从项目根目录执行）
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

from agent_platform.finance.backtesting import run_backtest, BacktestResult
from agent_platform.finance.analysis import analyze_security
from agent_platform.finance.synthesis_agent import synthesize

DISCLAIMER = "⚠️  仅供研究参考，不构成投资建议。"

# 与 Scripts/generate_sample_data.py 的 DEMO_UNIVERSE 保持一致
SAMPLE_SYMBOLS = {
    "DEMO001": "内置样例标的 — 样例科技股（离线，无需联网）",
    "DEMO002": "内置样例标的 — 样例消费股（离线，无需联网）",
    "DEMO003": "内置样例标的 — 样例周期股（离线，无需联网）",
    "DEMO004": "内置样例标的 — 样例金融股（离线，无需联网）",
}


def _build_signals_from_analysis(
    price_df: pd.DataFrame,
    symbol: str,
) -> list[tuple[str, str]]:
    """
    对每个交易日调用 analyze_security + synthesize 生成信号。
    为节省时间，每 5 个交易日重新评估一次（周频再平衡）。
    """
    signals: list[tuple[str, str]] = []
    dates = price_df["date"].tolist()

    for i, d in enumerate(dates):
        if i % 5 != 0:  # 周频
            continue
        try:
            tech = analyze_security(symbol, start=dates[0], end=d)
            result = synthesize(
                symbol=symbol,
                technical=tech.to_dict(),
                fundamental={},  # 脚本仅用技术信号，其他路输入置空
                industry={},
                regime={},
            )
            if result.signal != "hold":
                signals.append((str(d), result.signal))
        except Exception:
            continue

    return signals


def run(
    symbol: str,
    capital: float,
    start: date,
    end: date,
    verbose: bool,
) -> BacktestResult:
    print(f"\n{'='*60}")
    print(f"  回测标的  : {symbol}")
    print(f"  回测区间  : {start} ~ {end}")
    print(f"  初始资金  : {capital:,.0f} 元")
    print(f"{'='*60}")
    print(DISCLAIMER)
    print()

    # 1. 拉取行情
    print("📥 拉取行情数据 ...")
    try:
        tech = analyze_security(symbol, start=start, end=end)
    except Exception as e:
        print(f"❌ 行情拉取失败: {e}")
        sys.exit(1)

    # 构建 price_df（SecurityAnalysisResult 的价格数据在 price_history 字段）
    if tech.price_history is not None and not tech.price_history.empty:
        price_df = tech.price_history[["date", "close"]].copy()
    else:
        print("❌ 无可用价格数据")
        sys.exit(1)

    print(f"   共 {len(price_df)} 个交易日")

    # 2. 生成信号（基于 SynthesisAgent）
    print("🤖 生成交易信号（周频再平衡）...")
    signals = _build_signals_from_analysis(price_df, symbol)
    buy_cnt = sum(1 for _, s in signals if s == "buy")
    sell_cnt = sum(1 for _, s in signals if s == "sell")
    print(f"   buy={buy_cnt}  sell={sell_cnt}  总信号={len(signals)}")

    # 3. 执行回测
    print("⚙️  运行回测引擎 ...")
    result = run_backtest(
        symbol=symbol,
        price_df=price_df,
        signals=signals,
        initial_capital=capital,
    )

    # 4. 输出报告
    print()
    print(result.to_markdown())
    print()
    print(DISCLAIMER)

    if verbose:
        print("\n--- 逐笔交易 ---")
        for t in result.trades:
            direction = "📈" if t.return_pct >= 0 else "📉"
            print(
                f"  {direction} {t.entry_date} 买入 → {t.exit_date} 卖出  "
                f"收益 {t.return_pct:+.2f}%"
            )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="回测执行脚本（仅供研究参考，不构成投资建议）"
    )
    parser.add_argument(
        "--symbol", default="DEMO001",
        help="标的代码，默认 DEMO001（内置样例）",
    )
    parser.add_argument(
        "--capital", type=float, default=1_000_000.0,
        help="初始资金（元），默认 100 万",
    )
    parser.add_argument(
        "--start",
        default=str(date.today() - timedelta(days=365)),
        help="开始日期 YYYY-MM-DD，默认一年前",
    )
    parser.add_argument(
        "--end",
        default=str(date.today()),
        help="结束日期 YYYY-MM-DD，默认今天",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="打印逐笔交易明细",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="列出可用样例标的",
    )
    args = parser.parse_args()

    if args.list:
        print("\n可用样例标的（离线，无需联网）：")
        for sym, desc in SAMPLE_SYMBOLS.items():
            print(f"  {sym}  —  {desc}")
        print()
        return

    result = run(
        symbol=args.symbol,
        capital=args.capital,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        verbose=args.verbose,
    )

    # 退出码：Sharpe ≥ 0.5 → 0，否则 1
    sharpe = result.sharpe_ratio or 0.0
    sys.exit(0 if sharpe >= 0.5 else 1)


if __name__ == "__main__":
    main()
