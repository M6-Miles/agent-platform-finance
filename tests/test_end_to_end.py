"""
端到端 Pipeline 测试
====================
模拟完整分析流程：Technical → Fundamental → Industry → MarketRegime → Synthesis → Trader → RiskManager → TradingHarness
"""
from __future__ import annotations

import pytest


class TestEndToEndPipeline:
    """完整分析链路测试（单票）"""

    def test_full_pipeline_single_stock(self):
        """从技术分析到交易信号的完整流程"""
        from agent_platform.finance.analysis import analyze_security
        from agent_platform.finance.fundamental_agent import analyze_fundamental
        from agent_platform.finance.industry_agent import analyze_industry
        from agent_platform.finance.market_regime_agent import analyze_market_regime
        from agent_platform.finance.synthesis_agent import synthesize
        from agent_platform.finance.trader_agent import generate_trade_signal
        from agent_platform.finance.risk_manager_agent import assess_risk
        from agent_platform.finance.trading_harness import TradingHarness
        from agent_platform.finance.sample_data_provider import SampleMarketDataProvider

        symbol_tech = "DEMO001"  # 使用样例数据中的有效代码
        symbol = "000001"        # 其余 Agent 支持任意代码（离线降级）
        # 合成数据日期区间 2025-01-02 ~ 2025-12-19，不使用 datetime.now()
        start = "2025-03-01"
        end   = "2025-06-30"

        # 1. Technical - 显式使用 SampleMarketDataProvider
        provider = SampleMarketDataProvider()
        tech_result = analyze_security(symbol_tech, start, end, provider=provider)
        assert tech_result.symbol == symbol_tech
        tech_dict = tech_result.to_dict()

        # 2. Fundamental
        fund_dict = analyze_fundamental(symbol).to_dict()

        # 3. Industry
        ind_dict = analyze_industry(symbol).to_dict()

        # 4. MarketRegime
        regime_dict = analyze_market_regime().to_dict()

        # 5. Synthesis（使用 symbol_tech 保持一致）
        synth_result = synthesize(symbol_tech, tech_dict, fund_dict, ind_dict, regime_dict)
        assert 0.0 <= synth_result.confidence <= 1.0
        assert synth_result.signal in ("buy", "sell", "hold", "watch")
        synth_dict = synth_result.to_dict()

        # 6. Trader
        trader_dict = generate_trade_signal(synth_dict, regime_dict, tech_dict).to_dict()

        # 7. RiskManager
        risk_dict = assess_risk(trader_dict).to_dict()

        # 8. TradingHarness
        final_result = TradingHarness(min_confidence=0.3).run_preflight(synth_dict, trader_dict, risk_dict)
        assert final_result.final_action in ("execute", "block", "manual_review")

        # 验证所有输出均有 source / updated_at
        for d in [tech_dict, fund_dict, ind_dict, regime_dict, synth_dict, trader_dict, risk_dict]:
            assert d.get("source"), "缺少 source"
            assert d.get("updated_at"), "缺少 updated_at"

    def test_pipeline_bear_market_scenario(self):
        """熊市 + 高估场景，应输出 sell 信号"""
        from agent_platform.finance.synthesis_agent import synthesize

        tech = {
            "latest_close": 10.0,
            "latest_rsi": 75.0,  # 超买
            "latest_macd": -0.2,
            "latest_macd_signal": -0.1,  # 死叉
            "latest_ma5": 9.5,
            "latest_ma20": 10.2,  # 空头排列
        }
        fund = {"valuation_signal": "overvalued", "valuation_note": "PE过高"}
        ind = {"prosperity_signal": "sluggish", "industry_name": "煤炭"}
        regime = {"regime": "bear", "regime_note": "大跌"}

        result = synthesize("000001", tech, fund, ind, regime)
        assert result.confidence < 0.5
        assert result.signal in ("sell", "hold")

    def test_pipeline_bull_market_scenario(self):
        """牛市 + 低估场景，应输出 buy 信号"""
        from agent_platform.finance.synthesis_agent import synthesize

        tech = {
            "latest_close": 10.0,
            "latest_rsi": 35.0,  # 超卖
            "latest_macd": 0.3,
            "latest_macd_signal": 0.1,  # 金叉
            "latest_ma5": 10.2,
            "latest_ma20": 9.8,  # 多头排列
            "latest_bb_position_pct": 15.0,
        }
        fund = {"valuation_signal": "undervalued", "valuation_note": "PE低估"}
        ind = {"prosperity_signal": "booming", "industry_name": "科技"}
        regime = {"regime": "bull", "regime_note": "大涨"}

        result = synthesize("000001", tech, fund, ind, regime)
        assert result.confidence >= 0.6
        assert result.signal == "buy"


class TestMultiStockPipeline:
    """批量股票分析（≥20支）"""

    @pytest.mark.slow
    def test_batch_analysis_20_stocks(self):
        """批量分析 20 支股票（降级模式，不依赖 AkShare 真实数据）"""
        from agent_platform.finance.synthesis_agent import synthesize

        symbols = [f"DEMO{i:02d}" for i in range(1, 21)]
        results = []

        tech_template = {
            "latest_close": 10.0, "latest_rsi": 50.0, "latest_macd": 0.0,
            "latest_macd_signal": 0.0, "latest_ma5": 10.0, "latest_ma20": 10.0,
        }
        fund = {"valuation_signal": "fairly_valued", "valuation_note": "中性"}
        ind = {"prosperity_signal": "normal", "industry_name": "综合"}
        regime = {"regime": "consolidation", "regime_note": "震荡"}

        for sym in symbols:
            result = synthesize(sym, tech_template, fund, ind, regime)
            results.append(result)

        assert len(results) == 20
        for r in results:
            assert 0.0 <= r.confidence <= 1.0
            assert r.signal in ("buy", "sell", "hold", "watch")
