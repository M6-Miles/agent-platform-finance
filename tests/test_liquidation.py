"""
退市清算损失的语义测试（2026-08-06 新增）

背景
────
`Scripts/measure_survivorship_bias.py` 的 [C] 段按 0% / −50% / −100% 三档
报告退市清算敏感性。该脚本的 `apply_liquidation()` docstring 声称：

    「刻意让清算损失**流经回测引擎**而非事后修补收益序列：这样只有在清算日
      仍持仓的标的才承担损失，空仓的不受影响 —— 与真实情形一致。」

这句话是那三档数字的**全部**可信度来源，此前只是注释，没有测试。
本文件把它变成断言。

为什么这条特别值得测
────────────────────
存活者偏差报告的结论形如"加回死者后夏普从 +0.593 掉到 +0.399/+0.268/+0.138"。
若清算损失实际上被无条件加到每个标的的收益序列末端（而非仅限持仓者），
那三档的差值会被系统性放大，而报告读起来完全正常 —— 这类缺陷不会自己暴露。
本轮此前已因"按行序对齐"吃过一次同类教训（SPEC.md §3.1.3）。

期望值来自独立推导，不是记录当前输出
────────────────────────────────────
持仓到最后一日者，净值应恰好乘以 (1 − loss)；空仓者应恰好不变。
两者都是闭式值，与实现无关。

另有一条防回归：`run_pool()` 对同一个 frames 字典按三档各跑一次，
若 `apply_liquidation` 原地修改输入，第二、三档会累积前一档的清算行，
数字随之错误。故此处断言输入帧不被改动。
"""
from __future__ import annotations

import io
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "Scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "Scripts"))

# measure_survivorship_bias → measure_real_10y，后者在**模块级**执行
# sys.stdout = TextIOWrapper(sys.stdout.buffer, ...)（measure_real_10y.py:39）。
#
# 单纯"存下再还原"不够：被丢弃的那个 wrapper 仍然包着 **pytest 捕获用的
# tmpfile**，GC 时会把它关掉，于是会话结束时 pytest 报
# ValueError: I/O operation on closed file（已实测）。
# 所以这里在 import 期间换上一个**一次性 stdout**，让脚本去包我们自己的
# BytesIO —— pytest 的 tmpfile 全程不被碰到，谁来 GC 都无所谓。
# 保留模块级引用 _SACRIFICIAL 只为让生命周期显式、不依赖 GC 时机。
_SACRIFICIAL = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace")
_saved_stdout = sys.stdout
sys.stdout = _SACRIFICIAL
try:
    from measure_survivorship_bias import apply_liquidation, portfolio_from  # noqa: E402
finally:
    sys.stdout = _saved_stdout

from agent_platform.finance.backtesting import run_backtest  # noqa: E402


BASE = date(2024, 1, 1)
FLAT_PRICE = 100.0
N_DAYS = 11


def _flat_frame(n: int = N_DAYS, price: float = FLAT_PRICE) -> pd.DataFrame:
    """
    横盘价格序列。刻意用横盘：这样净值的任何变化都只能来自清算跳空，
    不会与价格波动混在一起，断言可以取精确值而非近似区间。
    """
    dts = [BASE + timedelta(days=i) for i in range(n)]
    return pd.DataFrame(
        {
            "date": dts,
            "open": [price] * n,
            "high": [price] * n,
            "low": [price] * n,
            "close": [price] * n,
            "volume": [1_000_000] * n,
            "ma20": [price] * n,   # 指标列：验证追加行会原样保留
        }
    )


def _weekday_frame(n: int, price: float = FLAT_PRICE) -> pd.DataFrame:
    """
    只包含工作日的序列（跳过周六日）。用于测日期贴合：真实池子不含周末，
    退市日 +1 自然日常落在周六，贴合后应落在周一。
    """
    dts = []
    d = BASE
    while len(dts) < n:
        if d.weekday() < 5:
            dts.append(d)
        d += timedelta(days=1)
    return pd.DataFrame(
        {
            "date": dts,
            "open": [price] * n,
            "high": [price] * n,
            "low": [price] * n,
            "close": [price] * n,
            "volume": [1_000_000] * n,
            "ma20": [price] * n,
        }
    )


def _held_signals(df: pd.DataFrame) -> dict:
    """第 0 日买入，其后一路持有 —— 清算日必然在仓。"""
    sigs = {d: "hold" for d in df["date"]}
    sigs[df["date"].iloc[0]] = "buy"
    return sigs


def _flat_signals(df: pd.DataFrame) -> dict:
    """全程空仓。"""
    return {d: "hold" for d in df["date"]}


class TestApplyLiquidationFrame:
    """先测纯 DataFrame 变换层面的行为。"""

    def test_zero_loss_is_a_no_op(self):
        df = _flat_frame()
        out = apply_liquidation(df, 0.0)
        assert len(out) == len(df)
        pd.testing.assert_frame_equal(out, df)

    def test_empty_frame_returns_empty(self):
        empty = _flat_frame().iloc[0:0]
        assert apply_liquidation(empty, 0.5).empty

    @pytest.mark.parametrize("loss", [0.5, 1.0, 0.25])
    def test_appends_exactly_one_row_at_discounted_price(self, loss: float):
        df = _flat_frame()
        out = apply_liquidation(df, loss)

        assert len(out) == len(df) + 1, "清算日应只追加一行"

        last = out.iloc[-1]
        expected = FLAT_PRICE * (1.0 - loss)
        # 四价全部设为清算价：否则引擎按开盘价成交时会漏掉这一跳。
        for col in ("open", "high", "low", "close"):
            assert float(last[col]) == pytest.approx(expected), f"{col} 应为清算价"
        assert int(last["volume"]) == 0
        assert last["date"] == df["date"].iloc[-1] + timedelta(days=1)

    def test_indicator_columns_carry_over(self):
        """
        指标列保留最后一行的值（脚本注释所述的刻意选择）：清算日不产生新信号。
        若追加行的指标变成 NaN，gen_signals 的行为将不可预期。
        """
        df = _flat_frame()
        out = apply_liquidation(df, 0.5)
        assert float(out.iloc[-1]["ma20"]) == pytest.approx(float(df.iloc[-1]["ma20"]))
        assert not out.isna().any().any(), "追加行不应引入 NaN"

    def test_does_not_mutate_input(self):
        """
        run_pool() 对同一 frames 字典按三档各跑一次。原地修改会让
        −50%/−100% 两档累积前一档的清算行，差值随之被放大。
        """
        df = _flat_frame()
        before = df.copy(deep=True)
        apply_liquidation(df, 0.5)
        pd.testing.assert_frame_equal(df, before)


class TestLiquidationDateSnapping:
    """
    追加日必须贴到池子真实交易日上（2026-08-06 修一个实测缺陷）。

    缺陷本身
    ────────
    原实现取 `最后交易日 + 1 自然日`。退市日多在周五，+1 天就是周六 ——
    合并池里**没有任何标的**在那天有数据。于是 `portfolio_from` 在该日
    按等权取均值时 n=1，一只标的的 −100% 清算被记成整个组合的 −100%。
    实测 225 只退市标的里 37 只（16.4%）踩中，把 loss=50%/100% 两档污染掉。

    正确行为：一只标的退市，在等权组合里就该只值 1/n。
    与 SPEC.md §3.1.3 的"按行序对齐"同类：日期没对齐，且都让数字更极端。
    """

    def test_snaps_onto_calendar_instead_of_natural_next_day(self):
        """核心：周五退市 + 只在工作日交易的日历 ⇒ 应贴到周一，而非周六。"""
        dead = _weekday_frame(n=10)                     # 末日为周五
        friday = dead["date"].iloc[-1]
        assert friday.weekday() == 4, "前置条件：末日应为周五"

        cal = set(_weekday_frame(n=20)["date"])          # 池子日历（无周末）
        out = apply_liquidation(dead, 1.0, calendar=cal)
        appended = out["date"].iloc[-1]

        assert appended != friday + timedelta(days=1), "不应落在周六"
        assert appended.weekday() == 0, "应贴到下一个周一"
        assert appended in cal, "追加日必须是池子里真实存在的交易日"

    def test_natural_next_day_would_have_missed_the_calendar(self):
        """
        显式记录缺陷条件本身：+1 自然日**不在**日历里。
        这条是上一条的"若没有修复会怎样"，两条一起才说明问题。
        """
        dead = _weekday_frame(n=10)
        cal = set(_weekday_frame(n=20)["date"])
        naive = dead["date"].iloc[-1] + timedelta(days=1)
        assert naive not in cal, "周六本就不在交易日历中 —— 这正是缺陷成因"

    def test_picks_earliest_later_date(self):
        """贴的是「最早晚于末日」的那天，不是日历里任意一天。"""
        dead = _flat_frame(n=5)
        last = dead["date"].iloc[-1]
        cal = {last + timedelta(days=k) for k in (30, 7, 3, 90)}
        out = apply_liquidation(dead, 0.5, calendar=cal)
        assert out["date"].iloc[-1] == last + timedelta(days=3)

    def test_ignores_dates_at_or_before_last_day(self):
        """日历里 ≤ 末日的日期不能被选中（否则会出现日期回退的重复行）。"""
        dead = _flat_frame(n=5)
        last = dead["date"].iloc[-1]
        cal = set(dead["date"]) | {last - timedelta(days=1)}
        out = apply_liquidation(dead, 0.5, calendar=cal)
        assert out["date"].iloc[-1] > last, "追加日必须严格晚于末日"

    def test_falls_back_when_calendar_has_nothing_later(self):
        """
        窗口末尾退市的标的：日历里没有更晚的交易日，退回 +1 自然日。
        实测全部 225 只都能贴上（0 只走到这条分支），但该分支必须安全。
        """
        dead = _flat_frame(n=5)
        last = dead["date"].iloc[-1]
        out = apply_liquidation(dead, 0.5, calendar=set(dead["date"]))
        assert out["date"].iloc[-1] == last + timedelta(days=1)

    def test_no_calendar_keeps_old_behaviour(self):
        """不传 calendar 时保持 +1 自然日 —— 纯 DataFrame 用法不受影响。"""
        dead = _flat_frame(n=5)
        out = apply_liquidation(dead, 0.5, calendar=None)
        assert out["date"].iloc[-1] == dead["date"].iloc[-1] + timedelta(days=1)


class TestLiquidationPortfolioWeight:
    """
    缺陷的**后果**层面：清算日在等权组合里必须按 1/n 稀释。

    上一个 class 测的是日期贴对了；这里测贴对之后组合权重才对 ——
    这才是那 37 只污染数字的直接原因，所以单独锁一条。
    """

    def test_liquidation_is_diluted_not_full_weight(self):
        """
        两只标的：A 全程存活，B 周五退市并 100% 清算。
        清算日组合收益应约为 A 当日收益与 B 的 −100% 的均值（≈ −50%），
        而不是 −100%。若日期没贴对，B 独占该日 ⇒ 组合当日 −100%。
        """
        dead = _weekday_frame(n=10)
        alive = _weekday_frame(n=20)
        cal = set(alive["date"]) | set(dead["date"])

        out = apply_liquidation(dead, 1.0, calendar=cal)
        liq_date = out["date"].iloc[-1]

        # 构造按日期索引的收益序列：A 该日收益取 0（横盘），B 取 −1。
        ret_by_date = {
            "alive": {d: 0.0 for d in alive["date"]},
            "dead": {liq_date: -1.0},
        }
        rets, dates = portfolio_from(ret_by_date)
        idx = dates.index(liq_date)

        assert rets[idx] == pytest.approx(-0.5), (
            "清算日应按 1/n 稀释（此处 n=2 ⇒ −50%），"
            "得到 −100% 说明该日只有退市标的在场"
        )

    def test_undiluted_when_date_falls_off_calendar(self):
        """
        对照：不贴日历时清算日落在周六，组合里只有它 ⇒ 权重 100%。
        这条把缺陷的量级固定下来，也解释了为何 loss 越大组合夏普并不单调
        （单个 −100% 日主导方差，见 measure_survivorship_bias 的 [C] 段）。
        """
        dead = _weekday_frame(n=10)
        alive = _weekday_frame(n=20)

        out = apply_liquidation(dead, 1.0, calendar=None)
        liq_date = out["date"].iloc[-1]
        assert liq_date not in set(alive["date"]), "前置条件：该日 A 不交易"

        ret_by_date = {
            "alive": {d: 0.0 for d in alive["date"]},
            "dead": {liq_date: -1.0},
        }
        rets, dates = portfolio_from(ret_by_date)
        assert rets[dates.index(liq_date)] == pytest.approx(-1.0), (
            "未贴日历时该日 n=1，一只标的的清算被当成整个组合的 −100%"
        )


class TestLiquidationThroughEngine:
    """核心：清算损失只落在持仓者身上。期望值为闭式解。"""

    @pytest.mark.parametrize("loss", [0.5, 1.0])
    def test_held_position_absorbs_full_loss(self, loss: float):
        df = _flat_frame()
        liq = apply_liquidation(df, loss)
        res = run_backtest("HELD", liq, _held_signals(liq), slippage_pct=0.0, commission_pct=0.0)

        eq = res.equity_curve
        # 横盘 + 零摩擦 ⇒ 清算前净值应为初始值；清算后应恰好乘 (1 − loss)。
        assert eq[-1] / eq[0] == pytest.approx(1.0 - loss, abs=1e-9)
        step = (eq[-1] - eq[-2]) / eq[-2]
        assert step == pytest.approx(-loss, abs=1e-9), "末步收益应等于 −清算损失"

    @pytest.mark.parametrize("loss", [0.0, 0.5, 1.0])
    def test_flat_position_is_untouched(self, loss: float):
        """
        这一条是 docstring 那句承诺的直接检验，也是本文件存在的理由：
        空仓者在任何清算档位下净值都不得变化。
        """
        df = _flat_frame()
        liq = apply_liquidation(df, loss)
        res = run_backtest("FLAT", liq, _flat_signals(liq), slippage_pct=0.0, commission_pct=0.0)

        eq = res.equity_curve
        assert eq[-1] == pytest.approx(eq[0], abs=1e-9), "空仓不应承担清算损失"
        assert res.time_in_market_pct == pytest.approx(0.0)
        assert res.max_drawdown_pct == pytest.approx(0.0)

    def test_held_and_flat_diverge_only_because_of_position(self):
        """
        同一价格序列、同一清算档位，唯一差别是信号 ⇒ 结果差异必须完全
        由持仓状态解释。这排除了"清算损失被无条件加到序列末端"的实现。
        """
        df = _flat_frame()
        liq = apply_liquidation(df, 1.0)
        held = run_backtest("H", liq, _held_signals(liq), slippage_pct=0.0, commission_pct=0.0)
        flat = run_backtest("F", liq, _flat_signals(liq), slippage_pct=0.0, commission_pct=0.0)

        assert held.equity_curve[-1] == pytest.approx(0.0, abs=1e-6), "归零档持仓者应清零"
        assert flat.equity_curve[-1] == pytest.approx(flat.equity_curve[0]), "空仓者应毫发无损"

    def test_drawdown_reflects_liquidation_for_holder(self):
        df = _flat_frame()
        liq = apply_liquidation(df, 0.5)
        res = run_backtest("HELD", liq, _held_signals(liq), slippage_pct=0.0, commission_pct=0.0)
        # 横盘无其他回撤来源，故最大回撤应恰为清算跌幅。
        assert res.max_drawdown_pct == pytest.approx(50.0, abs=1e-6)
