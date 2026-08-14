"""
多因子择时策略（Multi-Factor Timing Strategy）
==============================================
把 :mod:`factors` 的因子值变成 **[0, 1] 的目标仓位序列**。

四个环节，逐层收紧
----------------
1. **打分**：各因子先做**因果标准化**（见下），再按权重加权成 composite score；
   不可用因子剔除并对剩余权重重新归一化（不按 0 值参与）。
2. **趋势过滤**：只有中长期趋势条件成立才允许做多（要求五.1）。
3. **波动率目标定仓**：``target_vol / 已实现波动率``，高波动自动降仓（要求五.2）。
4. **换手控制**：偏离不足阈值就不动，另设最小调仓间隔（要求五.4）。

因果标准化：为什么用扩张/滚动分位，而不是 z-score
-----------------------------------------------
全样本 ``mean/std`` 标准化是最隐蔽的未来数据泄漏 —— 2016 年的打分里含有
2026 年的分布信息。要求四.4 明确禁止。这里用 :func:`causal_percentile`：
日期 t 的分位数只在 ``[t-window+1, t]``（或 ``[0, t]``）内计算，
**永远不包含 t 之后的任何一行**。代价是预热期较长且分位数会随历史增长而
"漂移"，但这是真实可交易的代价，不能用未来信息换取平滑。

方向统一
-------
波动率类因子 ``higher_is_better=False``（低波动为优），在标准化时取
``1 - percentile`` 翻转，使 composite score 一律"越大越好"。方向只在这一处
处理，避免散落各处出现符号错误。
"""
from __future__ import annotations

import logging
import math
from bisect import bisect_left, insort
from dataclasses import dataclass, field, replace
from typing import Any, Final

import numpy as np
import pandas as pd

from .factors import FactorSet, build_factor_set

logger = logging.getLogger(__name__)

# 因子族默认权重。四族等权是刻意的中性起点：
# 不按"哪族历史表现好"来配权，否则等于用全样本结果反向拟合权重。
DEFAULT_FAMILY_WEIGHTS: Final[dict[str, float]] = {
    "momentum": 0.40,
    "volatility": 0.30,
    "volume": 0.20,
    "valuation": 0.10,
}


def causal_percentile(
    series: pd.Series,
    *,
    window: int | None = 252,
    min_periods: int = 60,
) -> pd.Series:
    """
    因果分位数：位置 i 的输出 = ``series[i]`` 在窗口内的百分位（[0, 1]）。

    * ``window=None`` → 扩张窗口 ``[0, i]``；否则滚动窗口 ``[i-window+1, i]``。
    * 窗口**含当前值、不含任何未来值** —— 这是本函数存在的全部理由。
    * 有效样本不足 ``min_periods`` → NaN（不猜、不填 0.5）。
    * NaN 输入 → NaN 输出，且该值不进入后续窗口的比较集合。

    用 bisect 维护有序序列，复杂度约 O(n log n)，2400 行量级下可忽略。
    """
    if window is not None and window < 1:
        raise ValueError("window 必须 >= 1")
    if min_periods < 1:
        raise ValueError("min_periods 必须 >= 1")

    vals = series.to_numpy(dtype=float, copy=False)
    n = len(vals)
    out = np.full(n, np.nan, dtype=float)

    sorted_vals: list[float] = []
    # 记录每个位置放入的值（用于滚动窗口的移除）；NaN 位置记 None
    placed: list[float | None] = []

    for i in range(n):
        v = vals[i]
        if not math.isnan(v):
            insort(sorted_vals, v)
            placed.append(v)
        else:
            placed.append(None)

        # 滚动窗口：移除滑出窗口的那个值
        if window is not None:
            drop_idx = i - window
            if drop_idx >= 0:
                old = placed[drop_idx]
                if old is not None:
                    pos = bisect_left(sorted_vals, old)
                    if pos < len(sorted_vals) and sorted_vals[pos] == old:
                        del sorted_vals[pos]

        m = len(sorted_vals)
        if math.isnan(v) or m < min_periods:
            continue
        if m == 1:
            out[i] = 0.5
            continue
        # 用 (lo + hi)/2 处理并列值，等价于 rank(method="average")
        lo = bisect_left(sorted_vals, v)
        hi = lo
        while hi < m and sorted_vals[hi] == v:
            hi += 1
        avg_rank = (lo + hi - 1) / 2.0
        out[i] = avg_rank / (m - 1)

    return pd.Series(out, index=series.index, dtype=float)


@dataclass(frozen=True, slots=True)
class StrategyParams:
    """
    策略参数。**只允许在 train+validation 上选择**（要求四.5 / 六.4）。

    ``score_threshold``
        composite score 低于此分位不建仓；之上按 ``(score-thr)/(1-thr)`` 线性放大。
    ``target_vol``
        年化目标波动率。仓位 ≈ ``target_vol / 已实现年化波动率``。
    ``trend_ma`` / ``trend_slope_days``
        趋势过滤：收盘价需站上 ``trend_ma`` 均线，且该均线相比
        ``trend_slope_days`` 前上行。
    ``rebalance_threshold`` / ``rebalance_days``
        换手控制：目标与当前仓位偏离不足阈值、且距上次调仓不足间隔天数，则不动。
    ``norm_window``
        因果分位窗口（None=扩张）。
    """

    score_threshold: float = 0.55
    target_vol: float = 0.15
    trend_ma: int = 60
    trend_slope_days: int = 20
    rebalance_threshold: float = 0.10
    rebalance_days: int = 5
    norm_window: int | None = 252
    norm_min_periods: int = 60
    max_position: float = 1.0
    force_exit_on_trend_off: bool = True
    family_weights: tuple[tuple[str, float], ...] = tuple(
        sorted(DEFAULT_FAMILY_WEIGHTS.items())
    )

    def weights_dict(self) -> dict[str, float]:
        return dict(self.family_weights)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score_threshold": self.score_threshold,
            "target_vol": self.target_vol,
            "trend_ma": self.trend_ma,
            "trend_slope_days": self.trend_slope_days,
            "rebalance_threshold": self.rebalance_threshold,
            "rebalance_days": self.rebalance_days,
            "norm_window": self.norm_window,
            "norm_min_periods": self.norm_min_periods,
            "max_position": self.max_position,
            "force_exit_on_trend_off": self.force_exit_on_trend_off,
            "family_weights": self.weights_dict(),
        }

    def label(self) -> str:
        return (
            f"thr={self.score_threshold:g},tv={self.target_vol:g},"
            f"ma={self.trend_ma},rb={self.rebalance_threshold:g}/"
            f"{self.rebalance_days}"
        )


def default_param_grid() -> list[StrategyParams]:
    """
    候选参数网格（刻意保持小规模）。

    网格越大，即使只在 train+validation 上选，也越容易过拟合验证段。
    这里 3×3×2 = 18 组，覆盖"进场严格度 × 风险预算 × 趋势周期"三个方向。
    """
    grid: list[StrategyParams] = []
    base = StrategyParams()
    for thr in (0.45, 0.55, 0.65):
        for tv in (0.10, 0.15, 0.20):
            for ma in (60, 120):
                grid.append(replace(base, score_threshold=thr, target_vol=tv, trend_ma=ma))
    return grid


@dataclass(slots=True)
class StrategySignals:
    """策略输出：逐日目标仓位 + 中间量（便于审计与测试）。"""

    symbol: str
    dates: list[str]
    target_position: pd.Series
    composite_score: pd.Series
    trend_gate: pd.Series
    vol_scalar: pd.Series
    raw_position: pd.Series
    params: StrategyParams
    factor_meta: list[dict[str, Any]] = field(default_factory=list)
    used_factors: tuple[str, ...] = ()
    unavailable_factors: tuple[str, ...] = ()

    def positions_by_date(self) -> dict[str, float]:
        return {
            d: (0.0 if pd.isna(v) else float(v))
            for d, v in zip(self.dates, self.target_position.tolist(), strict=True)
        }

    def realized_turnover(self) -> float:
        """Σ|Δw|（含首次建仓）。与回测引擎口径一致，可交叉核对。"""
        w = self.target_position.fillna(0.0).tolist()
        prev = 0.0
        total = 0.0
        for v in w:
            total += abs(float(v) - prev)
            prev = float(v)
        return total


def compute_composite_score(
    fset: FactorSet,
    params: StrategyParams,
) -> tuple[pd.Series, tuple[str, ...], tuple[str, ...]]:
    """
    因子 → composite score（[0, 1]）。

    步骤：因果分位标准化 → 方向统一 → 族内等权 → 族间按权重加权。
    **不可用因子被完全剔除**，其所属族权重从分母中移除并对剩余族重新归一化；
    若某族全部因子不可用，该族权重转移给其它族，而不是给该族 0.5 的中性分。
    """
    n = len(fset.dates)
    idx = pd.Index(range(n))
    weights = params.weights_dict()

    family_scores: dict[str, list[pd.Series]] = {}
    used: list[str] = []
    unavailable: list[str] = []

    for name, fs in fset.series.items():
        if not fs.available:
            unavailable.append(name)
            continue
        pct = causal_percentile(
            fs.values.reset_index(drop=True),
            window=params.norm_window,
            min_periods=params.norm_min_periods,
        )
        if not fs.higher_is_better:
            pct = 1.0 - pct          # 方向统一：只在这一处翻转
        if pct.notna().sum() == 0:
            unavailable.append(name)
            continue
        family_scores.setdefault(fs.family, []).append(pct)
        used.append(name)

    if not family_scores:
        return pd.Series([np.nan] * n, index=idx, dtype=float), (), tuple(unavailable)

    # 族内等权平均（skipna：族内个别因子预热未完成时不拖垮整族）
    per_family: dict[str, pd.Series] = {
        fam: pd.concat(series_list, axis=1).mean(axis=1, skipna=True)
        for fam, series_list in family_scores.items()
    }

    present_weight = sum(weights.get(f, 0.0) for f in per_family)
    if present_weight <= 0:
        # 权重表未覆盖任何在场族 → 退化为在场族等权，而不是静默返回 NaN
        per_family_w = {f: 1.0 / len(per_family) for f in per_family}
    else:
        per_family_w = {f: weights.get(f, 0.0) / present_weight for f in per_family}

    score = pd.Series([0.0] * n, index=idx, dtype=float)
    weight_sum = pd.Series([0.0] * n, index=idx, dtype=float)
    for fam, s in per_family.items():
        w = per_family_w[fam]
        valid = s.notna()
        score = score.add((s.fillna(0.0) * w).where(valid, 0.0), fill_value=0.0)
        weight_sum = weight_sum.add(pd.Series(np.where(valid, w, 0.0), index=idx),
                                    fill_value=0.0)

    # 逐日按"当日实际参与的权重"归一化：预热期部分因子缺失时不会被稀释成低分
    blended = score / weight_sum.replace(0.0, np.nan)

    # ── 再做一次因果分位：修复"分位数平均导致离散度塌缩"的缺陷 ──────────────
    # 为什么必须有这一步：11 个近似独立的 U[0,1] 分位数取平均后，标准差约
    # 0.29/sqrt(11) ≈ 0.087，绝大多数日子挤在 0.5 附近。于是
    # score_threshold=0.55 这个参数几乎永不被跨过，策略长期空仓
    # （实测 avg_position=0.0009、时间在市 4%），阈值参数事实上失效。
    # 把 blended 相对**自身历史**再取一次因果分位，可恢复到 U[0,1]，
    # 阈值才重新具有"只在自身历史前 (1-thr) 分位时建仓"的语义。
    # 这一步仍然只用 [t-window+1, t] 的历史，不含任何未来值。
    composite = causal_percentile(
        blended, window=params.norm_window, min_periods=params.norm_min_periods
    )
    return composite, tuple(used), tuple(unavailable)


def compute_trend_gate(price_df: pd.DataFrame, params: StrategyParams) -> pd.Series:
    """
    趋势过滤门（1.0 允许做多 / 0.0 禁止）。

    条件：收盘价 ≥ MA(trend_ma) **且** MA(trend_ma) 相比 trend_slope_days 前上行。
    两个条件都只用 ``rolling`` 与 ``shift(+k)``，全部为过去数据。
    均线预热未完成 → 0.0（不允许做多）。这是保守方向：
    信息不足时不开仓，而不是默认开仓。
    """
    close = price_df["close"].astype(float).reset_index(drop=True)
    ma = close.rolling(params.trend_ma, min_periods=params.trend_ma).mean()
    ma_past = ma.shift(params.trend_slope_days)       # shift(+k) = 过去
    above = close >= ma
    rising = ma > ma_past
    gate = (above & rising).astype(float)
    gate = gate.where(ma.notna() & ma_past.notna(), 0.0)
    return gate


def compute_vol_scalar(price_df: pd.DataFrame, params: StrategyParams) -> pd.Series:
    """
    波动率目标定仓系数。

    ``scalar = target_vol / (rolling_std(20) * sqrt(252))``，夹在
    ``[0, max_position]``。高波动 → 系数小 → 仓位低；低波动 → 允许更高仓位，
    但**不超过 1.0**（要求五.3 禁止杠杆）。

    只用截至 t 的收益率计算已实现波动率 —— 不使用未来波动率。
    波动率不可得（预热期/NaN）→ 0.0：风险无法度量时不承担风险。
    已实现波动率为 0（连续停牌等）→ 0.0，而不是让 target/0 变成 inf 后被夹成满仓；
    零波动往往意味着"价格没在动/不可交易"，不是"无风险可满仓"。
    """
    close = price_df["close"].astype(float).reset_index(drop=True)
    ret = close.pct_change()
    vol_d = ret.rolling(20, min_periods=20).std(ddof=1)
    vol_ann = vol_d * math.sqrt(252)
    scalar = params.target_vol / vol_ann.replace(0.0, np.nan)
    scalar = scalar.clip(lower=0.0, upper=params.max_position)
    return scalar.fillna(0.0)


def apply_turnover_control(
    desired: pd.Series,
    params: StrategyParams,
    *,
    trend_gate: pd.Series | None = None,
) -> pd.Series:
    """
    换手控制：抑制微小调仓。

    规则：目标与当前仓位偏离 < ``rebalance_threshold`` **且** 距上次调仓
    < ``rebalance_days`` → 保持不动。两个条件是"与"关系：
    到期即可微调，偏离够大则立即调整。

    例外：``force_exit_on_trend_off`` 为真且趋势门关闭 → 立即清仓，不受阻尼限制。
    风控优先于降换手，这是刻意的取舍；代价是换手上升，已如实计入报告。

    前向单向递推，不引用任何未来值。
    """
    d = desired.fillna(0.0).astype(float).tolist()
    gate = (trend_gate.fillna(0.0).astype(float).tolist()
            if trend_gate is not None else [1.0] * len(d))

    out: list[float] = []
    current = 0.0
    last_rebalance = -10**9

    for i, want in enumerate(d):
        want = min(params.max_position, max(0.0, want))

        if params.force_exit_on_trend_off and gate[i] <= 0.0:
            if current != 0.0:
                current = 0.0
                last_rebalance = i
            out.append(current)
            continue

        gap = abs(want - current)
        due = (i - last_rebalance) >= params.rebalance_days
        if gap >= params.rebalance_threshold or (due and gap > 0.0):
            current = want
            last_rebalance = i
        out.append(current)

    return pd.Series(out, index=desired.index, dtype=float)


def generate_signals(
    symbol: str,
    price_df: pd.DataFrame,
    params: StrategyParams | None = None,
    *,
    valuation_history: pd.DataFrame | None = None,
    data_source: str = "",
) -> StrategySignals:
    """
    端到端生成目标仓位序列。

    输出的 ``target_position[t]`` 语义是"用截至 t 收盘的信息决定的仓位"，
    由 :func:`position_backtest.run_position_backtest` 在 **t+1 开盘**执行。
    本函数内部**不做**任何右移 —— 右移只在回测引擎里发生一次，
    避免两处都移导致偏移两天或都不移导致用当日未来价成交。
    """
    params = params or StrategyParams()
    df = price_df.copy()
    df["date"] = df["date"].astype(str)
    df = df.sort_values("date", kind="mergesort").reset_index(drop=True)

    fset = build_factor_set(
        symbol, df, valuation_history=valuation_history, source=data_source
    )
    composite, used, unavailable = compute_composite_score(fset, params)
    gate = compute_trend_gate(df, params)
    vol_scalar = compute_vol_scalar(df, params)

    thr = params.score_threshold
    denom = max(1.0 - thr, 1e-9)
    base = ((composite - thr) / denom).clip(lower=0.0, upper=1.0)

    raw = (base * vol_scalar * gate).clip(lower=0.0, upper=params.max_position)
    raw = raw.fillna(0.0)

    target = apply_turnover_control(raw, params, trend_gate=gate)

    # 最后一道硬闸：无论上游算成什么，仓位必须落在 [0, max_position]
    target = target.clip(lower=0.0, upper=params.max_position).fillna(0.0)

    return StrategySignals(
        symbol=symbol,
        dates=[str(d) for d in df["date"].tolist()],
        target_position=target,
        composite_score=composite,
        trend_gate=gate,
        vol_scalar=vol_scalar,
        raw_position=raw,
        params=params,
        factor_meta=fset.meta(),
        used_factors=used,
        unavailable_factors=unavailable,
    )


# ═══════════════════════════════════════════════════════════════════
#   Baseline：MA5/MA20 金叉死叉 → 仓位序列
# ═══════════════════════════════════════════════════════════════════

def ma_baseline_positions(price_df: pd.DataFrame) -> pd.Series:
    """
    把原 MA5/MA20 策略表达成 0/1 仓位序列，使其能进入同一个连续仓位引擎。

    这是**语义等价的适配器**，不是新策略：金叉后持仓 1.0，死叉后 0.0，
    与 ``Scripts/validate_deliverables.py:_ma_crossover_signals`` 的
    "金叉买入 / 死叉卖出、之间维持"完全一致。原函数与原引擎均未改动，
    baseline 的原始口径结果由 ``test_baseline_strategy_result_is_unchanged`` 钉死。

    为什么需要适配器：要求七要求 baseline 与多因子共享同一套成本、滑点、
    Sharpe 公式、无风险利率与测试段。跑同一个引擎是最不容易作弊的实现方式。
    """
    df = price_df.copy()
    df["date"] = df["date"].astype(str)
    df = df.sort_values("date", kind="mergesort").reset_index(drop=True)
    close = df["close"].astype(float)

    ma5 = close.rolling(5, min_periods=5).mean()
    ma20 = close.rolling(20, min_periods=20).mean()

    out: list[float] = []
    pos = 0.0
    prev_above: bool | None = None
    for i in range(len(df)):
        if pd.isna(ma5.iloc[i]) or pd.isna(ma20.iloc[i]):
            out.append(0.0)
            continue
        above = bool(ma5.iloc[i] > ma20.iloc[i])
        if prev_above is None:
            prev_above = above          # 首个可比日只建立基准，不产生信号
        elif above and not prev_above:
            pos = 1.0
        elif not above and prev_above:
            pos = 0.0
        prev_above = above
        out.append(pos)

    # 按**日期**索引，不按位置索引：回测引擎按日期对齐，位置索引会一个都对不上。
    return pd.Series(out, index=pd.Index([str(d) for d in df["date"].tolist()]),
                     dtype=float)
