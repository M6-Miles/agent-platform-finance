"""
量化（而非消除）存活者偏差
==========================
E-01 待办三项中的第③项。前两项（日历口径、滚动/样本外）已完成。

要回答的问题
------------
此前所有报告都带一句"存在存活者偏差，数字偏乐观"。方向是确定的（偏高），
但**量级未知** —— 这句话因此无法证伪，也无法用于判断 E-01 差多少。
本脚本把它换成一个可复现的数字：加回退市标的后，夏普变化多少。

即使结果是"差值很小"，也是有价值的零结果：它把"偏乐观"升级为
"偏差至多 X"，让 +0.593 这个数字第一次有了误差边界之外的边界。

三个口径，逐层加强
------------------
[A] 退市池自身的夏普      —— 与存活池对比，差值即偏差量级的直接估计
[B] 合并池等权组合夏普    —— 加回死者后的组合表现
[C] 退市清算损失敏感性    —— 退市当日按 0% / −50% / −100% 清算，三档全报

第 [C] 项是必须的：行情源的数据在退市日截断，若就此停止等于假设"最后一天原价
卖出"，这是最乐观的假设。真实退市多为老三板折价或直接归零。哪一档都不是
"正确答案"，所以三档并列给出，让读者看到结论对该假设的依赖程度。

方法学承诺（与 validate_rolling_oos.py 同一套）
----------------------------------------------
1. 标的选择规则事前设定：全部 A 股中「2016-08 前上市 且 窗口内退市」者全取，
   不抽样、不按跌幅筛选。剔除极端个例正是存活者偏差的机制本身。
2. 指标参数是教科书默认值，从未在本数据上拟合。
3. 复用 measure_real_10y 的 _enrich / build_regime_series / gen_signals /
   stats_from_returns，两份报告不会各自漂移。
4. 结果不好也不回头调参。本项的预期结果就是让数字变差。

已知局限（必须与结果一同披露）
------------------------------
(a) 数据源不同：存活池走新浪 stock_zh_a_daily，退市池走腾讯 stock_zh_a_hist_tx
    （新浪对退市代码一律 JSONDecodeError，已用存活标的 sh600519 做对照确认
    是退市专有而非接口故障；东财源在连续请求后对本机 IP 限流，故用腾讯）。
    腾讯源对 600005/000748/002604 返回的行数与起止日与东财**逐一相同**，
    这次跨源一致性是选它的依据。两源前复权基准仍可能有细微差异，表现为噪声。
(b) 数据源对部分退市标的只回传生命末段（例 600005 仅 70 行，尽管 1999 年
    上市）—— 多数退市标的在退市前早已长期停牌。不足 MIN_DAYS 的标的会被
    剔除并**报告数量与原因**，不静默丢弃。
(c) 这**不是**完整的点位复原（point-in-time universe）。存活池那 20 只是按
    沪深300 大市值挑的，退市池则是全部退市名单（多为小盘），两者市值分布
    不同。因此 [B] 的合并池不是"2016 年真实可选池"的忠实重建 —— 要做到那样
    需要 2016 年的指数成分快照，AkShare 未提供。故本脚本给出的是偏差的
    **方向与量级估计**，不是精确的偏差修正值。这一点不能含糊过去。

用法
----
  python Scripts/measure_survivorship_bias.py
  python Scripts/measure_survivorship_bias.py --quick 20   # 退市池只取前 20 只
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# 注意：**不要**在此包装 sys.stdout。下面 import measure_real_10y 时该模块会
# 执行 sys.stdout = TextIOWrapper(sys.stdout.buffer, ...)；若此处先包一层，
# 被弃用的那层在 GC 时会关掉底层 buffer，导致 ValueError: I/O operation on
# closed file。（2026-08-06 已踩过一次。）

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "Scripts"))

from agent_platform.finance.backtesting import run_backtest          # noqa: E402
from measure_real_10y import (                                       # noqa: E402
    _enrich, build_regime_series, gen_signals, stats_from_returns,
)

SURV_DIR = ROOT / "data" / "real"
DEL_DIR = ROOT / "data" / "real_delisted"
CANDIDATES_CSV = SURV_DIR / "_delisted_candidates.csv"

REBALANCE = 5
# MA20 + MACD(26) 预热约 26 日，再留出足够交易机会。不足此数的标的其夏普
# 标准误极大（SE≈sqrt(252/60)=2.05），纳入只会引入噪声而非信息。
MIN_DAYS = 60
# 退市清算损失三档。0.0 是最乐观（假设退市前一日原价卖出），1.0 是最悲观（归零）。
LIQUIDATION_SCENARIOS = [0.0, 0.5, 1.0]


def load_pool(directory: Path, *, limit: int = 0) -> tuple[dict[str, pd.DataFrame], list[tuple[str, int]]]:
    """
    读取一个池子的所有 CSV。返回 (合格 frames, 被剔除的 [(symbol, 行数)])。

    剔除只有一条规则：行数 < MIN_DAYS。剔除名单一并返回以便如实报告。
    """
    frames: dict[str, pd.DataFrame] = {}
    dropped: list[tuple[str, int]] = []
    if not directory.exists():
        return frames, dropped

    csvs = sorted(p for p in directory.glob("*.csv") if not p.name.startswith("_"))
    if limit:
        csvs = csvs[:limit]
    for p in csvs:
        df = pd.read_csv(p)
        if len(df) < MIN_DAYS:
            dropped.append((p.stem, len(df)))
            continue
        df["date"] = pd.to_datetime(df["date"]).dt.date
        frames[p.stem] = _enrich(df)
    return frames, dropped


def apply_liquidation(
    df: pd.DataFrame,
    loss: float,
    *,
    calendar: "set[date] | None" = None,
) -> pd.DataFrame:
    """
    在序列末尾追加一行"退市清算日"，收盘价 = 最后收盘 × (1 − loss)。

    刻意让清算损失**流经回测引擎**而非事后修补收益序列：这样只有在清算日
    仍持仓的标的才承担损失，空仓的不受影响 —— 与真实情形一致。

    `calendar`：池子的交易日并集。给定时，追加日**贴到并集中最早晚于最后
    交易日的那一天**；不给则退化为 +1 自然日。

    这个参数是修一个实测缺陷用的，不是可选的讲究（2026-08-06）：
    +1 自然日常落在周末/节假日，此时该日在合并池里**只有这一只标的在场**，
    `portfolio_from` 按等权取均值就等于 n=1，于是一只标的的 −100% 清算
    被当成整个组合的 −100%。实测 225 只里有 37 只（16.4%）踩中这条，
    直接把 loss=50%/100% 两档的组合夏普污染掉。贴到真实交易日后，清算损失
    正确地按 1/n 稀释 —— 一只退市在等权组合里就该只值 1/n。
    与"按行序对齐"是同一类错误（见 SPEC.md §3.1.3）：都是日期没对齐，
    且都让数字朝更极端的方向偏。
    """
    if loss <= 0 or df.empty:
        return df
    last = df.iloc[-1]
    row = {c: last[c] for c in df.columns}
    nxt = last["date"] + timedelta(days=1)
    if calendar:
        later = [d for d in calendar if d > last["date"]]
        if later:
            nxt = min(later)
    row["date"] = nxt
    close = float(last["close"]) * (1.0 - loss)
    for col in ("open", "high", "low", "close"):
        if col in row:
            row[col] = close
    if "volume" in row:
        row["volume"] = 0
    # 指标列保持最后一行的值：清算日不产生新信号（gen_signals 的 rebalance
    # 取模也几乎不会选中它），追加它的唯一目的是让持仓者吃到那一跳。
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


def run_pool(
    frames: dict[str, pd.DataFrame],
    regime_by_date: dict[date, dict],
    *,
    liquidation: float = 0.0,
    calendar: "set[date] | None" = None,
) -> dict:
    """
    对一个池子跑真实管线，返回逐股与按日期对齐的等权组合结果。

    `calendar` 必须传入**合并池**的交易日并集（见 apply_liquidation 的说明）。
    不传会导致清算日落在无人交易的日期上、在等权组合里取得 100% 权重。
    """
    per_symbol_cal: dict[str, float] = {}
    per_symbol_in: dict[str, float] = {}
    tims: list[float] = []
    dds: list[float] = []
    ret_by_date: dict[str, dict[date, float]] = {}

    for sym, df0 in frames.items():
        df = apply_liquidation(df0, liquidation, calendar=calendar)
        sigs = gen_signals(df, regime_by_date, regime_aware=False, rebalance=REBALANCE)
        res = run_backtest(sym, df, sigs)
        per_symbol_cal[sym] = res.sharpe_calendar
        per_symbol_in[sym] = res.sharpe_ratio
        tims.append(res.time_in_market_pct)
        dds.append(res.max_drawdown_pct)

        eq = res.equity_curve
        dts = list(df["date"])
        series: dict[date, float] = {}
        for i in range(1, min(len(eq), len(dts))):
            if eq[i - 1] > 0:
                series[dts[i]] = (eq[i] - eq[i - 1]) / eq[i - 1]
        ret_by_date[sym] = series

    return {
        "per_symbol_cal": per_symbol_cal,
        "per_symbol_in": per_symbol_in,
        "mean_cal": statistics.fmean(per_symbol_cal.values()) if per_symbol_cal else float("nan"),
        "mean_in": statistics.fmean(per_symbol_in.values()) if per_symbol_in else float("nan"),
        "mean_tim": statistics.fmean(tims) if tims else float("nan"),
        "mean_dd": statistics.fmean(dds) if dds else float("nan"),
        "ret_by_date": ret_by_date,
        "n": len(frames),
    }


def portfolio_from(ret_by_date: dict[str, dict[date, float]]) -> tuple[list[float], list[date]]:
    """按**日期**对齐取等权均值（不按行序 —— 见 SPEC.md §3.1.3）。"""
    all_dates = sorted({d for s in ret_by_date.values() for d in s})
    rets: list[float] = []
    dates: list[date] = []
    for d in all_dates:
        vals = [s[d] for s in ret_by_date.values() if d in s]
        if vals:
            dates.append(d)
            rets.append(statistics.fmean(vals))
    return rets, dates


def _stratified_portfolio(
    surv_rbd: dict[str, dict[date, float]],
    dele_rbd: dict[str, dict[date, float]],
    *,
    w_dead: float,
) -> list[float]:
    """
    按给定死者权重重新加权的组合日收益：
        r_t = (1 − w_dead) · mean(存活池 t 日收益) + w_dead · mean(退市池 t 日收益)

    为什么需要这一层
    ────────────────
    [B][C] 段的等权合并池里死者占 225/245 ≈ 92%，而真实可选池的十年死亡率
    只有个位数百分比。等权合并等于把死亡率当成 92%，会**高估**偏差。
    本函数把两池各自的等权均值按外部给定的 w_dead 混合，于是死者占比成为
    一个显式参数，而不是被样本构成偷偷决定。

    某一日只有一池在场时（例如 2026 年多数退市标的已消失），该日只取在场
    那一池的均值 —— 而不是把缺席方当 0 收益。当 0 处理会凭空造出一个
    "现金腿"，把波动压低、夏普抬高，那是另一种失真。
    """
    all_dates = sorted(
        {d for s in surv_rbd.values() for d in s}
        | {d for s in dele_rbd.values() for d in s}
    )
    out: list[float] = []
    for d in all_dates:
        sv = [s[d] for s in surv_rbd.values() if d in s]
        dv = [s[d] for s in dele_rbd.values() if d in s]
        if sv and dv:
            out.append(
                (1.0 - w_dead) * statistics.fmean(sv) + w_dead * statistics.fmean(dv)
            )
        elif sv:
            out.append(statistics.fmean(sv))
        elif dv:
            out.append(statistics.fmean(dv))
    return out


def _name_map() -> dict[str, str]:
    if not CANDIDATES_CSV.exists():
        return {}
    df = pd.read_csv(CANDIDATES_CSV, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    return dict(zip(df["code"], df["name"].astype(str)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", type=int, default=0, help="退市池只取前 N 只（自测用）")
    args = ap.parse_args()

    if not DEL_DIR.exists() or not any(DEL_DIR.glob("*.csv")):
        print("缺少退市标的缓存，请先运行 Scripts/fetch_delisted_history.py")
        return 1

    print("=" * 78)
    print("存活者偏差量化  —— 加回退市标的后夏普变化多少")
    print("=" * 78)
    print()

    regime = build_regime_series()
    surv, surv_dropped = load_pool(SURV_DIR)
    dele, dele_dropped = load_pool(DEL_DIR, limit=args.quick)
    names = _name_map()

    if not dele:
        print(f"退市池无一只满足 ≥{MIN_DAYS} 交易日门槛，无法测量。")
        return 1

    print(f"存活池   {len(surv):>4} 只   平均 {statistics.fmean(len(d) for d in surv.values()):.0f} 日/只")
    print(f"退市池   {len(dele):>4} 只   平均 {statistics.fmean(len(d) for d in dele.values()):.0f} 日/只")
    if dele_dropped:
        print(f"退市池剔除 {len(dele_dropped)} 只（行数 < {MIN_DAYS}，行情源只回传生命末段）：")
        preview = ", ".join(f"{s}({n})" for s, n in dele_dropped[:12])
        print(f"  {preview}" + (f" ... 另 {len(dele_dropped)-12} 只" if len(dele_dropped) > 12 else ""))
    if surv_dropped:
        print(f"存活池剔除 {len(surv_dropped)} 只：{surv_dropped}")
    print()

    # ── [A] 两池各自表现（清算损失 0，即最乐观口径）────────────────────────
    print("─" * 78)
    print("[A] 两池各自表现（退市按最后收盘价原价了结 = 最乐观假设）")
    print("─" * 78)

    r_surv = run_pool(surv, regime, liquidation=0.0)
    r_dele = run_pool(dele, regime, liquidation=0.0)

    p_surv, _ = portfolio_from(r_surv["ret_by_date"])
    p_dele, _ = portfolio_from(r_dele["ret_by_date"])
    st_surv = stats_from_returns(p_surv)
    st_dele = stats_from_returns(p_dele)

    print(f"{'池':<12}{'n':>5}{'逐股均值(日历)':>16}{'等权组合':>12}"
          f"{'95%CI':>22}{'在市%':>8}{'均值回撤%':>11}")
    print(f"{'存活':<12}{r_surv['n']:>5}{r_surv['mean_cal']:>+16.3f}{st_surv.sharpe:>+12.3f}"
          f"   [{st_surv.ci_low:>+7.3f}, {st_surv.ci_high:>+7.3f}]"
          f"{r_surv['mean_tim']:>8.1f}{r_surv['mean_dd']:>11.2f}")
    print(f"{'退市':<12}{r_dele['n']:>5}{r_dele['mean_cal']:>+16.3f}{st_dele.sharpe:>+12.3f}"
          f"   [{st_dele.ci_low:>+7.3f}, {st_dele.ci_high:>+7.3f}]"
          f"{r_dele['mean_tim']:>8.1f}{r_dele['mean_dd']:>11.2f}")
    print()
    gap_stock = r_dele["mean_cal"] - r_surv["mean_cal"]
    gap_port = st_dele.sharpe - st_surv.sharpe
    print(f"逐股均值差（退市 − 存活）  {gap_stock:>+8.3f}")
    print(f"等权组合差（退市 − 存活）  {gap_port:>+8.3f}")
    print("这两个差值是存活者偏差量级的直接估计：只看活下来的，等于系统性")
    print("地把左边那一档从样本里删掉。")
    print()

    # ── [B] 合并池 + [C] 清算损失敏感性 ────────────────────────────────────
    print("─" * 78)
    print("[B][C] 合并池等权组合，按退市清算损失三档")
    print("─" * 78)
    print("清算损失 = 退市当日仍持仓者承担的跳空损失。0% 最乐观，100% 为归零。")
    print("哪一档都不是'正确答案'，三档并列以显示结论对该假设的依赖程度。")
    print()

    # 合并池的构成披露。这一段决定下表差值该往哪个方向读，非常容易搞反 ——
    # 我自己就搞反过一次：本段初稿写于只抓到 10 只退市标的时（退市池仅占
    # 标的-日 15.4%），当时结论是"差值是下界，因为真实池里死者占比更高"。
    # 抓满 225 只后退市池占比升到 87%，方向**反转**：等权合并池里死者按数量
    # 占 225/245≈92%，而真实可选池十年死亡率是个位数百分比，故等权合并池
    # 严重**高估**死者权重，下表差值是偏差量级的上界而非下界。
    # 因此下面既报构成占比，也报按真实死亡率加权后的结果（[B2] 段），
    # 不让读者只看到一个权重失真的数字。
    surv_sd = sum(len(s) for s in r_surv["ret_by_date"].values())
    dele_sd = sum(len(s) for s in r_dele["ret_by_date"].values())
    total_sd = surv_sd + dele_sd
    merged_probe = dict(r_surv["ret_by_date"])
    merged_probe.update(r_dele["ret_by_date"])
    all_d = sorted({d for s in merged_probe.values() for d in s})
    with_dele = sum(
        1 for d in all_d
        if any(d in s for s in r_dele["ret_by_date"].values())
    )
    print("合并池构成（决定下表差值有多少是结构性稀释）：")
    print(f"  标的-日总数 {total_sd}，其中退市池贡献 {dele_sd}"
          f"（{dele_sd/total_sd*100:.1f}%）")
    print(f"  合并池共 {len(all_d)} 个交易日，其中 {with_dele} 日"
          f"（{with_dele/len(all_d)*100:.1f}%）至少有一只退市标的在场")
    print("  → 退市标的按定义只在其存活期出现，其余日期等权均值只剩存活标的。")
    print(f"    等权合并池里死者占 {len(dele)}/(20+{len(dele)})≈{len(dele)/(20+len(dele))*100:.0f}%，")
    print("    而真实可选池十年死亡率是个位数百分比（~229/3000），")
    print("    故下表差值是**上界**：真实偏差应按死者实际占比加权估计（见 [B2]）。")
    print()
    print(f"{'清算损失':<12}{'仅存活':>12}{'合并池':>12}{'差值':>12}{'合并池95%CI':>24}{'退市池组合':>13}")

    # 合并池的交易日并集，直接取自原始行情的 date 列（而非 ret_by_date，
    # 后者已被净值序列裁掉首日）。清算日必须贴到这个集合里的某一天，
    # 否则会独占该日、取得 100% 权重 —— 见 apply_liquidation 的说明。
    pool_calendar: set[date] = {
        d for f in list(surv.values()) + list(dele.values()) for d in f["date"]
    }

    rows: list[tuple[float, float, float, float]] = []
    # 每档 run_pool 要跑 225 只标的（约 90s）。[B2] 段用的是同样三档，
    # 故在此缓存，避免同一计算做两遍。
    cache_dele_rbd: dict[float, dict[str, dict[date, float]]] = {}
    for loss in LIQUIDATION_SCENARIOS:
        r_d = run_pool(dele, regime, liquidation=loss, calendar=pool_calendar)
        cache_dele_rbd[loss] = r_d["ret_by_date"]
        merged = dict(r_surv["ret_by_date"])
        merged.update(r_d["ret_by_date"])
        p_merged, _ = portfolio_from(merged)
        st_m = stats_from_returns(p_merged)
        p_d, _ = portfolio_from(r_d["ret_by_date"])
        st_d = stats_from_returns(p_d)
        diff = st_m.sharpe - st_surv.sharpe
        rows.append((loss, st_surv.sharpe, st_m.sharpe, diff))
        print(f"{loss*100:>7.0f}%     {st_surv.sharpe:>+12.3f}{st_m.sharpe:>+12.3f}"
              f"{diff:>+12.3f}   [{st_m.ci_low:>+7.3f}, {st_m.ci_high:>+7.3f}]"
              f"{st_d.sharpe:>+13.3f}")

    print()

    # ── [B2] 按真实死亡率重加权的估计 ────────────────────────────────────────
    print("─" * 78)
    print("[B2] 分层重加权估计（按真实可选池的死者占比）")
    print("─" * 78)
    print("上表 [B][C] 里死者权重≈92%（225 只 vs 20 只），")
    print("而真实 2016 年可选池约 3000 只 vs 同期 229 只退市，死亡率≈7.6%。")
    print("下表按「每日组合收益 = (1−w)·存活池均值 + w·退市池均值」重新加权，")
    print("w 从 5% 到 20% 展示对该假设的依赖。")
    print()
    print(f"{'w_dead':>8}", end="")
    for loss in LIQUIDATION_SCENARIOS:
        print(f"{'loss=' + format(loss, '.0%'):>14}", end="")
    print()

    W_DEAD_SCENARIOS = [0.05, 0.076, 0.10, 0.15, 0.20]

    for w in W_DEAD_SCENARIOS:
        print(f"{w:>8.3f}", end="")
        for loss in LIQUIDATION_SCENARIOS:
            rets = _stratified_portfolio(
                r_surv["ret_by_date"], cache_dele_rbd[loss], w_dead=w
            )
            sh = stats_from_returns(rets).sharpe
            print(f"{sh:>+14.4f}", end="")
        print()
    print()

    # ── 逐股配对检验：同一 regime 序列下两池的逐股夏普分布差异 ──────────────
    print("─" * 78)
    print("[D] 逐股夏普分布对比（非配对 —— 两池是不同标的，只报分布）")
    print("─" * 78)
    sv = sorted(r_surv["per_symbol_cal"].values())
    dv = sorted(r_dele["per_symbol_cal"].values())

    def _q(xs: list[float], p: float) -> float:
        if not xs:
            return float("nan")
        i = min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))
        return xs[i]

    print(f"{'池':<8}{'n':>5}{'最小':>10}{'P25':>10}{'中位':>10}{'P75':>10}{'最大':>10}{'<0 占比':>10}")
    for label, xs in (("存活", sv), ("退市", dv)):
        neg = sum(1 for x in xs if x < 0) / len(xs) * 100 if xs else float("nan")
        print(f"{label:<8}{len(xs):>5}{_q(xs,0):>+10.3f}{_q(xs,0.25):>+10.3f}"
              f"{_q(xs,0.5):>+10.3f}{_q(xs,0.75):>+10.3f}{_q(xs,1):>+10.3f}{neg:>9.1f}%")
    print()

    # 最差的 10 只退市标的（按日历口径夏普），仅作定性展示
    worst = sorted(r_dele["per_symbol_cal"].items(), key=lambda kv: kv[1])[:10]
    print("退市池最差 10 只（这类标的在存活样本里被结构性排除）：")
    for sym, sh in worst:
        print(f"  {sym}  {names.get(sym, ''):<10}  {sh:>+7.3f}")
    print()

    # ── 结论 ──────────────────────────────────────────────────────────────
    print("=" * 78)
    print("结论")
    print("=" * 78)
    lo_diff = min(r[3] for r in rows)
    hi_diff = max(r[3] for r in rows)
    print(f"存活者偏差量级估计：加回 {len(dele)} 只退市标的后，等权组合夏普变化")
    print(f"  {hi_diff:+.3f}（清算损失 0%，最乐观） ~ {lo_diff:+.3f}（清算损失 100%）")
    print(f"逐股口径差值 {gap_stock:+.3f}（退市池均值 {r_dele['mean_cal']:+.3f} vs 存活池 {r_surv['mean_cal']:+.3f}）")
    print()
    print("对 E-01 的影响：此前报告的等权组合 +0.593（95%CI [−0.039, +1.225]）")
    print("建立在纯存活样本上。按上表，真实可选池的对应值应低于该数字，")
    print("即 E-01 的实际差距比已报告的更大，而非更小。")
    print()
    print("局限（不可通过调参消除，见模块 docstring (a)(b)(c)）：")
    print("  · 数据源不同（存活池新浪 vs 退市池腾讯），前复权基准差异表现为噪声")
    print(f"  · 行情源截断部分退市标的历史，已剔除 {len(dele_dropped)} 只不足 {MIN_DAYS} 日者")
    print("  · 非点位复原：存活池为沪深300大市值，退市池为全部退市名单（多小盘），")
    print("    市值分布不同，故本结果是偏差的方向与量级估计，不是精确修正值")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
