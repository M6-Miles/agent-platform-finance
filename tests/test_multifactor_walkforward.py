"""
多因子策略 + Walk-forward 样本外验证 —— 回归测试
================================================
全部使用**确定性合成数据**（固定随机种子），不读网络、不依赖 ``data/real``
（后者已被 gitignore，在干净克隆里不存在）。

测试的组织按"要求"编号分组，每组开头写明它在防什么：
  - TestLookAhead        —— 未来数据泄漏（要求四）
  - TestWalkForward      —— 折划分与选参纪律（要求六）
  - TestPositionControl  —— 仓位边界与波动率定仓（要求五）
  - TestCosts            —— 成本与滑点真实扣减（要求九）
  - TestBenchmark        —— 基准同区间（要求八）
  - TestTurnover         —— 换手上报（要求五.4）
  - TestValuation        —— 不可用估值不得伪造（要求三.4）
  - TestBaselineUnchanged—— 原 MA 策略与原引擎逐位不变（要求一.11）
  - TestReport           —— 报告同时含 baseline 与多因子（要求十一）
  - TestFormulaUnchanged —— Sharpe 公式与 0.5 阈值未被改动（要求二 / 一.8）
"""
from __future__ import annotations

import ast
import inspect
import json
import math
import random
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "Scripts"))

from agent_platform.finance import backtesting as bt_mod
from agent_platform.finance import position_backtest as pb_mod
from agent_platform.finance import sharpe_stats as ss_mod
from agent_platform.finance.backtesting import run_backtest
from agent_platform.finance.factors import (
    ALL_FAMILIES,
    build_factor_set,
    cross_sectional_rank,
    valuation_factors,
    volume_factors,
)
from agent_platform.finance.multifactor_strategy import (
    StrategyParams,
    apply_turnover_control,
    causal_percentile,
    compute_vol_scalar,
    default_param_grid,
    generate_signals,
    ma_baseline_positions,
)
from agent_platform.finance.position_backtest import (
    SHARPE_TARGET,
    buy_and_hold_benchmark,
    run_position_backtest,
)
from agent_platform.finance.walk_forward import (
    InsufficientDataError,
    build_folds,
    run_walk_forward,
    robust_selection_score,
    select_params_on_train_validation,
)


# ═══════════════════════════════════════════════════════════════════
#   确定性数据构造
# ═══════════════════════════════════════════════════════════════════

def make_prices(
    n: int = 800,
    *,
    seed: int = 20260810,
    vol: float = 0.012,
    drift_amp: float = 0.0006,
    start: str = "2019-01-02",
) -> pd.DataFrame:
    """
    确定性 OHLCV 序列。

    漂移用 ``sin`` 振荡（均值≈0）而非常数正漂移 —— 刻意不给策略免费的上涨趋势，
    否则任何多头策略都会"看起来有效"。固定 seed 保证跨机可复现。
    """
    rnd = random.Random(seed)
    rows = []
    price = 100.0
    for i in range(n):
        price *= (1 + drift_amp * math.sin(i / 90.0) + rnd.gauss(0.0, vol))
        o = price * (1 + rnd.gauss(0.0, 0.002))
        h = max(o, price) * (1 + abs(rnd.gauss(0.0, 0.003)))
        l = min(o, price) * (1 - abs(rnd.gauss(0.0, 0.003)))
        rows.append({
            "open": o, "high": h, "low": l, "close": price,
            "volume": 1_000_000.0 * (1 + abs(rnd.gauss(0.0, 0.3))),
        })
    df = pd.DataFrame(rows)
    df.insert(0, "date", pd.date_range(start, periods=n, freq="B").strftime("%Y-%m-%d"))
    return df


def small_grid() -> list[StrategyParams]:
    """测试用小网格（4 组），保持测试快速但仍是真实的多候选选参。"""
    base = StrategyParams()
    return [
        replace(base, score_threshold=0.45, target_vol=0.10),
        replace(base, score_threshold=0.55, target_vol=0.15),
        replace(base, score_threshold=0.65, target_vol=0.20),
        replace(base, score_threshold=0.55, target_vol=0.20, trend_ma=120),
    ]


@pytest.fixture(scope="module")
def prices() -> pd.DataFrame:
    return make_prices()


# ═══════════════════════════════════════════════════════════════════
#   要求四：未来数据泄漏
# ═══════════════════════════════════════════════════════════════════

class TestLookAhead:
    """防的是：把未来价格、未来分布、未来波动率偷偷用进过去的信号。"""

    def test_future_data_change_does_not_change_past_signal(self, prices):
        """
        改动未来某日之后的**全部**价格，t 之前的信号必须逐位不变。

        这是因果性最直接的判据：若任何一处用了 shift(-k)、center=True 或
        全样本 mean/std，这个断言会立刻失败。
        """
        cut = 500
        base = generate_signals("T", prices, StrategyParams())

        mutated = prices.copy()
        # 把 cut 之后的价格整体抬高 50%、成交量翻倍（剧烈改动，放大任何泄漏）
        idx = mutated.index >= cut
        for col in ("open", "high", "low", "close"):
            mutated.loc[idx, col] = mutated.loc[idx, col] * 1.5
        mutated.loc[idx, "volume"] = mutated.loc[idx, "volume"] * 2.0

        after = generate_signals("T", mutated, StrategyParams())

        for name, a, b in (
            ("target_position", base.target_position, after.target_position),
            ("composite_score", base.composite_score, after.composite_score),
            ("trend_gate", base.trend_gate, after.trend_gate),
            ("vol_scalar", base.vol_scalar, after.vol_scalar),
        ):
            left = a.iloc[:cut].fillna(-999.0).tolist()
            right = b.iloc[:cut].fillna(-999.0).tolist()
            assert left == right, f"{name} 在 t<{cut} 处被未来数据改变（存在未来函数）"

    def test_causal_percentile_never_sees_future(self):
        """
        因果分位：逐点重算（只喂前缀）必须与一次性整列计算完全一致。

        若实现里含任何未来引用，"只喂前缀"与"喂全量"就会在同一位置给出不同值。
        """
        s = pd.Series([3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0, 5.0, 3.0, 5.0, 8.0])
        full = causal_percentile(s, window=None, min_periods=3)
        for i in range(len(s)):
            prefix = causal_percentile(s.iloc[: i + 1], window=None, min_periods=3)
            a, b = full.iloc[i], prefix.iloc[i]
            assert (pd.isna(a) and pd.isna(b)) or a == pytest.approx(b), (
                f"位置 {i}: 整列={a} 前缀={b}，说明用到了未来数据"
            )

    def test_rolling_percentile_matches_prefix_recompute(self):
        """滚动窗口版本同样只能看窗口内的过去。"""
        s = pd.Series([float(x) for x in [5, 2, 8, 1, 9, 3, 7, 4, 6, 2, 8, 1, 5]])
        full = causal_percentile(s, window=5, min_periods=3)
        for i in range(len(s)):
            prefix = causal_percentile(s.iloc[: i + 1], window=5, min_periods=3)
            a, b = full.iloc[i], prefix.iloc[i]
            assert (pd.isna(a) and pd.isna(b)) or a == pytest.approx(b), f"位置 {i}"

    def test_signal_is_shifted_before_execution(self, prices):
        """
        t 的目标仓位最早在 t+1 生效：第 i 天承担的仓位必须等于 target[i-1]。

        构造：只有第 5 天给出仓位 1.0，其余为 0。那么第 6 天（i=6）才应有暴露，
        第 5 天当日必须仍是 0 —— 否则等于用当天收盘后才知道的信号在当天成交。
        """
        df = prices.iloc[:30].reset_index(drop=True)
        targets = [0.0] * len(df)
        targets[5] = 1.0

        res = run_position_backtest("T", df, targets, strategy="probe")
        detail = {int(d["i"]): d for d in res.daily_detail}

        assert detail[5]["w_target"] == 0.0, "第 5 天不应有仓位（信号当天不能成交）"
        assert detail[6]["w_target"] == 1.0, "第 6 天应承担第 5 天的目标仓位"
        assert detail[7]["w_target"] == 0.0, "第 7 天应已清仓"

        # 第 6 天的毛收益应完全由第 6 天的 open→close 决定（隔夜段旧仓位为 0）
        o6, c6 = float(df["open"].iloc[6]), float(df["close"].iloc[6])
        assert detail[6]["gross_ret"] == pytest.approx((c6 - o6) / o6, rel=1e-12)

    def test_no_full_sample_normalization_in_source(self):
        """
        源码级禁令扫描：新模块内不得出现未来函数写法。

        这是"结构性防守"——即便将来有人改动实现，只要写出 shift(-1)、
        center=True 之类，这条测试就会红。
        """
        forbidden = ("shift(-", "center=True", "[::-1]", "bfill", "backfill")
        for mod in ("factors.py", "multifactor_strategy.py",
                    "position_backtest.py", "walk_forward.py"):
            path = ROOT / "src" / "agent_platform" / "finance" / mod
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # 用 AST 剥掉 docstring 再扫描：这些模块的 docstring 本身就在
            # **列举**被禁写法（"禁止 shift(-k)"），按文本扫描会误报。
            # 只有真正的可执行代码才算违规。
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = node.body
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        node.body = body[1:] or [ast.Pass()]
            code = ast.unparse(tree)
            for pat in forbidden:
                assert pat not in code, f"{mod} 出现未来函数写法 {pat!r}"

    def test_vol_scalar_uses_only_past_volatility(self, prices):
        """波动率定仓不得使用未来波动率：改未来价格，过去的 scalar 不变。"""
        cut = 300
        p = StrategyParams()
        base = compute_vol_scalar(prices, p)
        mutated = prices.copy()
        mutated.loc[mutated.index >= cut, "close"] *= 3.0
        after = compute_vol_scalar(mutated, p)
        assert base.iloc[:cut].fillna(-1).tolist() == after.iloc[:cut].fillna(-1).tolist()


# ═══════════════════════════════════════════════════════════════════
#   要求六：walk-forward
# ═══════════════════════════════════════════════════════════════════

def test_robust_selection_score_uses_weaker_segment():
    assert robust_selection_score(1.2, 0.3) == pytest.approx(0.3)
    assert robust_selection_score(-0.4, 0.8) == pytest.approx(-0.4)


class TestWalkForward:
    """防的是：折重叠、时间倒序、以及用 test 段调参。"""

    def test_walk_forward_ranges_do_not_overlap(self, prices):
        """六个边界必须严格满足 train < validation < test，且折间时间递增。"""
        dates = [str(d) for d in prices["date"].tolist()]
        folds = build_folds(dates, n_folds=3)
        assert len(folds) == 3

        prev_test_start = ""
        for f in folds:
            for key in ("train_start", "train_end", "validation_start",
                        "validation_end", "test_start", "test_end"):
                assert getattr(f, key), f"fold {f.fold_id} 缺少 {key}"

            assert f.train_start <= f.train_end
            assert f.train_end < f.validation_start, "train 与 validation 重叠"
            assert f.validation_start <= f.validation_end
            assert f.validation_end < f.test_start, "validation 与 test 重叠"
            assert f.test_start <= f.test_end

            # 折间递增：后一折的 test 起点必须晚于前一折
            assert f.test_start > prev_test_start
            prev_test_start = f.test_start

        # 折间 test 段互不重叠
        for a, b in zip(folds, folds[1:]):
            assert a.test_end < b.test_start, "相邻折的 test 段重叠"

    def test_fold_construction_rejects_reversed_ranges(self):
        """边界倒序必须在构造期抛错，而不是留到统计阶段。"""
        from agent_platform.finance.walk_forward import Fold
        with pytest.raises(ValueError, match="train_end < validation_start"):
            Fold(1, "2020-01-01", "2020-06-30", "2020-06-30", "2020-09-30",
                 "2020-10-01", "2020-12-31")
        with pytest.raises(ValueError, match="validation_end < test_start"):
            Fold(1, "2020-01-01", "2020-03-31", "2020-04-01", "2020-09-30",
                 "2020-09-30", "2020-12-31")

    def test_insufficient_data_raises_instead_of_shrinking(self):
        """
        样本不足必须报错/unavailable，不允许缩短窗口后仍宣称有效（要求六.7）。
        """
        short = make_prices(200)
        dates = [str(d) for d in short["date"].tolist()]
        with pytest.raises(InsufficientDataError, match="样本仅"):
            build_folds(dates, n_folds=3)

        # 上层封装：返回 available=False 且给出原因，而不是伪造指标
        wf = run_walk_forward("T", short, candidates=small_grid(), n_folds=3)
        assert wf.available is False
        assert wf.unavailable_reason and "样本仅" in wf.unavailable_reason
        assert wf.folds == []
        assert wf.mean_test_sharpe() is None

    def test_window_below_statistical_floor_is_rejected(self):
        """把窗口缩到没有统计意义也必须被拒绝。"""
        dates = [str(d) for d in make_prices(800)["date"].tolist()]
        with pytest.raises(InsufficientDataError, match="统计意义下限"):
            build_folds(dates, n_folds=3, train_days=60,
                        validation_days=30, test_days=30)

    def test_test_range_is_not_used_for_parameter_selection(self, prices):
        """
        篡改 test 段数据后，所选参数必须完全不变。

        做法：跑两次完整 walk-forward，第二次把每折 test 段之后的价格大幅改动。
        若选参过程碰过 test 段，选出的参数就会变。
        """
        grid = small_grid()
        wf1 = run_walk_forward("T", prices, candidates=grid, n_folds=2)
        assert wf1.available

        # 找到第 1 折 test 起点，把它及之后的价格全部改掉
        te_start = wf1.folds[0].fold.test_start
        mutated = prices.copy()
        mask = mutated["date"].astype(str) >= te_start
        for col in ("open", "high", "low", "close"):
            mutated.loc[mask, col] = mutated.loc[mask, col] * 0.6
        mutated.loc[mask, "volume"] = mutated.loc[mask, "volume"] * 5.0

        wf2 = run_walk_forward("T", mutated, candidates=grid, n_folds=2)
        assert wf2.available

        f1, f2 = wf1.folds[0], wf2.folds[0]
        assert f1.chosen_params == f2.chosen_params, (
            "篡改 test 段改变了所选参数 —— 说明 test 段参与了选参"
        )
        # train 结果也不应受影响（train 完全在 test 之前）
        assert f1.train["sharpe_calendar"] == pytest.approx(
            f2.train["sharpe_calendar"], rel=1e-12
        )

    def test_selector_signature_has_no_test_data(self):
        """
        结构性保证：选参函数的签名里根本没有 test 参数。

        这比注释更强 —— 想用 test 调参必须改签名，无法"顺手"做到。
        """
        params = inspect.signature(select_params_on_train_validation).parameters
        for name in params:
            assert "test" not in name.lower(), f"选参函数出现 test 相关参数: {name}"
        assert "train_df" in params and "validation_df" in params

    def test_fold_saves_all_required_fields(self, prices):
        """每折必须保存：选定参数、train/validation/test 结果、区间、数据源。"""
        wf = run_walk_forward("T", prices, candidates=small_grid(), n_folds=2,
                             data_source="synthetic-deterministic")
        assert wf.available
        for f in wf.folds:
            d = f.to_dict()
            for key in ("train_start", "train_end", "validation_start",
                        "validation_end", "test_start", "test_end",
                        "chosen_params", "train_result", "validation_result",
                        "test_result", "data_source", "selection_metric"):
                assert key in d, f"折结果缺少 {key}"
            assert d["data_source"] == "synthetic-deterministic"
            assert d["chosen_params"]
            assert d["test_result"]["start_date"] >= d["test_start"]
            assert d["test_result"]["end_date"] <= d["test_end"]


# ═══════════════════════════════════════════════════════════════════
#   要求五：仓位控制
# ═══════════════════════════════════════════════════════════════════

class TestPositionControl:
    """防的是：负仓位、杠杆、用未来波动率定仓、换手无上报。"""

    def test_position_is_between_zero_and_one(self, prices):
        """所有参数组合下，仓位恒在 [0, 1]。"""
        for p in default_param_grid():
            sig = generate_signals("T", prices, p)
            tp = sig.target_position
            assert tp.notna().all(), "仓位序列不应含 NaN"
            assert float(tp.min()) >= 0.0, f"出现负仓位 {tp.min()} @ {p.label()}"
            assert float(tp.max()) <= 1.0, f"出现杠杆 {tp.max()} @ {p.label()}"
            assert float(sig.raw_position.min()) >= 0.0
            assert float(sig.raw_position.max()) <= 1.0

    def test_engine_clamps_out_of_range_targets(self, prices):
        """
        即使上游传入 -5 或 3.0，引擎也必须夹到 [0,1]。

        禁止融资与负仓位是硬约束，不能依赖上游自觉。
        """
        df = prices.iloc[:50].reset_index(drop=True)
        res = run_position_backtest("T", df, [3.0, -5.0] * 25, strategy="probe")
        assert res.max_position <= 1.0
        assert res.min_position >= 0.0
        for d in res.daily_detail:
            assert 0.0 <= d["w_target"] <= 1.0
            assert 0.0 <= d["w_prev"] <= 1.0

    def test_high_volatility_reduces_position(self):
        """
        高波动必须降仓：同一 target_vol 下，高波动序列的 vol_scalar 必须更小。

        用两条只差波动率的确定性序列（同种子、同漂移），避免混入其它差异。
        """
        p = StrategyParams(target_vol=0.15)
        lo = make_prices(400, seed=7, vol=0.006)
        hi = make_prices(400, seed=7, vol=0.030)

        s_lo = compute_vol_scalar(lo, p).iloc[60:]
        s_hi = compute_vol_scalar(hi, p).iloc[60:]

        assert s_lo.mean() > s_hi.mean(), (
            f"高波动未降仓：低波动均值 {s_lo.mean():.4f} <= 高波动 {s_hi.mean():.4f}"
        )
        # 逐点也应几乎处处成立（同种子 → 同方向的随机数）
        both = pd.DataFrame({"lo": s_lo.reset_index(drop=True),
                             "hi": s_hi.reset_index(drop=True)}).dropna()
        frac = float((both["lo"] >= both["hi"]).mean())
        assert frac > 0.9, f"仅 {frac:.1%} 的日子满足高波动仓位更低"
        assert float(s_hi.max()) <= p.max_position

    def test_vol_scalar_zero_when_volatility_unknown_or_zero(self):
        """波动率不可得或为 0 → 仓位 0（不承担无法度量的风险，也不变成 inf）。"""
        p = StrategyParams(target_vol=0.15)
        flat = pd.DataFrame({
            "date": pd.date_range("2020-01-02", periods=60, freq="B").strftime("%Y-%m-%d"),
            "close": [100.0] * 60,
        })
        s = compute_vol_scalar(flat, p)
        assert float(s.max()) == 0.0, "零波动不应被当成可满仓"
        assert s.iloc[:20].eq(0.0).all(), "预热期应为 0"

    def test_turnover_control_suppresses_small_moves(self):
        """
        偏离小于阈值且未到调仓间隔 → 不动。

        构造一串每天微幅上升的目标，阈值 0.2、间隔 1000 天（实际禁用到期调仓），
        则仓位只应在累计偏离跨过 0.2 时跳变，而非逐日跟随。
        """
        p = StrategyParams(rebalance_threshold=0.2, rebalance_days=10_000,
                           force_exit_on_trend_off=False)
        desired = pd.Series([i * 0.01 for i in range(60)])
        out = apply_turnover_control(desired, p)
        changes = sum(1 for a, b in zip(out.tolist(), out.tolist()[1:])
                      if a != b)
        assert changes < 10, f"换手控制未生效，调仓 {changes} 次"
        assert float(out.max()) <= float(desired.max())

    def test_turnover_control_force_exits_when_trend_off(self):
        """趋势门关闭必须立即清仓，不受换手阻尼限制（风控优先）。"""
        p = StrategyParams(rebalance_threshold=0.9, rebalance_days=10_000,
                           force_exit_on_trend_off=True)
        desired = pd.Series([1.0] * 10)
        gate = pd.Series([1.0] * 5 + [0.0] * 5)
        out = apply_turnover_control(desired, p, trend_gate=gate)
        assert out.iloc[4] == 1.0
        assert out.iloc[5] == 0.0, "趋势门关闭后未立即清仓"
        assert out.iloc[9] == 0.0

    def test_trend_filter_blocks_long_in_downtrend(self):
        """
        中长期下跌趋势中不得做多。

        构造单调下跌序列：收盘价永远在 MA60 之下、MA60 持续下行，
        趋势门必须全程为 0，最终仓位必须恒为 0。
        """
        n = 300
        df = pd.DataFrame({
            "date": pd.date_range("2020-01-02", periods=n, freq="B").strftime("%Y-%m-%d"),
            "close": [100.0 * (0.995 ** i) for i in range(n)],
        })
        df["open"] = df["close"]
        df["high"] = df["close"] * 1.001
        df["low"] = df["close"] * 0.999
        df["volume"] = 1_000_000.0

        sig = generate_signals("DOWN", df, StrategyParams())
        assert float(sig.trend_gate.max()) == 0.0, "下跌趋势中趋势门未关闭"
        assert float(sig.target_position.max()) == 0.0, "下跌趋势中仍然建仓"


# ═══════════════════════════════════════════════════════════════════
#   要求九：成本与滑点
# ═══════════════════════════════════════════════════════════════════

class TestCosts:
    """防的是：报告写了成本但净值里没扣、或扣得与上报不一致。"""

    def _targets(self, n: int) -> list[float]:
        """周期性满仓/空仓，保证产生真实换手。"""
        return [1.0 if (i // 10) % 2 == 0 else 0.0 for i in range(n)]

    def test_transaction_cost_is_deducted(self, prices):
        """含成本的净值必须严格低于零成本版本（性质 1）。"""
        df = prices.iloc[:200].reset_index(drop=True)
        t = self._targets(len(df))
        free = run_position_backtest("T", df, t, strategy="free",
                                    slippage_pct=0.0, commission_pct=0.0)
        paid = run_position_backtest("T", df, t, strategy="paid",
                                    slippage_pct=0.1, commission_pct=0.03)
        assert paid.total_cost > 0.0
        assert free.total_cost == 0.0
        assert paid.equity_curve[-1] < free.equity_curve[-1], "成本未从净值中扣除"
        assert paid.total_return_pct < free.total_return_pct

    def test_slippage_is_deducted(self, prices):
        """滑点越大，收益单调不增（性质 2）。"""
        df = prices.iloc[:200].reset_index(drop=True)
        t = self._targets(len(df))
        rets = []
        for slip in (0.0, 0.05, 0.1, 0.5, 1.0):
            r = run_position_backtest("T", df, t, strategy="s",
                                      slippage_pct=slip, commission_pct=0.0)
            rets.append(r.total_return_pct)
            assert r.slippage_cost >= 0.0
        for a, b in zip(rets, rets[1:]):
            assert b <= a + 1e-9, f"滑点增大反而提高收益: {rets}"
        assert rets[-1] < rets[0]

    def test_zero_trades_means_zero_cost(self, prices):
        """零交易 → 零成本（性质 3）。"""
        df = prices.iloc[:100].reset_index(drop=True)
        r = run_position_backtest("T", df, [0.0] * len(df), strategy="flat")
        assert r.total_turnover == 0.0
        assert r.commission_cost == 0.0
        assert r.slippage_cost == 0.0
        assert r.stamp_duty_cost == 0.0
        assert r.total_cost == 0.0
        assert r.total_trades == 0
        assert r.total_return_pct == pytest.approx(0.0, abs=1e-12)

    def test_more_trades_means_proportionally_more_cost(self, prices):
        """
        交易越多、成本越高，且大致成比例（性质 4）。

        比的是 ``总成本/总换手`` —— 单位换手成本应基本恒定（仅因净值变化而略有
        差异），这比"成本单调递增"更强，能抓出"成本按次数而非按金额计"的错误。
        """
        df = prices.iloc[:300].reset_index(drop=True)
        few = [1.0 if (i // 50) % 2 == 0 else 0.0 for i in range(len(df))]
        many = [1.0 if (i // 5) % 2 == 0 else 0.0 for i in range(len(df))]

        r_few = run_position_backtest("T", df, few, strategy="few")
        r_many = run_position_backtest("T", df, many, strategy="many")

        assert r_many.total_turnover > r_few.total_turnover
        assert r_many.total_cost > r_few.total_cost

        unit_few = r_few.total_cost / r_few.total_turnover
        unit_many = r_many.total_cost / r_many.total_turnover
        # 单位换手成本 = 费率 × 当时净值；净值波动 ±50% 内认为成比例
        assert unit_many == pytest.approx(unit_few, rel=0.5), (
            f"单位换手成本差异过大: few={unit_few:.2f} many={unit_many:.2f}"
        )

    def test_reported_cost_matches_actual_deduction(self, prices):
        """
        上报成本必须等于净值里真实扣掉的那一份（性质 5）。

        用逐日明细**独立重建**净值与成本，再与上报字段对齐。
        任何"报告里写了、净值里没扣"的做法都会在这里失败。
        """
        df = prices.iloc[:250].reset_index(drop=True)
        t = self._targets(len(df))
        r = run_position_backtest("T", df, t, strategy="audit",
                                  slippage_pct=0.1, commission_pct=0.03,
                                  stamp_duty_pct=0.05)

        eq = r.initial_capital
        rebuilt_comm = rebuilt_slip = rebuilt_stamp = 0.0
        rebuilt_turnover = 0.0
        for d in r.daily_detail:
            # 成本以**货币金额**记账（修复后不再是收益率的加减项）
            assert d["cost_cash"] == pytest.approx(
                d["cost_slippage"] + d["cost_commission"] + d["cost_stamp"], rel=1e-12
            )
            # 费率 × 实际成交额
            assert d["cost_slippage"] == pytest.approx(
                d["trade_notional"] * 0.001, rel=1e-12
            )
            assert d["cost_commission"] == pytest.approx(
                d["trade_notional"] * 0.0003, rel=1e-12
            )
            assert d["cost_stamp"] == pytest.approx(
                d["sell_notional"] * 0.0005, rel=1e-12
            )
            # 逐日独立重建净值：隔夜 → 扣费 → 盘中
            stock_prev = eq * d["w_prev"]
            cash_prev = eq - stock_prev
            e_open = stock_prev * (1.0 + d["r_open"]) + cash_prev
            assert e_open == pytest.approx(d["equity_open"], rel=1e-9)
            e_after_cost = e_open - d["cost_cash"]
            e_close = e_after_cost + e_after_cost * d["w_target"] * d["r_close"]
            assert e_close == pytest.approx(d["equity_after"], rel=1e-9)

            rebuilt_comm += d["cost_commission"]
            rebuilt_slip += d["cost_slippage"]
            rebuilt_stamp += d["cost_stamp"]
            rebuilt_turnover += d["turnover"]
            eq = e_close

        assert rebuilt_comm == pytest.approx(r.commission_cost, rel=1e-9)
        assert rebuilt_slip == pytest.approx(r.slippage_cost, rel=1e-9)
        assert rebuilt_stamp == pytest.approx(r.stamp_duty_cost, rel=1e-9)
        assert rebuilt_turnover == pytest.approx(r.total_turnover, rel=1e-12)
        assert eq / r.initial_capital == pytest.approx(r.equity_curve[-1], rel=1e-9)
        assert r.total_cost == pytest.approx(
            r.commission_cost + r.slippage_cost + r.stamp_duty_cost, rel=1e-12
        )

    def test_stamp_duty_only_on_sell_side(self, prices):
        """印花税只在卖出方向计（买入方向不应产生印花税）。"""
        df = prices.iloc[:40].reset_index(drop=True)
        only_buy = [0.0] * 5 + [1.0] * 35        # 只有一次买入，末尾不卖
        r = run_position_backtest("T", df, only_buy, strategy="buyonly",
                                  slippage_pct=0.0, commission_pct=0.0,
                                  stamp_duty_pct=0.05)
        assert r.stamp_duty_cost == pytest.approx(0.0, abs=1e-9)

        with_sell = [0.0] * 5 + [1.0] * 15 + [0.0] * 20
        r2 = run_position_backtest("T", df, with_sell, strategy="withsell",
                                   slippage_pct=0.0, commission_pct=0.0,
                                   stamp_duty_pct=0.05)
        assert r2.stamp_duty_cost > 0.0

    def test_baseline_and_multifactor_share_identical_cost_params(self, prices):
        """
        要求七：baseline 与多因子必须用同一套成本口径。

        用同一组费率跑两腿，断言"单位换手成本"一致 —— 若某一腿悄悄用了
        不同费率，这个比值会不同。
        """
        df = prices
        kw = dict(slippage_pct=0.1, commission_pct=0.03, stamp_duty_pct=0.0)
        ma = run_position_backtest("T", df, ma_baseline_positions(df),
                                   strategy="ma", **kw)
        sig = generate_signals("T", df, StrategyParams())
        mf = run_position_backtest("T", df, sig.positions_by_date(),
                                   strategy="mf", **kw)
        assert ma.total_turnover > 0 and mf.total_turnover > 0
        # 费率一致 → 每单位换手的佣金/滑点比例应完全一致
        assert (ma.commission_cost / ma.slippage_cost) == pytest.approx(
            mf.commission_cost / mf.slippage_cost, rel=1e-6
        )
        assert ma.n_days == mf.n_days
        assert ma.start_date == mf.start_date and ma.end_date == mf.end_date


# ═══════════════════════════════════════════════════════════════════
#   要求八：基准
# ═══════════════════════════════════════════════════════════════════

class TestCompoundingModel:
    """
    2026-08-10 修复回归：隔夜与盘中收益必须**复合**，换手必须按**漂移后**仓位计。

    修复前的错误实现::

        gross_ret = w_prev * r_open + w_new * r_close     # 加法
        turnover  = abs(w_new - w_prev)                   # 忽略隔夜漂移

    修复前满仓无成本时 100 → 开盘 110 → 收盘 121 给 0.20，正确值是 0.21，
    差的正是交乘项 r_open*r_close。本类的每个测试都是可手算的确定性数值。
    """

    @staticmethod
    def _mk(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
        """(date, open, close) → 价格表。high/low 由 open/close 包出来。"""
        return pd.DataFrame([
            {"date": d, "open": o, "high": max(o, c), "low": min(o, c),
             "close": c, "volume": 1e6}
            for d, o, c in rows
        ])

    def test_full_position_no_cost_compounds_to_21_percent(self):
        """
        满仓、无成本：前收 100 → 开盘 110 → 收盘 121，日收益必须是 21%。

        这是本轮修复的核心判据。加法模型给 0.20，复合模型给 0.21。
        前两天用于把仓位建到满仓，使第 3 天的 w_prev 确实等于 1。
        """
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0),
                       ("2020-01-03", 110.0, 121.0)])
        res = run_position_backtest("X", df, [1.0, 1.0, 1.0], strategy="p",
                                    slippage_pct=0.0, commission_pct=0.0)
        d = res.daily_detail[1]
        assert d["w_prev"] == pytest.approx(1.0, abs=1e-12)
        assert d["r_open"] == pytest.approx(0.10, abs=1e-12)
        assert d["r_close"] == pytest.approx(0.10, abs=1e-12)
        assert d["net_ret"] == pytest.approx(0.21, abs=1e-12), (
            f"日收益 {d['net_ret']} != 0.21（加法模型会给 0.20）"
        )
        # 反证：加法模型的值必须被排除
        assert d["net_ret"] != pytest.approx(0.20, abs=1e-6)

    def test_full_position_daily_return_equals_close_to_close(self):
        """
        不变量：满仓、无成本、目标仓位不变时，
        日收益 ≡ ``close_t / close_{t-1} - 1``（要求 2）。

        用 30 天确定性随机序列（open 与 close 不同，隔夜与盘中都非零）逐日核对。
        """
        rnd = random.Random(7)
        rows, c = [], 100.0
        for i in range(30):
            c *= (1 + rnd.gauss(0, 0.02))
            o = c * (1 + rnd.gauss(0, 0.005))
            rows.append((f"2020-02-{i + 1:02d}", o, c))
        df = self._mk(rows)
        res = run_position_backtest("X", df, [1.0] * 30, strategy="p",
                                    slippage_pct=0.0, commission_pct=0.0)
        cl = df["close"].tolist()
        for d in res.daily_detail[1:]:      # 第 1 日在建仓，尚未满仓
            i = int(d["i"])
            expect = cl[i] / cl[i - 1] - 1.0
            assert d["net_ret"] == pytest.approx(expect, rel=1e-12, abs=1e-15), (
                f"第 {i} 日 {d['net_ret']} != close-to-close {expect}"
            )

    def test_half_position_no_cost_matches_hand_computation(self):
        """
        半仓、无成本，逐位手算：

        E=1，股票 0.5、现金 0.5
        隔盘 +10%（100→110）：股票 0.55，E_open = 1.05
        漂移仓位 w_drift = 0.55/1.05 = 0.523809523809...
        调仓回 0.5：股票 0.525、现金 0.525
        盘中 +10%（110→121）：股票 0.5775，E = 1.1025 → 日收益 10.25%
        """
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0),
                       ("2020-01-03", 110.0, 121.0)])
        res = run_position_backtest("X", df, [0.5, 0.5, 0.5], strategy="p",
                                    slippage_pct=0.0, commission_pct=0.0)
        d = res.daily_detail[1]
        assert d["w_prev"] == pytest.approx(0.5, abs=1e-12)
        assert d["w_drift"] == pytest.approx(0.55 / 1.05, abs=1e-12)
        assert d["equity_open"] == pytest.approx(1_050_000.0, rel=1e-12)
        assert d["net_ret"] == pytest.approx(0.1025, abs=1e-12)
        assert d["equity_after"] == pytest.approx(1_102_500.0, rel=1e-12)

    def test_buy_and_hold_equity_equals_compounded_close_minus_cost(self):
        """
        满仓买入持有多日：净值必须等于 close-to-close 复合收益扣除一次建仓成本。

        基准第 1 日开盘建仓（付一次滑点+佣金），之后不再调仓（无漂移调仓，
        因为目标恒为 1，而满仓状态下漂移后仍是 1）。因此：

            E_n / E_0 = close_n / (open_1 * (1 + slip + comm))
        """
        rnd = random.Random(11)
        rows, c = [], 50.0
        for i in range(25):
            c *= (1 + rnd.gauss(0, 0.015))
            o = c * (1 + rnd.gauss(0, 0.004))
            rows.append((f"2020-03-{i + 1:02d}", o, c))
        df = self._mk(rows)

        slip, comm = 0.1, 0.03
        bh = buy_and_hold_benchmark("BH", df, slippage_pct=slip,
                                    commission_pct=comm)
        open1 = float(df["open"].iloc[1])   # 第 1 日（索引 1）开盘建仓
        close_n = float(df["close"].iloc[-1])
        fee_rate = slip / 100 + comm / 100
        expect = close_n / (open1 * (1 + fee_rate))
        assert bh.equity_curve[-1] == pytest.approx(expect, rel=1e-9), (
            f"买入持有净值 {bh.equity_curve[-1]} != 手算 {expect}"
        )
        # 只应有一次建仓换手
        assert bh.total_turnover == pytest.approx(1 / (1 + fee_rate), rel=1e-9)

    def test_overnight_gain_drifts_weight_and_creates_real_turnover(self):
        """
        隔夜上涨后仓位漂移：即使目标仓位不变，也必须产生真实换手（要求 3）。

        半仓 + 隔夜 +10% → w_drift = 0.5238095…，回到 0.5 需卖出
        |0.5 - 0.5238095| = 0.0238095 权重。旧实现按 |w_new - w_prev| = 0，
        会完全漏掉这笔真实成交及其成本。
        """
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0),
                       ("2020-01-03", 110.0, 110.0)])
        res = run_position_backtest("X", df, [0.5, 0.5, 0.5], strategy="p",
                                    slippage_pct=0.0, commission_pct=0.0)
        d = res.daily_detail[1]
        expect_turnover = abs(0.5 - 0.55 / 1.05)
        assert d["w_drift"] > d["w_target"], "隔夜上涨后应超配"
        assert d["turnover"] == pytest.approx(expect_turnover, abs=1e-12)
        assert d["turnover"] > 0.0, "漏掉了隔夜漂移造成的真实调仓"
        # 卖出方向：超配 → 卖出
        assert d["sell_notional"] == pytest.approx(
            expect_turnover * d["equity_open"], rel=1e-12
        )

    def test_overnight_loss_drifts_weight_downward(self):
        """
        隔夜下跌后仓位漂移向下，需**买入**补回目标：

        半仓 + 隔夜 -10%（100→90）：股票 0.45、现金 0.5，E_open = 0.95
        w_drift = 0.45/0.95 = 0.473684210526…  < 0.5 → 买入 0.0263157894…
        买入方向不应产生印花税。
        """
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0),
                       ("2020-01-03", 90.0, 90.0)])
        res = run_position_backtest("X", df, [0.5, 0.5, 0.5], strategy="p",
                                    slippage_pct=0.0, commission_pct=0.0,
                                    stamp_duty_pct=0.05)
        d = res.daily_detail[1]
        assert d["w_drift"] == pytest.approx(0.45 / 0.95, abs=1e-12)
        assert d["w_drift"] < d["w_target"], "隔夜下跌后应低配"
        assert d["turnover"] == pytest.approx(abs(0.5 - 0.45 / 0.95), abs=1e-12)
        assert d["sell_notional"] == pytest.approx(0.0, abs=1e-12)
        assert d["cost_stamp"] == pytest.approx(0.0, abs=1e-12), (
            "买入方向不应产生印花税"
        )

    def test_intraday_move_drifts_closing_weight_into_next_day(self):
        """
        盘中涨跌同样造成漂移：**收盘时**的实际仓位不等于当日目标仓位，
        且必须原样带入次日，作为次日隔夜收益的承担基数。

        手算（无成本，目标恒为 0.5）：
          第 2 日 从空仓建到 0.5，收盘 100 → 110（+10%）
            股票 0.5 × 1.1 = 0.55，现金 0.5 → E = 1.05
            收盘实际仓位 w_close = 0.55 / 1.05 = 0.5238095238…  ≠ 目标 0.5
          第 3 日 开盘、收盘均为 110（零收益）
            带入的 w_prev 必须是 0.5238095238…，于是 w_drift 同值，
            回到 0.5 需成交 |0.5 - 0.5238095238| = 0.0238095238…

        若把收盘仓位直接记为目标仓位（w_prev = w_target = 0.5），
        次日 w_drift 会变成 0.5、换手变成 0，这笔真实成交被凭空抹掉。
        """
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 110.0),
                       ("2020-01-03", 110.0, 110.0)])
        res = run_position_backtest("X", df, [0.5, 0.5, 0.5], strategy="p",
                                    slippage_pct=0.0, commission_pct=0.0)
        w_close = 0.55 / 1.05
        d1, d2 = res.daily_detail[0], res.daily_detail[1]

        # 第 2 日：盘中 +10% 使收盘仓位偏离目标
        assert d1["w_target"] == pytest.approx(0.5, abs=1e-12)
        assert d1["r_close"] == pytest.approx(0.10, abs=1e-12)

        # 第 3 日：带入的期初仓位必须是漂移后的真实仓位，不是目标仓位
        assert d2["w_prev"] == pytest.approx(w_close, abs=1e-12), (
            "收盘漂移被抹掉：期初仓位被记成了目标仓位"
        )
        assert d2["w_prev"] != pytest.approx(0.5, abs=1e-9)
        assert d2["r_open"] == pytest.approx(0.0, abs=1e-12)
        assert d2["w_drift"] == pytest.approx(w_close, abs=1e-12)
        assert d2["turnover"] == pytest.approx(w_close - 0.5, abs=1e-12)
        assert d2["turnover"] > 0.0, "漏掉了盘中漂移造成的真实调仓"

    def test_costs_are_charged_on_actual_trade_notional(self):
        """
        滑点与佣金必须按**开盘时的实际成交金额**扣除（要求 4）。

        构造：第 1 日从 0 建仓到满仓。费用会减少调仓后净值，因此实际成交额
        q 满足 q = E_open - q*c，即 q = E_open/(1+c)。
        """
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0)])
        res = run_position_backtest("X", df, [1.0, 1.0], strategy="p",
                                    initial_capital=1_000_000.0,
                                    slippage_pct=0.1, commission_pct=0.03)
        d = res.daily_detail[0]
        fee_rate = 0.001 + 0.0003
        expected_notional = 1_000_000.0 / (1.0 + fee_rate)
        expected_cost = expected_notional * fee_rate
        assert d["w_prev"] == pytest.approx(0.0, abs=1e-12)
        assert d["equity_open"] == pytest.approx(1_000_000.0, rel=1e-12)
        assert d["trade_side"] == "buy"
        assert d["trade_notional"] == pytest.approx(expected_notional, rel=1e-12)
        assert d["stock_after"] - d["stock_at_open"] == pytest.approx(
            expected_notional, rel=1e-12
        )
        assert d["cost_slippage"] == pytest.approx(expected_notional * 0.001, rel=1e-12)
        assert d["cost_commission"] == pytest.approx(expected_notional * 0.0003, rel=1e-12)
        assert d["cost_cash"] == pytest.approx(expected_cost, rel=1e-12)
        assert d["w_after_trade"] == pytest.approx(1.0, abs=1e-12)
        assert d["cash_after"] == pytest.approx(0.0, abs=1e-8)
        # 价格不动 → 净值恰好少掉成本
        assert res.equity_curve[-1] == pytest.approx(
            1 - expected_cost / 1_000_000, rel=1e-12
        )

    def test_partial_buy_uses_implicit_cost_solution(self):
        """部分加仓的成交额必须同时满足股票变化和调仓后目标权重。"""
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0),
                       ("2020-01-03", 100.0, 100.0)])
        res = run_position_backtest(
            "X", df, [0.2, 0.6, 0.6], strategy="p",
            slippage_pct=0.1, commission_pct=0.03,
        )
        d = res.daily_detail[1]
        fee_rate = 0.0013
        expected = (
            0.6 * d["equity_open"] - d["stock_at_open"]
        ) / (1.0 + 0.6 * fee_rate)
        assert d["trade_side"] == "buy"
        assert d["trade_notional"] == pytest.approx(expected, rel=1e-12)
        assert d["stock_after"] == pytest.approx(
            d["stock_at_open"] + expected, rel=1e-12
        )
        assert d["w_after_trade"] == pytest.approx(0.6, abs=1e-12)

    def test_partial_sell_uses_implicit_cost_solution(self):
        """部分减仓必须使用含卖出印花税的隐式成交额方程。"""
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0),
                       ("2020-01-03", 100.0, 100.0)])
        res = run_position_backtest(
            "X", df, [0.8, 0.3, 0.3], strategy="p",
            slippage_pct=0.1, commission_pct=0.03, stamp_duty_pct=0.05,
        )
        d = res.daily_detail[1]
        fee_rate = 0.001 + 0.0003 + 0.0005
        expected = (
            d["stock_at_open"] - 0.3 * d["equity_open"]
        ) / (1.0 - 0.3 * fee_rate)
        assert d["trade_side"] == "sell"
        assert d["trade_notional"] == pytest.approx(expected, rel=1e-12)
        assert d["sell_notional"] == pytest.approx(expected, rel=1e-12)
        assert d["stock_after"] == pytest.approx(
            d["stock_at_open"] - expected, rel=1e-12
        )
        assert d["cost_stamp"] == pytest.approx(expected * 0.0005, rel=1e-12)
        assert d["w_after_trade"] == pytest.approx(0.3, abs=1e-12)

    def test_stamp_duty_charged_only_on_sell_notional(self):
        """
        印花税只按**卖出成交额**计，买入额不计。

        第 1 日 0 → 1 满仓（买入 1_000_000，无印花税）；
        第 2 日 1 → 0 清仓。清仓时 E_open 已含成本与涨跌，
        卖出额 = w_drift × E_open，印花税 = 该额 × 0.05%。
        """
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0),
                       ("2020-01-03", 100.0, 100.0)])
        res = run_position_backtest("X", df, [1.0, 0.0, 0.0], strategy="p",
                                    initial_capital=1_000_000.0,
                                    slippage_pct=0.0, commission_pct=0.0,
                                    stamp_duty_pct=0.05)
        buy_day, sell_day = res.daily_detail[0], res.daily_detail[1]
        assert buy_day["sell_notional"] == pytest.approx(0.0, abs=1e-12)
        assert buy_day["cost_stamp"] == pytest.approx(0.0, abs=1e-12)

        assert sell_day["w_drift"] == pytest.approx(1.0, abs=1e-12)
        assert sell_day["w_target"] == pytest.approx(0.0, abs=1e-12)
        assert sell_day["sell_notional"] == pytest.approx(
            sell_day["equity_open"], rel=1e-12
        )
        assert sell_day["cost_stamp"] == pytest.approx(
            sell_day["equity_open"] * 0.0005, rel=1e-12
        )
        assert res.stamp_duty_cost == pytest.approx(sell_day["cost_stamp"], rel=1e-12)

    def test_no_implicit_leverage_when_fully_invested_with_costs(self):
        """
        满仓且有成本时不得出现负现金（隐性杠杆）。

        成本先从净值中以现金扣除，再按目标权重分配，因此
        股票市值 = (E_open - 成本) × w_target ≤ E_open - 成本。
        """
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 105.0),
                       ("2020-01-03", 105.0, 110.0)])
        res = run_position_backtest("X", df, [1.0, 1.0, 1.0], strategy="p",
                                    slippage_pct=0.5, commission_pct=0.5,
                                    stamp_duty_pct=0.1)
        for d in res.daily_detail:
            assert d["stock_after"] <= d["equity_after_cost"] + 1e-9
            assert d["cash_after"] >= -1e-9, "出现负现金（隐性杠杆）"
            assert d["w_after_trade"] == pytest.approx(d["w_target"], abs=1e-12)
            assert d["equity_after"] > 0.0

    def test_daily_account_is_conserved_and_closing_weight_is_recorded(self):
        """逐日股票、现金、费用、净值与实际收盘仓位必须相互一致。"""
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 110.0),
                       ("2020-01-03", 105.0, 95.0),
                       ("2020-01-04", 98.0, 103.0)])
        res = run_position_backtest(
            "X", df, [0.7, 0.2, 0.9, 0.9], strategy="p",
            slippage_pct=0.1, commission_pct=0.03, stamp_duty_pct=0.05,
        )
        closing_weights = []
        for d in res.daily_detail:
            assert d["stock_after"] + d["cash_after"] == pytest.approx(
                d["equity_after_cost"], rel=1e-12, abs=1e-7
            )
            stock_change = abs(d["stock_after"] - d["stock_at_open"])
            assert d["trade_notional"] == pytest.approx(stock_change, rel=1e-12)
            expected_close_stock = d["stock_after"] * (1.0 + d["r_close"])
            expected_w_close = expected_close_stock / d["equity_after"]
            assert d["w_close"] == pytest.approx(expected_w_close, abs=1e-12)
            closing_weights.append(d["w_close"])
        assert res.avg_position == pytest.approx(
            sum(closing_weights) / len(closing_weights), rel=1e-12
        )

    @pytest.mark.parametrize("field", ["slippage_pct", "commission_pct", "stamp_duty_pct"])
    def test_negative_cost_rate_is_rejected(self, field):
        df = self._mk([("2020-01-01", 100.0, 100.0),
                       ("2020-01-02", 100.0, 100.0)])
        with pytest.raises(ValueError, match="不得为负"):
            run_position_backtest("X", df, [1.0, 1.0], **{field: -0.01})

    def test_gross_minus_net_equals_cost_drag(self):
        """
        无成本反事实收益与实际收益之差，必须恰好等于成本造成的拖累。

        这条断言把"报告里的成本"与"净值里扣掉的成本"死死绑在一起：
        E_gross - E_net = 成本 × (1 + w_target × r_close)
        （成本在开盘扣除，之后不再参与盘中增长）
        """
        rnd = random.Random(3)
        rows, c = [], 80.0
        for i in range(20):
            c *= (1 + rnd.gauss(0, 0.02))
            o = c * (1 + rnd.gauss(0, 0.006))
            rows.append((f"2020-04-{i + 1:02d}", o, c))
        df = self._mk(rows)
        t = [1.0 if (i // 3) % 2 == 0 else 0.2 for i in range(len(df))]
        res = run_position_backtest("X", df, t, strategy="p",
                                    slippage_pct=0.1, commission_pct=0.03,
                                    stamp_duty_pct=0.05)
        for d in res.daily_detail:
            e_gross = d["equity_before"] * (1.0 + d["gross_ret"])
            e_net = d["equity_after"]
            drag = d["cost_cash"] * (1.0 + d["w_target"] * d["r_close"])
            assert e_gross - e_net == pytest.approx(drag, rel=1e-9, abs=1e-6), (
                f"第 {int(d['i'])} 日成本拖累不自洽"
            )


class TestBenchmark:
    """防的是：把策略收益和另一个日期区间的基准比。"""

    def test_benchmark_uses_same_date_range(self, prices):
        """基准与两条策略腿必须共享起止日期、交易日数与价格序列。"""
        df = prices
        kw = dict(slippage_pct=0.1, commission_pct=0.03)
        bh = buy_and_hold_benchmark("T", df, **kw)
        ma = run_position_backtest("T", df, ma_baseline_positions(df),
                                   strategy="ma", **kw)
        sig = generate_signals("T", df, StrategyParams())
        mf = run_position_backtest("T", df, sig.positions_by_date(),
                                   strategy="mf", **kw)

        assert bh.start_date == ma.start_date == mf.start_date
        assert bh.end_date == ma.end_date == mf.end_date
        assert bh.n_days == ma.n_days == mf.n_days
        assert len(bh.calendar_returns) == len(ma.calendar_returns) \
            == len(mf.calendar_returns)
        assert bh.start_date == str(df["date"].iloc[0])
        assert bh.end_date == str(df["date"].iloc[-1])

    def test_benchmark_is_fully_invested(self, prices):
        """买入持有应全程满仓、只有一次建仓换手。"""
        bh = buy_and_hold_benchmark("T", prices)
        assert bh.time_in_market_pct == pytest.approx(100.0)
        assert bh.total_turnover == pytest.approx(1 / 1.0013, rel=1e-12)
        assert bh.avg_position == pytest.approx(1.0, rel=1e-12)

    def test_benchmark_on_different_range_is_detectably_different(self, prices):
        """
        反证：换区间的基准与同区间基准结果不同 —— 说明区间确实进入了计算。

        若这两者相同，说明区间参数根本没被使用，同区间断言就是空的。
        """
        full = buy_and_hold_benchmark("T", prices)
        half = buy_and_hold_benchmark("T", prices.iloc[:400].reset_index(drop=True))
        assert full.end_date != half.end_date
        assert full.total_return_pct != pytest.approx(half.total_return_pct, rel=1e-6)


# ═══════════════════════════════════════════════════════════════════
#   换手上报
# ═══════════════════════════════════════════════════════════════════

class TestTurnover:
    def test_turnover_is_reported(self, prices):
        """换手必须出现在结果与序列化输出中，且与独立重算一致。"""
        df = prices
        sig = generate_signals("T", df, StrategyParams())
        r = run_position_backtest("T", df, sig.positions_by_date(), strategy="mf")

        assert r.total_turnover > 0.0
        assert r.annualized_turnover > 0.0
        d = r.to_dict()
        assert "total_turnover" in d and "annualized_turnover" in d
        assert d["total_turnover"] == pytest.approx(round(r.total_turnover, 4))

        # 独立重算：Σ(实际成交额 / 当日开盘净值)。费用会减少净值，
        # 所以这不再等于简单的目标仓位差之和。
        manual = sum(
            dd["trade_notional"] / dd["equity_open"] for dd in r.daily_detail
        )
        assert manual == pytest.approx(r.total_turnover, rel=1e-12)

        # 反证：按旧口径 Σ|Δw_target| 算出的换手与上报值不同，
        # 说明漂移确实被计入（否则这条断言会失败）。
        wt = [dd["w_target"] for dd in r.daily_detail]
        old_caliber = abs(wt[0]) + sum(abs(b - a) for a, b in zip(wt, wt[1:]))
        assert old_caliber != pytest.approx(r.total_turnover, rel=1e-6), (
            "换手与旧口径相同，说明隔夜漂移未被计入"
        )

    def test_annualized_turnover_scales_with_period(self, prices):
        """年化换手 = 总换手 / 年数，口径必须可核验。"""
        df = prices.iloc[:252].reset_index(drop=True)
        t = [1.0 if (i // 10) % 2 == 0 else 0.0 for i in range(len(df))]
        r = run_position_backtest("T", df, t, strategy="x")
        years = r.n_days / 252
        assert r.annualized_turnover == pytest.approx(r.total_turnover / years, rel=1e-9)


# ═══════════════════════════════════════════════════════════════════
#   要求三.4：估值不可用
# ═══════════════════════════════════════════════════════════════════

class TestValuation:
    """防的是：用当期估值回填历史、随机造估值、把不可用按 0 参与打分。"""

    def test_missing_valuation_is_not_fabricated(self, prices):
        """无历史估值序列 → 标记 unavailable + 给出原因，且值全为 NaN。"""
        fs = valuation_factors(prices, history=None)
        assert len(fs) == 3
        for f in fs:
            assert f.available is False, f"{f.name} 在无数据时被标记为可用"
            assert f.unavailable_reason, f"{f.name} 缺少不可用原因"
            assert "未来数据泄漏" in f.unavailable_reason
            assert f.values.notna().sum() == 0, f"{f.name} 在无数据时产生了数值"
            # 关键：不能是 0 —— 0 在分位口径下会被当成"最差但真实"的观测
            assert not (f.values == 0.0).any(), f"{f.name} 用 0 值填充了缺失估值"

    def test_unavailable_valuation_excluded_from_scoring(self, prices):
        """不可用因子必须被剔除，不进入 composite score 的分母。"""
        sig = generate_signals("T", prices, StrategyParams())
        assert set(sig.unavailable_factors) >= {"pe_inv", "pb_inv", "roe"}
        for name in ("pe_inv", "pb_inv", "roe"):
            assert name not in sig.used_factors
        # 其余三族仍然生效
        used_families = {
            m["family"] for m in sig.factor_meta
            if m["name"] in sig.used_factors
        }
        assert used_families == {"momentum", "volatility", "volume"}
        assert sig.composite_score.notna().sum() > 0, "剔除估值后打分不应全空"

    def test_valuation_history_uses_backward_asof_only(self, prices):
        """
        提供历史序列时，日期 t 只能匹配 t 或更早的披露值。

        构造：披露日在样本中段。披露日之前必须为 NaN，之后才有值。
        若实现用了 forward/nearest，披露日之前就会出现数值 —— 那正是回填泄漏。
        """
        df = prices.iloc[:100].reset_index(drop=True)
        disclose = str(df["date"].iloc[50])
        hist = pd.DataFrame({"date": [disclose], "pe": [10.0], "pb": [1.0],
                             "roe": [15.0]})
        fs = {f.name: f for f in valuation_factors(df, history=hist)}
        pe = fs["pe_inv"]
        assert pe.available
        assert pe.values.iloc[:50].isna().all(), "披露日之前出现了估值（未来数据回填）"
        assert pe.values.iloc[50] == pytest.approx(0.1)
        assert pe.values.iloc[99] == pytest.approx(0.1)

    def test_negative_pe_is_not_treated_as_cheap(self, prices):
        """负 PE（亏损）不得因取倒数而变成高分。"""
        df = prices.iloc[:20].reset_index(drop=True)
        hist = pd.DataFrame({"date": [str(df["date"].iloc[0])], "pe": [-8.0],
                             "pb": [-1.0], "roe": [-3.0]})
        fs = {f.name: f for f in valuation_factors(df, history=hist)}
        assert fs["pe_inv"].values.notna().sum() == 0, "负 PE 未被剔除"
        assert fs["pb_inv"].values.notna().sum() == 0, "负 PB 未被剔除"
        # ROE 允许为负（它本身就是有方向的比率，不取倒数）
        assert fs["roe"].values.iloc[0] == pytest.approx(-3.0)

    def test_all_four_families_are_present(self, prices):
        """四大因子族必须齐备（可用性另论），不能少建一族。"""
        fset = build_factor_set("T", prices)
        assert set(fset.families_present()) == set(ALL_FAMILIES)
        assert len(fset.available_names()) >= 8

    def test_missing_or_zero_volume_is_handled(self):
        """
        成交量缺失/为零必须正确处理：均量为 0 时比值为 NaN，不得回退成 1.0。
        """
        n = 40
        df = pd.DataFrame({
            "date": pd.date_range("2020-01-02", periods=n, freq="B").strftime("%Y-%m-%d"),
            "close": [100.0 + i for i in range(n)],
            "volume": [0.0] * n,          # 全零成交量（长期停牌）
        })
        fs = {f.name: f for f in volume_factors(df)}
        ratio = fs["vol_ratio_20"].values
        assert ratio.notna().sum() == 0, "零均量下比值不应产生数值"
        assert not (ratio == 1.0).any(), "零均量被回退成 1.0（把缺失伪装成正常）"

        # 整列缺失 → 整族 unavailable
        df2 = df.drop(columns=["volume"])
        fs2 = volume_factors(df2)
        assert all(f.available is False for f in fs2)
        assert all("volume" in (f.unavailable_reason or "") for f in fs2)

    def test_cross_sectional_rank_ignores_nan(self):
        """横截面排名：NaN 不参与、不被填成中位数。"""
        s = pd.Series({"a": 1.0, "b": float("nan"), "c": 3.0, "d": 2.0})
        r = cross_sectional_rank(s)
        assert pd.isna(r["b"])
        assert r["a"] == pytest.approx(0.0)
        assert r["c"] == pytest.approx(1.0)
        assert r["d"] == pytest.approx(0.5)


# ═══════════════════════════════════════════════════════════════════
#   要求一.11：baseline 逐位不变
# ═══════════════════════════════════════════════════════════════════

class TestBaselineUnchanged:
    """防的是：为了让多因子好看而悄悄改动 MA baseline 或原引擎。"""

    def test_baseline_strategy_result_is_unchanged(self):
        """
        原 ``_ma_crossover_signals`` + 原 ``run_backtest`` 在确定性数据上的
        输出被逐位钉死。

        期望值不是手算的，而是本轮**未修改**这两处代码时实测得到的。
        任何对 MA 信号逻辑、执行时序、成本或 Sharpe 公式的改动都会让它失败。
        """
        from validate_deliverables import _ma_crossover_signals

        df = make_prices(400, seed=99)
        sigs = _ma_crossover_signals(df)
        res = run_backtest("BASE", df, sigs)

        # 结构性不变量
        assert len(sigs) == 22
        assert sigs[0] == ("2019-02-06", "buy")
        assert sigs[-1] == ("2020-07-10", "sell")
        assert res.total_trades == 11
        assert res.winning_trades == 6
        assert res.losing_trades == 5

        # 数值不变量（逐位钉死；容差仅为浮点重排序留余量）
        assert res.total_return_pct == pytest.approx(4.610118, abs=1e-4)
        assert res.sharpe_ratio == pytest.approx(0.247950, abs=1e-4)
        assert res.sharpe_calendar == pytest.approx(0.131843, abs=1e-4)
        assert res.max_drawdown_pct == pytest.approx(12.315545, abs=1e-4)
        assert res.time_in_market_pct == pytest.approx(58.395990, abs=1e-4)
        assert res.win_rate_pct == pytest.approx(54.545455, abs=1e-4)
        assert res.avg_slippage_pct == pytest.approx(0.1)

    def test_original_ma_signal_function_is_untouched(self):
        """
        原 MA 信号函数的源码指纹未变。

        比对的是"逻辑骨架"而非全文，避免注释/空白改动造成假失败，
        但任何窗口长度、比较方向、信号词的改动都会被抓到。
        """
        from validate_deliverables import _ma_crossover_signals

        src = inspect.getsource(_ma_crossover_signals)
        for token in ('rolling(5)', 'rolling(20)', '"ma5"', '"ma20"',
                      'ma5"] > ', '"buy"', '"sell"', 'prev_above'):
            assert token in src, f"原 MA 信号函数缺少 {token!r}，疑被改动"

    def test_position_adapter_matches_original_ma_semantics(self):
        """
        ``ma_baseline_positions`` 与原信号函数语义一致。

        逐日核对：原函数给出 buy 的次日之后仓位应为 1，sell 之后为 0。
        这保证适配器没有偷偷变成另一个（更好的）策略。
        """
        from validate_deliverables import _ma_crossover_signals

        df = make_prices(400, seed=99)
        sigs = dict(_ma_crossover_signals(df))
        pos = ma_baseline_positions(df)

        expected = 0.0
        for d in df["date"].tolist():
            d = str(d)
            if d in sigs:
                expected = 1.0 if sigs[d] == "buy" else 0.0
            assert pos.loc[d] == expected, f"{d}: 适配器仓位 {pos.loc[d]} != {expected}"

    def test_engine_source_untouched(self):
        """
        原引擎关键常量与执行时序未被改动。
        """
        src = (ROOT / "src" / "agent_platform" / "finance" / "backtesting.py"
               ).read_text(encoding="utf-8")
        assert "_RISK_FREE_RATE = 0.02" in src
        assert "_TRADING_DAYS_PER_YEAR = 252" in src
        assert "entry_price = next_open * (1 + slippage_cost + commission_cost)" in src
        assert "exit_price = next_open * (1 - slippage_cost - commission_cost)" in src
        assert "slippage_pct: float = 0.1" in src
        assert "commission_pct: float = 0.03" in src


# ═══════════════════════════════════════════════════════════════════
#   要求十一：报告
# ═══════════════════════════════════════════════════════════════════

class TestReport:
    """防的是：报告只报多因子、或未达标却写 PASS。"""

    @pytest.fixture(scope="class")
    def cli_output(self, tmp_path_factory):
        """真实调用 CLI（离线），返回 (returncode, stdout, json, md)。"""
        out = tmp_path_factory.mktemp("cmp")
        # 输出到项目内的临时目录：脚本会拒绝写项目外路径。
        # 刻意**不放在 docs/ 下**，避免测试产物污染正式报告目录；
        # 用完在 teardown 里删掉，测试不留残留。
        proj_tmp = ROOT / ".pytest_artifacts" / "compare_cli"
        proj_tmp.mkdir(parents=True, exist_ok=True)
        jp = proj_tmp / "cmp.json"
        mp = proj_tmp / "cmp.md"
        env = {
            "LLM_PROVIDER": "mock",
            "MARKET_DATA_PROVIDER": "sample",
            "DEEPSEEK_API_KEY": "",
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "PYTHONIOENCODING": "utf-8",
            "PATH": __import__("os").environ.get("PATH", ""),
            "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        }
        try:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "Scripts" / "compare_strategies.py"),
                 "--offline", "--limit", "2",
                 "--json-out", str(jp), "--md-out", str(mp)],
                capture_output=True, text=True, encoding="utf-8", env=env,
                cwd=str(ROOT), timeout=900,
            )
            assert proc.returncode == 0, f"CLI 失败:\n{proc.stdout}\n{proc.stderr}"
            data = json.loads(jp.read_text(encoding="utf-8"))
            md = mp.read_text(encoding="utf-8")
            _ = out
            yield proc, data, md
        finally:
            # 只删自己建的临时目录，绝不触碰 docs/ 下的正式报告
            shutil.rmtree(ROOT / ".pytest_artifacts", ignore_errors=True)

    def test_report_contains_baseline_and_multifactor(self, cli_output):
        """报告必须同时含 baseline 与多因子，并且并列可比。"""
        _proc, data, md = cli_output

        assert data["full_sample"]["per_symbol"], "报告缺少逐标的结果"
        for r in data["full_sample"]["per_symbol"]:
            assert "ma_baseline" in r, "报告缺少 MA baseline"
            assert "multifactor" in r, "报告缺少多因子"
            assert "benchmark" in r, "报告缺少基准"
            # 要求七的必报字段
            for key in ("strategy", "start_date", "end_date", "data_source",
                        "total_return_pct", "annualized_return_pct",
                        "sharpe_calendar", "max_drawdown_pct",
                        "annualized_volatility_pct", "win_rate_pct",
                        "total_trades", "total_turnover", "commission_cost",
                        "slippage_cost", "benchmark_return_pct",
                        "excess_return_vs_benchmark_pct", "meets_sharpe_target"):
                assert key in r["ma_baseline"], f"MA baseline 缺字段 {key}"
                assert key in r["multifactor"], f"多因子缺字段 {key}"
            # 公平性：同区间
            assert r["ma_baseline"]["start_date"] == r["multifactor"]["start_date"]
            assert r["ma_baseline"]["end_date"] == r["multifactor"]["end_date"]

        assert "MA基线" in md and "多因子" in md and "买入持有" in md

        agg = data["full_sample"]["aggregate"]
        for key in ("ma_baseline_sharpe", "multifactor_sharpe", "benchmark_sharpe"):
            a = agg[key]
            # 要求七末段：均值/中位数/最差/达标数/未达标数全部必报
            for f in ("mean", "median", "worst", "n_meeting_threshold",
                      "n_below_threshold", "count"):
                assert f in a, f"{key} 缺聚合字段 {f}"

    def test_report_declares_below_threshold_honestly(self, cli_output):
        """
        未达标必须输出 BELOW 0.5；达标才允许 PASS。判定与数值必须自洽。
        """
        _proc, data, md = cli_output
        v = data["verdict"]
        assert v["result"] in ("PASS", "BELOW 0.5")
        assert v["threshold"] == SHARPE_TARGET
        if v["value"] is None:
            assert v["result"] == "BELOW 0.5"
        elif v["value"] >= SHARPE_TARGET:
            assert v["result"] == "PASS"
        else:
            assert v["result"] == "BELOW 0.5", "未达标却未标 BELOW 0.5"
            assert "BELOW 0.5" in md
        assert v["result"] in _proc.stdout

    def test_report_contains_walk_forward_folds(self, cli_output):
        """报告必须含逐折 walk-forward 结果与六个边界。"""
        _proc, data, _md = cli_output
        wf = data["walk_forward"]
        assert wf["per_symbol"]
        found = False
        for w in wf["per_symbol"]:
            for f in w.get("folds", []):
                for key in ("train_start", "train_end", "validation_start",
                            "validation_end", "test_start", "test_end"):
                    assert key in f
                found = True
        assert found, "报告里没有任何折"
        assert "out_of_sample_test_sharpe" in wf

    def test_report_contains_limitations_and_disclaimer(self, cli_output):
        """报告必须写明数据局限与免责声明（不得只报好消息）。"""
        _proc, data, md = cli_output
        text = " ".join(data["limitations"])
        assert "估值因子" in text and "unavailable" in text
        assert "存活者偏差" in text
        assert "不构成任何投资建议" in text
        assert "数据局限与免责声明" in md

    def test_report_records_failures_and_unavailable(self, cli_output):
        """失败与不可用必须如实出现在报告结构里，不能被静默丢弃。"""
        _proc, data, _md = cli_output
        assert "failures" in data and "n_failed" in data
        assert data["n_failed"] == len(data["failures"])
        assert "n_unavailable" in data["walk_forward"]


# ═══════════════════════════════════════════════════════════════════
#   要求二 / 一.8：公式与阈值未改
# ═══════════════════════════════════════════════════════════════════

class TestFormulaUnchanged:
    """
    防的是：为了达标而偷偷改 Sharpe 公式、年化方式或 0.5 阈值。
    """

    def test_sharpe_formula_and_threshold_are_unchanged(self):
        """
        三重校验：
          1. 常量未变（无风险利率 2%、年化 252）；
          2. 公式源码骨架未变；
          3. 已知输入的输出逐位不变（最强的一条）。
        """
        assert bt_mod._RISK_FREE_RATE == 0.02
        assert bt_mod._TRADING_DAYS_PER_YEAR == 252

        src = inspect.getsource(bt_mod._compute_sharpe)
        assert "(1 + risk_free_rate) ** (1 / _TRADING_DAYS_PER_YEAR) - 1" in src
        assert "(mean_r - daily_rf) / std_r * math.sqrt(_TRADING_DAYS_PER_YEAR)" in src
        assert "len(daily_returns) - 1" in src, "方差自由度被改动"

        # 已知输入 → 已知输出（独立重算一遍公式核对）
        rets = [0.01, -0.005, 0.002, 0.0, 0.008, -0.003, 0.004]
        got = bt_mod._compute_sharpe(rets)
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        daily_rf = (1 + 0.02) ** (1 / 252) - 1
        expect = (mean_r - daily_rf) / std * math.sqrt(252)
        assert got == pytest.approx(expect, rel=1e-12)
        assert got == pytest.approx(6.371634, abs=1e-4)

        # 阈值：0.5 的三个定义点均未变
        assert pb_mod.SHARPE_TARGET == 0.5
        sig = inspect.signature(ss_mod.compute_sharpe_stats)
        assert sig.parameters["threshold"].default == 0.5
        bt_src = (ROOT / "src" / "agent_platform" / "finance" / "backtesting.py"
                  ).read_text(encoding="utf-8")
        assert "self.sharpe_calendar >= 0.5" in bt_src

        ui = (ROOT / "src" / "agent_platform" / "ui" / "streamlit_app.py"
              ).read_text(encoding="utf-8")
        assert "threshold: float = 0.5" in ui

    def test_new_engine_reuses_original_sharpe_function(self):
        """
        新引擎必须复用原函数对象本身，而不是抄一份公式。

        ``is`` 断言让"另起一套公式"在物理上不可能通过测试。
        """
        assert pb_mod._compute_sharpe is bt_mod._compute_sharpe
        assert pb_mod._TRADING_DAYS_PER_YEAR is bt_mod._TRADING_DAYS_PER_YEAR
        assert pb_mod._RISK_FREE_RATE == bt_mod._RISK_FREE_RATE
        assert pb_mod._compute_max_drawdown is bt_mod._compute_max_drawdown

        src = inspect.getsource(pb_mod)
        assert "math.sqrt(_TRADING_DAYS_PER_YEAR)" in src
        # 不得出现自定义的无风险利率或年化天数
        assert "0.02" not in src.replace("_RISK_FREE_RATE", "")
        assert "365" not in src

    def test_engine_sharpe_on_hand_computable_series(self):
        """
        新引擎 Sharpe 的**确定性手算**校验。

        取代原先"新旧引擎夏普差值 < 0.5"的宽松断言 —— 那条断言过宽：
        0.5 的容差足以放过一个真实的建模错误（事实上旧的加法复合缺陷
        在该容差下就没有被抓住）。这里改为构造一条日收益完全可手算的序列，
        把 Sharpe 钉死到 1e-12。

        构造：满仓、无成本、目标仓位恒为 1 → 日收益 ≡ close_t/close_{t-1}-1。
        收盘价取整数，日收益是有限小数，可用 Fraction 精确重算。
        """
        # 首日重复 100.0：第 1 日用于把仓位从 0 建到满仓，当日收益为 0
        # （信号右移一天 + 开盘建仓，当日不承担隔夜涨跌）。
        closes = [100.0, 100.0, 110.0, 99.0, 108.9, 130.68, 117.612]
        df = pd.DataFrame({
            "date": [f"2020-01-{i + 1:02d}" for i in range(len(closes))],
            "open": closes,          # open==close：隔夜承担全部涨跌，盘中为 0
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1e6] * len(closes),
        })
        res = run_position_backtest(
            "HAND", df, [1.0] * len(closes), strategy="hand",
            slippage_pct=0.0, commission_pct=0.0,
        )

        # 手算日收益：建仓日 0，随后 +10%, -10%, +10%, +20%, -10%
        expect_rets = [0.0, 0.10, -0.10, 0.10, 0.20, -0.10]
        assert len(res.calendar_returns) == len(expect_rets)
        for got, exp in zip(res.calendar_returns, expect_rets):
            assert got == pytest.approx(exp, abs=1e-12)

        # 手算 Sharpe（与引擎共用同一个 _compute_sharpe，但此处独立重算公式）
        mean_r = sum(expect_rets) / len(expect_rets)
        var = sum((r - mean_r) ** 2 for r in expect_rets) / (len(expect_rets) - 1)
        std = math.sqrt(var)
        daily_rf = (1 + 0.02) ** (1 / 252) - 1
        expect_sharpe = (mean_r - daily_rf) / std * math.sqrt(252)
        assert res.sharpe_calendar == pytest.approx(expect_sharpe, rel=1e-12)

        # 净值：1.1*0.9*1.1*1.2*0.9 = 1.17612
        assert res.equity_curve[-1] == pytest.approx(1.17612, rel=1e-12)
        assert res.total_return_pct == pytest.approx(17.612, rel=1e-10)

    def test_all_three_legs_use_the_same_fixed_engine(self, prices):
        """
        baseline / 多因子 / 基准三腿必须走同一个（修复后的）引擎函数。

        用 monkeypatch 之外的方式验证：三腿结果对象类型相同，且满足
        同一条复合不变量 —— 满仓段的日收益等于 close-to-close。
        基准全程满仓，因此它整条序列都可校验。
        """
        df = prices.iloc[:60].reset_index(drop=True)
        bh = buy_and_hold_benchmark("T", df, slippage_pct=0.0, commission_pct=0.0)
        ma = run_position_backtest("T", df, ma_baseline_positions(df),
                                   strategy="ma", slippage_pct=0.0,
                                   commission_pct=0.0)
        sig = generate_signals("T", df, StrategyParams())
        mf = run_position_backtest("T", df, sig.positions_by_date(),
                                   strategy="mf", slippage_pct=0.0,
                                   commission_pct=0.0)
        assert type(bh) is type(ma) is type(mf)

        # 基准第 2 天起满仓（第 1 天开盘建仓），日收益应等于 close-to-close
        cl = df["close"].tolist()
        for d in bh.daily_detail[1:]:
            i = int(d["i"])
            expect = cl[i] / cl[i - 1] - 1.0
            assert d["net_ret"] == pytest.approx(expect, rel=1e-9, abs=1e-12), (
                f"基准第 {i} 日未满足复合不变量"
            )

    def test_verdict_uses_calendar_caliber(self, prices):
        """达标判定必须用日历口径（可实现口径），不得用被放大的持仓口径。"""
        df = prices
        sig = generate_signals("T", df, StrategyParams())
        r = run_position_backtest("T", df, sig.positions_by_date(), strategy="mf")
        assert r.meets_sharpe_target == (r.sharpe_calendar >= 0.5)
        # 若持仓口径更高，判定不得跟着变高
        if r.sharpe_in_position > r.sharpe_calendar:
            assert not (r.sharpe_in_position >= 0.5 > r.sharpe_calendar
                        and r.meets_sharpe_target)
