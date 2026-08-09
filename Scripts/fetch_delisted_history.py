"""
拉取**已退市**标的的十年日线并落盘缓存
======================================
目的：为"量化（而非消除）存活者偏差"提供数据。这是 E-01 待办三项中的第③项。

为什么需要这一步
----------------
data/real/ 里那 20 只标的全部活到 2026 年 —— 退市、暴雷、长期停牌的公司被
结构性排除在外。这使**所有**基于该样本的数字对真实可实现收益偏乐观，
偏差方向确定（偏高），但量级此前一直未知，只能在报告里写"偏乐观"。
本脚本把"未知量级"变成"可测量的差值"。

标的选择规则（事前设定，与收益完全无关）
----------------------------------------
规则：**全部** A 股代码中，满足「2016-08-05 之前已上市」且「在 2016-08-05 ~
2026-08-04 窗口内退市/终止上市」者，全取，不抽样、不筛选。
来源：ak.stock_info_sh_delist + ak.stock_info_sz_delist（229 只，去重后）。
排除项只有一条且与收益无关：B 股（900/200 开头，以外币计价，不可比）。

**刻意不做**的事：不按跌幅挑选、不剔除"跌得太惨"的极端个例。
剔除极端个例正是存活者偏差本身的机制，在测量偏差的脚本里重演它会使结果失效。

数据源差异（必须记录）
----------------------
存活标的走 ak.stock_zh_a_daily（新浪）。该接口对已退市代码一律
JSONDecodeError（已用存活标的 sh600519 做对照，同一接口正常返回 2426 行，
故失败是退市专有而非接口故障）。

退市标的改走 ak.stock_zh_a_hist_tx（腾讯），同样取前复权（adjust="qfq"）。
选腾讯而非东财的原因是实测约束，不是偏好：东财 ak.stock_zh_a_hist 首次探测
成功（600005/000748/002604 均返回数据），但连续请求后 push2his.eastmoney.com
对本机 IP 一律 RemoteDisconnected —— 用存活标的 600519 做对照同样失败，
且 quote.eastmoney.com 根路径仍返回 200，故是该 API 主机的限流而非退市专有、
也不是网络故障。腾讯源对同样三只标的返回**完全一致**的行数与起止日
（70/55/605 行，起止日逐一相同），这一致性本身是一次有用的跨源交叉校验。

两个源的复权基准仍可能有细微差异。该差异在比较时表现为噪声而非系统性偏向，
测量脚本中已就此说明；若某日发现两源系统性偏离，应重测而非取其一。

已知局限
--------
东财对部分退市标的只回传其生命末段（例：600005 仅 70 行，始于 2016-10-10，
尽管它 1999 年就上市）。这类标的的历史被截断，会在测量脚本中按
最小交易日门槛处理并如实报告剔除数量与原因，不静默丢弃。

用法
----
  python Scripts/fetch_delisted_history.py --candidates   # 只生成候选名单
  python Scripts/fetch_delisted_history.py                # 拉缺失的
  python Scripts/fetch_delisted_history.py --limit 20     # 先试 20 只
  python Scripts/fetch_delisted_history.py --summary      # 只看缓存现状
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CACHE_DIR = ROOT / "data" / "real_delisted"
CANDIDATES_CSV = ROOT / "data" / "real" / "_delisted_candidates.csv"

START = date(2016, 8, 5)
END = date(2026, 8, 4)

# 与存活样本一致的列
KEEP = ["date", "open", "high", "low", "close", "volume"]

# 腾讯源已返回英文列名；东财源（备用路径）返回中文。两套都映射，
# 便于日后东财解封时切回而不必改动下游。
CN_MAP = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
}

# 抓取节流参数。上一版对东财一秒内连发 6 次即被该 API 主机封掉（连存活标的
# 对照也失败），因此这里默认逐只间隔 + 指数退避，而不是能跑多快跑多快。
RETRIES = 3
BACKOFF_S = 3.0        # 首次退避秒数，之后每次翻倍
SLEEP_S = 0.6          # 成功后到下一只之间的固定间隔
# 退市日之后再多取几天：终止上市日与最后交易日往往不是同一天。
DELIST_BUFFER_DAYS = 10
# MA20 信号所需的最短长度（预热 26 日 + 若干个再平衡周期）。
# 不足此长度的标的不静默丢弃，由测量脚本如实报告剔除数与原因。
MIN_TRADING_DAYS = 60


def build_candidates() -> "object":
    """按事前规则生成退市候选名单。规则见模块 docstring。"""
    import akshare as ak
    import pandas as pd

    sh = ak.stock_info_sh_delist()
    sh = sh.rename(columns={
        "公司代码": "code", "公司简称": "name",
        "上市日期": "listed", "暂停上市日期": "delisted",
    })
    sh["mkt"] = "sh"

    sz = ak.stock_info_sz_delist()
    sz = sz.rename(columns={
        "证券代码": "code", "证券简称": "name",
        "上市日期": "listed", "终止上市日期": "delisted",
    })
    sz["mkt"] = "sz"

    both = pd.concat([sh, sz], ignore_index=True)[
        ["code", "name", "listed", "delisted", "mkt"]
    ]
    both["code"] = both["code"].astype(str).str.zfill(6)
    both["listed"] = pd.to_datetime(both["listed"], errors="coerce")
    both["delisted"] = pd.to_datetime(both["delisted"], errors="coerce")

    n_raw = len(both)

    # 唯一的排除项：B 股（外币计价，与 A 股不可比）
    is_b = both["code"].str.startswith(("900", "200"))
    n_b = int(is_b.sum())
    both = both[~is_b]

    lo, hi = pd.Timestamp(START), pd.Timestamp(END)
    sel = both[
        (both["listed"] < lo) & (both["delisted"] >= lo) & (both["delisted"] <= hi)
    ].copy()
    # 同一代码可能在两个表里各出现一次（如 600680），保留最早的退市日
    sel = sel.sort_values("delisted").drop_duplicates(subset="code", keep="first")
    sel = sel.sort_values("delisted").reset_index(drop=True)

    CANDIDATES_CSV.parent.mkdir(parents=True, exist_ok=True)
    out = sel.copy()
    out["listed"] = out["listed"].dt.date
    out["delisted"] = out["delisted"].dt.date
    out.to_csv(CANDIDATES_CSV, index=False, encoding="utf-8")

    print(f"原始退市记录      {n_raw}")
    print(f"排除 B 股         {n_b}")
    print(f"候选（去重后）    {len(sel)}   →  {CANDIDATES_CSV.name}")
    print()
    print("按退市年份分布：")
    for yr, cnt in sel["delisted"].dt.year.value_counts().sort_index().items():
        print(f"  {yr}   {cnt:>3}")
    return sel


def load_candidates() -> list[tuple[str, str, str, str]]:
    """→ [(code, name, delisted_date_str, mkt_prefix), ...]

    mkt 取自候选表而非由代码前缀推断：688/605 之类的新板号段用前缀判断易错，
    而交易所自己的退市表已经给了归属。
    """
    import pandas as pd

    if not CANDIDATES_CSV.exists():
        print(f"候选名单不存在，先生成：{CANDIDATES_CSV}")
        build_candidates()
    df = pd.read_csv(CANDIDATES_CSV, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    return [
        (r.code, str(r.name_), str(r.delisted), str(r.mkt))
        for r in df.rename(columns={"name": "name_"}).itertuples(index=False)
    ]


def cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.csv"


def summarize() -> int:
    import pandas as pd

    print("=" * 72)
    print("退市标的行情缓存现状")
    print("=" * 72)
    if not CACHE_DIR.exists():
        print(f"缓存目录不存在：{CACHE_DIR}")
        return 1

    files = sorted(CACHE_DIR.glob("*.csv"))
    files = [f for f in files if not f.name.startswith("_")]
    if not files:
        print("（空）")
        return 1

    total_rows = 0
    short = 0
    for f in files:
        df = pd.read_csv(f)
        total_rows += len(df)
        if len(df) < 60:
            short += 1

    print(f"已缓存 {len(files)} 只，共 {total_rows} 行，平均 {total_rows/len(files):.0f} 行/只")
    print(f"其中不足 60 个交易日（MA20 信号不足）：{short} 只")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", action="store_true", help="只生成候选名单")
    ap.add_argument("--summary", action="store_true", help="只打印缓存现状")
    ap.add_argument("--force", action="store_true", help="忽略已有缓存，全部重拉")
    ap.add_argument("--limit", type=int, default=0, help="只拉前 N 只（试跑用）")
    args = ap.parse_args()

    if args.candidates:
        build_candidates()
        return 0
    if args.summary:
        return summarize()

    import akshare as ak
    import pandas as pd

    cands = load_candidates()
    if args.limit:
        cands = cands[: args.limit]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print(f"拉取退市标的日线（腾讯，前复权）  {START} ~ {END}")
    print("=" * 72)
    print(f"标的 {len(cands)} 只  →  {CACHE_DIR}")
    print(f"每只最多重试 {RETRIES} 次，退避 {BACKOFF_S}s 起倍增")
    print()

    n_fetched = n_skipped = n_failed = n_empty = 0
    failures: list[tuple[str, str]] = []
    t_start = time.time()

    for i, (sym, name, delisted, mkt) in enumerate(cands, 1):
        p = cache_path(sym)
        if p.exists() and not args.force:
            n_skipped += 1
            continue

        # end_date 以退市日为上界（留 10 天缓冲以防交易所表里的日期与
        # 最后交易日差几天）。腾讯源按年分块请求，不设上界会让 2017 年退市的
        # 标的白跑 9 个空年份 —— 这既慢又是无谓的请求量。
        try:
            d_end = date.fromisoformat(delisted)
        except ValueError:
            d_end = END
        eff_end = min(END, d_end + timedelta(days=DELIST_BUFFER_DAYS))

        t0 = time.time()
        raw = None
        last_err = ""
        for attempt in range(1, RETRIES + 1):
            try:
                raw = ak.stock_zh_a_hist_tx(
                    symbol=f"{mkt}{sym}",
                    start_date=START.strftime("%Y%m%d"),
                    end_date=eff_end.strftime("%Y%m%d"),
                    adjust="qfq",
                )
                break
            except Exception as exc:  # noqa: BLE001 — 单只失败不该中断整批
                last_err = f"{type(exc).__name__}: {str(exc)[:100]}"
                if attempt < RETRIES:
                    time.sleep(BACKOFF_S * (2 ** (attempt - 1)))

        if raw is None:
            print(f"[{i:3d}/{len(cands)}] {sym} {name:<10} 失败  {last_err}")
            failures.append((sym, last_err))
            n_failed += 1
            continue

        try:
            if raw.empty:
                n_empty += 1
                failures.append((sym, "腾讯返回空（窗口内无行情）"))
                print(f"[{i:3d}/{len(cands)}] {sym} {name:<10} 空")
                continue

            df = raw.rename(columns=CN_MAP)
            missing = [c for c in KEEP if c not in df.columns]
            if missing:
                raise ValueError(f"缺列 {missing}；实际列 {list(df.columns)[:8]}")
            df = df[KEEP].copy()
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df[(df["date"] >= START) & (df["date"] <= END)]
            df = df.sort_values("date").reset_index(drop=True)
            if df.empty:
                n_empty += 1
                failures.append((sym, "窗口内无行情（过滤后为空）"))
                print(f"[{i:3d}/{len(cands)}] {sym} {name:<10} 空")
                continue
            df.insert(0, "symbol", sym)
            df.to_csv(p, index=False, encoding="utf-8")
            print(f"[{i:3d}/{len(cands)}] {sym} {name:<10} {len(df):>5} 行  "
                  f"{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}  "
                  f"(退市 {delisted}, {time.time()-t0:.1f}s)")
            n_fetched += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {str(exc)[:120]}"
            print(f"[{i:3d}/{len(cands)}] {sym} {name:<10} 失败  {msg}")
            failures.append((sym, msg))
            n_failed += 1

    print()
    print("-" * 72)
    print(f"新拉 {n_fetched}  跳过 {n_skipped}  空 {n_empty}  失败 {n_failed}  "
          f"耗时 {time.time()-t_start:.0f}s")
    if failures:
        print()
        print("未取到明细（测量脚本会如实报告这部分的缺失，不静默丢弃）：")
        for sym, msg in failures[:40]:
            print(f"  {sym}  {msg}")
        if len(failures) > 40:
            print(f"  ... 另有 {len(failures)-40} 只")
    print()
    return summarize()


if __name__ == "__main__":
    raise SystemExit(main())
