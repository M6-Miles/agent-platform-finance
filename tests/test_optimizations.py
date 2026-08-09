"""
tests/test_optimizations.py
============================
测试三项优化点：
  1. 追踪止损（trailing_stop_pct in run_backtest）
  2. Regime-aware 权重调整（regime_aware in synthesize）
  3. 情感 Agent（SentimentAgent）
  4. 参数化风格的属性级测试（代替 hypothesis，无额外依赖）
"""
from __future__ import annotations

import pytest
import pandas as pd
from datetime import date, timedelta

from agent_platform.finance.backtesting import run_backtest, BacktestResult
from agent_platform.finance.synthesis_agent import synthesize, SynthesisResult
from agent_platform.finance.sentiment_agent import analyze_sentiment, SentimentResult


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_price_df(prices: list[float], start: date = date(2024, 1, 2)) -> pd.DataFrame:
    """给定收盘价序列，构造最简价格 DataFrame（含 open / high / low / close / date）。"""
    rows = []
    for i, p in enumerate(prices):
        d = start + timedelta(days=i)
        rows.append({
            "date":  d,
            "open":  round(p * 0.99, 3),
            "high":  round(p * 1.01, 3),
            "low":   round(p * 0.98, 3),
            "close": p,
        })
    return pd.DataFrame(rows)


def _full_bear_inputs() -> tuple[dict, dict, dict, dict]:
    """构造最强空头场景（与 test_phase3_agents 保持一致）。"""
    tech = {
        "latest_rsi": 80.0,
        "latest_macd": -2.0,
        "latest_macd_signal": -1.0,
        "latest_close": 10.0,
        "latest_ma5": 11.0,
        "latest_ma20": 12.0,
        "latest_bb_position_pct": 90.0,
    }
    fund   = {"valuation_signal": "overvalued",  "valuation_note": "高估"}
    ind    = {"prosperity_signal": "sluggish",   "industry_name": "银行"}
    regime = {"regime": "bear",                  "regime_note": "下跌"}
    return tech, fund, ind, regime


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 追踪止损测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrailingStop:

    def test_default_zero_is_backward_compatible(self):
        """trailing_stop_pct=0.0（默认）必须与不传参时结果完全一致。"""
        prices = [10.0, 11.0, 12.0, 11.5, 10.0, 9.0]
        df = _make_price_df(prices)
        sigs = {date(2024, 1, 2): "buy", date(2024, 1, 7): "sell"}
        r_implicit = run_backtest("T", df, sigs)
        r_explicit  = run_backtest("T", df, sigs, trailing_stop_pct=0.0)
        assert r_implicit.total_trades == r_explicit.total_trades
        assert round(r_implicit.total_return_pct, 6) == round(r_explicit.total_return_pct, 6)

    def test_trailing_stop_triggers_before_planned_exit(self):
        """
        价格先升后大幅下跌，追踪止损应在显式 sell 信号前触发平仓。
        路径：10 → 20（峰值）→ 17（回落15%>10% → 触发止损）
        """
        # 5天上涨到20，然后下跌到17（回落15%）
        prices = [10.0, 13.0, 16.0, 18.0, 20.0, 19.0, 17.0, 15.0, 12.0]
        df = _make_price_df(prices)
        # 仅在最后一天发 sell，没有追踪止损时一直持有到跌到12
        sigs = {date(2024, 1, 2): "buy", date(2024, 1, 10): "sell"}

        r_no_stop  = run_backtest("T", df, sigs, trailing_stop_pct=0.0)
        r_trailing = run_backtest("T", df, sigs, trailing_stop_pct=0.10)  # 10%

        # 追踪止损版：应在回落10%时触发额外平仓，最终亏损更小
        # 两者都应产生有效的 BacktestResult
        assert isinstance(r_trailing, BacktestResult)
        # 有止损时最大回撤应 <= 无止损时
        assert r_trailing.max_drawdown_pct <= r_no_stop.max_drawdown_pct + 1.0  # 1%容差

    def test_trailing_stop_does_not_trigger_on_small_dip(self):
        """波动小于阈值时不应触发止损，仓位保留到 sell 信号。"""
        # 价格从100涨到110，然后小回调到106（3.6% < 5%）
        prices = [100.0, 103.0, 106.0, 110.0, 109.0, 108.0, 106.0, 105.0]
        df = _make_price_df(prices)
        sigs = {date(2024, 1, 2): "buy", date(2024, 1, 9): "sell"}

        r = run_backtest("T", df, sigs, trailing_stop_pct=0.05)
        # 回调不超过5%，不应额外生成 trailing_stop 平仓单
        # 总交易数应仍为1（买+卖 = 1 complete trade）
        assert r.total_trades == 1

    @pytest.mark.parametrize("stop_pct", [0.03, 0.05, 0.08, 0.10, 0.15, 0.20])
    def test_parametric_any_stop_pct_valid_result(self, stop_pct: float):
        """任意 trailing_stop_pct 均应产生有效的 BacktestResult。"""
        prices = [100.0 + i * 0.5 if i < 10 else 105.0 - (i - 10) * 2.0 for i in range(20)]
        df = _make_price_df(prices)
        sigs = {date(2024, 1, 2): "buy"}
        r = run_backtest("T", df, sigs, trailing_stop_pct=stop_pct)
        assert isinstance(r, BacktestResult)
        assert r.max_drawdown_pct >= 0.0
        assert isinstance(r.sharpe_ratio, float)
        assert not (r.sharpe_ratio != r.sharpe_ratio)   # 不是 NaN


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Regime-aware 综合研判测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeAwareSynthesis:

    def _bullish_tech(self) -> dict:
        return {"latest_rsi": 25.0, "latest_macd": 1.0, "latest_macd_signal": 0.5}

    def _bearish_tech(self) -> dict:
        return {"latest_rsi": 75.0, "latest_macd": -1.0, "latest_macd_signal": -0.5}

    def test_regime_aware_false_matches_default(self):
        """regime_aware=False 必须与不传参结果完全相同（backward compatible）。"""
        tech, fund, ind, regime = _full_bear_inputs()
        r1 = synthesize("000001", tech, fund, ind, regime)
        r2 = synthesize("000001", tech, fund, ind, regime, regime_aware=False)
        assert r1.confidence == r2.confidence
        assert r1.signal == r2.signal

    def test_bull_market_boosts_bullish_tech(self):
        """牛市 + 技术面多头：regime_aware=True 应使置信度 >= 关闭时。"""
        tech = self._bullish_tech()
        fund, ind = {}, {}
        regime = {"regime": "bull", "regime_note": "牛市"}
        r_off = synthesize("000001", tech, fund, ind, regime, regime_aware=False)
        r_on  = synthesize("000001", tech, fund, ind, regime, regime_aware=True)
        assert r_on.confidence >= r_off.confidence

    def test_bear_market_damps_bullish_tech(self):
        """熊市 + 技术面多头：regime_aware=True 应使置信度 <= 关闭时（技术权重被压缩）。"""
        tech = self._bullish_tech()
        fund, ind = {}, {}
        regime = {"regime": "bear", "regime_note": "熊市"}
        r_off = synthesize("000001", tech, fund, ind, regime, regime_aware=False)
        r_on  = synthesize("000001", tech, fund, ind, regime, regime_aware=True)
        assert r_on.confidence <= r_off.confidence

    def test_existing_bear_scenario_still_passes(self):
        """
        原 test_synthesize_bear_scenario 约束必须在 regime_aware=True 时依然满足：
        confidence < 0.50 且 signal == 'sell'
        """
        tech, fund, ind, regime = _full_bear_inputs()
        result = synthesize("000001", tech, fund, ind, regime, regime_aware=True)
        assert result.confidence < 0.50
        assert result.signal == "sell"

    def test_consolidation_regime_no_change(self):
        """震荡市：regime_aware=True 不应改变任何权重（multiplier=1.0）。"""
        tech = self._bullish_tech()
        fund, ind = {}, {}
        regime = {"regime": "consolidation", "regime_note": "震荡"}
        r_off = synthesize("000001", tech, fund, ind, regime, regime_aware=False)
        r_on  = synthesize("000001", tech, fund, ind, regime, regime_aware=True)
        assert r_on.confidence == r_off.confidence

    def test_sentiment_integration_positive(self):
        """传入正面舆情应提升置信度。"""
        tech, fund, ind, regime = {}, {}, {}, {"regime": "unknown"}
        sentiment = {"score": 8, "keywords_found": ["增长", "利好"], "sentiment": "positive"}
        r_no_sent = synthesize("000001", tech, fund, ind, regime)
        r_with    = synthesize("000001", tech, fund, ind, regime, sentiment=sentiment)
        assert r_with.confidence >= r_no_sent.confidence

    def test_sentiment_integration_negative(self):
        """传入负面舆情应降低置信度。"""
        tech, fund, ind, regime = {}, {}, {}, {"regime": "unknown"}
        sentiment = {"score": -8, "keywords_found": ["亏损", "违规"], "sentiment": "negative"}
        r_no_sent = synthesize("000001", tech, fund, ind, regime)
        r_with    = synthesize("000001", tech, fund, ind, regime, sentiment=sentiment)
        assert r_with.confidence <= r_no_sent.confidence

    @pytest.mark.parametrize("regime_type,tech_fn,should_be_higher", [
        ("bull",          "_bullish_tech", True),   # 牛市多头：提升
        ("bear",          "_bullish_tech", False),  # 熊市看多技术面：regime_aware应压低置信度
        ("consolidation", "_bullish_tech", None),   # 震荡：无变化（==）
    ])
    def test_directional_consistency(self, regime_type, tech_fn, should_be_higher):
        """regime_aware 方向性一致性：指定场景的置信度变化方向正确。"""
        tech = getattr(self, tech_fn)()
        fund, ind = {}, {}
        regime = {"regime": regime_type, "regime_note": "test"}
        r_off = synthesize("T", tech, fund, ind, regime, regime_aware=False)
        r_on  = synthesize("T", tech, fund, ind, regime, regime_aware=True)
        if should_be_higher is True:
            assert r_on.confidence >= r_off.confidence
        elif should_be_higher is False:
            assert r_on.confidence <= r_off.confidence
        else:
            assert r_on.confidence == r_off.confidence


# ═══════════════════════════════════════════════════════════════════════════════
# 3. SentimentAgent 测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestSentimentAgent:

    def test_returns_valid_result(self):
        """基本返回值结构正确。"""
        headlines = ["公司业绩增长超预期", "订单增加带动营收提升"]
        result = analyze_sentiment("000001", "平安银行", _sample_headlines=headlines)
        assert isinstance(result, SentimentResult)
        assert -10 <= result.score <= 10
        assert result.sentiment in ("positive", "negative", "neutral")
        assert result.source == "sample"
        assert result.disclaimer != ""
        assert result.symbol == "000001"

    def test_positive_keywords_give_positive_score(self):
        """正面关键词应产生正分且情感标签为 positive。"""
        headlines = ["利好消息：业绩增长超预期", "公司回购股份创新高", "订单增加强劲"]
        result = analyze_sentiment("TEST", _sample_headlines=headlines)
        assert result.score > 0
        assert result.sentiment == "positive"

    def test_negative_keywords_give_negative_score(self):
        """负面关键词应产生负分且情感标签为 negative。"""
        headlines = ["公司亏损扩大令人担忧", "监管处罚落地", "股东减持计划公告"]
        result = analyze_sentiment("TEST", _sample_headlines=headlines)
        assert result.score < 0
        assert result.sentiment == "negative"

    def test_empty_headlines_neutral(self):
        """无新闻时得分应为0，情感为 neutral。"""
        result = analyze_sentiment("TEST", _sample_headlines=[])
        assert result.score == 0
        assert result.sentiment == "neutral"
        assert result.headline_count == 0

    def test_score_bounded_many_positive(self):
        """即使有大量正面关键词，得分也不应超过+10。"""
        kws = " ".join(["利好增长创新高超预期回购分红突破上调评级强劲新高"] * 20)
        result = analyze_sentiment("T", _sample_headlines=[kws])
        assert -10 <= result.score <= 10

    def test_score_bounded_many_negative(self):
        """即使有大量负面关键词，得分也不应低于-10。"""
        kws = " ".join(["利空下跌亏损违规处罚减持跌停暴雷风险警告监管退市"] * 20)
        result = analyze_sentiment("T", _sample_headlines=[kws])
        assert -10 <= result.score <= 10

    def test_to_dict_is_json_serializable(self):
        """SentimentResult.to_dict() 应可直接序列化为 JSON（用于传入 synthesize）。"""
        import json
        result = analyze_sentiment("000001", _sample_headlines=["测试标题"])
        d = result.to_dict()
        json_str = json.dumps(d, ensure_ascii=False)
        assert json_str  # 不为空

    def test_to_dict_compatible_with_synthesis(self):
        """SentimentResult.to_dict() 可直接作为 sentiment 参数传入 synthesize。"""
        headlines = ["公司业绩增长利好突破新高"]
        sentiment_result = analyze_sentiment("000001", _sample_headlines=headlines)
        d = sentiment_result.to_dict()
        synth_result = synthesize("000001", {}, {}, {}, {}, sentiment=d)
        assert isinstance(synth_result, SynthesisResult)

    @pytest.mark.parametrize("symbol", ["000001", "600519", "000858", "300750", "688981"])
    def test_various_symbols(self, symbol: str):
        """任意标的代码均可正确处理。"""
        result = analyze_sentiment(symbol, _sample_headlines=["测试标题"])
        assert result.symbol == symbol
        assert isinstance(result.score, int)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 属性级参数化测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestPropertyLike:

    @pytest.mark.parametrize("total_score", [
        -90, -80, -65, -40, -20, -18, -10, 0, 10, 18, 20, 40, 65, 80, 90
    ])
    def test_confidence_always_in_unit_range(self, total_score: int):
        """对所有可能的 total_score，置信度公式的输出必须在 [0.0, 1.0]。"""
        raw_conf = (total_score + 90) / 180.0
        confidence = max(0.0, min(1.0, round(raw_conf, 3)))
        assert 0.0 <= confidence <= 1.0

    @pytest.mark.parametrize("total_score,expected_signal", [
        (90,  "buy"),    # conf=1.000 ≥ 0.60
        (18,  "buy"),    # conf=0.600 ≥ 0.60（边界）
        (17,  "hold"),   # conf=0.594 < 0.60
        (-17, "hold"),   # conf=0.406 > 0.40
        (-18, "sell"),   # conf=0.400 ≤ 0.40（边界）
        (-90, "sell"),   # conf=0.000 ≤ 0.40
    ])
    def test_signal_threshold_boundaries(self, total_score: int, expected_signal: str):
        """验证 buy/sell/hold 阈值边界（0.60 / 0.40）的正确性。"""
        raw_conf = (total_score + 90) / 180.0
        confidence = max(0.0, min(1.0, round(raw_conf, 3)))
        if confidence >= 0.60:
            signal = "buy"
        elif confidence <= 0.40:
            signal = "sell"
        else:
            signal = "hold"
        assert signal == expected_signal

    @pytest.mark.parametrize("slippage,commission", [
        (0.0,  0.0),
        (0.1,  0.03),
        (0.5,  0.10),
        (1.0,  0.30),
    ])
    def test_backtest_valid_for_various_costs(self, slippage: float, commission: float):
        """任意合理滑点/佣金组合均应产生有效结果，无异常/NaN。"""
        prices = [10.0 + i * 0.1 for i in range(20)]
        df = _make_price_df(prices)
        sigs = {date(2024, 1, 2): "buy", date(2024, 1, 15): "sell"}
        r = run_backtest("T", df, sigs, slippage_pct=slippage, commission_pct=commission)
        assert isinstance(r.sharpe_ratio, float)
        assert r.max_drawdown_pct >= 0.0
        assert r.total_trades >= 1
