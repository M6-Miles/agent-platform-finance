"""
Walk-forward 样本外验证
=======================
把时间轴切成多折，每折三段 **严格时间递增、互不重叠**：

    |──── train ────|── validation ──|── test ──|
    train_end  <  validation_start
    validation_end  <  test_start

参数只在 **train + validation** 上选（train 看行为、validation 排序打分），
``test`` 段在参数定下来之后**只评一次**，绝不回头改参数。这条纪律在代码上的
体现是 :func:`select_params_on_train_validation` 的签名里**根本拿不到 test 数据** ——
它只接收 train/validation 切片。想在 test 上调参需要修改函数签名，
无法"顺手"做到。测试 ``test_test_range_is_not_used_for_parameter_selection``
通过篡改 test 段数据、断言所选参数不变来验证这一点。

样本不足时的行为
--------------
要求六.7：数据不够必须明确报错或返回 unavailable，**不允许缩短到失去统计
意义后仍声称有效**。因此 :func:`build_folds` 在总长度不足时抛
:class:`InsufficientDataError`，而 :func:`run_walk_forward` 捕获它并返回
``available=False`` 的结果对象 —— 报告里会显示 unavailable，不会显示一个
用 30 个样本算出来的"漂亮"夏普。

因子预热与折切分的关系
--------------------
因子（MA60、252 日分位）需要长预热期。若在每折内部重新计算因子，
test 段开头几十天会因预热不足而空仓，等于人为削弱样本外表现。
做法是：**在完整历史上一次性计算信号**（这本身是因果的，t 只用 ≤t 的数据），
再按折的日期区间**切片**评估。切片不会引入未来信息：test 段第一天用到的
历史确实发生在它之前。这与"用 test 段数据调参"是两件不同的事。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from .multifactor_strategy import (
    StrategyParams,
    default_param_grid,
    generate_signals,
)
from .position_backtest import run_position_backtest

logger = logging.getLogger(__name__)

# 每段最少交易日数。低于这个量级的夏普没有统计意义
# （Lo 2002 的 SE ≈ sqrt((1+SR²/2)/T)，T=60 时 SE≈0.13，
#  T=20 时 SE≈0.22，置信区间宽到无法区分 0 与 0.5）。
MIN_TRAIN_DAYS = 252
MIN_VALIDATION_DAYS = 126
MIN_TEST_DAYS = 126
# 因子预热：MA60 需 60 日，252 日分位窗口的 min_periods=60，取 120 作为安全余量
FACTOR_WARMUP_DAYS = 120


class InsufficientDataError(ValueError):
    """样本长度不足，无法构造具有统计意义的 walk-forward 划分。"""


def robust_selection_score(train_sharpe: float, validation_sharpe: float) -> float:
    """返回两段表现的保守下界，避免单窗口高分掩盖另一段失效。"""
    return min(float(train_sharpe), float(validation_sharpe))


@dataclass(frozen=True, slots=True)
class Fold:
    """
    单折的六个边界。字段名与要求六.1 逐字对应。

    ``__post_init__`` 里做的不是防御性编程，而是**不变量断言**：
    折的边界一旦重叠或倒序，整个样本外结论就是假的，必须在构造期炸掉。
    """

    fold_id: int
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str

    def __post_init__(self) -> None:
        if not (self.train_start <= self.train_end):
            raise ValueError(f"fold {self.fold_id}: train_start > train_end")
        if not (self.train_end < self.validation_start):
            raise ValueError(
                f"fold {self.fold_id}: 要求 train_end < validation_start，"
                f"实际 {self.train_end} vs {self.validation_start}"
            )
        if not (self.validation_start <= self.validation_end):
            raise ValueError(f"fold {self.fold_id}: validation_start > validation_end")
        if not (self.validation_end < self.test_start):
            raise ValueError(
                f"fold {self.fold_id}: 要求 validation_end < test_start，"
                f"实际 {self.validation_end} vs {self.test_start}"
            )
        if not (self.test_start <= self.test_end):
            raise ValueError(f"fold {self.fold_id}: test_start > test_end")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "fold_id": self.fold_id,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "validation_start": self.validation_start,
            "validation_end": self.validation_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
        }


def build_folds(
    dates: list[str],
    *,
    n_folds: int = 3,
    train_days: int = MIN_TRAIN_DAYS,
    validation_days: int = MIN_VALIDATION_DAYS,
    test_days: int = MIN_TEST_DAYS,
    anchored: bool = True,
) -> list[Fold]:
    """
    构造 walk-forward 折。

    ``anchored=True``（默认）→ train 起点固定在样本开头、随折增长（anchored /
    expanding walk-forward）；``False`` → 固定长度滚动窗口。两者都保证
    ``train_end < validation_start < validation_end < test_start``。

    折与折之间的 test 段互不重叠、时间递增（每折向前推进 ``test_days``）。

    抛 :class:`InsufficientDataError` 而不是自动缩短窗口 —— 见模块 docstring。
    """
    if n_folds < 1:
        raise ValueError("n_folds 必须 >= 1")
    for name, v, floor in (
        ("train_days", train_days, MIN_TRAIN_DAYS),
        ("validation_days", validation_days, MIN_VALIDATION_DAYS),
        ("test_days", test_days, MIN_TEST_DAYS),
    ):
        if v < floor:
            raise InsufficientDataError(
                f"{name}={v} 低于统计意义下限 {floor}。"
                f"缩短窗口会让夏普的标准误大到无法与阈值区分，故拒绝执行。"
            )

    ds = sorted(str(d) for d in dates)
    need = train_days + validation_days + test_days * n_folds
    if len(ds) < need:
        raise InsufficientDataError(
            f"样本仅 {len(ds)} 个交易日，构造 {n_folds} 折需要至少 {need} 个"
            f"（train {train_days} + validation {validation_days} + "
            f"test {test_days}×{n_folds}）。"
            f"不缩短窗口、不减折数以强行出结果。"
        )

    folds: list[Fold] = []
    for k in range(n_folds):
        offset = k * test_days
        tr_lo = 0 if anchored else offset
        tr_hi = train_days + offset            # exclusive
        va_lo = tr_hi
        va_hi = va_lo + validation_days        # exclusive
        te_lo = va_hi
        te_hi = te_lo + test_days              # exclusive
        if te_hi > len(ds):
            raise InsufficientDataError(
                f"第 {k + 1} 折越界：需要索引 {te_hi}，样本仅 {len(ds)}"
            )
        folds.append(Fold(
            fold_id=k + 1,
            train_start=ds[tr_lo],
            train_end=ds[tr_hi - 1],
            validation_start=ds[va_lo],
            validation_end=ds[va_hi - 1],
            test_start=ds[te_lo],
            test_end=ds[te_hi - 1],
        ))
    return folds


def _slice(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    d = df.copy()
    d["date"] = d["date"].astype(str)
    mask = (d["date"] >= start) & (d["date"] <= end)
    return d.loc[mask].reset_index(drop=True)


def _slice_positions(
    positions: dict[str, float], start: str, end: str
) -> dict[str, float]:
    return {k: v for k, v in positions.items() if start <= k <= end}


@dataclass(slots=True)
class FoldResult:
    """单折结果。保存要求六.6 要求的全部内容。"""

    fold: Fold
    symbol: str
    data_source: str
    chosen_params: dict[str, Any]
    train: dict[str, Any] | None
    validation: dict[str, Any] | None
    test: dict[str, Any] | None
    n_candidates: int
    selection_metric: str
    available: bool = True
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.fold.to_dict(),
            "symbol": self.symbol,
            "data_source": self.data_source,
            "chosen_params": self.chosen_params,
            "n_candidates": self.n_candidates,
            "selection_metric": self.selection_metric,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "train_result": self.train,
            "validation_result": self.validation,
            "test_result": self.test,
        }


@dataclass(slots=True)
class WalkForwardResult:
    symbol: str
    data_source: str
    available: bool
    folds: list[FoldResult] = field(default_factory=list)
    unavailable_reason: str | None = None
    sample_start: str = ""
    sample_end: str = ""
    n_days: int = 0

    def test_sharpes(self) -> list[float]:
        return [
            float(f.test["sharpe_calendar"])
            for f in self.folds
            if f.available and f.test is not None
        ]

    def mean_test_sharpe(self) -> float | None:
        xs = self.test_sharpes()
        return sum(xs) / len(xs) if xs else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "data_source": self.data_source,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "sample_start": self.sample_start,
            "sample_end": self.sample_end,
            "n_days": self.n_days,
            "n_folds": len(self.folds),
            "mean_test_sharpe": self.mean_test_sharpe(),
            "folds": [f.to_dict() for f in self.folds],
        }


def select_params_on_train_validation(
    symbol: str,
    train_df: pd.DataFrame,
    validation_df: pd.DataFrame,
    *,
    candidates: list[StrategyParams],
    signal_builder: Callable[[StrategyParams], dict[str, float]],
    data_source: str = "",
    slippage_pct: float = 0.1,
    commission_pct: float = 0.03,
    stamp_duty_pct: float = 0.0,
    min_train_sharpe: float | None = None,
    selection_policy: str = "validation",
) -> tuple[StrategyParams, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """
    在 train + validation 上挑参数。

    **函数签名里没有 test 数据** —— 这是"test 不参与选参"的结构性保证，
    而非注释承诺。默认按 validation Sharpe 排序；可选 ``robust`` 挑战规则按
    train/validation 两段 Sharpe 的较低值排序，以惩罚单窗口偶然高分。
    并列时取候选列表中靠前者（``default_param_grid`` 顺序固定），
    保证选择过程完全确定、可复现。

    返回 ``(最优参数, train 结果, validation 结果, 全部候选的评估记录)``。
    """
    if not candidates:
        raise ValueError("candidates 不能为空")
    if selection_policy not in {"validation", "robust"}:
        raise ValueError("selection_policy 必须是 validation 或 robust")

    trials: list[dict[str, Any]] = []
    best: tuple[float, int, StrategyParams, dict[str, Any], dict[str, Any]] | None = None

    for rank, params in enumerate(candidates):
        positions = signal_builder(params)

        tr_dates = [str(d) for d in train_df["date"].tolist()]
        va_dates = [str(d) for d in validation_df["date"].tolist()]

        tr_res = run_position_backtest(
            symbol, train_df,
            {d: positions.get(d, 0.0) for d in tr_dates},
            strategy="multifactor", data_source=data_source,
            slippage_pct=slippage_pct, commission_pct=commission_pct,
            stamp_duty_pct=stamp_duty_pct,
        )
        va_res = run_position_backtest(
            symbol, validation_df,
            {d: positions.get(d, 0.0) for d in va_dates},
            strategy="multifactor", data_source=data_source,
            slippage_pct=slippage_pct, commission_pct=commission_pct,
            stamp_duty_pct=stamp_duty_pct,
        )

        trials.append({
            "params": params.to_dict(),
            "label": params.label(),
            "train_sharpe": tr_res.sharpe_calendar,
            "validation_sharpe": va_res.sharpe_calendar,
            "robust_selection_score": robust_selection_score(
                tr_res.sharpe_calendar, va_res.sharpe_calendar
            ),
            "validation_return_pct": va_res.total_return_pct,
            "validation_turnover": va_res.total_turnover,
        })

        # 可选过滤：train 段太差的参数不进入排序（避免只靠 validation 侥幸）
        if min_train_sharpe is not None and tr_res.sharpe_calendar < min_train_sharpe:
            continue

        key = (
            robust_selection_score(tr_res.sharpe_calendar, va_res.sharpe_calendar)
            if selection_policy == "robust"
            else va_res.sharpe_calendar
        )
        if best is None or key > best[0]:
            best = (key, rank, params, tr_res.to_dict(), va_res.to_dict())

    if best is None:
        # 所有候选都被 min_train_sharpe 过滤掉 → 退回网格首项（确定性），
        # 并如实记录：不静默假装选到了好参数
        params = candidates[0]
        positions = signal_builder(params)
        tr_dates = [str(d) for d in train_df["date"].tolist()]
        va_dates = [str(d) for d in validation_df["date"].tolist()]
        tr_res = run_position_backtest(
            symbol, train_df, {d: positions.get(d, 0.0) for d in tr_dates},
            strategy="multifactor", data_source=data_source,
            slippage_pct=slippage_pct, commission_pct=commission_pct,
            stamp_duty_pct=stamp_duty_pct,
        )
        va_res = run_position_backtest(
            symbol, validation_df, {d: positions.get(d, 0.0) for d in va_dates},
            strategy="multifactor", data_source=data_source,
            slippage_pct=slippage_pct, commission_pct=commission_pct,
            stamp_duty_pct=stamp_duty_pct,
        )
        logger.warning(
            "%s: 全部候选未通过 min_train_sharpe=%s 过滤，回退到网格首项",
            symbol, min_train_sharpe,
        )
        return params, tr_res.to_dict(), va_res.to_dict(), trials

    return best[2], best[3], best[4], trials


def run_walk_forward(
    symbol: str,
    price_df: pd.DataFrame,
    *,
    candidates: list[StrategyParams] | None = None,
    n_folds: int = 3,
    train_days: int = MIN_TRAIN_DAYS,
    validation_days: int = MIN_VALIDATION_DAYS,
    test_days: int = MIN_TEST_DAYS,
    data_source: str = "",
    valuation_history: pd.DataFrame | None = None,
    slippage_pct: float = 0.1,
    commission_pct: float = 0.03,
    stamp_duty_pct: float = 0.0,
    selection_policy: str = "validation",
) -> WalkForwardResult:
    """
    对单只标的执行完整 walk-forward。

    每折流程：
      1. 切 train / validation / test（边界由 :class:`Fold` 断言保证不重叠）；
      2. 在 train+validation 上选参（拿不到 test）；
      3. 用选定参数在 test 上**评一次**，结果直接入账，不做任何回头调整。
    """
    candidates = candidates or default_param_grid()

    df = price_df.copy()
    df["date"] = df["date"].astype(str)
    df = df.sort_values("date", kind="mergesort").reset_index(drop=True)
    dates = [str(d) for d in df["date"].tolist()]

    try:
        folds = build_folds(
            dates, n_folds=n_folds, train_days=train_days,
            validation_days=validation_days, test_days=test_days,
        )
    except InsufficientDataError as exc:
        # 明确返回 unavailable，不缩短窗口、不伪造指标
        return WalkForwardResult(
            symbol=symbol, data_source=data_source, available=False,
            unavailable_reason=str(exc),
            sample_start=dates[0] if dates else "",
            sample_end=dates[-1] if dates else "",
            n_days=len(dates),
        )

    # 在完整历史上算一次信号，再按折切片。切片不引入未来信息（见模块 docstring）。
    signal_cache: dict[str, dict[str, float]] = {}

    def _signals_for(params: StrategyParams) -> dict[str, float]:
        key = params.label() + f"|nw={params.norm_window}"
        if key not in signal_cache:
            sig = generate_signals(
                symbol, df, params,
                valuation_history=valuation_history, data_source=data_source,
            )
            signal_cache[key] = sig.positions_by_date()
        return signal_cache[key]

    results: list[FoldResult] = []
    for fold in folds:
        tr_df = _slice(df, fold.train_start, fold.train_end)
        va_df = _slice(df, fold.validation_start, fold.validation_end)
        te_df = _slice(df, fold.test_start, fold.test_end)

        if len(tr_df) < 2 or len(va_df) < 2 or len(te_df) < 2:
            results.append(FoldResult(
                fold=fold, symbol=symbol, data_source=data_source,
                chosen_params={}, train=None, validation=None, test=None,
                n_candidates=len(candidates),
                selection_metric=(
                    "max_min_train_validation_sharpe_calendar"
                    if selection_policy == "robust" else "validation_sharpe_calendar"
                ),
                available=False,
                unavailable_reason="切片后某段不足 2 个交易日",
            ))
            continue

        best_params, tr_res, va_res, _trials = select_params_on_train_validation(
            symbol, tr_df, va_df,
            candidates=candidates,
            signal_builder=_signals_for,
            data_source=data_source,
            slippage_pct=slippage_pct, commission_pct=commission_pct,
            stamp_duty_pct=stamp_duty_pct,
            selection_policy=selection_policy,
        )

        # 参数已固定，test 只评一次
        positions = _signals_for(best_params)
        te_dates = [str(d) for d in te_df["date"].tolist()]
        te_res = run_position_backtest(
            symbol, te_df,
            {d: positions.get(d, 0.0) for d in te_dates},
            strategy="multifactor", data_source=data_source,
            slippage_pct=slippage_pct, commission_pct=commission_pct,
            stamp_duty_pct=stamp_duty_pct,
        )

        results.append(FoldResult(
            fold=fold, symbol=symbol, data_source=data_source,
            chosen_params=best_params.to_dict(),
            train=tr_res, validation=va_res, test=te_res.to_dict(),
            n_candidates=len(candidates),
            selection_metric=(
                "max_min_train_validation_sharpe_calendar"
                if selection_policy == "robust" else "validation_sharpe_calendar"
            ),
            available=True,
        ))

    return WalkForwardResult(
        symbol=symbol, data_source=data_source, available=True,
        folds=results,
        sample_start=dates[0], sample_end=dates[-1], n_days=len(dates),
    )
