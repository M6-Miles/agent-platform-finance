"""
多因子研究：因子计算层（严格因果 / 无未来数据）
================================================
本模块只负责**把价格量数据变成因子值**，不做打分、不做仓位、不做回测。

核心纪律：因果性（causality）
----------------------------
日期 t 的因子值只能使用 **t 及 t 之前** 的数据。实现上这意味着：

* 只允许 ``rolling(n)`` / ``ewm`` / ``pct_change(n)`` / ``diff(n)`` 这类**向后看**的算子；
  pandas 的 ``rolling(n)`` 在位置 i 上取的是 ``[i-n+1, i]``，含当前、不含未来，合法。
* **禁止** ``shift(-k)``、``rolling(...).shift(-k)``、``[::-1]`` 反向滚动，
  以及任何 ``center=True``（居中窗口会吃到未来一半的数据）。
* **禁止**用全样本统计量（``df.mean()`` / ``df.std()`` / ``df.quantile()``）做标准化 ——
  那等于把"未来才知道的分布"泄漏给过去。标准化只允许两种口径：
  (a) 横截面：同一日期 t 上跨标的比较（天然只用 t 时刻信息）；
  (b) 扩张窗口：只用 ``[0, t]`` 的历史。
  本模块提供 (a)，见 :func:`cross_sectional_rank`。

为什么把"不可用"做成一等公民
--------------------------
估值因子（PE/PB/ROE）在本项目里**没有历史时间序列**：
``offline_sample_data.py`` 只有每只标的的**当期快照**（单个标量）。把当期 PE 回填到
2016 年，是最典型的未来数据泄漏 —— 那个 PE 在 2016 年根本不存在。因此本模块规定：

* 估值因子在无历史数据时返回 :class:`FactorSeries` 且 ``available=False``；
* 不可用因子**不参与打分**（在打分层剔除并对剩余权重归一化），
  **不按 0 值参与**（0 在 z-score 口径下等于"中位数"，会被当成真实观测）；
* 不可用原因必须写进 ``unavailable_reason``，随报告输出。

参考：项目 Rule/data_must_have_source.md（数据必须带来源）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 因子族名称常量。用常量而非裸字符串：拼写错误在导入期暴露，
# 而不是在报告里静默变成一个"永远不可用"的因子。
FAMILY_MOMENTUM: Final[str] = "momentum"
FAMILY_VOLATILITY: Final[str] = "volatility"
FAMILY_VOLUME: Final[str] = "volume"
FAMILY_VALUATION: Final[str] = "valuation"

ALL_FAMILIES: Final[tuple[str, ...]] = (
    FAMILY_MOMENTUM,
    FAMILY_VOLATILITY,
    FAMILY_VOLUME,
    FAMILY_VALUATION,
)

# 需要的最少行数（用于判断 rolling 窗口是否够长）
MIN_ROWS_FOR_FACTORS: Final[int] = 61


@dataclass(frozen=True, slots=True)
class FactorSeries:
    """
    单个因子的时间序列 + 可用性标记。

    ``values`` 的索引与输入价格表的 ``date`` 一致。窗口不足处为 NaN ——
    **保留 NaN 而不是填 0**：填 0 会让"还没算出来"和"算出来正好是 0"
    这两件事无法区分，下游打分时会把预热期当成真实中性信号。
    """

    name: str
    family: str
    values: pd.Series
    available: bool = True
    unavailable_reason: str | None = None
    higher_is_better: bool = True
    source: str = ""

    def to_meta(self) -> dict[str, Any]:
        """只返回元信息（不含数值），供报告与审计使用。"""
        return {
            "name": self.name,
            "family": self.family,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "higher_is_better": self.higher_is_better,
            "source": self.source,
            "n_valid": int(self.values.notna().sum()) if self.available else 0,
        }


@dataclass(slots=True)
class FactorSet:
    """一只标的的全部因子。"""

    symbol: str
    dates: pd.Series
    series: dict[str, FactorSeries] = field(default_factory=dict)

    def add(self, fs: FactorSeries) -> None:
        if fs.name in self.series:
            raise ValueError(f"因子 {fs.name!r} 重复注册")
        self.series[fs.name] = fs

    def available_names(self) -> tuple[str, ...]:
        return tuple(n for n, f in self.series.items() if f.available)

    def unavailable_names(self) -> tuple[str, ...]:
        return tuple(n for n, f in self.series.items() if not f.available)

    def families_present(self) -> tuple[str, ...]:
        return tuple(sorted({f.family for f in self.series.values()}))

    def to_frame(self) -> pd.DataFrame:
        """把可用因子拼成 DataFrame（不可用因子不进表，避免被当成 0）。"""
        data = {n: f.values for n, f in self.series.items() if f.available}
        out = pd.DataFrame(data)
        out.index = pd.Index(range(len(self.dates)))
        return out

    def meta(self) -> list[dict[str, Any]]:
        return [f.to_meta() for f in self.series.values()]


def _require_columns(df: pd.DataFrame, cols: tuple[str, ...]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"价格表缺少必需列: {missing}（现有列: {list(df.columns)}）")


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """
    统一预处理：按日期升序、重置索引。

    这里**必须**排序：因子依赖 rolling 的时间顺序，乱序输入会让
    "过去 20 日"变成"任意 20 行"，因果性直接失效。
    """
    _require_columns(df, ("date", "close"))
    out = df.copy()
    out["date"] = out["date"].astype(str)
    out = out.sort_values("date", kind="mergesort").reset_index(drop=True)
    return out


# ═══════════════════════════════════════════════════════════════════
#   一、动量因子
# ═══════════════════════════════════════════════════════════════════

def momentum_factors(df: pd.DataFrame, *, source: str = "") -> list[FactorSeries]:
    """
    动量因子族。全部基于收盘价的向后看窗口。

    * ``mom_20``      20 日收益率
    * ``mom_60``      60 日收益率
    * ``trend_str``   中短期趋势强度 = MA20 相对 MA60 的偏离度
    * ``ma_diff``     MA5 与 MA20 的相对差（短期动量）

    ``pct_change(n)`` 在位置 i 上等于 ``close[i]/close[i-n] - 1``，
    只用到 i 及更早的数据，合法。
    """
    d = _prepare(df)
    close = d["close"].astype(float)

    ma5 = close.rolling(5, min_periods=5).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()

    out = [
        FactorSeries(
            name="mom_20", family=FAMILY_MOMENTUM,
            values=close.pct_change(20),
            higher_is_better=True, source=source,
        ),
        FactorSeries(
            name="mom_60", family=FAMILY_MOMENTUM,
            values=close.pct_change(60),
            higher_is_better=True, source=source,
        ),
        # 趋势强度：MA20 高于 MA60 越多，中期趋势越强。
        # 用相对值（除以 MA60）而非绝对差，避免高价股天然占优。
        FactorSeries(
            name="trend_str", family=FAMILY_MOMENTUM,
            values=(ma20 - ma60) / ma60.replace(0.0, np.nan),
            higher_is_better=True, source=source,
        ),
        FactorSeries(
            name="ma_diff", family=FAMILY_MOMENTUM,
            values=(ma5 - ma20) / ma20.replace(0.0, np.nan),
            higher_is_better=True, source=source,
        ),
    ]
    return out


# ═══════════════════════════════════════════════════════════════════
#   二、波动率因子
# ═══════════════════════════════════════════════════════════════════

def volatility_factors(df: pd.DataFrame, *, source: str = "") -> list[FactorSeries]:
    """
    波动率因子族。低波动为优（``higher_is_better=False``）。

    * ``vol_20``       20 日收益标准差（年化前的日度值）
    * ``atr_14``       14 日 ATR / 收盘价（相对化，跨标的可比）
    * ``downside_20``  20 日下行波动率（只算负收益）
    * ``hl_range_20``  20 日均 (high-low)/close

    ATR 用 ``max(high-low, |high-prev_close|, |low-prev_close|)``，
    其中 ``prev_close`` 是 ``shift(1)``（过去），合法。
    """
    d = _prepare(df)
    close = d["close"].astype(float)
    ret = close.pct_change()

    vol20 = ret.rolling(20, min_periods=20).std(ddof=1)

    # 下行波动率：只保留负收益，正收益置 0。
    # 注意用 where 而非 dropna：dropna 会改变窗口对齐，把不同日期混进同一窗口。
    downside = ret.where(ret < 0.0, 0.0)
    downside_20 = downside.rolling(20, min_periods=20).std(ddof=1)

    factors = [
        FactorSeries(
            name="vol_20", family=FAMILY_VOLATILITY, values=vol20,
            higher_is_better=False, source=source,
        ),
        FactorSeries(
            name="downside_20", family=FAMILY_VOLATILITY, values=downside_20,
            higher_is_better=False, source=source,
        ),
    ]

    if "high" in d.columns and "low" in d.columns:
        high = d["high"].astype(float)
        low = d["low"].astype(float)
        prev_close = close.shift(1)          # shift(+1) = 过去，合法
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr14 = tr.rolling(14, min_periods=14).mean() / close.replace(0.0, np.nan)
        hl_range = ((high - low) / close.replace(0.0, np.nan)).rolling(
            20, min_periods=20
        ).mean()
        factors.append(FactorSeries(
            name="atr_14", family=FAMILY_VOLATILITY, values=atr14,
            higher_is_better=False, source=source,
        ))
        factors.append(FactorSeries(
            name="hl_range_20", family=FAMILY_VOLATILITY, values=hl_range,
            higher_is_better=False, source=source,
        ))
    else:
        reason = "价格表缺少 high/low 列，无法计算 ATR 与高低价波动范围"
        empty = pd.Series([np.nan] * len(d), dtype=float)
        factors.append(FactorSeries(
            name="atr_14", family=FAMILY_VOLATILITY, values=empty,
            available=False, unavailable_reason=reason,
            higher_is_better=False, source=source,
        ))
        factors.append(FactorSeries(
            name="hl_range_20", family=FAMILY_VOLATILITY, values=empty,
            available=False, unavailable_reason=reason,
            higher_is_better=False, source=source,
        ))

    return factors


# ═══════════════════════════════════════════════════════════════════
#   三、成交量因子
# ═══════════════════════════════════════════════════════════════════

def volume_factors(df: pd.DataFrame, *, source: str = "") -> list[FactorSeries]:
    """
    成交量因子族。

    * ``vol_ratio_20``  当日成交量 / 过去 20 日均量（量能放大）
    * ``vol_chg_5``     成交量 5 日变化率
    * ``pv_corr_20``    20 日价量配合度（收益与量变化的滚动相关）

    缺失/零成交量的处理（本族最容易出错的地方）
    -----------------------------------------
    停牌、数据缺失、集合竞价异常都会让 volume 为 0 或 NaN。此处规则：

    * 均量为 0 或 NaN → 比值置 **NaN**（不可计算），**不置 1.0**。
      置 1.0 等于宣称"量能正常"，是把缺失伪装成中性观测。
    * 单日 volume 为 0 会真实地让比值为 0（缩量到极致），这是有效观测，保留。
    * 整列 volume 缺失 → 整族标记 unavailable。
    """
    d = _prepare(df)
    n = len(d)

    if "volume" not in d.columns:
        reason = "价格表缺少 volume 列"
        empty = pd.Series([np.nan] * n, dtype=float)
        return [
            FactorSeries(name=nm, family=FAMILY_VOLUME, values=empty,
                         available=False, unavailable_reason=reason, source=source)
            for nm in ("vol_ratio_20", "vol_chg_5", "pv_corr_20")
        ]

    volume = pd.to_numeric(d["volume"], errors="coerce").astype(float)

    if volume.notna().sum() == 0:
        reason = "volume 列全为空或非数值"
        empty = pd.Series([np.nan] * n, dtype=float)
        return [
            FactorSeries(name=nm, family=FAMILY_VOLUME, values=empty,
                         available=False, unavailable_reason=reason, source=source)
            for nm in ("vol_ratio_20", "vol_chg_5", "pv_corr_20")
        ]

    # 均量：min_periods=20 保证预热期为 NaN 而非用不足量的窗口硬算
    avg20 = volume.rolling(20, min_periods=20).mean()
    # 0 或 NaN 的均量 → NaN（不可计算），绝不回退成 1.0
    safe_avg = avg20.replace(0.0, np.nan)
    vol_ratio = volume / safe_avg

    # 变化率：分母为 0 时 pct_change 会给 inf，显式转 NaN
    vol_chg = volume.pct_change(5).replace([np.inf, -np.inf], np.nan)

    close = d["close"].astype(float)
    ret = close.pct_change()
    vol_delta = volume.pct_change().replace([np.inf, -np.inf], np.nan)
    # rolling.corr 在窗口内方差为 0 时给 NaN，符合"不可计算"语义
    pv_corr = ret.rolling(20, min_periods=20).corr(vol_delta)

    return [
        FactorSeries(name="vol_ratio_20", family=FAMILY_VOLUME, values=vol_ratio,
                     higher_is_better=True, source=source),
        FactorSeries(name="vol_chg_5", family=FAMILY_VOLUME, values=vol_chg,
                     higher_is_better=True, source=source),
        FactorSeries(name="pv_corr_20", family=FAMILY_VOLUME, values=pv_corr,
                     higher_is_better=True, source=source),
    ]


# ═══════════════════════════════════════════════════════════════════
#   四、估值因子
# ═══════════════════════════════════════════════════════════════════

def valuation_factors(
    df: pd.DataFrame,
    *,
    history: pd.DataFrame | None = None,
    source: str = "",
) -> list[FactorSeries]:
    """
    估值因子族（PE / PB / ROE）。

    ``history`` 必须是**逐日或逐期的估值时间序列**，含 ``date`` 列与
    ``pe`` / ``pb`` / ``roe`` 中至少一列。若为 None，全族标记 unavailable。

    为什么默认不可用
    --------------
    本项目离线数据只有**当期估值快照**（``offline_sample_data.py`` 里每只标的
    一个标量 PE/PB/ROE），没有历史序列。把当期 PE 回填到过去，属于教科书级
    未来数据泄漏：2016 年的模型不可能知道 2026 年的 PE。三条禁令：

    1. 不用当期估值回填历史 → 因此不构造任何 ``fillna(当期值)``；
    2. 不生成随机估值 → 本函数不含任何随机数发生器；
    3. 不按 0 值参与打分 → 返回 ``available=False``，由打分层剔除并重归一化权重。

    只有当调用方真的提供了历史序列时，本族才可用。``merge_asof`` 用
    ``direction="backward"``：日期 t 只能匹配 t 或更早的披露值。
    """
    d = _prepare(df)
    n = len(d)
    empty = pd.Series([np.nan] * n, dtype=float)

    if history is None or len(history) == 0:
        reason = (
            "无历史估值时间序列：离线数据仅含当期 PE/PB/ROE 快照。"
            "用当期值回填历史属未来数据泄漏，故标记为不可用而非填充。"
        )
        return [
            FactorSeries(name=nm, family=FAMILY_VALUATION, values=empty,
                         available=False, unavailable_reason=reason,
                         higher_is_better=hib, source=source)
            for nm, hib in (("pe_inv", True), ("pb_inv", True), ("roe", True))
        ]

    hist = history.copy()
    if "date" not in hist.columns:
        raise ValueError("history 必须含 date 列")
    hist["date"] = hist["date"].astype(str)
    hist = hist.sort_values("date", kind="mergesort").reset_index(drop=True)

    # merge_asof 只接受数值或 datetime 键 —— 字符串日期会抛
    # "Incompatible merge dtype, both sides must have numeric dtype"。
    # 因此在这里转成 datetime64 做匹配，再丢掉临时列。
    left = d[["date"]].copy()
    left["_dt"] = pd.to_datetime(left["date"], errors="coerce")
    right = hist.copy()
    right["_dt"] = pd.to_datetime(right["date"], errors="coerce")
    right = right.drop(columns=["date"]).dropna(subset=["_dt"])
    if left["_dt"].isna().any():
        raise ValueError("价格表 date 列含无法解析为日期的值")

    # merge_asof + backward：t 只匹配 <= t 的最近一条披露，杜绝未来值
    merged = pd.merge_asof(
        left.sort_values("_dt", kind="mergesort"),
        right.sort_values("_dt", kind="mergesort"),
        on="_dt", direction="backward",
    ).drop(columns=["_dt"])

    out: list[FactorSeries] = []
    # PE / PB 取倒数：低估值为优，取倒数后统一成"越大越好"，
    # 避免在打分层再引入方向翻转逻辑（方向散落各处最容易出错）。
    for col, name in (("pe", "pe_inv"), ("pb", "pb_inv")):
        if col in merged.columns:
            raw = pd.to_numeric(merged[col], errors="coerce").astype(float)
            # 负 PE/PB（亏损或负净资产）不是"极度便宜"，倒数会给出错误的高分，
            # 因此置 NaN 而非参与打分。
            raw = raw.where(raw > 0.0, np.nan)
            out.append(FactorSeries(
                name=name, family=FAMILY_VALUATION, values=1.0 / raw,
                higher_is_better=True, source=source,
            ))
        else:
            out.append(FactorSeries(
                name=name, family=FAMILY_VALUATION, values=empty,
                available=False,
                unavailable_reason=f"history 缺少 {col} 列",
                higher_is_better=True, source=source,
            ))

    if "roe" in merged.columns:
        out.append(FactorSeries(
            name="roe", family=FAMILY_VALUATION,
            values=pd.to_numeric(merged["roe"], errors="coerce").astype(float),
            higher_is_better=True, source=source,
        ))
    else:
        out.append(FactorSeries(
            name="roe", family=FAMILY_VALUATION, values=empty,
            available=False, unavailable_reason="history 缺少 roe 列",
            higher_is_better=True, source=source,
        ))

    return out


# ═══════════════════════════════════════════════════════════════════
#   五、组装与横截面标准化
# ═══════════════════════════════════════════════════════════════════

def build_factor_set(
    symbol: str,
    df: pd.DataFrame,
    *,
    valuation_history: pd.DataFrame | None = None,
    source: str = "",
) -> FactorSet:
    """为单只标的构造四族因子。"""
    d = _prepare(df)
    fs = FactorSet(symbol=symbol, dates=d["date"])
    for f in momentum_factors(d, source=source):
        fs.add(f)
    for f in volatility_factors(d, source=source):
        fs.add(f)
    for f in volume_factors(d, source=source):
        fs.add(f)
    for f in valuation_factors(d, history=valuation_history, source=source):
        fs.add(f)
    return fs


def cross_sectional_rank(values: pd.Series) -> pd.Series:
    """
    横截面百分位排名，输出 [0, 1]。

    ``values`` 的索引是**标的**（不是日期）—— 即"同一天、不同股票"的一个切片。
    这是本项目唯一允许的标准化口径之一：它只用到 t 时刻的信息，
    不需要知道任何未来分布，因此天然免疫未来数据泄漏。

    禁止的替代做法：用 ``全样本 mean/std`` 做 z-score。那会把 2026 年的
    分布信息注入 2016 年的打分，是要求四.4 明确禁止的。

    NaN 保持 NaN（不参与排名，也不被填成中位数）。
    只有 1 个有效值时返回 0.5（无法区分优劣，给中性值）。
    """
    valid = values.dropna()
    out = pd.Series([np.nan] * len(values), index=values.index, dtype=float)
    if len(valid) == 0:
        return out
    if len(valid) == 1:
        out.loc[valid.index] = 0.5
        return out
    ranks = valid.rank(method="average", ascending=True)
    out.loc[valid.index] = (ranks - 1.0) / (len(valid) - 1.0)
    return out
