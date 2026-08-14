"""
测试 Phase 3 Agents：
  - SynthesisAgent
  - TraderAgent
  - RiskManagerAgent
  - TradingHarness
"""
from __future__ import annotations

import pytest

# ─────────────────────── SynthesisAgent ───────────────────────────

from agent_platform.finance.synthesis_agent import (
    SYNTHESIS_SCHEMA,
    SynthesisResult,
    _score_technical,
    _score_fundamental,
    _score_industry,
    _score_regime,
    synthesize,
)


class TestScoreFunctions:
    def test_score_technical_rsi_oversold(self):
        tech = {"latest_rsi": 25.0}
        score, bulls, bears = _score_technical(tech)
        assert score > 0
        assert len(bulls) > 0
        assert "超卖" in bulls[0]

    def test_score_technical_rsi_overbought(self):
        tech = {"latest_rsi": 75.0}
        score, bulls, bears = _score_technical(tech)
        assert score < 0
        assert len(bears) > 0

    def test_score_fundamental_undervalued(self):
        fund = {"valuation_signal": "undervalued", "valuation_note": "PE低"}
        score, bulls, bears = _score_fundamental(fund)
        assert score > 0
        assert len(bulls) > 0

    def test_score_fundamental_overvalued(self):
        fund = {"valuation_signal": "overvalued", "valuation_note": "PE高"}
        score, bulls, bears = _score_fundamental(fund)
        assert score < 0
        assert len(bears) > 0

    def test_score_industry_booming(self):
        ind = {"prosperity_signal": "booming", "industry_name": "银行"}
        score, bulls, bears = _score_industry(ind)
        assert score > 0

    def test_score_regime_bull(self):
        regime = {"regime": "bull", "regime_note": "5日大涨"}
        score, bulls, bears = _score_regime(regime)
        assert score > 0

    def test_score_regime_bear(self):
        regime = {"regime": "bear", "regime_note": "5日暴跌"}
        score, bulls, bears = _score_regime(regime)
        assert score < 0


class TestSynthesize:
    def _make_inputs(self):
        tech = {
            "latest_close": 10.0,
            "latest_rsi": 50.0,
            "latest_macd": 0.1,
            "latest_macd_signal": 0.05,
            "latest_ma5": 9.8,
            "latest_ma20": 9.5,
        }
        fund = {"valuation_signal": "undervalued", "valuation_note": "PE低"}
        ind = {"prosperity_signal": "booming", "industry_name": "科技"}
        regime = {"regime": "bull", "regime_note": "牛市"}
        return tech, fund, ind, regime

    def test_synthesize_returns_result(self):
        tech, fund, ind, regime = self._make_inputs()
        result = synthesize("000001", tech, fund, ind, regime)
        assert isinstance(result, SynthesisResult)

    def test_synthesize_confidence_in_range(self):
        tech, fund, ind, regime = self._make_inputs()
        result = synthesize("000001", tech, fund, ind, regime)
        assert 0.0 <= result.confidence <= 1.0

    def test_synthesize_signal_valid(self):
        tech, fund, ind, regime = self._make_inputs()
        result = synthesize("000001", tech, fund, ind, regime)
        assert result.signal in ("buy", "sell", "hold", "watch")

    def test_synthesize_has_bull_bear_args(self):
        tech, fund, ind, regime = self._make_inputs()
        result = synthesize("000001", tech, fund, ind, regime)
        # 应该有多方论据（因为全是利好信号）
        assert len(result.bull_arguments) > 0

    def test_synthesize_to_dict_schema_fields(self):
        tech, fund, ind, regime = self._make_inputs()
        result = synthesize("000001", tech, fund, ind, regime)
        d = result.to_dict()
        for req in SYNTHESIS_SCHEMA["required"]:
            assert req in d

    def test_synthesize_bear_scenario(self):
        tech = {"latest_close": 10.0, "latest_rsi": 80.0}
        fund = {"valuation_signal": "overvalued", "valuation_note": "高估"}
        ind = {"prosperity_signal": "sluggish", "industry_name": "煤炭"}
        regime = {"regime": "bear", "regime_note": "熊市"}
        result = synthesize("000001", tech, fund, ind, regime)
        # 全是利空 → 置信度应偏低 → signal 应为 sell
        assert result.confidence < 0.50
        assert result.signal == "sell"


# ─────────────────────── TraderAgent ──────────────────────────────

from agent_platform.finance.trader_agent import (
    TRADER_SCHEMA,
    TraderResult,
    _calc_position,
    generate_trade_signal,
)


class TestCalcPosition:
    def test_sell_returns_zero(self):
        pos = _calc_position("sell", 0.8, "bull")
        assert pos == 0.0

    def test_hold_returns_zero(self):
        pos = _calc_position("hold", 0.8, "bull")
        assert pos == 0.0

    def test_buy_bull_boosts(self):
        pos_bull = _calc_position("buy", 0.8, "bull")
        pos_normal = _calc_position("buy", 0.8, "consolidation")
        assert pos_bull >= pos_normal

    def test_buy_bear_reduces(self):
        pos_bear = _calc_position("buy", 0.8, "bear")
        pos_normal = _calc_position("buy", 0.8, "consolidation")
        assert pos_bear <= pos_normal


class TestGenerateTradeSignal:
    def test_returns_trader_result(self):
        synthesis = {"symbol": "000001", "signal": "buy", "confidence": 0.7}
        regime = {"regime": "bull"}
        result = generate_trade_signal(synthesis, regime)
        assert isinstance(result, TraderResult)

    def test_to_dict_has_required_keys(self):
        synthesis = {"symbol": "000001", "signal": "buy", "confidence": 0.7}
        regime = {"regime": "bull"}
        result = generate_trade_signal(synthesis, regime)
        d = result.to_dict()
        for req in TRADER_SCHEMA["required"]:
            assert req in d

    def test_stop_loss_with_technical(self):
        synthesis = {"symbol": "000001", "signal": "buy", "confidence": 0.7}
        regime = {"regime": "bull"}
        technical = {"latest_close": 10.0, "latest_atr": 0.5}
        result = generate_trade_signal(synthesis, regime, technical)
        assert result.stop_loss_price is not None
        assert result.stop_loss_price < 10.0
        assert result.take_profit_price is not None
        assert result.take_profit_price > 10.0

    def test_human_approval_not_raised_under_limit(self):
        synthesis = {"symbol": "000001", "signal": "buy", "confidence": 0.6}
        regime = {"regime": "consolidation"}
        # 置信度 0.6 → position ~ 6%，低于 10% 阈值
        result = generate_trade_signal(synthesis, regime)
        assert result.position_pct_suggestion <= 10.0


# ─────────────────────── RiskManagerAgent ────────────────────────

from agent_platform.finance.risk_manager_agent import (
    RISK_MANAGER_SCHEMA,
    RiskManagerResult,
    assess_risk,
)


class TestAssessRisk:
    def test_returns_risk_manager_result(self):
        trader = {
            "symbol": "000001",
            "signal": "buy",
            "position_pct_suggestion": 5.0,
            "entry_price": 100.0,
            "stop_loss_price": 90.0, "take_profit_price": 120.0,
        }
        result = assess_risk(trader)
        assert isinstance(result, RiskManagerResult)

    def test_to_dict_has_required_keys(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 5.0,
                  "entry_price": 100.0, "stop_loss_price": 90.0, "take_profit_price": 120.0}
        result = assess_risk(trader)
        d = result.to_dict()
        for req in RISK_MANAGER_SCHEMA["required"]:
            assert req in d

    def test_position_capped_at_max_single(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 8.0,
                  "entry_price": 100.0, "stop_loss_price": 90.0, "take_profit_price": 120.0}
        result = assess_risk(trader, max_single_position_pct=2.0)
        assert result.approved_position_pct == 2.0
        assert len(result.risk_flags) > 0

    def test_industry_concentration_reduces_position(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 2.0,
                  "entry_price": 100.0, "stop_loss_price": 90.0, "take_profit_price": 120.0}
        result = assess_risk(trader, current_industry_position_pct=29.0, max_industry_pct=30.0)
        # 当前 29% + 建议 2% = 31% > 30% → 应削减
        assert result.approved_position_pct < 2.0

    def test_drawdown_triggers_reduce_signal(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 2.0,
                  "entry_price": 100.0, "stop_loss_price": 90.0, "take_profit_price": 120.0}
        result = assess_risk(trader, current_drawdown_pct=20.0, max_drawdown_pct=15.0)
        assert result.final_signal == "reduce"
        assert result.approved_position_pct == 0.0

    def test_no_risk_flags_when_all_pass(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 1.5,
                  "entry_price": 100.0, "stop_loss_price": 90.0, "take_profit_price": 120.0}
        result = assess_risk(trader)
        assert len(result.risk_flags) == 0

    def test_loss_budget_caps_position_not_raw_position(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 50.0,
                  "entry_price": 100.0, "stop_loss_price": 90.0, "take_profit_price": 120.0}
        result = assess_risk(trader)
        assert result.approved_position_pct == 20.0
        assert result.stop_distance_pct == 10.0
        assert result.estimated_loss_pct == 2.0
        assert result.risk_budget_pct == 2.0

    def test_missing_stop_loss_blocks_auto_position(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 5.0,
                  "entry_price": 100.0, "stop_loss_price": None, "take_profit_price": 120.0}
        result = assess_risk(trader)
        assert result.approved_position_pct == 0.0
        assert any("无法计算单笔亏损" in flag for flag in result.risk_flags)

    def test_missing_take_profit_blocks_auto_position(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 5.0,
                  "entry_price": 100.0, "stop_loss_price": 90.0, "take_profit_price": None}
        result = assess_risk(trader)
        assert result.approved_position_pct == 0.0
        assert any("缺少有效止盈价" in flag for flag in result.risk_flags)

    def test_low_risk_reward_ratio_blocks_auto_position(self):
        trader = {"symbol": "000001", "signal": "buy", "position_pct_suggestion": 5.0,
                  "entry_price": 100.0, "stop_loss_price": 90.0, "take_profit_price": 110.0}
        result = assess_risk(trader)
        assert result.approved_position_pct == 0.0
        assert result.risk_reward_ratio == 1.0
        assert any("风险收益比" in flag for flag in result.risk_flags)

    def test_loss_budget_rejects_non_positive_limit(self):
        trader = {"symbol": "000001", "signal": "hold", "position_pct_suggestion": 0.0}
        with pytest.raises(ValueError, match="max_loss_pct"):
            assess_risk(trader, max_loss_pct=0.0)


# ─────────────────────── TradingHarness ───────────────────────────

from agent_platform.finance.trading_harness import (
    TradingHarness,
    TradingHarnessResult,
)


class TestTradingHarness:
    def _make_inputs(self):
        synthesis = {"symbol": "000001", "signal": "buy", "confidence": 0.7}
        trader = {
            "symbol": "000001",
            "signal": "buy",
            "position_pct_suggestion": 5.0,
            "entry_price": 100.0,
            "stop_loss_price": 90.0,
            "take_profit_price": 120.0,
            "rationale": "技术面向好",
            "source": "trader",
            "updated_at": "2026-01-01T00:00:00Z",
            "disclaimer": "仅供参考",
        }
        risk = {
            "symbol": "000001",
            "approved_position_pct": 5.0,
            "stop_loss_price": 90.0,
            "take_profit_price": 120.0,
            "risk_reward_ratio": 2.0,
            "final_signal": "buy",
            "source": "risk_manager",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        return synthesis, trader, risk

    def test_run_preflight_returns_result(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        result = harness.run_preflight(synthesis, trader, risk)
        assert isinstance(result, TradingHarnessResult)

    def test_all_checks_pass_approved_true(self):
        harness = TradingHarness(min_confidence=0.5)
        synthesis, trader, risk = self._make_inputs()
        result = harness.run_preflight(synthesis, trader, risk)
        assert result.approved is True
        assert result.final_action == "execute"

    def test_confidence_below_threshold_fails(self):
        harness = TradingHarness(min_confidence=0.8)
        synthesis = {"symbol": "000001", "signal": "buy", "confidence": 0.6}
        _, trader, risk = self._make_inputs()
        result = harness.run_preflight(synthesis, trader, risk)
        assert result.approved is False
        confidence_check = [c for c in result.checks if c.check_name == "置信度阈值"][0]
        assert confidence_check.passed is False

    def test_keyword_blocker_detects_violation(self):
        harness = TradingHarness(enable_keyword_check=True)
        synthesis, trader, risk = self._make_inputs()
        trader["rationale"] = "绝对稳赚的机会"
        result = harness.run_preflight(synthesis, trader, risk)
        keyword_check = [c for c in result.checks if c.check_name == "违禁词拦截"][0]
        assert keyword_check.passed is False

    def test_drawdown_protection_blocks_execution(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        risk["final_signal"] = "reduce"
        result = harness.run_preflight(synthesis, trader, risk)
        assert result.final_action == "block"
        drawdown_check = [c for c in result.checks if c.check_name == "回撤保护"][0]
        assert drawdown_check.passed is False

    def test_to_markdown_includes_all_checks(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        result = harness.run_preflight(synthesis, trader, risk)
        md = result.to_markdown()
        assert "Pre-Flight Checklist" in md
        assert "数据溯源" in md
        assert "违禁词拦截" in md
        assert "仓位合规" in md
        assert "Schema 有效性" in md
        assert "置信度阈值" in md
        assert "回撤保护" in md
        assert "交易时段" in md
        assert "流动性" in md
        assert "止盈止损" in md

    def test_low_evaluator_score_requires_manual_review(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        result = harness.run_preflight(
            synthesis, trader, risk,
            evaluator_summary={
                "minimum_score": 70.0,
                "requires_manual_review": True,
            },
        )
        assert result.final_action == "manual_review"
        check = next(c for c in result.checks if c.check_name == "独立质量评估")
        assert check.passed is False

    def test_execution_context_checks_trading_hours_and_liquidity(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        live = {"data_status": "live"}
        result = harness.run_preflight(
            synthesis, trader, risk, live, live, live, live,
            execution_context={
                "as_of": "2026-08-12T10:00:00+08:00",
                "latest_volume": 1_000_000,
                "latest_close": 10.0,
            },
        )
        assert result.final_action == "execute"
        assert next(c for c in result.checks if c.check_name == "交易时段").passed
        assert next(c for c in result.checks if c.check_name == "流动性").passed

    def test_missing_liquidity_requires_manual_review(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        live = {"data_status": "live"}
        result = harness.run_preflight(
            synthesis, trader, risk, live, live, live, live,
            execution_context={"as_of": "2026-08-12T10:00:00+08:00"},
        )
        assert result.final_action == "manual_review"
        assert not next(c for c in result.checks if c.check_name == "流动性").passed

    def test_outside_trading_hours_requires_manual_review(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        live = {"data_status": "live"}
        result = harness.run_preflight(
            synthesis, trader, risk, live, live, live, live,
            execution_context={
                "as_of": "2026-08-12T20:00:00+08:00",
                "latest_volume": 1_000_000,
                "latest_close": 10.0,
            },
        )
        assert result.final_action == "manual_review"
        assert not next(c for c in result.checks if c.check_name == "交易时段").passed

    def test_live_agent_data_can_execute(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        live = {"data_status": "live"}
        result = harness.run_preflight(
            synthesis, trader, risk, live, live, live, live,
        )
        assert result.final_action == "execute"
        assert result.data_quality_summary["passed"] is True
        assert result.data_quality_summary["counts"]["live"] == 4

    def test_offline_agent_data_requires_manual_review(self):
        harness = TradingHarness()
        synthesis, trader, risk = self._make_inputs()
        offline = {"data_status": "offline_sample"}
        result = harness.run_preflight(
            synthesis, trader, risk, offline, offline, offline, offline,
        )
        assert result.final_action == "manual_review"
        assert result.data_quality_summary["passed"] is False
        assert result.data_quality_summary["counts"]["offline_sample"] == 4
