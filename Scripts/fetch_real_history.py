"""
拉取真实 A 股 10 年日线并落盘缓存
==================================
目的：把 E-01（回测夏普 > 0.5）从"不可证伪"变成"可证伪"。

为什么需要这一步
----------------
1 年日频数据的年化夏普标准误约 sqrt(252/250) ≈ 1.00。
2026-08-05 那轮实测 -0.440，95% CI 约 [-2.4, +1.5]，把 +0.5 整个包住 ——
无法区分"策略确实不行"与"样本太短看不出来"。
10 年（约 2430 个交易日）把 SE 压到 sqrt(252/2430) ≈ 0.32，
E-01 才第一次成为一个可检验的命题。

标的选择规则（事前设定，与收益无关）
------------------------------------
沪深300 大市值成分股，2016-08 之前已上市且持续交易，按行业分散挑选 20 只。
**不按历史收益挑选**，避免把结果选出来。

已知偏差（必须与结果一同披露）
------------------------------
生存者偏差：这 20 只都活到了 2026 年，退市/长期停牌的标的被结构性排除。
该偏差把收益推**高**。因此若最终夏普仍为负，是比表面数字更强的负面结论；
反之若为正，则不能排除偏差贡献。

用法
----
  python Scripts/fetch_real_history.py            # 只拉缺失的
  python Scripts/fetch_real_history.py --force    # 全部重拉
  python Scripts/fetch_real_history.py --summary   # 只看缓存现状
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CACHE_DIR = ROOT / "data" / "real"
START = date(2016, 8, 5)
END = date(2026, 8, 4)

# 沪深300 大市值、2016-08 前上市、按行业分散。选择依据是流动性与行业覆盖，
# 不是历史收益。修改这份名单等于改变实验对象，需在 progress.txt 记录原因。
SYMBOLS: list[tuple[str, str]] = [
    ("600519", "贵州茅台/白酒"),
    ("601318", "中国平安/保险"),
    ("600036", "招商银行/银行"),
    ("000858", "五粮液/白酒"),
    ("600276", "恒瑞医药/医药"),
    ("000333", "美的集团/家电"),
    ("601166", "兴业银行/银行"),
    ("600030", "中信证券/券商"),
    ("000651", "格力电器/家电"),
    ("601888", "中国中免/免税"),
    ("600887", "伊利股份/乳业"),
    ("002415", "海康威视/安防"),
    ("600009", "上海机场/交运"),
    ("601012", "隆基绿能/光伏"),
    ("600585", "海螺水泥/建材"),
    ("601088", "中国神华/煤炭"),
    ("000002", "万科A/地产"),
    ("600028", "中国石化/石化"),
    ("601398", "工商银行/银行"),
    ("002594", "比亚迪/新能源车"),
]


def cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.csv"


def summarize() -> int:
    import pandas as pd

    print("=" * 72)
    print("真实行情缓存现状")
    print("=" * 72)
    if not CACHE_DIR.exists():
        print(f"缓存目录不存在：{CACHE_DIR}")
        return 1

    total_rows = 0
    n_ok = 0
    print(f"{'symbol':<10}{'label':<20}{'rows':>7}  {'range':<26}")
    for sym, label in SYMBOLS:
        p = cache_path(sym)
        if not p.exists():
            print(f"{sym:<10}{label:<20}{'--':>7}  (缺失)")
            continue
        df = pd.read_csv(p)
        total_rows += len(df)
        n_ok += 1
        rng = f"{df['date'].iloc[0]} -> {df['date'].iloc[-1]}" if len(df) else "(空)"
        print(f"{sym:<10}{label:<20}{len(df):>7}  {rng:<26}")

    print("-" * 72)
    print(f"已缓存 {n_ok}/{len(SYMBOLS)} 只，共 {total_rows} 行")
    if n_ok:
        import math

        avg = total_rows / n_ok
        print(f"平均每股 {avg:.0f} 个交易日  →  单股夏普 SE ≈ sqrt(252/{avg:.0f}) = {math.sqrt(252/avg):.3f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略已有缓存，全部重拉")
    ap.add_argument("--summary", action="store_true", help="只打印缓存现状")
    args = ap.parse_args()

    if args.summary:
        return summarize()

    from agent_platform.finance.akshare_data_provider import AkShareMarketDataProvider

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    provider = AkShareMarketDataProvider()

    print("=" * 72)
    print(f"拉取真实 A 股日线（前复权）  {START} ~ {END}")
    print("=" * 72)
    print(f"标的 {len(SYMBOLS)} 只  →  {CACHE_DIR}")
    print()

    n_fetched = n_skipped = n_failed = 0
    failures: list[tuple[str, str]] = []
    t_start = time.time()

    for i, (sym, label) in enumerate(SYMBOLS, 1):
        p = cache_path(sym)
        if p.exists() and not args.force:
            print(f"[{i:2d}/{len(SYMBOLS)}] {sym} {label:<18} 已缓存，跳过")
            n_skipped += 1
            continue

        t0 = time.time()
        try:
            df = provider.get_price_history(sym, start=START, end=END)
            # 只留回测需要的列，缩小体积
            keep = ["date", "open", "high", "low", "close", "volume"]
            df = df[[c for c in keep if c in df.columns]].copy()
            df.insert(0, "symbol", sym)
            df.to_csv(p, index=False, encoding="utf-8")
            print(f"[{i:2d}/{len(SYMBOLS)}] {sym} {label:<18} "
                  f"{len(df):>5} 行  {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}  "
                  f"({time.time()-t0:.0f}s)")
            n_fetched += 1
        except Exception as exc:  # noqa: BLE001 — 单只失败不该中断整批
            msg = f"{type(exc).__name__}: {str(exc)[:150]}"
            print(f"[{i:2d}/{len(SYMBOLS)}] {sym} {label:<18} 失败  {msg}")
            failures.append((sym, msg))
            n_failed += 1

    print()
    print("-" * 72)
    print(f"新拉 {n_fetched}  跳过 {n_skipped}  失败 {n_failed}  "
          f"总耗时 {time.time()-t_start:.0f}s")
    if failures:
        print()
        print("失败明细（这些标的将不参与测量，需在结果中说明）：")
        for sym, msg in failures:
            print(f"  {sym}  {msg}")
    print()
    return summarize()


if __name__ == "__main__":
    raise SystemExit(main())
