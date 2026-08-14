"""
DCF 估值专项测试
================
说明书要求「实现真实可解释的 DCF 估值字段、公式、输入假设、边界校验和测试；
不能用固定常数冒充 DCF」。本文件的核心任务就是**证明 DCF 不是常数**，
并锁定所有边界校验行为。

测试分组
--------
TestAccountingIdentity   —— 输入推导链（净利润/净资产/隐含 ROE）符合会计恒等式
TestNotAConstant         —— 反「固定常数冒充」：不同输入必须产出不同结果
TestWACC                 —— CAPM 折现率
TestGrowth               —— 可持续增长率与增长衰减
TestBoundaryValidation   —— 五项边界校验必须硬失败，不得返回貌似合理的数字
TestWarnings             —— 非阻断性警告
TestBetaFromVolatility   —— 波动率反推 β 的钳制
TestSerialization        —— to_dict / to_markdown 结构
"""
from __future__ import annotations

import math

import pytest

from agent_platform.finance.dcf_valuation import (
    DEFAULT_EQUITY_RISK_PREMIUM,
    DEFAULT_FCF_CONVERSION,
    DEFAULT_PAYOUT_RATIO,
    DEFAULT_RISK_FREE_RATE,
    DEFAULT_TERMINAL_GROWTH,
    DCFAssumptions,
    DCFResult,
    beta_from_volatility,
    compute_cost_of_equity,
    compute_wacc,
    run_dcf,
    sustainable_growth,
)

# 一组基准输入：PE=25.6 / PB=3.2 / 市值 12.5 亿（对应离线样例 DEMO001）
BASE = {"pe_ttm": 25.6, "pb": 3.2, "total_market_value_cny": 1_250_000_000.0}


# ═══════════════════════════════════════════════════════════════
class TestAccountingIdentity:
    """输入推导必须可追溯到市场乘数，不得凭空捏造财务科目。"""

    def test_implied_roe_equals_pb_over_pe(self):
        """隐含 ROE = PB / PE × 100（净利润/净资产的恒等式）。"""
        r = run_dcf(**BASE)
        expected = BASE["pb"] / BASE["pe_ttm"] * 100
        assert r.implied_roe_pct == pytest.approx(expected, rel=1e-9)
        assert r.implied_roe_pct == pytest.approx(12.5, rel=1e-9)

    def test_net_income_equals_mv_over_pe(self):
        """净利润 = 总市值 / PE_TTM。"""
        r = run_dcf(**BASE)
        assert r.net_income_cny == pytest.approx(
            BASE["total_market_value_cny"] / BASE["pe_ttm"], rel=1e-9
        )

    def test_book_value_equals_mv_over_pb(self):
        """净资产 = 总市值 / PB。"""
        r = run_dcf(**BASE)
        assert r.book_value_cny == pytest.approx(
            BASE["total_market_value_cny"] / BASE["pb"], rel=1e-9
        )

    def test_fcf_base_is_net_income_times_conversion(self):
        """FCF 基数 = 净利润 × 现金转化率（默认 0.75），非凭空取值。"""
        r = run_dcf(**BASE)
        assert r.fcf_base_cny == pytest.approx(
            r.net_income_cny * DEFAULT_FCF_CONVERSION, rel=1e-9
        )

    def test_market_value_echoed_for_audit(self):
        """市值必须回显，供审计比对安全边际计算。"""
        r = run_dcf(**BASE)
        assert r.market_value_cny == pytest.approx(BASE["total_market_value_cny"])


# ═══════════════════════════════════════════════════════════════
class TestNotAConstant:
    """
    反「固定常数冒充 DCF」的关键证据。

    若实现是常数，以下测试全部会失败 —— 不同标的的估值、增长率、
    折现率、信号必须随输入变化。
    """

    CASES = [
        # (pe, pb, 说明)
        (10.0, 3.0, "高ROE低估值"),
        (25.6, 3.2, "中性"),
        (50.0, 2.0, "低ROE高估值"),
    ]

    def test_distinct_inputs_give_distinct_equity_values(self):
        vals = [
            run_dcf(pe_ttm=pe, pb=pb, total_market_value_cny=1e9).equity_value_cny
            for pe, pb, _ in self.CASES
        ]
        assert len(set(vals)) == len(vals), f"股权价值出现重复，疑似常数: {vals}"

    def test_distinct_inputs_give_distinct_growth(self):
        gs = [
            run_dcf(pe_ttm=pe, pb=pb, total_market_value_cny=1e9).growth_stage1
            for pe, pb, _ in self.CASES
        ]
        assert len(set(gs)) == len(gs), f"增长率出现重复，疑似常数: {gs}"

    def test_distinct_inputs_give_distinct_implied_roe(self):
        roes = [
            run_dcf(pe_ttm=pe, pb=pb, total_market_value_cny=1e9).implied_roe_pct
            for pe, pb, _ in self.CASES
        ]
        assert sorted(roes) == sorted({30.0, 12.5, 4.0}), roes

    def test_signal_flips_across_valuation_extremes(self):
        """极端便宜与极端昂贵必须给出相反信号。"""
        cheap = run_dcf(pe_ttm=8.0, pb=2.4, total_market_value_cny=1e9)
        rich = run_dcf(pe_ttm=80.0, pb=1.6, total_market_value_cny=1e9)
        assert cheap.valuation_signal != rich.valuation_signal
        assert cheap.margin_of_safety_pct > rich.margin_of_safety_pct

    def test_market_value_scales_equity_value_linearly(self):
        """同乘数下，市值翻倍则内在价值翻倍（模型齐次性）。"""
        a = run_dcf(pe_ttm=20.0, pb=2.0, total_market_value_cny=1e9)
        b = run_dcf(pe_ttm=20.0, pb=2.0, total_market_value_cny=2e9)
        assert b.equity_value_cny == pytest.approx(2 * a.equity_value_cny, rel=1e-9)
        # 安全边际与规模无关
        assert b.margin_of_safety_pct == pytest.approx(a.margin_of_safety_pct, rel=1e-9)

    def test_beta_changes_result(self):
        """β 变化必须改变折现率进而改变估值（折现率非常数）。"""
        low = run_dcf(**BASE, assumptions=DCFAssumptions(beta=0.6))
        high = run_dcf(**BASE, assumptions=DCFAssumptions(beta=1.6))
        assert low.wacc < high.wacc
        assert low.equity_value_cny > high.equity_value_cny


# ═══════════════════════════════════════════════════════════════
class TestWACC:
    def test_capm_formula(self):
        """WACC = rf + β × ERP。"""
        a = DCFAssumptions(risk_free_rate=0.03, equity_risk_premium=0.06, beta=1.2)
        assert compute_wacc(a) == pytest.approx(0.03 + 1.2 * 0.06, rel=1e-12)

    def test_default_wacc_value(self):
        a = DCFAssumptions()
        expected = DEFAULT_RISK_FREE_RATE + 1.0 * DEFAULT_EQUITY_RISK_PREMIUM
        assert compute_wacc(a) == pytest.approx(expected, rel=1e-12)

    def test_result_wacc_matches_assumptions(self):
        a = DCFAssumptions(beta=1.4)
        r = run_dcf(**BASE, assumptions=a)
        assert r.wacc == pytest.approx(compute_wacc(a), rel=1e-12)

    def test_wacc_applies_debt_weight_and_tax_shield(self):
        a = DCFAssumptions(
            risk_free_rate=0.03,
            equity_risk_premium=0.06,
            beta=1.2,
            debt_weight=0.4,
            pretax_cost_of_debt=0.05,
            corporate_tax_rate=0.25,
        )
        expected = 0.6 * compute_cost_of_equity(a) + 0.4 * 0.05 * (1 - 0.25)
        assert compute_wacc(a) == pytest.approx(expected)

    def test_invalid_debt_weight_rejected(self):
        with pytest.raises(ValueError, match="debt_weight"):
            compute_wacc(DCFAssumptions(debt_weight=1.1))


# ═══════════════════════════════════════════════════════════════
class TestGrowth:
    def test_sustainable_growth_formula(self):
        """g = ROE × (1 − 派息率)。"""
        a = DCFAssumptions(payout_ratio=0.35)
        g = sustainable_growth(12.5, a)
        assert g == pytest.approx(0.125 * 0.65, rel=1e-12)

    def test_growth_respects_cap(self):
        """高 ROE 必须被 growth_cap 截断，避免永续高增长的荒谬假设。"""
        a = DCFAssumptions(growth_cap=0.20, payout_ratio=0.0)
        assert sustainable_growth(90.0, a) == pytest.approx(0.20)

    def test_negative_roe_gives_non_positive_growth(self):
        a = DCFAssumptions()
        assert sustainable_growth(-5.0, a) <= 0.0

    def test_default_payout_used(self):
        a = DCFAssumptions()
        g = sustainable_growth(20.0, a)
        assert g == pytest.approx(0.20 * (1 - DEFAULT_PAYOUT_RATIO), rel=1e-12)

    def test_projection_has_forecast_years_rows(self):
        r = run_dcf(**BASE, assumptions=DCFAssumptions(forecast_years=7))
        assert len(r.yearly_projection) == 7

    def test_growth_fades_monotonically_toward_terminal(self):
        """两阶段模型：增长率应从 g1 单调衰减，避免第6年断崖。"""
        r = run_dcf(**BASE, assumptions=DCFAssumptions(forecast_years=5))
        growths = [row["growth_rate"] for row in r.yearly_projection]
        assert growths == sorted(growths, reverse=True), growths
        assert growths[0] == pytest.approx(r.growth_stage1, rel=1e-9)
        # 末年增长率应接近但不低于永续增长率
        assert growths[-1] >= r.terminal_growth - 1e-9

    def test_discounted_fcf_present_in_projection(self):
        r = run_dcf(**BASE)
        row = r.yearly_projection[0]
        for key in ("year", "growth_rate", "fcf_cny", "discount_factor", "present_value_cny"):
            assert key in row, f"逐年明细缺少 {key}"
        assert row["present_value_cny"] < row["fcf_cny"], "折现后现值应小于名义现金流"


# ═══════════════════════════════════════════════════════════════
class TestBoundaryValidation:
    """
    五项边界校验。要求：不适用时必须 applicable=False 且给出中文原因，
    **不得**返回一个貌似合理的数字。
    """

    def _assert_rejected(self, r: DCFResult, keyword: str):
        assert r.applicable is False
        assert r.reason_not_applicable, "拒绝时必须给出原因"
        assert keyword in r.reason_not_applicable, r.reason_not_applicable
        assert r.equity_value_cny is None, "不适用时不得返回估值"
        assert r.margin_of_safety_pct is None
        assert r.valuation_signal == "unknown"

    def test_reject_negative_pe(self):
        """亏损企业 DCF 盈利基数不成立。"""
        r = run_dcf(pe_ttm=-10.0, pb=3.2, total_market_value_cny=1e9)
        self._assert_rejected(r, "PE_TTM")

    def test_reject_zero_pe(self):
        r = run_dcf(pe_ttm=0.0, pb=3.2, total_market_value_cny=1e9)
        self._assert_rejected(r, "PE_TTM")

    def test_reject_missing_pe(self):
        r = run_dcf(pe_ttm=None, pb=3.2, total_market_value_cny=1e9)
        self._assert_rejected(r, "PE_TTM")

    def test_reject_negative_pb(self):
        """净资产为负，DCF 不适用。"""
        r = run_dcf(pe_ttm=20.0, pb=-1.5, total_market_value_cny=1e9)
        self._assert_rejected(r, "PB")

    def test_reject_missing_pb(self):
        r = run_dcf(pe_ttm=20.0, pb=None, total_market_value_cny=1e9)
        self._assert_rejected(r, "PB")

    def test_reject_zero_market_value(self):
        r = run_dcf(pe_ttm=20.0, pb=2.0, total_market_value_cny=0.0)
        self._assert_rejected(r, "市值")

    def test_reject_missing_market_value(self):
        r = run_dcf(pe_ttm=20.0, pb=2.0, total_market_value_cny=None)
        self._assert_rejected(r, "市值")

    def test_reject_wacc_not_above_terminal_growth(self):
        """WACC ≤ 永续增长率时 Gordon 公式退化，必须拒绝而非输出天价。"""
        a = DCFAssumptions(
            risk_free_rate=0.01, equity_risk_premium=0.01, beta=1.0,
            terminal_growth=0.05,
        )
        r = run_dcf(**BASE, assumptions=a)
        self._assert_rejected(r, "Gordon")

    def test_reject_forecast_years_too_small(self):
        a = DCFAssumptions(forecast_years=0)
        r = run_dcf(**BASE, assumptions=a)
        self._assert_rejected(r, "预测年限")

    def test_reject_forecast_years_too_large(self):
        a = DCFAssumptions(forecast_years=99)
        r = run_dcf(**BASE, assumptions=a)
        self._assert_rejected(r, "预测年限")


# ═══════════════════════════════════════════════════════════════
class TestWarnings:
    """警告是非阻断的，但必须出现，让使用者知道假设的薄弱处。"""

    def test_warns_when_implied_roe_far_from_reported(self):
        """隐含 ROE 与披露 ROE 偏离过大 → 警告（数据口径不一致）。"""
        r = run_dcf(**BASE, roe_pct=18.5)   # 隐含 12.5 vs 披露 18.5
        assert any("ROE" in w for w in r.warnings), r.warnings

    def test_no_roe_warning_when_consistent(self):
        r = run_dcf(**BASE, roe_pct=12.5)
        assert not any("披露" in w and "ROE" in w for w in r.warnings), r.warnings

    def test_warns_when_net_debt_unknown(self):
        r = run_dcf(**BASE)
        assert any("净债务" in w for w in r.warnings), r.warnings

    def test_no_net_debt_warning_when_provided(self):
        r = run_dcf(**BASE, assumptions=DCFAssumptions(net_debt_cny=1e8))
        assert not any("净债务未知" in w for w in r.warnings), r.warnings

    def test_net_debt_reduces_equity_value(self):
        no_debt = run_dcf(**BASE, assumptions=DCFAssumptions(net_debt_cny=0.0))
        with_debt = run_dcf(**BASE, assumptions=DCFAssumptions(net_debt_cny=2e8))
        assert with_debt.equity_value_cny == pytest.approx(
            no_debt.equity_value_cny - 2e8, rel=1e-9
        )

    def test_warns_on_default_beta(self):
        r = run_dcf(**BASE)
        assert any("β" in w or "beta" in w.lower() for w in r.warnings), r.warnings

    def test_warns_when_terminal_value_dominates(self):
        """终值占比过高说明结果高度依赖永续假设，必须提示。"""
        r = run_dcf(**BASE)
        share = r.pv_terminal_cny / r.enterprise_value_cny
        if share > 0.75:
            assert any("终值" in w for w in r.warnings), r.warnings


# ═══════════════════════════════════════════════════════════════
class TestValueDecomposition:
    def test_ev_equals_explicit_plus_terminal(self):
        r = run_dcf(**BASE)
        assert r.enterprise_value_cny == pytest.approx(
            r.pv_explicit_cny + r.pv_terminal_cny, rel=1e-9
        )

    def test_explicit_pv_equals_sum_of_yearly_pv(self):
        r = run_dcf(**BASE)
        total = sum(row["present_value_cny"] for row in r.yearly_projection)
        assert r.pv_explicit_cny == pytest.approx(total, rel=1e-9)

    def test_margin_of_safety_definition(self):
        """安全边际 = (内在价值 / 市值 − 1) × 100。"""
        r = run_dcf(**BASE)
        expected = (r.equity_value_cny / r.market_value_cny - 1) * 100
        assert r.margin_of_safety_pct == pytest.approx(expected, abs=1e-4)

    def test_all_components_positive_for_profitable_case(self):
        r = run_dcf(**BASE)
        assert r.pv_explicit_cny > 0
        assert r.pv_terminal_cny > 0
        assert r.enterprise_value_cny > 0
        assert math.isfinite(r.equity_value_cny)


# ═══════════════════════════════════════════════════════════════
class TestBetaFromVolatility:
    def test_none_returns_default_one(self):
        assert beta_from_volatility(None) == pytest.approx(1.0)

    def test_low_volatility_clamped_to_floor(self):
        assert beta_from_volatility(1.0) == pytest.approx(0.5)

    def test_high_volatility_clamped_to_ceiling(self):
        assert beta_from_volatility(500.0) == pytest.approx(2.0)

    def test_monotonic_in_volatility(self):
        a = beta_from_volatility(15.0)
        b = beta_from_volatility(35.0)
        assert a < b

    def test_formula_within_band(self):
        """β = ρ × σ_stock / σ_market，未触及钳制时应等于公式值。"""
        beta = beta_from_volatility(30.0, market_volatility_pct=20.0, correlation=0.6)
        assert beta == pytest.approx(0.6 * 30.0 / 20.0, rel=1e-9)


# ═══════════════════════════════════════════════════════════════
class TestSerialization:
    REQUIRED_KEYS = (
        "applicable", "reason_not_applicable", "net_income_cny", "book_value_cny",
        "fcf_base_cny", "implied_roe_pct", "wacc", "growth_stage1", "terminal_growth",
        "pv_explicit_cny", "pv_terminal_cny", "enterprise_value_cny",
        "equity_value_cny", "market_value_cny", "margin_of_safety_pct",
        "valuation_signal", "yearly_projection", "assumptions", "formula", "warnings",
    )

    def test_to_dict_has_all_keys(self):
        d = run_dcf(**BASE).to_dict()
        for key in self.REQUIRED_KEYS:
            assert key in d, f"to_dict 缺少字段 {key}"

    def test_to_dict_on_rejected_result(self):
        d = run_dcf(pe_ttm=None, pb=None, total_market_value_cny=None).to_dict()
        assert d["applicable"] is False
        assert d["reason_not_applicable"]

    def test_assumptions_are_recorded(self):
        """输入假设必须可审计地随结果一起返回。"""
        d = run_dcf(**BASE, assumptions=DCFAssumptions(beta=1.3)).to_dict()
        assert d["assumptions"]["beta"] == pytest.approx(1.3)
        assert "risk_free_rate" in d["assumptions"]
        assert "payout_ratio" in d["assumptions"]

    def test_proxy_is_never_labelled_as_full_financial_statement_dcf(self):
        d = run_dcf(**BASE).to_dict()
        assert d["model_type"] == "earnings_to_fcff_proxy"
        assert d["confidence_level"] == "low"
        assert "非完整现金流量表" in d["source"]
        assert d["limitations"]

    def test_formula_is_explained(self):
        """可解释性要求：公式必须以文本形式给出。"""
        r = run_dcf(**BASE)
        assert "WACC" in r.formula
        assert len(r.formula) > 40

    def test_terminal_growth_echoed(self):
        r = run_dcf(**BASE)
        assert r.terminal_growth == pytest.approx(DEFAULT_TERMINAL_GROWTH)

    def test_to_markdown_contains_key_sections(self):
        md = run_dcf(**BASE).to_markdown()
        assert "DCF" in md
        assert "WACC" in md

    def test_to_markdown_on_rejected_result(self):
        md = run_dcf(pe_ttm=-1.0, pb=1.0, total_market_value_cny=1e9).to_markdown()
        assert isinstance(md, str) and md.strip()

    def test_assumptions_to_dict_roundtrip(self):
        a = DCFAssumptions(beta=1.1, forecast_years=6)
        d = a.to_dict()
        assert d["beta"] == pytest.approx(1.1)
        assert d["forecast_years"] == 6


# ═══════════════════════════════════════════════════════════════
class TestFundamentalAgentIntegration:
    """DCF 必须真正接入基本面 Agent，而不是一个没人调用的模块。"""

    def test_offline_fundamental_carries_dcf(self):
        from agent_platform.finance.fundamental_agent import analyze_fundamental

        r = analyze_fundamental("DEMO001", force_offline=True)
        assert r.dcf is not None, "基本面结果未携带 DCF"
        assert r.dcf["applicable"] is True
        assert r.dcf["equity_value_cny"] > 0
        assert r.dcf["formula"]

    def test_offline_fundamental_carries_debt_ratio(self):
        from agent_platform.finance.fundamental_agent import analyze_fundamental

        r = analyze_fundamental("DEMO001", force_offline=True)
        assert r.debt_to_asset_pct is not None, "缺少资产负债率"
        assert 0 <= r.debt_to_asset_pct <= 100

    def test_dcf_differs_across_symbols(self):
        """不同标的的 DCF 结果必须不同（再次反常数）。"""
        from agent_platform.finance.fundamental_agent import analyze_fundamental

        vals = {
            s: analyze_fundamental(s, force_offline=True).dcf["margin_of_safety_pct"]
            for s in ("DEMO001", "DEMO002", "DEMO003", "DEMO004")
        }
        assert len(set(vals.values())) == 4, f"跨标的 DCF 结果重复: {vals}"

    def test_dcf_in_to_dict_payload(self):
        from agent_platform.finance.fundamental_agent import analyze_fundamental

        d = analyze_fundamental("DEMO002", force_offline=True).to_dict()
        assert "dcf" in d and "debt_to_asset_pct" in d
