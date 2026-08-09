"""
锁死回测页的夏普判定纪律
========================

被测对象：`streamlit_app.backtest_sharpe_verdict()`。

为什么需要独立测试
------------------
2026-08-06 之前，回测页显示的是 `bt.sharpe_ratio` 对比硬阈值 0.5，
并在 >= 0.5 时给出绿色的"满足目标"。这一处同时犯了本项目已经付出代价的两个错：

  (1) **口径错**：sharpe_ratio 只统计持仓日却按 sqrt(252) 年化，等于假装全年
      在市，系统性放大约 1/sqrt(时间在市) 倍。实测时间在市 48.7%，放大约 1.43 倍。
  (2) **把点估计当显著性**：一年窗口下夏普 SE≈1.00，此时"0.62 达标"与
      "0.38 未达标"都不成立。SPEC.md §3.1 记录了这个陷阱造成的两轮自欺。

判定逻辑因此被抽成纯函数，好让这两条能被测试**证伪**而不是靠肉眼审查。
本文件的每个用例都对应一种具体的退化方式。

测试策略
--------
不把当前输出记为期望值 —— 那种写法对逻辑错误不敏感。这里的断言全部来自
"哪种判定在语义上必须成立"，例如"持仓口径 3.0 而日历口径 0.1 时绝不能判达标"。
闭式解层面的数值正确性由 tests/test_sharpe_stats.py 覆盖，本文件只管接线。
"""
from __future__ import annotations

import pytest

from agent_platform.finance.backtesting import BacktestResult
from agent_platform.ui.streamlit_app import backtest_sharpe_verdict


# ─── 构造工具 ────────────────────────────────────────────────────────────────

def _result(
    *,
    sharpe_calendar: float,
    sharpe_ratio: float = 0.0,
    n_days: int = 253,
    time_in_market_pct: float = 50.0,
) -> BacktestResult:
    """构造一个只有判定相关字段有意义的 BacktestResult。

    equity_curve 长度决定日历口径的 n_obs（= 长度 - 1），这是修复后的口径：
    引擎已让 equity_curve 按交易日对齐，空仓日追加重复净值。
    """
    return BacktestResult(
        symbol="TEST", start_date="2024-01-01", end_date="2024-12-31",
        total_trades=10, winning_trades=6, losing_trades=4, win_rate_pct=60.0,
        total_return_pct=12.0, annualized_return_pct=12.0,
        annualized_volatility_pct=20.0, sharpe_ratio=sharpe_ratio,
        max_drawdown_pct=15.0, avg_slippage_pct=0.1, trades=[],
        equity_curve=[1.0] * n_days,
        sharpe_calendar=sharpe_calendar,
        annualized_volatility_calendar_pct=14.0,
        time_in_market_pct=time_in_market_pct,
    )


# 十年 ≈ 2426 个交易日，SE≈0.322；一年 ≈ 253 个，SE≈1.00
TEN_YEARS = 2426
ONE_YEAR = 253


# ─── 口径：必须读日历口径，不读持仓口径 ──────────────────────────────────────

class TestUsesCalendarCaliber:

    def test_high_in_position_sharpe_cannot_produce_pass(self):
        """最关键的一条回归测试。

        持仓口径 3.0 远超目标，日历口径只有 0.1 —— 即"只在持仓日看很漂亮，
        但把空仓日算进来就不行"。若判定退回读 sharpe_ratio，这条立刻失败。
        十年样本使 SE 足够小，排除"因为样本短所以测不出"这个混淆因素。

        断言取 `!= "pass"` 而非 `== "fail"`：日历口径 0.1 在 n=2426 下
        CI 为 [-0.532, +0.732]，仍覆盖 0.5，诚实判定就是"无法区分"。
        写成 "fail" 是我最初的错误期望 —— 那等于要求代码在 CI 覆盖阈值时
        给出方向性结论，恰是本模块要禁止的行为。真正能到 "fail" 的场景
        见 test_clearly_negative_calendar_sharpe_fails。
        """
        stats, level, msg = backtest_sharpe_verdict(
            _result(sharpe_calendar=0.10, sharpe_ratio=3.00, n_days=TEN_YEARS + 1)
        )
        assert stats.sharpe == pytest.approx(0.10), "点估计必须取自日历口径"
        assert level != "pass", "持仓口径再高也不能换来达标判定"
        assert "满足目标" not in msg

    def test_clearly_negative_calendar_sharpe_fails(self):
        """日历口径 −0.5、十年样本 ⇒ CI 上界 +0.132 < 0.5，可给出"显著低于"。

        与上一条配对：证明 "fail" 这条分支确实可达，上一条的
        "inconclusive" 不是因为代码永远不敢下结论。
        """
        stats, level, msg = backtest_sharpe_verdict(
            _result(sharpe_calendar=-0.50, sharpe_ratio=2.00, n_days=TEN_YEARS + 1)
        )
        assert stats.ci_high < 0.5
        assert level == "fail"
        assert "显著低于" in msg

    def test_calendar_pass_with_low_in_position_still_passes(self):
        """反方向：日历口径达标而持仓口径很低，判定应看日历口径。

        （这种组合在真实数据上罕见，但它把"是否读对字段"与"是否恰好同向"分开。）
        """
        stats, level, _ = backtest_sharpe_verdict(
            _result(sharpe_calendar=1.50, sharpe_ratio=0.05, n_days=TEN_YEARS + 1)
        )
        assert stats.sharpe == pytest.approx(1.50)
        assert level == "pass"

    def test_in_position_field_never_enters_the_statistics(self):
        """同一日历口径 + 任意持仓口径 ⇒ 判定完全不变。"""
        base = backtest_sharpe_verdict(
            _result(sharpe_calendar=0.30, sharpe_ratio=0.30, n_days=TEN_YEARS + 1)
        )
        for inpos in (-5.0, 0.0, 0.9, 12.0):
            got = backtest_sharpe_verdict(
                _result(sharpe_calendar=0.30, sharpe_ratio=inpos, n_days=TEN_YEARS + 1)
            )
            assert got[0].sharpe == base[0].sharpe
            assert got[0].ci_low == pytest.approx(base[0].ci_low)
            assert got[1] == base[1], f"持仓口径 {inpos} 改变了判定"


# ─── 显著性：CI 覆盖阈值时不得宣称达标 ───────────────────────────────────────

class TestSignificanceDiscipline:

    @pytest.mark.parametrize("cal", [0.51, 0.62, 0.90, 1.40, 2.00])
    def test_one_year_window_can_never_pass(self, cal):
        """一年窗口 SE≈1.00，点估计再漂亮也不足以排除 0.5。

        这直接复刻旧 UI 的错误：点估计 0.62 曾被显示为绿色"满足目标"。
        """
        stats, level, msg = backtest_sharpe_verdict(
            _result(sharpe_calendar=cal, n_days=ONE_YEAR + 1)
        )
        assert stats.threshold_in_ci is True
        assert level == "inconclusive", f"一年窗口下 {cal} 不应判达标"
        assert "无法区分" in stats.verdict
        assert "样本太短看不出来" in msg

    def test_point_estimate_above_threshold_is_not_enough(self):
        """点估计 > 0.5 但 CI 下界 < 0.5 ⇒ inconclusive，不是 pass。"""
        stats, level, _ = backtest_sharpe_verdict(
            _result(sharpe_calendar=0.60, n_days=TEN_YEARS + 1)
        )
        assert stats.sharpe > 0.5
        assert stats.ci_low < 0.5
        assert level == "inconclusive", "点估计高于阈值不构成达标"

    def test_pass_requires_ci_low_above_threshold(self):
        """判 pass 的充要条件是 CI 下界严格高于阈值。"""
        stats, level, msg = backtest_sharpe_verdict(
            _result(sharpe_calendar=1.40, n_days=TEN_YEARS + 1)
        )
        assert stats.ci_low > 0.5
        assert level == "pass"
        assert "不可外推为策略整体达标" in msg, "达标也须提示不可外推"

    def test_fail_requires_ci_high_below_threshold(self):
        stats, level, _ = backtest_sharpe_verdict(
            _result(sharpe_calendar=-0.50, n_days=TEN_YEARS + 1)
        )
        assert stats.ci_high < 0.5
        assert level == "fail"
        assert "显著低于" in stats.verdict

    def test_longer_sample_can_flip_inconclusive_to_pass(self):
        """同一点估计，仅延长样本即可从"无法区分"变为"达标"。

        这是引入 SE/CI 的全部意义：结论取决于样本量，而不只是点估计。
        """
        cal = 1.20
        short = backtest_sharpe_verdict(_result(sharpe_calendar=cal, n_days=ONE_YEAR + 1))
        long_ = backtest_sharpe_verdict(_result(sharpe_calendar=cal, n_days=TEN_YEARS + 1))
        assert short[1] == "inconclusive"
        assert long_[1] == "pass"
        assert long_[0].std_error < short[0].std_error


# ─── n_obs 取自日历对齐后的 equity_curve ─────────────────────────────────────

class TestObservationCount:

    def test_n_obs_is_equity_curve_length_minus_one(self):
        stats, _, _ = backtest_sharpe_verdict(_result(sharpe_calendar=0.3, n_days=1001))
        assert stats.n_obs == 1000

    def test_se_shrinks_with_sqrt_n(self):
        """SE ≈ sqrt(252/n)：样本 ×4 ⇒ SE ÷2。"""
        a, _, _ = backtest_sharpe_verdict(_result(sharpe_calendar=0.0, n_days=253))
        b, _, _ = backtest_sharpe_verdict(_result(sharpe_calendar=0.0, n_days=1009))
        assert a.std_error == pytest.approx(1.0, abs=0.01)
        assert b.std_error == pytest.approx(0.5, abs=0.01)

    @pytest.mark.parametrize("curve", [[], [1.0]])
    def test_degenerate_equity_curve_does_not_raise(self, curve):
        """空/单点净值曲线不能抛异常 —— 无交易的回测会走到这里。"""
        r = _result(sharpe_calendar=0.0)
        r.equity_curve = curve
        stats, level, msg = backtest_sharpe_verdict(r)
        assert stats.n_obs == max(len(curve) - 1, 0)
        assert level in {"inconclusive", "pass", "fail"}
        assert msg


# ─── 返回的 level 必须与 UI 的分发字典对齐 ───────────────────────────────────

class TestLevelContract:

    def test_levels_match_ui_dispatch_keys(self):
        """level 取值必须恰好是 UI 分发字典的键。

        这条测试的由来：改造过程中 helper 一度返回 "info"/"success"/"warning"，
        而 UI 按 "inconclusive"/"pass"/"fail" 分发 —— 每次渲染都会 KeyError。
        单看两边各自的代码都是对的，只有契约测试能抓住这种错配。
        """
        import agent_platform.ui.streamlit_app as app

        expected = {"inconclusive", "pass", "fail"}
        seen = {
            backtest_sharpe_verdict(_result(sharpe_calendar=c, n_days=n))[1]
            for c, n in [
                (0.60, ONE_YEAR + 1),      # inconclusive
                (1.40, TEN_YEARS + 1),     # pass
                (-0.50, TEN_YEARS + 1),    # fail
            ]
        }
        assert seen == expected, "三种判定都应可达"

        src = app.__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        for key in expected:
            assert f'"{key}"' in text, f"UI 未处理 level={key}"

    def test_custom_threshold_is_honoured(self):
        """阈值可配置，且判定随之改变（默认 0.5 不是写死的）。"""
        r = _result(sharpe_calendar=1.40, n_days=TEN_YEARS + 1)
        assert backtest_sharpe_verdict(r, threshold=0.5)[1] == "pass"
        assert backtest_sharpe_verdict(r, threshold=3.0)[1] == "fail"
        mid = backtest_sharpe_verdict(r, threshold=1.30)
        assert mid[1] == "inconclusive"
        assert "1.3" in mid[2], "提示文本应反映实际阈值"
