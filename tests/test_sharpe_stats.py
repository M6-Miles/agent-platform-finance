"""
tests/test_sharpe_stats.py
==========================
测试夏普统计模块（sharpe_stats.py）。

写法说明
--------
本文件的期望值**全部来自闭式手算**，不是把当前实现的输出录下来当基准。
理由：这个模块的存在意义就是防止"把噪声当效果"，如果测试只是
录制现有输出，公式写错时测试照样绿灯，等于没测。

关键手算锚点（q=252、SR=0 时 SE = sqrt(252/T)）：
    T=252   → sqrt(1)   = 1.0
    T=504   → sqrt(0.5) = 0.70710678
    T=1260  → sqrt(0.2) = 0.44721360
    T=2520  → sqrt(0.1) = 0.31622777

另外复刻两个真实历史陷阱，
确保本模块确实能把它们判出来。
"""
from __future__ import annotations

import math

import pytest

from agent_platform.finance.sharpe_stats import (
    TRADING_DAYS_PER_YEAR,
    SharpeStats,
    _norm_sf,
    ar1_inflation_factor,
    compute_sharpe_stats,
    paired_diff_stats,
    required_n_obs,
    sharpe_std_error,
)


# ─── 标准误：闭式手算 ─────────────────────────────────────────────────────────

class TestSharpeStdError:

    @pytest.mark.parametrize("n_obs, expected", [
        (252,  1.0),            # 1 年   sqrt(252/252)
        (504,  0.70710678),     # 2 年   sqrt(1/2)
        (1260, 0.44721360),     # 5 年   sqrt(1/5)
        (2520, 0.31622777),     # 10 年  sqrt(1/10)
    ])
    def test_zero_sharpe_matches_sqrt_q_over_t(self, n_obs, expected):
        """SR=0 时公式退化为 sqrt(q/T)，可直接手算。"""
        assert sharpe_std_error(0.0, n_obs) == pytest.approx(expected, abs=1e-8)

    def test_nonzero_sharpe_adds_small_correction(self):
        """SR=1、T=252：SE = sqrt(1 + 1/504) = 1.00099157。"""
        assert sharpe_std_error(1.0, 252) == pytest.approx(
            math.sqrt(1.0 + 1.0 / 504.0), abs=1e-9)

    def test_correction_term_is_small_at_realistic_sharpe(self):
        """现实夏普量级下，SR^2/(2q) 修正项不足 1%，主导项是 sqrt(q/T)。"""
        se_zero = sharpe_std_error(0.0, 2520)
        se_real = sharpe_std_error(0.8, 2520)
        assert se_real > se_zero                       # 修正项恒为正
        assert (se_real - se_zero) / se_zero < 0.01

    @pytest.mark.parametrize("n_obs", [0, 1])
    def test_too_few_obs_is_infinite(self, n_obs):
        """观测不足无法估计，返回 inf 而非静默给个小数字。"""
        assert sharpe_std_error(0.0, n_obs) == float("inf")

    def test_strictly_decreasing_in_n(self):
        """样本越长标准误越小——单调性是这个模块的核心论点。"""
        ses = [sharpe_std_error(0.5, n) for n in (252, 504, 1260, 2520, 5040)]
        assert all(a > b for a, b in zip(ses, ses[1:]))

    def test_sign_of_sharpe_does_not_matter(self):
        """SE 只依赖 SR^2，正负同值应给出相同标准误。"""
        assert sharpe_std_error(+0.44, 2426) == pytest.approx(
            sharpe_std_error(-0.44, 2426), abs=1e-12)


# ─── 反解样本量 ───────────────────────────────────────────────────────────────

class TestRequiredNObs:

    @pytest.mark.parametrize("target_se, expected", [
        (1.00, 252),     # 252/1
        (0.50, 1008),    # 252/0.25   ≈ 4 年
        (0.25, 4032),    # 252/0.0625 ≈ 16 年
    ])
    def test_closed_form_at_zero_sharpe(self, target_se, expected):
        assert required_n_obs(target_se, 0.0) == expected

    def test_round_trip_with_std_error(self):
        """反解出的 T 代回 sharpe_std_error，应当刚好压到目标以内。"""
        for target in (0.5, 0.4, 0.3, 0.25):
            n = required_n_obs(target, 0.0)
            assert sharpe_std_error(0.0, n) <= target + 1e-9

    def test_nonpositive_target_returns_zero(self):
        assert required_n_obs(0.0) == 0
        assert required_n_obs(-1.0) == 0


# ─── AR(1) 膨胀系数 ──────────────────────────────────────────────────────────

class TestAr1InflationFactor:

    @pytest.mark.parametrize("series", [[], [1.0], [1.0, 2.0]])
    def test_too_short_returns_one(self, series):
        assert ar1_inflation_factor(series) == 1.0

    def test_constant_series_returns_one(self):
        """零方差时分母为 0，必须安全退回 1.0 而不是除零。"""
        assert ar1_inflation_factor([5.0] * 10) == 1.0

    def test_ramp_matches_hand_computed_rho(self):
        """
        1..10 的斜坡：rho 恰好 = 57.75/82.5 = 0.7，
        故系数 = sqrt(1.7/0.3) = 2.38047614。
        """
        got = ar1_inflation_factor([float(i) for i in range(1, 11)])
        assert got == pytest.approx(math.sqrt(1.7 / 0.3), abs=1e-7)

    def test_negative_autocorrelation_returns_one(self):
        """
        负自相关会**降低**长期方差。本函数刻意不给 <1 的系数
        （那会把标准误报小），一律返回 1.0。
        """
        alternating = [1.0 if i % 2 == 0 else -1.0 for i in range(20)]
        assert ar1_inflation_factor(alternating) == 1.0

    def test_never_below_one(self):
        """作为"至少还要乘多少"的系数，恒 >= 1。"""
        for series in ([1.0, -1.0, 1.0, -1.0], [0.0, 0.0, 1.0], [3.0, 1.0, 2.0]):
            assert ar1_inflation_factor(series) >= 1.0

    def test_clamped_below_theoretical_max(self):
        """rho 被夹在 0.95，故系数不超过 sqrt(1.95/0.05)=sqrt(39)≈6.245。"""
        near_unit_root = [float(i) for i in range(200)]
        assert 1.0 < ar1_inflation_factor(near_unit_root) <= math.sqrt(39.0) + 1e-9


# ─── 主入口：置信区间与判定 ──────────────────────────────────────────────────

class TestComputeSharpeStats:

    def test_ci_is_symmetric_around_point_estimate(self):
        st = compute_sharpe_stats(0.6, 1260)
        assert st.sharpe - st.ci_low == pytest.approx(st.ci_high - st.sharpe, abs=1e-12)

    def test_ci_half_width_is_z_times_se(self):
        st = compute_sharpe_stats(0.6, 1260)
        assert (st.ci_high - st.ci_low) / 2 == pytest.approx(
            1.959964 * st.std_error, abs=1e-6)

    def test_t_vs_zero_is_sharpe_over_se(self):
        st = compute_sharpe_stats(0.8, 2520)
        assert st.t_vs_zero == pytest.approx(st.sharpe / st.std_error, abs=1e-12)

    def test_t_vs_threshold_uses_threshold(self):
        st = compute_sharpe_stats(0.8, 2520, threshold=0.5)
        assert st.t_vs_threshold == pytest.approx(
            (0.8 - 0.5) / st.std_error, abs=1e-12)

    def test_se_inflation_widens_ci(self):
        base = compute_sharpe_stats(0.5, 1260)
        infl = compute_sharpe_stats(0.5, 1260, se_inflation=2.0)
        assert infl.std_error == pytest.approx(2.0 * base.std_error, abs=1e-12)
        assert infl.ci_low < base.ci_low
        assert infl.ci_high > base.ci_high

    def test_se_inflation_below_one_is_ignored(self):
        """膨胀系数 <1 会把不确定性报小，实现用 max(1.0, ·) 挡掉。"""
        base = compute_sharpe_stats(0.5, 1260)
        shrunk = compute_sharpe_stats(0.5, 1260, se_inflation=0.1)
        assert shrunk.std_error == pytest.approx(base.std_error, abs=1e-12)

    def test_degenerate_n_obs_is_never_significant(self):
        """n=1 时 SE=inf，必须给出"无法区分"，不能反而判成显著。"""
        st = compute_sharpe_stats(3.0, 1)
        assert st.std_error == float("inf")
        assert st.threshold_in_ci is True
        assert st.t_vs_zero == 0.0
        assert st.p_vs_zero == 1.0
        assert "无法区分" in st.verdict

    def test_to_dict_exposes_all_fields(self):
        d = compute_sharpe_stats(0.5, 1260).to_dict()
        for key in ("sharpe", "n_obs", "periods_per_year", "std_error",
                    "ci_low", "ci_high", "t_vs_zero", "p_vs_zero", "threshold",
                    "t_vs_threshold", "p_vs_threshold", "threshold_in_ci"):
            assert key in d, f"to_dict 缺少字段 {key}"

    def test_default_threshold_is_e01_target(self):
        assert compute_sharpe_stats(0.0, 252).threshold == 0.5

    def test_default_periods_per_year_is_daily(self):
        assert compute_sharpe_stats(0.0, 252).periods_per_year == TRADING_DAYS_PER_YEAR


# ─── 判定文本的三种分支 ──────────────────────────────────────────────────────

class TestVerdict:

    def test_indistinguishable_when_threshold_inside_ci(self):
        st = compute_sharpe_stats(0.6, 252)          # SE≈1.0，CI 很宽
        assert st.threshold_in_ci is True
        assert "无法区分" in st.verdict

    def test_significantly_above(self):
        st = compute_sharpe_stats(2.0, 2520, threshold=0.5)
        assert st.threshold_in_ci is False
        assert "显著高于" in st.verdict

    def test_significantly_below(self):
        st = compute_sharpe_stats(-1.0, 2520, threshold=0.5)
        assert st.threshold_in_ci is False
        assert "显著低于" in st.verdict

    def test_to_line_contains_point_estimate_and_se(self):
        st = compute_sharpe_stats(0.811, 2425)
        line = st.to_line()
        assert "0.811" in line
        assert "95%CI" in line


# ─── 复刻真实历史陷阱 ────────────────────────────────────────────────────────

class TestHistoricalTraps:
    """
    这些用例锁住本模块的存在理由：同一个夏普点估计，样本长度不同，
    结论应当从"不可证伪"变成"可证伪"。
    """

    def test_one_year_window_cannot_falsify_e01(self):
        """
        2026-08-05 实测：真实 A 股 1 年窗口夏普 -0.440。
        SE≈1.00 → 95% CI 约 [-2.40, +1.52]，把目标 +0.5 整个包住。
        当时无法区分"策略确实不行"与"样本太短"。
        """
        st = compute_sharpe_stats(-0.440, 252, threshold=0.5)
        assert st.std_error == pytest.approx(1.000192, abs=1e-5)
        assert st.ci_low == pytest.approx(-2.4003, abs=1e-3)
        assert st.ci_high == pytest.approx(+1.5203, abs=1e-3)
        assert st.threshold_in_ci is True, "1 年窗口本应无法证伪 E-01"

    def test_ten_year_window_makes_same_sharpe_falsifiable(self):
        """
        同一个 -0.440，放到 10 年（T=2426）：SE≈0.322，
        CI 约 [-1.072, +0.192] 不再覆盖 0.5 → 显著低于目标，p≈0.0018。
        这就是延长窗口的全部价值：点估计没变，可检验性变了。
        """
        st = compute_sharpe_stats(-0.440, 2426, threshold=0.5)
        assert st.std_error == pytest.approx(0.32236, abs=1e-4)
        assert st.threshold_in_ci is False
        assert st.sharpe < st.threshold
        assert st.p_vs_threshold < 0.01
        assert "显著低于" in st.verdict

    def test_ten_year_portfolio_result_still_cannot_claim_pass(self):
        """
        +0.811 / n=2425：这是 2026-08-06 早先报出的十年等权组合数字，
        后被查出系跨标的按行序（而非按日期）平均所致，已更正为 +0.593，
        见下一条与 SPEC.md §3.1.3。此处保留该输入是因为它锁的是**判定纪律**
        而非那个具体数值：点估计 0.811 高于目标，CI [+0.179, +1.443] 仍覆盖
        0.5，故仍不得宣称达标 —— 即便当年那个更好看的数字是真的也不行。
        """
        st = compute_sharpe_stats(0.811, 2425, threshold=0.5)
        assert st.ci_low == pytest.approx(0.1788, abs=1e-3)
        assert st.ci_high == pytest.approx(1.4432, abs=1e-3)
        assert st.threshold_in_ci is True, "CI 覆盖 0.5 时不能宣称达标"
        assert st.ci_low > 0, "该（已作废的）输入下 CI 下界确实为正"

    def test_corrected_ten_year_portfolio_covers_both_threshold_and_zero(self):
        """
        2026-08-06 更正后的十年实测等权组合：夏普 +0.593，n=2425。

        与上一条的区别正是这次更正的要点：+0.811 时 CI 下界 +0.179 > 0，
        尚可宣称"显著优于零"；更正到 +0.593 后下界变为 -0.039 < 0，
        CI 同时覆盖 0.5 与 0 —— 达标不成立，"优于零"也不再成立。
        0.219 的对齐误差恰好跨过了"能否声称优于零"这条线。
        """
        st = compute_sharpe_stats(0.593, 2425, threshold=0.5)
        assert st.std_error == pytest.approx(0.322475, abs=1e-5)
        assert st.ci_low == pytest.approx(-0.0391, abs=1e-3)
        assert st.ci_high == pytest.approx(+1.2251, abs=1e-3)
        assert st.threshold_in_ci is True, "CI 覆盖 0.5 → 不得宣称达标"
        assert st.ci_low < 0, "CI 同时覆盖 0 → 亦不得宣称显著优于零"
        assert "无法区分" in st.verdict

    @pytest.mark.parametrize("sharpe", [0.304, 0.331, 0.332, 0.341])
    def test_trailing_stop_sweep_differences_are_all_noise(self, sharpe):
        """
        2026-08-05 追踪止损扫参得到 +0.304 / +0.331 / +0.332 / +0.341，
        曾误读为"0.08 档更好"。在 T=252 下每一档都无法与 0.5 区分，
        档位之间的差异更是远小于单档标准误。
        """
        st = compute_sharpe_stats(sharpe, 252, threshold=0.5)
        assert st.threshold_in_ci is True
        assert st.std_error > 0.9          # 差异 0.037 相对 SE≈1.0 完全不可分辨


# ─── 配对检验 ────────────────────────────────────────────────────────────────

class TestPairedDiffStats:

    def test_length_mismatch_returns_zeros(self):
        assert paired_diff_stats([1.0, 2.0], [1.0]) == (0.0, 0.0, 0.0)

    def test_too_short_returns_zeros(self):
        assert paired_diff_stats([1.0], [2.0]) == (0.0, 0.0, 0.0)

    def test_mean_is_b_minus_a(self):
        mean, _, _ = paired_diff_stats([1.0, 2.0, 3.0], [1.5, 2.0, 4.0])
        assert mean == pytest.approx((0.5 + 0.0 + 1.0) / 3, abs=1e-12)

    def test_antisymmetry_under_swap(self):
        a = [0.1, 0.2, 0.3, 0.4]
        b = [0.15, 0.1, 0.5, 0.2]
        m_ab, se_ab, t_ab = paired_diff_stats(a, b)
        m_ba, se_ba, t_ba = paired_diff_stats(b, a)
        assert m_ba == pytest.approx(-m_ab, abs=1e-12)
        assert se_ba == pytest.approx(se_ab, abs=1e-12)
        assert t_ba == pytest.approx(-t_ab, abs=1e-12)

    def test_identical_arms_give_exactly_zero(self):
        """
        两臂逐位相同 → 差值全 0。这正是 regime_aware 首轮测量的现象，
        当时据此判断"代码路径从未触发"，而非"效果小"。
        """
        arm = [0.3, -0.1, 0.5, 0.2]
        assert paired_diff_stats(arm, list(arm)) == (0.0, 0.0, 0.0)

    def test_constant_shift_reports_zero_se_and_zero_t(self):
        """
        恒定差值时样本方差为 0，实现返回 se=0 且 t=0（而非 inf）。
        这是刻意的保守取值：零方差多半意味着退化输入，
        不应因此报出无穷大的显著性。均值差本身仍如实返回。
        """
        mean, se, t = paired_diff_stats([1.0, 2.0, 3.0], [1.5, 2.5, 3.5])
        assert mean == pytest.approx(0.5, abs=1e-12)
        assert se == 0.0
        assert t == 0.0

    def test_zero_mean_with_spread_gives_zero_t_but_positive_se(self):
        mean, se, t = paired_diff_stats(
            [0.0, 0.0, 0.0, 0.0], [0.1, -0.1, 0.2, -0.2])
        assert mean == pytest.approx(0.0, abs=1e-12)
        assert se == pytest.approx(math.sqrt((0.10 / 3) / 4), abs=1e-9)
        assert t == pytest.approx(0.0, abs=1e-12)

    def test_paired_se_far_tighter_than_single_arm(self):
        """
        配对设计的全部意义：共同噪声在差分中相消。
        同样 20 个标的，单股 SE≈1.00，配对 SE 可低一到两个数量级。
        """
        a = [0.2 * i for i in range(20)]
        b = [0.2 * i + 0.01 for i in range(20)]   # 恒定微小改进
        _, se, _ = paired_diff_stats(a, b)
        assert se < 0.1

    def test_tiny_effect_can_still_be_significant(self):
        """
        显著 != 有意义。差值稳定在 0.0055 左右时 t 高达约 48，
        但 0.0055 的夏普改善毫无实践价值。
        这正是十年复测中 regime_aware 出现 t=+2.33「显著」而
        效应量仅 +0.0056 的情形，测试把这个陷阱固定下来。
        """
        diffs = [0.005 if i % 2 == 0 else 0.006 for i in range(20)]
        a = [0.0] * 20
        b = list(diffs)
        mean, se, t = paired_diff_stats(a, b)
        assert mean == pytest.approx(0.0055, abs=1e-12)
        assert abs(t) > 40.0        # t 很大
        assert abs(mean) < 0.01     # 但效应量极小


# ─── 正态尾概率 ──────────────────────────────────────────────────────────────

class TestNormSf:

    @pytest.mark.parametrize("z, expected", [
        (0.0,       0.5),
        (1.0,       0.15865525),
        (1.644854,  0.05),
        (1.959964,  0.025),
        (2.575829,  0.005),
    ])
    def test_known_critical_values(self, z, expected):
        assert _norm_sf(z) == pytest.approx(expected, abs=1e-6)

    def test_monotonically_decreasing(self):
        vals = [_norm_sf(z) for z in (0.0, 0.5, 1.0, 2.0, 3.0)]
        assert all(a > b for a, b in zip(vals, vals[1:]))


# ─── 数据类约束 ──────────────────────────────────────────────────────────────

class TestSharpeStatsDataclass:

    def test_is_immutable(self):
        st = compute_sharpe_stats(0.5, 1260)
        with pytest.raises((AttributeError, TypeError)):
            st.sharpe = 99.0     # type: ignore[misc]

    def test_returns_sharpe_stats_instance(self):
        assert isinstance(compute_sharpe_stats(0.5, 1260), SharpeStats)
