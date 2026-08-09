"""
多空辩论专项测试（说明书 A-04：两轮结构化 Bull/Bear 辩论）
==========================================================

覆盖点：
1. 两轮结构：第 1 轮开场陈述，第 2 轮交叉反驳，Claim/Evidence/Reasoning/Rebuttal 齐全
2. 证据抽取字段名与 synthesis_agent 打分函数对齐（防"静默漏证据"回归）
3. 一致性检查（Consistency Check）能抓出引用矛盾与估值冲突
4. 偏见检测（Bias Detector）区分阻断级（一边倒）与提示级（数据质量）
5. with_debate=True **不改变** signal / confidence / 目标价 / reasoning（保护 E-01 回测口径）
6. 离线端到端：真实四路 Agent 输出 + 禁网，四个维度都能取到证据
7. 主链（node_synthesis_agent）确实启用了辩论，且不因标记被判为 failed
"""
from __future__ import annotations

import builtins

import pytest

from agent_platform.finance.bull_bear_debate import (
    DEBATE_ROUND_SCHEMA,
    DEBATE_SCHEMA,
    DEBATE_TURN_SCHEMA,
    EVIDENCE_SCHEMA,
    Evidence,
    bias_detector,
    consistency_check,
    extract_evidence,
    run_debate,
)
from agent_platform.finance.synthesis_agent import synthesize

# ─── 测试夹具：字段名严格照抄各 Agent to_dict() 的真实键名 ─────────────────────

TECH_BULL = {
    "latest_rsi": 25.0,            # <30 → 多方
    "latest_macd": 1.5,
    "latest_macd_signal": 0.5,     # 柱 +1.0 → 多方
    "latest_close": 154.41,
    "latest_ma5": 153.21,
    "latest_ma20": 147.91,         # close>MA5>MA20 → 多方
    "latest_bb_position_pct": 10.0,  # <20 → 多方
    "source": "akshare 日线",
    "updated_at": "2026-08-07T00:00:00Z",
    "data_status": "live",
}

TECH_BEAR = {
    "latest_rsi": 82.0,            # >70 → 空方
    "latest_macd": 0.2,
    "latest_macd_signal": 1.2,     # 柱 -1.0 → 空方
    "latest_close": 100.0,
    "latest_ma5": 105.0,
    "latest_ma20": 110.0,          # close<MA5<MA20 → 空方
    "latest_bb_position_pct": 92.0,  # >80 → 空方
    "source": "akshare 日线",
    "updated_at": "2026-08-07T00:00:00Z",
    "data_status": "live",
}

FUND_NEUTRAL = {
    "valuation_signal": "fairly_valued",
    "roe_pct": 10.0,
    "debt_to_asset_pct": 40.0,
    "source": "MCP:get_offline_fundamental",
    "updated_at": "2026-08-07T00:00:00Z",
    "data_status": "offline_sample",
}

FUND_BEAR = {
    "valuation_signal": "overvalued",   # → 空方
    "roe_pct": 3.0,                     # <5 → 空方
    "debt_to_asset_pct": 82.0,          # >70 → 空方
    "source": "MCP:get_offline_fundamental",
    "updated_at": "2026-08-07T00:00:00Z",
    "data_status": "offline_sample",
}

IND_NEUTRAL = {
    "prosperity_signal": "normal",
    "industry_name": "测试行业",
    "source": "MCP:get_offline_industry",
    "updated_at": "2026-08-07T00:00:00Z",
    "data_status": "offline_sample",
}

REGIME_NEUTRAL = {
    "regime": "consolidation",
    "risk_appetite": "medium",
    "source": "MCP:get_offline_market_regime",
    "updated_at": "2026-08-07T00:00:00Z",
    "data_status": "offline_sample",
}


def _ev(metric: str, direction: str, agent: str, status: str = "live") -> Evidence:
    return Evidence(
        metric=metric, value=1.0, source=f"{agent} 数据源",
        updated_at="2026-08-07T00:00:00Z", data_status=status,
        direction=direction, origin_agent=agent,
    )


def _required_ok(schema: dict, payload: dict) -> list[str]:
    """返回 schema required 中缺失的键（不引入 jsonschema 依赖）。"""
    return [k for k in schema.get("required", []) if k not in payload]


# ═══════════════════════════════════════════════════════════════
#   1. 两轮结构
# ═══════════════════════════════════════════════════════════════

class TestTwoRoundStructure:
    def test_exactly_two_rounds(self):
        r = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL)
        assert len(r.rounds) == 2

    def test_round_one_is_opening_without_rebuttal(self):
        r = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL)
        r1 = r.rounds[0]
        assert r1.round_index == 1
        assert r1.bull.rebuttal is None
        assert r1.bear.rebuttal is None

    def test_round_two_has_rebuttal_on_both_sides(self):
        r = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL)
        r2 = r.rounds[1]
        assert r2.round_index == 2
        assert r2.bull.rebuttal and r2.bear.rebuttal

    def test_rebuttal_targets_opponent_round_one_claim(self):
        """反驳必须指向对方第 1 轮的主张，不能凭空反驳。"""
        r = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL)
        r1, r2 = r.rounds
        assert r2.bull.rebuts_claim == r1.bear.claim
        assert r2.bear.rebuts_claim == r1.bull.claim

    def test_every_turn_has_claim_and_reasoning(self):
        r = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL)
        for rnd in r.rounds:
            for turn in (rnd.bull, rnd.bear):
                assert turn.claim.strip()
                assert turn.reasoning.strip()

    def test_no_opponent_evidence_rebuttal_says_so(self):
        """对方无证据时必须明确说明，不得虚构对手论点。"""
        tech = {"latest_rsi": 25.0, "source": "s", "updated_at": "u", "data_status": "live"}
        r = run_debate("T", tech, {}, {}, {})
        # 多方有 1 项证据、空方 0 项 → 面对"对方无证据"的是**多方**。
        assert "未提出可溯源证据" in r.rounds[1].bull.rebuttal
        # 空方自身无证据，但对方有证据，仍须给出实质反驳而非空串。
        assert r.rounds[1].bear.rebuttal
        assert "未提出可溯源证据" not in r.rounds[1].bear.rebuttal


# ═══════════════════════════════════════════════════════════════
#   2. 证据抽取字段名对齐（回归防护）
# ═══════════════════════════════════════════════════════════════

class TestEvidenceExtraction:
    def test_technical_real_field_names_are_matched(self):
        """
        回归防护：曾用 rsi14 / macd_hist / ma20 等不存在的键名，
        导致技术面静默抽不到任何证据。
        """
        bull, bear = extract_evidence(TECH_BULL, {}, {}, {})
        metrics = {e.metric for e in bull}
        assert "latest_rsi" in metrics
        assert "macd_histogram" in metrics
        assert "ma_alignment" in metrics
        assert "latest_bb_position_pct" in metrics
        assert bear == []

    def test_technical_bear_side(self):
        bull, bear = extract_evidence(TECH_BEAR, {}, {}, {})
        metrics = {e.metric for e in bear}
        assert {"latest_rsi", "macd_histogram", "ma_alignment",
                "latest_bb_position_pct"} <= metrics
        assert bull == []

    def test_macd_histogram_computed_from_two_fields(self):
        """MACD 柱由 latest_macd - latest_macd_signal 现算，不依赖臆造字段。"""
        tech = {"latest_macd": 2.0, "latest_macd_signal": 0.5,
                "source": "s", "updated_at": "u", "data_status": "live"}
        bull, _ = extract_evidence(tech, {}, {}, {})
        hist = [e for e in bull if e.metric == "macd_histogram"][0]
        assert hist.value == pytest.approx(1.5)

    def test_industry_sluggish_is_bear_evidence(self):
        """回归防护：曾误用 'declining'，而真实枚举是 'sluggish'。"""
        ind = {"prosperity_signal": "sluggish", "source": "s",
               "updated_at": "u", "data_status": "offline_sample"}
        _, bear = extract_evidence({}, {}, ind, {})
        assert "prosperity_signal" in {e.metric for e in bear}

    def test_regime_dimension_produces_evidence(self):
        """市场/宏观维度必须能贡献证据，否则 agent_coverage 会缺 market_regime。"""
        regime = {"regime": "bull", "risk_appetite": "high",
                  "index_change_pct_5d": 2.5, "northbound_flow_cny": 3.2e8,
                  "source": "s", "updated_at": "u", "data_status": "offline_sample"}
        bull, _ = extract_evidence({}, {}, {}, regime)
        metrics = {e.metric for e in bull}
        assert {"regime", "risk_appetite", "index_change_pct_5d",
                "northbound_flow_cny"} <= metrics
        assert all(e.origin_agent == "market_regime" for e in bull)

    def test_consolidation_regime_is_neutral(self):
        """consolidation 不构成方向性证据（与 _score_regime 给 0 分一致）。"""
        regime = {"regime": "consolidation", "risk_appetite": "medium",
                  "source": "s", "updated_at": "u", "data_status": "offline_sample"}
        bull, bear = extract_evidence({}, {}, {}, regime)
        assert [e for e in bull + bear if e.metric == "regime"] == []

    def test_small_index_move_is_noise_not_evidence(self):
        regime = {"regime": "unknown", "index_change_pct_5d": 0.3,
                  "source": "s", "updated_at": "u", "data_status": "offline_sample"}
        bull, bear = extract_evidence({}, {}, {}, regime)
        assert [e for e in bull + bear if e.metric == "index_change_pct_5d"] == []

    def test_dcf_margin_of_safety_becomes_evidence(self):
        fund = dict(FUND_NEUTRAL)
        fund["dcf"] = {"applicable": True, "margin_of_safety_pct": -37.95}
        _, bear = extract_evidence({}, fund, {}, {})
        assert "dcf_margin_of_safety_pct" in {e.metric for e in bear}

    def test_inapplicable_dcf_yields_no_evidence(self):
        fund = dict(FUND_NEUTRAL)
        fund["dcf"] = {"applicable": False, "margin_of_safety_pct": None}
        bull, bear = extract_evidence({}, fund, {}, {})
        assert [e for e in bull + bear if e.metric == "dcf_margin_of_safety_pct"] == []

    def test_evidence_carries_source_and_updated_at(self):
        bull, _ = extract_evidence(TECH_BULL, {}, {}, {})
        for e in bull:
            assert e.source
            assert e.updated_at

    def test_missing_source_is_marked_not_fabricated(self):
        tech = {"latest_rsi": 25.0}
        bull, _ = extract_evidence(tech, {}, {}, {})
        assert "source缺失" in bull[0].source
        assert bull[0].data_status == "unavailable"


# ═══════════════════════════════════════════════════════════════
#   3. 证据权重
# ═══════════════════════════════════════════════════════════════

class TestEvidenceWeight:
    @pytest.mark.parametrize("status,expected", [
        ("live", 1.0), ("offline_sample", 0.5),
        ("fallback", 0.4), ("unavailable", 0.0),
    ])
    def test_weight_by_data_status(self, status, expected):
        assert _ev("m", "bullish", "technical", status).weight == expected

    def test_live_outweighs_sample(self):
        assert _ev("m", "bullish", "t", "live").weight > \
               _ev("m", "bullish", "t", "offline_sample").weight


# ═══════════════════════════════════════════════════════════════
#   4. 一致性检查
# ═══════════════════════════════════════════════════════════════

class TestConsistencyCheck:
    def test_clean_case_passes(self):
        cc = consistency_check(
            [_ev("latest_rsi", "bullish", "technical")],
            [_ev("roe_pct", "bearish", "fundamental")],
            FUND_NEUTRAL,
        )
        assert cc["passed"] is True
        assert cc["issues"] == []

    def test_same_metric_both_sides_is_contradiction(self):
        cc = consistency_check(
            [_ev("latest_rsi", "bullish", "technical")],
            [_ev("latest_rsi", "bearish", "technical")],
            FUND_NEUTRAL,
        )
        assert cc["passed"] is False
        assert any("引用矛盾" in i for i in cc["issues"])

    def test_undervalued_but_dcf_says_overvalued(self):
        fund = {"valuation_signal": "undervalued",
                "dcf": {"applicable": True, "margin_of_safety_pct": -45.0}}
        cc = consistency_check([], [], fund)
        assert cc["passed"] is False
        assert any("估值结论冲突" in i for i in cc["issues"])

    def test_overvalued_but_dcf_says_undervalued(self):
        fund = {"valuation_signal": "overvalued",
                "dcf": {"applicable": True, "margin_of_safety_pct": 55.0}}
        cc = consistency_check([], [], fund)
        assert cc["passed"] is False

    def test_live_status_from_sample_source_is_contradiction(self):
        bad = Evidence(metric="roe_pct", value=1.0, source="离线样例数据",
                       updated_at="u", data_status="live",
                       direction="bullish", origin_agent="fundamental")
        cc = consistency_check([bad], [], FUND_NEUTRAL)
        assert cc["passed"] is False
        assert any("溯源矛盾" in i for i in cc["issues"])

    def test_checked_items_reported(self):
        cc = consistency_check([], [], FUND_NEUTRAL)
        assert cc["checked_items"] == [
            "metric_overlap", "valuation_vs_dcf", "status_vs_source",
        ]


# ═══════════════════════════════════════════════════════════════
#   5. 偏见检测：阻断级 vs 提示级
# ═══════════════════════════════════════════════════════════════

class TestBiasDetector:
    def test_one_sided_evidence_is_blocking(self):
        b = bias_detector([_ev("a", "bullish", "technical"),
                           _ev("b", "bullish", "fundamental")], [])
        assert b["passed"] is False
        assert b["one_sided"] is True
        assert any("一边倒" in i for i in b["blocking_issues"])

    def test_no_evidence_is_blocking(self):
        b = bias_detector([], [])
        assert b["passed"] is False
        assert b["blocking_issues"]
        assert b["agent_coverage"] == []

    def test_weak_evidence_is_warning_not_blocking(self):
        """
        离线样例下弱证据占比恒为 100%。若按阻断处理，离线 20 股验收会全部
        阻断，阻断信号失去区分度。故必须降级为提示。
        """
        b = bias_detector(
            [_ev("a", "bullish", "technical", "offline_sample")],
            [_ev("b", "bearish", "fundamental", "offline_sample")],
        )
        assert b["passed"] is True
        assert b["blocking_issues"] == []
        assert any("证据质量不足" in w for w in b["warnings"])

    def test_single_agent_coverage_is_warning(self):
        b = bias_detector([_ev("a", "bullish", "technical")],
                          [_ev("b", "bearish", "technical")])
        assert b["passed"] is True
        assert any("证据来源集中" in w for w in b["warnings"])

    def test_balanced_live_evidence_is_clean(self):
        b = bias_detector([_ev("a", "bullish", "technical")],
                          [_ev("b", "bearish", "fundamental")])
        assert b["passed"] is True
        assert b["warnings"] == []
        assert b["weak_evidence_ratio"] == 0.0

    def test_issues_contains_both_severities(self):
        """issues 保留完整清单（阻断项在前），供展示层使用。"""
        b = bias_detector(
            [_ev("a", "bullish", "technical", "offline_sample")], [])
        assert b["issues"] == b["blocking_issues"] + b["warnings"]

    def test_agent_coverage_lists_all_dimensions(self):
        b = bias_detector(
            [_ev("a", "bullish", "technical"), _ev("b", "bullish", "industry")],
            [_ev("c", "bearish", "fundamental"), _ev("d", "bearish", "market_regime")],
        )
        assert b["agent_coverage"] == [
            "fundamental", "industry", "market_regime", "technical",
        ]


# ═══════════════════════════════════════════════════════════════
#   6. 阻断策略
# ═══════════════════════════════════════════════════════════════

class TestBlockingPolicy:
    def test_normal_two_sided_run_is_not_blocked(self):
        """双方都有证据、来源覆盖 2 个 Agent → 不得阻断。"""
        r = run_debate("T", TECH_BULL, FUND_BEAR, IND_NEUTRAL, REGIME_NEUTRAL)
        assert r.blocked is False
        assert r.blocking_reasons == []

    def test_weak_evidence_marks_but_does_not_block(self):
        """
        回归测试：全部证据为非实时数据时，必须"标记"而非"阻断"。

        这是本模块修复过的真实缺陷：早期实现把弱证据也算作阻断条件，
        导致离线模式下每一只股票都被阻断，20 股离线验收全部失效，
        阻断信号失去区分度、反而掩盖真正的引用矛盾。
        """
        tech_off = dict(TECH_BULL, data_status="offline_sample")
        r = run_debate("T", tech_off, FUND_BEAR, IND_NEUTRAL, REGIME_NEUTRAL)

        # 前提：确实构成"弱证据占比 100%"的场景
        assert r.bias_report["weak_evidence_ratio"] == 1.0
        # 必须标记
        assert any("证据质量不足" in w for w in r.warnings)
        # 但绝不阻断
        assert r.blocked is False
        assert r.blocking_reasons == []
        # 弱证据属提示级，不得计入 blocking_issues
        assert r.bias_report["blocking_issues"] == []
        assert r.bias_report["passed"] is True

    def test_one_sided_run_is_blocked(self):
        tech = {"latest_rsi": 25.0, "latest_macd": 2.0, "latest_macd_signal": 0.5,
                "source": "s", "updated_at": "u", "data_status": "live"}
        r = run_debate("T", tech, {}, {}, {})
        assert r.blocked is True
        assert any("一边倒" in x for x in r.blocking_reasons)

    def test_empty_input_is_blocked(self):
        r = run_debate("T", {}, {}, {}, {})
        assert r.blocked is True

    def test_contradiction_is_blocked(self):
        """同一指标被双方引用 → 一致性检查阻断。"""
        tech = {"latest_rsi": 25.0, "source": "s", "updated_at": "u",
                "data_status": "live"}
        r = run_debate("T", tech, {}, {}, {})
        # 构造矛盾需要直接走 consistency_check，这里验证阻断原因带前缀
        cc = consistency_check([_ev("x", "bullish", "technical")],
                               [_ev("x", "bearish", "technical")], {})
        assert cc["passed"] is False
        assert r.blocked is True  # 一边倒（空方 0 项）


# ═══════════════════════════════════════════════════════════════
#   7. Schema 一致性
# ═══════════════════════════════════════════════════════════════

class TestSchemas:
    def test_debate_result_matches_schema_required(self):
        d = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL).to_dict()
        assert _required_ok(DEBATE_SCHEMA, d) == []

    def test_rounds_match_round_schema(self):
        d = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL).to_dict()
        assert len(d["rounds"]) >= DEBATE_SCHEMA["properties"]["rounds"]["minItems"]
        for rnd in d["rounds"]:
            assert _required_ok(DEBATE_ROUND_SCHEMA, rnd) == []
            for turn in (rnd["bull"], rnd["bear"]):
                assert _required_ok(DEBATE_TURN_SCHEMA, turn) == []
                for ev in turn["evidence"]:
                    assert _required_ok(EVIDENCE_SCHEMA, ev) == []

    def test_evidence_data_status_in_enum(self):
        d = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL).to_dict()
        allowed = EVIDENCE_SCHEMA["properties"]["data_status"]["enum"]
        for rnd in d["rounds"]:
            for turn in (rnd["bull"], rnd["bear"]):
                for ev in turn["evidence"]:
                    assert ev["data_status"] in allowed

    def test_result_has_source_updated_at_disclaimer(self):
        r = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL)
        assert r.source
        assert r.updated_at.endswith("Z")
        assert "仅供研究参考" in r.disclaimer

    def test_markdown_renders(self):
        md = run_debate("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL,
                        REGIME_NEUTRAL).to_markdown()
        assert "多空辩论" in md
        assert "开场陈述" in md
        assert "交叉反驳" in md


# ═══════════════════════════════════════════════════════════════
#   8. 关键保护：辩论不得改变 signal / confidence
# ═══════════════════════════════════════════════════════════════

class TestDebateDoesNotAffectSignal:
    """
    E-01 回测以 synthesize() 的 signal/confidence 为输入。辩论一旦回写这两个
    字段，回测口径就变了，夏普不可复现。这组测试是该风险的回归防护。
    """

    CASES = [
        ("bull", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL),
        ("bear", TECH_BEAR, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL),
        ("empty", {}, {}, {}, {}),
    ]

    @pytest.mark.parametrize("name,tech,fund,ind,regime", CASES)
    def test_with_debate_does_not_change_signal(self, name, tech, fund, ind, regime):
        off = synthesize("T", tech, fund, ind, regime)
        on = synthesize("T", tech, fund, ind, regime, with_debate=True)
        assert off.signal == on.signal
        assert off.confidence == on.confidence
        assert off.target_price_low == on.target_price_low
        assert off.target_price_high == on.target_price_high
        assert off.reasoning == on.reasoning
        assert off.bull_arguments == on.bull_arguments
        assert off.bear_arguments == on.bear_arguments

    def test_default_is_off_so_backtest_path_unaffected(self):
        r = synthesize("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL, REGIME_NEUTRAL)
        assert r.debate is None
        assert r.debate_blocked is False
        assert r.debate_warnings == ()

    def test_with_debate_populates_fields(self):
        r = synthesize("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL,
                       REGIME_NEUTRAL, with_debate=True)
        assert r.debate is not None
        assert len(r.debate["rounds"]) == 2

    def test_to_dict_exposes_debate(self):
        d = synthesize("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL,
                       REGIME_NEUTRAL, with_debate=True).to_dict()
        assert d["debate"] is not None
        assert "debate_blocked" in d
        assert "debate_warnings" in d

    def test_regime_aware_still_unaffected_by_debate(self):
        off = synthesize("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL,
                         REGIME_NEUTRAL, regime_aware=True)
        on = synthesize("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL,
                        REGIME_NEUTRAL, regime_aware=True, with_debate=True)
        assert off.confidence == on.confidence
        assert off.signal == on.signal

    def test_blocked_debate_does_not_flip_signal(self):
        """一边倒被阻断时，signal 仍由打分决定，不被辩论改写。"""
        tech = {"latest_rsi": 25.0, "latest_macd": 2.0, "latest_macd_signal": 0.5,
                "source": "s", "updated_at": "u", "data_status": "live"}
        off = synthesize("T", tech, {}, {}, {})
        on = synthesize("T", tech, {}, {}, {}, with_debate=True)
        assert on.debate_blocked is True
        assert on.signal == off.signal
        assert on.confidence == off.confidence


# ═══════════════════════════════════════════════════════════════
#   9. 说明书要求的最终输出字段
# ═══════════════════════════════════════════════════════════════

class TestRequiredOutputFields:
    def test_all_spec_fields_present(self):
        d = synthesize("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL,
                       REGIME_NEUTRAL, with_debate=True).to_dict()
        for k in ("signal", "confidence", "bull_arguments", "bear_arguments",
                  "reasoning", "source", "updated_at", "disclaimer"):
            assert k in d, f"缺少说明书要求字段 {k}"

    def test_debate_carries_consistency_and_bias(self):
        d = synthesize("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL,
                       REGIME_NEUTRAL, with_debate=True).to_dict()
        assert "consistency_check" in d["debate"]
        assert "bias_report" in d["debate"]

    def test_markdown_shows_block_mark(self):
        tech = {"latest_rsi": 25.0, "latest_macd": 2.0, "latest_macd_signal": 0.5,
                "source": "s", "updated_at": "u", "data_status": "live"}
        md = synthesize("T", tech, {}, {}, {}, with_debate=True).to_markdown()
        assert "辩论校验" in md or "阻断" in md


# ═══════════════════════════════════════════════════════════════
#   10. 离线端到端（禁网）
# ═══════════════════════════════════════════════════════════════

class TestOfflineEndToEnd:
    @pytest.fixture
    def no_network(self, monkeypatch):
        """硬禁 akshare 导入，确保本组测试零网络调用。"""
        real_import = builtins.__import__

        def mock_import(name, *args, **kw):
            if name == "akshare":
                raise ImportError("测试禁网：akshare 不可用")
            return real_import(name, *args, **kw)

        monkeypatch.setattr(builtins, "__import__", mock_import)

    def test_four_agents_all_contribute_evidence(self, no_network):
        """
        回归防护：字段名写错时 agent_coverage 会静默缺维度。
        这里用真实 Agent 输出跑通，要求四个维度全部到场。
        """
        from agent_platform.finance.analysis import analyze_security
        from agent_platform.finance.fundamental_agent import analyze_fundamental
        from agent_platform.finance.industry_agent import analyze_industry
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        tech = analyze_security("DEMO001").to_dict()
        fund = analyze_fundamental("DEMO001", force_offline=True).to_dict()
        ind = analyze_industry("DEMO001", force_offline=True).to_dict()
        regime = analyze_market_regime(force_offline=True).to_dict()

        r = run_debate("DEMO001", tech, fund, ind, regime)
        coverage = r.bias_report["agent_coverage"]
        assert "technical" in coverage
        assert "fundamental" in coverage
        assert "industry" in coverage
        assert "market_regime" in coverage

    def test_offline_run_not_blocked(self, no_network):
        """离线样例不得因数据质量被阻断，否则 20 股验收全灭。"""
        from agent_platform.finance.analysis import analyze_security
        from agent_platform.finance.fundamental_agent import analyze_fundamental
        from agent_platform.finance.industry_agent import analyze_industry
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        tech = analyze_security("DEMO001").to_dict()
        fund = analyze_fundamental("DEMO001", force_offline=True).to_dict()
        ind = analyze_industry("DEMO001", force_offline=True).to_dict()
        regime = analyze_market_regime(force_offline=True).to_dict()

        r = run_debate("DEMO001", tech, fund, ind, regime)
        assert r.blocked is False, r.blocking_reasons

    def test_offline_evidence_never_claims_live(self, no_network):
        """样例数据不得被标成实时数据。"""
        from agent_platform.finance.fundamental_agent import analyze_fundamental

        fund = analyze_fundamental("DEMO001", force_offline=True).to_dict()
        bull, bear = extract_evidence({}, fund, {}, {})
        for e in bull + bear:
            assert e.data_status != "live"


# ═══════════════════════════════════════════════════════════════
#   11. 主链确实启用了辩论
# ═══════════════════════════════════════════════════════════════

class TestGraphWiring:
    def test_synthesis_node_enables_debate(self):
        from agent_platform.finance.securities_graph import node_synthesis_agent

        out = node_synthesis_agent({
            "symbol": "T",
            "technical_analysis": TECH_BULL,
            "fundamental_analysis": FUND_NEUTRAL,
            "industry_analysis": IND_NEUTRAL,
            "market_regime": REGIME_NEUTRAL,
        })
        assert out["status"] == "synthesis_done"
        assert out["synthesis"]["debate"] is not None
        assert len(out["synthesis"]["debate"]["rounds"]) == 2

    def test_debate_mark_does_not_write_errors(self):
        """
        errors 非空会被 resolve_research_status() 判为 failed。
        辩论标记必须走 synthesis 字典，不得污染 errors。
        """
        from agent_platform.finance.securities_graph import node_synthesis_agent

        tech = {"latest_rsi": 25.0, "latest_macd": 2.0, "latest_macd_signal": 0.5,
                "source": "s", "updated_at": "u", "data_status": "live"}
        out = node_synthesis_agent({
            "symbol": "T",
            "technical_analysis": tech,
            "fundamental_analysis": {},
            "industry_analysis": {},
            "market_regime": {},
        })
        assert out["synthesis"]["debate_blocked"] is True
        assert "errors" not in out

    def test_node_confidence_unchanged_by_debate(self):
        from agent_platform.finance.securities_graph import node_synthesis_agent

        out = node_synthesis_agent({
            "symbol": "T",
            "technical_analysis": TECH_BULL,
            "fundamental_analysis": FUND_NEUTRAL,
            "industry_analysis": IND_NEUTRAL,
            "market_regime": REGIME_NEUTRAL,
        })
        expected = synthesize("T", TECH_BULL, FUND_NEUTRAL, IND_NEUTRAL,
                              REGIME_NEUTRAL).confidence
        assert out["confidence"] == pytest.approx(expected)
