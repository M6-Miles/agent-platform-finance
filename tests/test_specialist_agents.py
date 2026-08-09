"""
测试 Phase 2 Specialist Agents：
  - FundamentalAgent (fundamental_agent.py)
  - IndustryAgent    (industry_agent.py)
  - MarketRegimeAgent(market_regime_agent.py)

AkShare 调用全部 monkeypatch，保证离线可运行。
"""
from __future__ import annotations

import pytest

# ─────────────────────── FundamentalAgent ────────────────────────

from agent_platform.finance.fundamental_agent import (
    FUNDAMENTAL_SCHEMA,
    FundamentalResult,
    _valuation_signal,
    analyze_fundamental,
)


class TestValuationSignal:
    def test_undervalued(self):
        signal, note = _valuation_signal(10.0, 1.5)
        assert signal == "undervalued"
        assert "低估" in note or "undervalued" in note.lower() or note != ""

    def test_overvalued_pe(self):
        signal, _ = _valuation_signal(50.0, 2.0)
        assert signal == "overvalued"

    def test_overvalued_pb(self):
        signal, _ = _valuation_signal(20.0, 6.0)
        assert signal == "overvalued"

    def test_fairly_valued(self):
        signal, _ = _valuation_signal(20.0, 2.5)
        assert signal == "fairly_valued"

    def test_unknown_when_none(self):
        signal, _ = _valuation_signal(None, None)
        assert signal == "unknown"

    def test_fairly_valued_when_pe_none_pb_ok(self):
        # PE 缺失但 PB 可用 → 仍可判断，不应返回 unknown
        signal, _ = _valuation_signal(None, 1.5)
        assert signal == "fairly_valued"


class TestFundamentalResult:
    def _make(self, **kw) -> FundamentalResult:
        defaults = dict(
            symbol="000001",
            name="平安银行",
            source="akshare/test",
            updated_at="2026-01-01T00:00:00Z",
            pe_ttm=12.0,
            pb=1.2,
            total_market_value_cny=3e11,
            roe_pct=10.5,
            valuation_signal="undervalued",
            valuation_note="PE 低估",
            disclaimer="仅供研究参考",
            data_status="offline_sample",
            fallback_reason=None,
        )
        defaults.update(kw)
        return FundamentalResult(**defaults)

    def test_to_dict_has_required_keys(self):
        r = self._make()
        d = r.to_dict()
        for k in ("symbol", "source", "updated_at", "valuation_signal"):
            assert k in d

    def test_to_markdown_contains_symbol(self):
        r = self._make()
        md = r.to_markdown()
        assert "000001" in md

    def test_to_markdown_contains_disclaimer(self):
        r = self._make()
        assert "仅供研究参考" in r.to_markdown()

    def test_to_markdown_contains_pe_pb(self):
        r = self._make(pe_ttm=12.0, pb=1.2)
        md = r.to_markdown()
        assert "12.0" in md   # PE 格式化为 :.1f
        assert "1.20" in md   # PB 格式化为 :.2f

    def test_to_dict_none_values_preserved(self):
        r = self._make(pe_ttm=None, pb=None)
        d = r.to_dict()
        assert d["pe_ttm"] is None

    def test_schema_required_fields_present(self):
        r = self._make()
        d = r.to_dict()
        for req in FUNDAMENTAL_SCHEMA["required"]:
            assert req in d


class TestAnalyzeFundamental:
    def test_returns_fundamental_result(self, monkeypatch):
        monkeypatch.setattr(
            "agent_platform.finance.fundamental_agent.ak",
            None,
            raising=False,
        )
        # AkShare 不可用时应降级到 sample 数据
        result = analyze_fundamental("000001", "平安银行")
        assert isinstance(result, FundamentalResult)

    def test_sample_source_on_akshare_failure(self, monkeypatch):
        import builtins
        real_import = builtins.__import__
        def mock_import(name, *args, **kw):
            if name == "akshare":
                raise ImportError("mock akshare unavailable")
            return real_import(name, *args, **kw)
        monkeypatch.setattr(builtins, "__import__", mock_import)
        result = analyze_fundamental("000001")
        assert result.source != ""

    def test_result_has_disclaimer(self, monkeypatch):
        result = analyze_fundamental("DEMO")
        assert "仅供研究参考" in result.disclaimer

    def test_valuation_signal_valid_enum(self, monkeypatch):
        result = analyze_fundamental("DEMO")
        assert result.valuation_signal in ("undervalued", "fairly_valued", "overvalued", "unknown")

    def test_updated_at_is_nonempty(self, monkeypatch):
        result = analyze_fundamental("DEMO")
        assert result.updated_at != ""

    def test_schema_valid_output(self, monkeypatch):
        """to_dict() 满足 FUNDAMENTAL_SCHEMA required 字段。"""
        result = analyze_fundamental("DEMO")
        d = result.to_dict()
        for req in FUNDAMENTAL_SCHEMA["required"]:
            assert req in d, f"缺少必填字段: {req}"


# ─────────────────────── IndustryAgent ───────────────────────────

from agent_platform.finance.industry_agent import (
    INDUSTRY_SCHEMA,
    IndustryResult,
    _prosperity_from_fund_flow,
    _guess_industry,
    analyze_industry,
)


class TestProsperityFromFundFlow:
    def test_booming(self):
        signal, note = _prosperity_from_fund_flow(8e8)
        assert signal == "booming"
        assert "流入" in note

    def test_sluggish(self):
        signal, note = _prosperity_from_fund_flow(-9e8)
        assert signal == "sluggish"
        assert "流出" in note

    def test_normal_small_positive(self):
        signal, _ = _prosperity_from_fund_flow(1e8)
        assert signal == "normal"

    def test_normal_small_negative(self):
        signal, _ = _prosperity_from_fund_flow(-1e8)
        assert signal == "normal"

    def test_none_returns_unknown(self):
        signal, note = _prosperity_from_fund_flow(None)
        assert signal == "unknown"
        assert "不可用" in note


class TestGuessIndustry:
    def test_60_prefix(self):
        ind = _guess_industry("600519")
        assert "金融" in ind or "工业" in ind

    def test_30_prefix(self):
        ind = _guess_industry("300750")
        assert "科技" in ind or "创业" in ind

    def test_unknown_prefix(self):
        ind = _guess_industry("999999")
        assert "未知" in ind


class TestIndustryResult:
    def _make(self, **kw) -> IndustryResult:
        defaults = dict(
            symbol="000001",
            industry_name="银行",
            source="akshare/test",
            updated_at="2026-01-01T00:00:00Z",
            prosperity_signal="booming",
            prosperity_note="资金净流入",
            top_stocks=[{"rank": 1, "code": "000001", "name": "平安银行", "change_pct": 2.5}],
            fund_flow_3d_cny=6e8,
            disclaimer="仅供研究参考",
            data_status="offline_sample",
            fallback_reason=None,
        )
        defaults.update(kw)
        return IndustryResult(**defaults)

    def test_to_dict_has_required_keys(self):
        r = self._make()
        d = r.to_dict()
        for k in INDUSTRY_SCHEMA["required"]:
            assert k in d

    def test_to_markdown_contains_industry_name(self):
        r = self._make()
        assert "银行" in r.to_markdown()

    def test_to_markdown_shows_top_stock(self):
        r = self._make()
        assert "平安银行" in r.to_markdown()

    def test_to_markdown_no_top_stocks(self):
        r = self._make(top_stocks=[])
        md = r.to_markdown()
        assert "数据不可用" in md

    def test_fund_flow_none_in_markdown(self):
        r = self._make(fund_flow_3d_cny=None)
        assert "N/A" in r.to_markdown()


class TestAnalyzeIndustry:
    def test_returns_industry_result(self):
        result = analyze_industry("DEMO")
        assert isinstance(result, IndustryResult)

    def test_has_required_schema_fields(self):
        result = analyze_industry("DEMO")
        d = result.to_dict()
        for req in INDUSTRY_SCHEMA["required"]:
            assert req in d

    def test_prosperity_signal_valid_enum(self):
        result = analyze_industry("DEMO")
        assert result.prosperity_signal in ("booming", "normal", "sluggish", "unknown")

    def test_disclaimer_present(self):
        result = analyze_industry("DEMO")
        assert "仅供研究参考" in result.disclaimer

    def test_updated_at_nonempty(self):
        result = analyze_industry("DEMO")
        assert result.updated_at != ""


# ─────────────────────── MarketRegimeAgent ───────────────────────

from agent_platform.finance.market_regime_agent import (
    MARKET_REGIME_SCHEMA,
    MarketRegimeResult,
    _determine_regime,
    analyze_market_regime,
)


class TestDetermineRegime:
    def test_bull_large_gain(self):
        regime, _, _ = _determine_regime(5.0, None)
        assert regime == "bull"

    def test_bear_large_loss(self):
        regime, _, _ = _determine_regime(-5.0, None)
        assert regime == "bear"

    def test_consolidation_small_move(self):
        regime, _, _ = _determine_regime(1.0, None)
        assert regime == "consolidation"

    def test_unknown_no_data(self):
        regime, _, _ = _determine_regime(None, None)
        assert regime == "unknown"

    def test_high_risk_appetite_northbound_positive(self):
        _, risk, _ = _determine_regime(1.0, 8e8)
        assert risk == "high"

    def test_low_risk_appetite_northbound_negative(self):
        _, risk, _ = _determine_regime(1.0, -8e8)
        assert risk == "low"

    def test_medium_risk_appetite_small_northbound(self):
        _, risk, _ = _determine_regime(0.5, 1e8)
        assert risk == "medium"


class TestMarketRegimeResult:
    def _make(self, **kw) -> MarketRegimeResult:
        defaults = dict(
            regime="bull",
            risk_appetite="high",
            index_code="sh000001",
            index_close=3200.0,
            index_change_pct_5d=4.5,
            northbound_flow_cny=9e8,
            regime_note="5日大涨",
            source="akshare/stock_zh_index_daily",
            updated_at="2026-01-01T00:00:00Z",
            disclaimer="仅供研究参考",
            data_status="offline_sample",
            fallback_reason=None,
        )
        defaults.update(kw)
        return MarketRegimeResult(**defaults)

    def test_to_dict_required_keys(self):
        r = self._make()
        d = r.to_dict()
        for k in MARKET_REGIME_SCHEMA["required"]:
            assert k in d

    def test_to_markdown_contains_index_code(self):
        r = self._make()
        assert "sh000001" in r.to_markdown()

    def test_to_markdown_none_close(self):
        r = self._make(index_close=None, index_change_pct_5d=None)
        md = r.to_markdown()
        assert "N/A" in md

    def test_to_markdown_northbound_none(self):
        r = self._make(northbound_flow_cny=None)
        assert "N/A" in r.to_markdown()

    def test_bull_label_in_markdown(self):
        r = self._make(regime="bull")
        assert "牛市" in r.to_markdown()

    def test_bear_label_in_markdown(self):
        r = self._make(regime="bear")
        assert "熊市" in r.to_markdown()


class TestAnalyzeMarketRegime:
    def test_returns_market_regime_result(self):
        result = analyze_market_regime()
        assert isinstance(result, MarketRegimeResult)

    def test_schema_required_fields(self):
        result = analyze_market_regime()
        d = result.to_dict()
        for req in MARKET_REGIME_SCHEMA["required"]:
            assert req in d

    def test_regime_valid_enum(self):
        result = analyze_market_regime()
        assert result.regime in ("bull", "bear", "consolidation", "unknown")

    def test_risk_appetite_valid_enum(self):
        result = analyze_market_regime()
        assert result.risk_appetite in ("high", "medium", "low", "unknown")

    def test_disclaimer_present(self):
        result = analyze_market_regime()
        assert "仅供研究参考" in result.disclaimer

    def test_updated_at_nonempty(self):
        result = analyze_market_regime()
        assert result.updated_at != ""

    def test_custom_index_code(self):
        result = analyze_market_regime(index_code="sh000300")
        assert result.index_code == "sh000300"


# ─────────────────────── TraderAgent ─────────────────────────────

from agent_platform.finance.trader_agent import (
    _calc_position,
    generate_trade_signal,
    HumanApprovalRequired,
    _MAX_AUTO_POSITION_PCT,
)


class TestCalcPosition:
    def test_sell_returns_zero(self):
        assert _calc_position("sell", 0.9, "bull") == 0.0

    def test_hold_returns_zero(self):
        assert _calc_position("hold", 0.9, "bull") == 0.0

    def test_bull_multiplier_can_exceed_max(self):
        """bull 加成后仓位可超过 _MAX_AUTO_POSITION_PCT（由调用方拦截）。"""
        pos = _calc_position("buy", 1.0, "bull")
        assert pos > _MAX_AUTO_POSITION_PCT

    def test_bear_reduces_position(self):
        base = _calc_position("buy", 0.8, "consolidation")
        bear = _calc_position("buy", 0.8, "bear")
        assert bear < base

    def test_normal_buy_within_max(self):
        pos = _calc_position("buy", 0.8, "consolidation")
        assert pos <= _MAX_AUTO_POSITION_PCT


class TestHumanApprovalRequired:
    def _regime(self, r="bull"):
        return {"regime": r, "risk_appetite": "high", "source": "test",
                "updated_at": "2026-01-01T00:00:00Z"}

    def _synthesis(self, signal="buy", confidence=1.0):
        return {"symbol": "600519", "signal": signal, "confidence": confidence,
                "source": "test", "updated_at": "2026-01-01T00:00:00Z"}

    def test_high_confidence_bull_raises(self):
        """confidence=1.0 + bull → 仓位 12% > 阈值，必须抛出 HumanApprovalRequired。"""
        with pytest.raises(HumanApprovalRequired):
            generate_trade_signal(
                synthesis=self._synthesis(signal="buy", confidence=1.0),
                regime=self._regime("bull"),
            )

    def test_low_confidence_no_raise(self):
        """confidence=0.5 + bull → 仓位 6% ≤ 阈值，正常返回。"""
        result = generate_trade_signal(
            synthesis=self._synthesis(signal="buy", confidence=0.5),
            regime=self._regime("bull"),
        )
        assert result.position_pct_suggestion <= _MAX_AUTO_POSITION_PCT

    def test_sell_never_raises(self):
        """sell 信号仓位恒为 0，永不触发审批。"""
        result = generate_trade_signal(
            synthesis=self._synthesis(signal="sell", confidence=1.0),
            regime=self._regime("bull"),
        )
        assert result.position_pct_suggestion == 0.0


# ─────────────────────── TradingHarness position check ───────────

from agent_platform.finance.trading_harness import TradingHarness


class TestTradingHarnessPositionCheck:
    def _trader(self, suggested: float):
        return {"symbol": "T", "signal": "buy", "position_pct_suggestion": suggested,
                "rationale": "test", "source": "trader",
                "updated_at": "2026-01-01T00:00:00Z",
                "disclaimer": "仅供研究参考，不构成投资建议"}

    def _risk(self, approved: float):
        return {"symbol": "T", "approved_position_pct": approved,
                "risk_flags": [], "final_signal": "buy",
                "risk_note": "ok", "source": "risk_manager",
                "updated_at": "2026-01-01T00:00:00Z",
                "disclaimer": "仅供研究参考，不构成投资建议"}

    def test_suggested_within_approved_passes(self):
        """suggested=3 ≤ approved=5 → 仓位合规。"""
        h = TradingHarness()
        result = h._check_position(self._trader(3.0), self._risk(5.0))
        assert result.passed is True

    def test_suggested_equals_approved_passes(self):
        """suggested=5 == approved=5 → 仓位合规（边界）。"""
        h = TradingHarness()
        result = h._check_position(self._trader(5.0), self._risk(5.0))
        assert result.passed is True

    def test_suggested_exceeds_approved_fails(self):
        """suggested=5 > approved=2 → 必须不合规。"""
        h = TradingHarness()
        result = h._check_position(self._trader(5.0), self._risk(2.0))
        assert result.passed is False
