"""
LangGraph 证券分析工作流测试
=================================
覆盖以下场景（全部离线，无真实网络调用）：

 LG-01  图编译成功
 LG-02  四个并行分析节点执行并正确汇合至 synthesis
 LG-03  synthesis 不会提前运行（并行节点未完成时）
 LG-04  高置信度路径进入 trader_agent
 LG-05  低置信度跳过交易（no_trade）
 LG-06  preflight execute 状态正确
 LG-07  preflight manual_review → interrupt 暂停，resume approve → execute
 LG-08  preflight manual_review → interrupt 暂停，resume reject → block
 LG-09  HumanApprovalRequired → interrupt，resume approve
 LG-10  HumanApprovalRequired → interrupt，resume reject
 LG-11  checkpoint 恢复：同一 thread_id 可查询状态
 LG-12  节点失败时异常可见且下游不运行
 LG-13  并行状态不相互覆盖
 LG-14  服务层 deep_research 确实调用 LangGraph
"""
from __future__ import annotations

import uuid

import pytest

# ── 导入被测模块 ───────────────────────────────────────────────────────────────
from agent_platform.finance.securities_graph import (
    SecuritiesAnalysisState,
    build_securities_graph,
    node_synthesis_agent,
    run_securities_analysis,
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command


# ── 公共 fixture ───────────────────────────────────────────────────────────────

SAMPLE_TECH = {
    "symbol": "600519", "latest_close": 1800.0, "total_return_pct": 5.0,
    "latest_rsi": 55.0, "latest_macd": 0.5, "latest_macd_signal": 0.3,
    "latest_ma5": 1790.0, "latest_ma20": 1750.0, "latest_bb_position_pct": 50.0,
    "source": "sample", "updated_at": "2026-08-01T00:00:00Z",
}
SAMPLE_FUND = {
    "symbol": "600519", "name": "贵州茅台", "pe_ttm": 30.0, "pb": 8.0,
    "valuation_signal": "fairly_valued", "valuation_note": "PE合理",
    "source": "sample", "updated_at": "2026-08-01T00:00:00Z",
    "total_market_value_cny": 2e12, "roe_pct": 30.0, "disclaimer": "仅供参考",
}
SAMPLE_IND = {
    "symbol": "600519", "industry_name": "白酒",
    "prosperity_signal": "normal", "prosperity_note": "中性",
    "source": "sample", "updated_at": "2026-08-01T00:00:00Z",
    "top_stocks": [], "fund_flow_3d_cny": None, "disclaimer": "仅供参考",
}
SAMPLE_REGIME = {
    "regime": "bull", "risk_appetite": "high",
    "index_code": "000001", "index_close": 3200.0,
    "index_change_pct_5d": 4.0, "northbound_flow_cny": 1e9,
    "regime_note": "大盘上升", "source": "sample",
    "updated_at": "2026-08-01T00:00:00Z", "disclaimer": "仅供参考",
}
SAMPLE_SYNTH_HIGH = {
    "symbol": "600519", "signal": "buy", "confidence": 0.75,
    "target_price_low": 1850.0, "target_price_high": 1900.0,
    "bull_arguments": ["RSI超卖"], "bear_arguments": [],
    "reasoning": "多头信号", "source": "synthesis",
    "updated_at": "2026-08-01T00:00:00Z", "disclaimer": "仅供参考",
}
SAMPLE_SYNTH_LOW = {**SAMPLE_SYNTH_HIGH, "signal": "sell", "confidence": 0.2}
SAMPLE_TRADER = {
    "symbol": "600519", "signal": "buy",
    "position_pct_suggestion": 7.5, "rationale": "测试",
    "source": "trader", "updated_at": "2026-08-01T00:00:00Z",
    "disclaimer": "仅供参考",
    "target_price_low": 1850.0, "target_price_high": 1900.0, "stop_loss_price": 1700.0,
}
SAMPLE_RISK = {
    "symbol": "600519", "approved_position_pct": 2.0,
    "risk_flags": [], "final_signal": "buy", "risk_note": "通过",
    "source": "risk_manager", "updated_at": "2026-08-01T00:00:00Z",
    "disclaimer": "仅供参考",
}


def _patch_all_agents(monkeypatch):
    """将四个并行 Agent 全部 patch 为确定性 sample 数据，避免网络调用。"""
    from agent_platform.finance import (
        analysis, fundamental_agent, industry_agent, market_regime_agent,
    )

    class _FakeTechResult:
        def to_dict(self):
            return SAMPLE_TECH
        def to_markdown(self):
            return "tech md"

    class _FakeFundResult:
        def to_dict(self):
            return SAMPLE_FUND
        def to_markdown(self):
            return "fund md"

    class _FakeIndResult:
        def to_dict(self):
            return SAMPLE_IND
        def to_markdown(self):
            return "ind md"

    class _FakeRegResult:
        def to_dict(self):
            return SAMPLE_REGIME
        def to_markdown(self):
            return "regime md"

    monkeypatch.setattr(analysis, "analyze_security",
                        lambda *a, **kw: _FakeTechResult())
    monkeypatch.setattr(fundamental_agent, "analyze_fundamental",
                        lambda *a, **kw: _FakeFundResult())
    monkeypatch.setattr(industry_agent, "analyze_industry",
                        lambda *a, **kw: _FakeIndResult())
    monkeypatch.setattr(market_regime_agent, "analyze_market_regime",
                        lambda *a, **kw: _FakeRegResult())


def _patch_synthesis(monkeypatch, synth_dict=None):
    from agent_platform.finance import synthesis_agent

    class _FakeSynthResult:
        def __init__(self):
            d = synth_dict or SAMPLE_SYNTH_HIGH
            self.signal = d["signal"]
            self.confidence = d["confidence"]
        def to_dict(self):
            return synth_dict or SAMPLE_SYNTH_HIGH
        def to_markdown(self):
            return "synth md"

    monkeypatch.setattr(synthesis_agent, "synthesize",
                        lambda *a, **kw: _FakeSynthResult())


def _patch_trader(monkeypatch, raise_har=False, position_pct=7.5):
    from agent_platform.finance import trader_agent

    def _fake_generate(*a, **kw):
        if raise_har:
            # Issue 2 修复后，必须携带 trader_result payload
            class _FakeTraderResult:
                def to_dict(self):
                    return {**SAMPLE_TRADER, "position_pct_suggestion": 15.0}
                def to_markdown(self):
                    return "trader md (pending approval)"
            exc = trader_agent.HumanApprovalRequired("仓位超过10%")
            exc.trader_result = _FakeTraderResult()
            raise exc
        class _Res:
            def to_dict(self):
                return {**SAMPLE_TRADER, "position_pct_suggestion": position_pct}
            def to_markdown(self):
                return "trader md"
        return _Res()

    monkeypatch.setattr(trader_agent, "generate_trade_signal", _fake_generate)


def _patch_risk(monkeypatch):
    from agent_platform.finance import risk_manager_agent

    class _FakeRisk:
        def to_dict(self):
            return SAMPLE_RISK
        def to_markdown(self):
            return "risk md"

    monkeypatch.setattr(risk_manager_agent, "assess_risk",
                        lambda *a, **kw: _FakeRisk())


def _patch_harness(monkeypatch, action="execute"):
    from agent_platform.finance import trading_harness

    class _FakeHarnessResult:
        final_action = action
        def to_dict(self):
            return {
                "symbol": "600519", "approved": action == "execute",
                "final_action": action, "checks": [],
                "trader_result": SAMPLE_TRADER, "risk_result": SAMPLE_RISK,
                "timestamp": "2026-08-01T00:00:00Z",
            }
        def to_markdown(self):
            return "harness md"

    monkeypatch.setattr(
        trading_harness.TradingHarness, "run_preflight",
        lambda self, **kw: _FakeHarnessResult(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# LG-01  图编译成功
# ══════════════════════════════════════════════════════════════════════════════

def test_lg01_graph_compiles():
    """LG-01: 图能够成功编译，无异常。"""
    g = build_securities_graph()
    assert g is not None
    # 验证节点集合
    node_names = set(g.nodes.keys())
    required = {
        "technical_agent", "fundamental_agent", "industry_agent",
        "market_regime_agent", "synthesis_agent", "trader_agent",
        "human_approval", "risk_manager", "trading_harness",
    }
    assert required.issubset(node_names), f"缺少节点：{required - node_names}"


# ══════════════════════════════════════════════════════════════════════════════
# LG-02  四个并行节点执行并汇合
# ══════════════════════════════════════════════════════════════════════════════

def test_lg02_parallel_nodes_all_run(monkeypatch):
    """LG-02: 四个分析节点都运行，synthesis 收到所有四路结果。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    g = build_securities_graph()
    state = run_securities_analysis("600519", graph=g)

    assert state.get("technical_analysis") is not None, "technical_analysis 缺失"
    assert state.get("fundamental_analysis") is not None, "fundamental_analysis 缺失"
    assert state.get("industry_analysis") is not None, "industry_analysis 缺失"
    assert state.get("market_regime") is not None, "market_regime 缺失"
    assert state.get("synthesis") is not None, "synthesis 缺失（四路汇合失败）"


# ══════════════════════════════════════════════════════════════════════════════
# LG-03  synthesis 不会提前运行（缺少必要输入时报错而非伪造结果）
# ══════════════════════════════════════════════════════════════════════════════

def test_lg03_synthesis_requires_all_inputs():
    """LG-03: synthesis 节点在输入不完整时返回 error，不伪造结果。"""
    incomplete_state: SecuritiesAnalysisState = {
        "symbol": "600519",
        "request_id": "test",
        "technical_analysis": SAMPLE_TECH,
        "fundamental_analysis": None,   # 缺失
        "industry_analysis": SAMPLE_IND,
        "market_regime": SAMPLE_REGIME,
        "confidence": 0.0,
        "har_required": False,
        "har_detail": None,
        "status": "pending",
        "errors": [],
        "trace": {},
    }
    result = node_synthesis_agent(incomplete_state)
    assert result["synthesis"] is None, "输入不完整时不应生成 synthesis"
    assert result["status"] == "error"
    assert len(result["errors"]) > 0
    assert "fundamental_analysis" in result["errors"][0]


# ══════════════════════════════════════════════════════════════════════════════
# LG-04  高置信度路径进入 trader_agent
# ══════════════════════════════════════════════════════════════════════════════

def test_lg04_high_confidence_enters_trader(monkeypatch):
    """LG-04: 置信度 > 0.3 的路径应执行 trader_agent 并产生 trade_signal。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch, SAMPLE_SYNTH_HIGH)  # confidence=0.75
    _patch_trader(monkeypatch, raise_har=False)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    g = build_securities_graph()
    state = run_securities_analysis("600519", graph=g)

    assert state.get("trade_signal") is not None, "trade_signal 应存在（高置信度路径）"
    assert state.get("final_action") == "execute"


# ══════════════════════════════════════════════════════════════════════════════
# LG-05  低置信度跳过交易
# ══════════════════════════════════════════════════════════════════════════════

def test_lg05_low_confidence_skips_trading(monkeypatch):
    """LG-05: 置信度 ≤ 0.3 时不进入 trader_agent，trade_signal 为 None。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch, SAMPLE_SYNTH_LOW)  # confidence=0.2

    g = build_securities_graph()
    state = run_securities_analysis("600519", graph=g)

    assert state.get("trade_signal") is None, "低置信度不应有 trade_signal"
    assert state.get("risk_result") is None, "低置信度不应有 risk_result"
    assert state.get("preflight_result") is None, "低置信度不应有 preflight_result"


# ══════════════════════════════════════════════════════════════════════════════
# LG-06  preflight execute 状态
# ══════════════════════════════════════════════════════════════════════════════

def test_lg06_preflight_execute(monkeypatch):
    """LG-06: harness 返回 execute 时 final_action=='execute'。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    g = build_securities_graph()
    state = run_securities_analysis("600519", graph=g)

    assert state.get("final_action") == "execute"
    assert state.get("preflight_result") is not None


# ══════════════════════════════════════════════════════════════════════════════
# LG-07  manual_review → approve
# ══════════════════════════════════════════════════════════════════════════════

def test_lg07_manual_review_approve(monkeypatch):
    """LG-07: harness 返回 manual_review → interrupt → approve → execute。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="manual_review")

    cp = MemorySaver()
    g = build_securities_graph(checkpointer=cp)
    tid = uuid.uuid4().hex[:12]
    rid = tid

    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": rid,
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
    }
    config = {"configurable": {"thread_id": tid}}

    # 第一次 invoke → 应被 interrupt 暂停
    g.invoke(initial, config=config)
    # 状态应处于 interrupt（manual_review）
    snap = g.get_state(config)
    assert snap.next, "应有待执行的下一步（interrupt 暂停中）"

    # 人工 approve → resume
    result2 = g.invoke(Command(resume="approve"), config=config)
    assert result2.get("final_action") == "execute", \
        f"approve 后 final_action 应为 execute，实际：{result2.get('final_action')}"


# ══════════════════════════════════════════════════════════════════════════════
# LG-08  manual_review → reject
# ══════════════════════════════════════════════════════════════════════════════

def test_lg08_manual_review_reject(monkeypatch):
    """LG-08: manual_review → interrupt → reject → block。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="manual_review")

    cp = MemorySaver()
    g = build_securities_graph(checkpointer=cp)
    tid = uuid.uuid4().hex[:12]
    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": tid,
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
    }
    config = {"configurable": {"thread_id": tid}}

    g.invoke(initial, config=config)  # pause at interrupt
    result = g.invoke(Command(resume="reject"), config=config)
    assert result.get("final_action") == "block", \
        f"reject 后 final_action 应为 block，实际：{result.get('final_action')}"


# ══════════════════════════════════════════════════════════════════════════════
# LG-09  HumanApprovalRequired → approve
# ══════════════════════════════════════════════════════════════════════════════

def test_lg09_har_approve(monkeypatch):
    """LG-09: HumanApprovalRequired → human_approval interrupt → approve → risk_manager。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)   # 触发 HumanApprovalRequired
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    cp = MemorySaver()
    g = build_securities_graph(checkpointer=cp)
    tid = uuid.uuid4().hex[:12]
    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": tid,
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
    }
    config = {"configurable": {"thread_id": tid}}

    g.invoke(initial, config=config)   # pause at human_approval
    snap = g.get_state(config)
    assert snap.next, "HumanApprovalRequired 后应有待执行步骤（interrupt 暂停中）"

    result = g.invoke(Command(resume="approve"), config=config)
    # approve 后应继续执行 risk_manager + harness → execute
    assert result.get("final_action") in ("execute", "block", "manual_review"), \
        "approve 后应完成后续流程"
    # 确认流程确实经过了 risk_manager
    assert result.get("risk_result") is not None, "approve 后应执行 risk_manager"


# ══════════════════════════════════════════════════════════════════════════════
# LG-10  HumanApprovalRequired → reject
# ══════════════════════════════════════════════════════════════════════════════

def test_lg10_har_reject(monkeypatch):
    """LG-10: HumanApprovalRequired → human_approval interrupt → reject → block。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    cp = MemorySaver()
    g = build_securities_graph(checkpointer=cp)
    tid = uuid.uuid4().hex[:12]
    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": tid,
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
    }
    config = {"configurable": {"thread_id": tid}}

    g.invoke(initial, config=config)
    result = g.invoke(Command(resume="reject"), config=config)
    assert result.get("final_action") == "block", \
        f"reject 后 final_action 应为 block，实际：{result.get('final_action')}"


# ══════════════════════════════════════════════════════════════════════════════
# LG-11  checkpoint 恢复
# ══════════════════════════════════════════════════════════════════════════════

def test_lg11_checkpoint_state_query(monkeypatch):
    """LG-11: 同一 thread_id 可查询状态（execute/manual_review/block 三种均可查）。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    cp = MemorySaver()
    g = build_securities_graph(checkpointer=cp)
    tid = "fixed-thread-001"
    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": tid,
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
    }
    config = {"configurable": {"thread_id": tid}}
    g.invoke(initial, config=config)

    # 可查询状态
    snap = g.get_state(config)
    assert snap is not None, "应能查询到 checkpoint 状态"
    saved = snap.values
    assert saved.get("symbol") == "600519"
    assert saved.get("final_action") == "execute"


# ══════════════════════════════════════════════════════════════════════════════
# LG-12  节点失败时异常可见且下游不运行
# ══════════════════════════════════════════════════════════════════════════════

def test_lg12_node_failure_propagates(monkeypatch):
    """LG-12: technical_agent 失败 → errors 有记录，synthesis 收不到 technical_analysis。"""
    from agent_platform.finance import analysis as analysis_mod

    monkeypatch.setattr(analysis_mod, "analyze_security",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟技术分析失败")))

    from agent_platform.finance import fundamental_agent, industry_agent, market_regime_agent

    class _FakeFund:
        def to_dict(self): return SAMPLE_FUND
        def to_markdown(self): return ""
    class _FakeInd:
        def to_dict(self): return SAMPLE_IND
        def to_markdown(self): return ""
    class _FakeReg:
        def to_dict(self): return SAMPLE_REGIME
        def to_markdown(self): return ""

    monkeypatch.setattr(fundamental_agent, "analyze_fundamental", lambda *a, **kw: _FakeFund())
    monkeypatch.setattr(industry_agent, "analyze_industry", lambda *a, **kw: _FakeInd())
    monkeypatch.setattr(market_regime_agent, "analyze_market_regime", lambda *a, **kw: _FakeReg())

    g = build_securities_graph()
    state = run_securities_analysis("600519", graph=g)

    # technical_analysis 应为 None
    assert state.get("technical_analysis") is None
    # errors 中应有 technical_agent 记录
    errors = state.get("errors") or []
    assert any("technical_agent" in e for e in errors), \
        f"errors 中应包含 technical_agent 记录，实际：{errors}"
    # synthesis 缺少必要输入 → status=error
    assert state.get("status") == "error"
    # trade_signal 不应存在（下游未运行）
    assert state.get("trade_signal") is None, "技术分析失败后不应生成 trade_signal"


# ══════════════════════════════════════════════════════════════════════════════
# LG-13  并行状态不相互覆盖
# ══════════════════════════════════════════════════════════════════════════════

def test_lg13_parallel_state_no_overwrite(monkeypatch):
    """LG-13: 四个并行节点各写自己的 key，互不覆盖。"""
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    g = build_securities_graph()
    state = run_securities_analysis("600519", graph=g)

    # 四个独立 key 都存在
    assert state.get("technical_analysis", {}).get("source") == "sample"
    assert state.get("fundamental_analysis", {}).get("source") == "sample"
    assert state.get("industry_analysis", {}).get("source") == "sample"
    assert state.get("market_regime", {}).get("source") == "sample"

    # 互不污染：fundamental 中不含 technical 的字段
    assert "latest_close" not in state.get("fundamental_analysis", {})
    assert "pe_ttm" not in state.get("technical_analysis", {})


# ══════════════════════════════════════════════════════════════════════════════
# LG-14  服务层 deep_research 调用 LangGraph
# ══════════════════════════════════════════════════════════════════════════════

def test_lg14_service_layer_uses_langgraph(monkeypatch):
    """LG-14: ApplicationService.deep_research 应调用 run_securities_analysis（LangGraph）。"""
    from agent_platform.finance import securities_graph as sg_mod

    called = []

    def _fake_run(symbol, request_id=None, graph=None, thread_id=None, data_mode="auto"):
        called.append(symbol)
        return {
            "symbol": symbol, "request_id": request_id or "x",
            "status": "preflight_done", "final_action": "execute",
            "confidence": 0.7, "errors": [], "trace": {},
            "trace_entries": [],  # Issue 7 修复后需要此字段
            "technical_analysis": {**SAMPLE_TECH, "_markdown": ""},
            "fundamental_analysis": {**SAMPLE_FUND, "_markdown": ""},
            "industry_analysis": {**SAMPLE_IND, "_markdown": ""},
            "market_regime": {**SAMPLE_REGIME, "_markdown": ""},
            "synthesis": {**SAMPLE_SYNTH_HIGH, "_markdown": ""},
            "trade_signal": {**SAMPLE_TRADER, "_markdown": ""},
            "risk_result": {**SAMPLE_RISK, "_markdown": ""},
            "preflight_result": {"final_action": "execute", "_markdown": ""},
            "har_required": False, "har_detail": None,
        }

    monkeypatch.setattr(sg_mod, "run_securities_analysis", _fake_run)

    from agent_platform.services.application_service import ApplicationService
    svc = ApplicationService()
    result = svc.deep_research("600519")

    assert called == ["600519"], "deep_research 应调用 run_securities_analysis（LangGraph）"
    assert result.symbol == "600519"
    assert result.synthesis is not None
    assert hasattr(result, "thread_id"), "DeepResearchResult 必须包含 thread_id（Issue 1）"


# ══════════════════════════════════════════════════════════════════════════════
# LG-09b  approve 后 risk_manager 收到原始 trade_signal
# ══════════════════════════════════════════════════════════════════════════════

def test_lg09b_risk_receives_trade_signal_after_approve(monkeypatch):
    """LG-09b: HumanApprovalRequired approve 后，risk_manager 收到完整的原始 trade_signal。

    Issue 2 修复验证：node_trader_agent 在触发 HAR 时必须把 TraderResult 存入
    state["trade_signal"]，使 approve 后 risk_manager 不会收到空信号。
    """
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)   # 携带 trader_result payload
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    cp = MemorySaver()
    g = build_securities_graph(checkpointer=cp)
    tid = uuid.uuid4().hex[:12]
    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": tid,
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
        "trace_entries": [],
    }
    config = {"configurable": {"thread_id": tid}}

    # 第一次调用 → 暂停在 human_approval
    g.invoke(initial, config=config)
    snap = g.get_state(config)
    assert snap.next, "应在 human_approval 处暂停"

    # trade_signal 必须在暂停时已存入 state（不能是 None）
    paused_state = snap.values
    assert paused_state.get("trade_signal") is not None, \
        "node_trader_agent 触发 HAR 时应将 TraderResult 存入 trade_signal（Issue 2）"
    assert paused_state["trade_signal"].get("position_pct_suggestion") == 15.0, \
        "trade_signal 应包含触发 HAR 的原始仓位（15%）"

    # approve → 继续执行
    result = g.invoke(Command(resume="approve"), config=config)
    assert result.get("risk_result") is not None, \
        "approve 后 risk_manager 必须执行（trade_signal 有值）"
    # risk_manager 不应报 trade_signal 为空的错误
    errors = result.get("errors") or []
    assert not any("trade_signal 为空" in e for e in errors), \
        f"risk_manager 不应报 trade_signal 为空：{errors}"


# ══════════════════════════════════════════════════════════════════════════════
# LG-15  数据源错误 → 结构化 error state（不 reraise）
# ══════════════════════════════════════════════════════════════════════════════

def test_lg15_datasource_error_to_error_state(monkeypatch):
    """LG-15: 数据源/网络错误 → 写入 errors，不抛出，下游可感知。

    Issue 8 修复验证：general Exception（如网络超时）应写入 errors 字段，
    而非向上 reraise 导致整个图崩溃。
    """
    from agent_platform.finance import analysis, industry_agent, market_regime_agent

    # 先 patch 其余正常节点
    class _FakeTechResult:
        def to_dict(self):
            return SAMPLE_TECH
        def to_markdown(self):
            return "tech md"

    class _FakeIndResult:
        def to_dict(self):
            return SAMPLE_IND
        def to_markdown(self):
            return "ind md"

    class _FakeRegResult:
        def to_dict(self):
            return SAMPLE_REGIME
        def to_markdown(self):
            return "regime md"

    monkeypatch.setattr(analysis, "analyze_security",
                        lambda *a, **kw: _FakeTechResult())
    monkeypatch.setattr(industry_agent, "analyze_industry",
                        lambda *a, **kw: _FakeIndResult())
    monkeypatch.setattr(market_regime_agent, "analyze_market_regime",
                        lambda *a, **kw: _FakeRegResult())

    # 单独 patch fundamental_agent 为抛出 IOError（网络错误）
    def _bad_fundamental(*a, **kw):
        raise IOError("网络超时")

    monkeypatch.setattr("agent_platform.finance.fundamental_agent.analyze_fundamental",
                        _bad_fundamental)

    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch)

    g = build_securities_graph()
    tid = uuid.uuid4().hex[:12]
    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": tid,
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
        "trace_entries": [],
    }
    config = {"configurable": {"thread_id": tid}}

    # 不应抛出，应正常返回
    result = g.invoke(initial, config=config)
    errors = result.get("errors") or []
    assert any("fundamental_agent" in e for e in errors), \
        f"数据源错误应记录到 errors 字段，当前：{errors}"
    # trace_entries 应有 fundamental_agent 且状态为 error
    te = result.get("trace_entries") or []
    fund_entries = [e for e in te if e.get("node") == "fundamental_agent"]
    assert fund_entries, "trace_entries 应含 fundamental_agent 条目"
    assert fund_entries[0].get("status") == "error", \
        "fundamental_agent trace_entries 状态应为 error"


# ══════════════════════════════════════════════════════════════════════════════
# LG-16  编程错误（TypeError）→ reraise
# ══════════════════════════════════════════════════════════════════════════════

def test_lg16_programming_error_reraises(monkeypatch):
    """LG-16: TypeError/AttributeError/NameError 属于编程错误，应 reraise 暴露 bug。

    Issue 8 修复验证：这类错误不应被吞掉写入 errors，而应直接传播到调用者。
    """
    _patch_all_agents(monkeypatch)

    from agent_platform.finance import fundamental_agent

    def _bad_fundamental(*a, **kw):
        raise TypeError("模拟编程错误：参数类型不对")

    monkeypatch.setattr(fundamental_agent, "analyze_fundamental", _bad_fundamental)

    g = build_securities_graph()
    tid = uuid.uuid4().hex[:12]
    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": tid,
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
        "trace_entries": [],
    }
    config = {"configurable": {"thread_id": tid}}

    # TypeError 应向上传播，不被吞掉
    with pytest.raises(TypeError, match="模拟编程错误"):
        g.invoke(initial, config=config)


# ══════════════════════════════════════════════════════════════════════════════
# LG-17  offline 模式零网络调用
# ══════════════════════════════════════════════════════════════════════════════

def test_lg17_offline_no_network(monkeypatch):
    """LG-17: data_mode="offline" 时零网络调用（socket/requests/httpx/tushare/akshare 全封锁）。

    Issue 6 修复验证：不仅替换 akshare 模块，还通过 socket.connect 封锁任何 TCP 连接，
    同时替换 requests/httpx 请求入口和 tushare 模块，确保没有任何网络通路。
    """
    import sys
    import socket
    import types

    # ── 封锁所有 TCP 连接（socket 层）─────────────────────────────────────────
    def _no_connect(*a, **kw):
        raise RuntimeError("offline 模式禁止任何网络连接（socket.connect）")

    monkeypatch.setattr(socket.socket, "connect", _no_connect)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError("offline 模式禁止任何网络连接（create_connection）")))

    # ── 封锁 requests ──────────────────────────────────────────────────────────
    bad_requests = types.ModuleType("requests")
    def _bad_requests_get(*a, **kw):
        raise RuntimeError("offline 模式禁止 requests 调用")
    bad_requests.get  = _bad_requests_get
    bad_requests.post = _bad_requests_get
    bad_requests.request = _bad_requests_get
    if "requests" in sys.modules:
        monkeypatch.setattr(sys.modules["requests"], "get",  _bad_requests_get)
        monkeypatch.setattr(sys.modules["requests"], "post", _bad_requests_get)
        monkeypatch.setattr(sys.modules["requests"], "request", _bad_requests_get)

    # ── 封锁 httpx ────────────────────────────────────────────────────────────
    if "httpx" in sys.modules:
        def _bad_httpx(*a, **kw):
            raise RuntimeError("offline 模式禁止 httpx 调用")
        monkeypatch.setattr(sys.modules["httpx"], "get",  _bad_httpx)
        monkeypatch.setattr(sys.modules["httpx"], "post", _bad_httpx)

    # ── 封锁 akshare（替换为抛异常哑模块）────────────────────────────────────
    bad_ak = types.ModuleType("akshare")
    def _bad_ak(*a, **kw):
        raise RuntimeError("offline 模式不应调用 AkShare")
    bad_ak.stock_zh_a_spot_em          = _bad_ak
    bad_ak.stock_board_industry_name_em = _bad_ak
    bad_ak.index_zh_a_hist             = _bad_ak
    monkeypatch.setitem(sys.modules, "akshare", bad_ak)

    # ── 封锁 tushare（替换为抛异常哑模块）────────────────────────────────────
    bad_ts = types.ModuleType("tushare")
    def _bad_ts(*a, **kw):
        raise RuntimeError("offline 模式不应调用 Tushare")
    bad_ts.pro_api = _bad_ts
    monkeypatch.setitem(sys.modules, "tushare", bad_ts)

    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch)

    g = build_securities_graph()
    tid = uuid.uuid4().hex[:12]
    initial: SecuritiesAnalysisState = {
        "symbol": "600519", "request_id": tid,
        "data_mode": "offline",         # 关键：注入 offline 模式
        "har_required": False, "har_detail": None,
        "confidence": 0.0, "status": "pending", "errors": [], "trace": {},
        "trace_entries": [],
    }
    config = {"configurable": {"thread_id": tid}}

    # 不应触发任何网络错误
    result = g.invoke(initial, config=config)
    errors = result.get("errors") or []
    net_errors = [e for e in errors if any(kw in e for kw in
                  ("AkShare", "Tushare", "network", "socket", "requests", "httpx"))]
    assert not net_errors, f"offline 模式不应有任何网络错误：{net_errors}"
    assert result["evaluator_summary"]["minimum_score"] >= 0
    assert "evaluator_agent" in {
        entry["node"] for entry in result.get("trace_entries", [])
    }


# ══════════════════════════════════════════════════════════════════════════════
# LG-18  完整 20 股票 offline 批量验证（封锁全部网络）
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
def test_lg18_offline_batch_20_stocks_no_network(monkeypatch):
    """LG-18: 全网络封锁下，offline 批量运行 20 只股票，全部无网络错误。

    Issue 6 修复验证（批量）：不仅验证单个图调用，还验证完整 20 只股票的批量验收。
    注意：此测试使用真实 SampleMarketDataProvider（不 patch Agent），
    仅封锁网络出口（socket.connect 等）。
    """
    import sys
    import socket
    import types

    # ── 封锁所有网络连接 ──────────────────────────────────────────────────────
    def _no_connect(*a, **kw):
        raise RuntimeError("offline 批量测试禁止任何网络连接")

    monkeypatch.setattr(socket.socket, "connect", _no_connect)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError("offline 批量测试禁止网络连接")))

    # 封锁 akshare / tushare 模块
    bad_ak = types.ModuleType("akshare")
    bad_ts = types.ModuleType("tushare")
    def bad_fn(*args, **kwargs):
        raise RuntimeError("offline 批量测试禁止网络模块调用")
    bad_ak.stock_zh_a_spot_em          = bad_fn
    bad_ak.stock_board_industry_name_em = bad_fn
    bad_ak.index_zh_a_hist             = bad_fn
    bad_ts.pro_api = bad_fn
    monkeypatch.setitem(sys.modules, "akshare", bad_ak)
    monkeypatch.setitem(sys.modules, "tushare", bad_ts)

    # ── 导入样例数据集 ────────────────────────────────────────────────────────
    import pathlib, importlib.util
    root = pathlib.Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "generate_sample_data",
        str(root / "Scripts" / "generate_sample_data.py"),
    )
    gsd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gsd)
    gsd.write_dataset(force=False)
    symbols = [row[0] for row in gsd.TEST_UNIVERSE]
    assert len(symbols) >= 20, f"需要 ≥20 只股票，实际：{len(symbols)}"

    from agent_platform.finance.securities_graph import build_securities_graph, run_securities_analysis
    graph = build_securities_graph()

    errors_by_stock: dict[str, list[str]] = {}
    for sym in symbols[:20]:
        state = run_securities_analysis(symbol=sym, graph=graph, data_mode="offline")
        stock_errors = state.get("errors") or []
        # 网络相关错误（应为0）
        net_err = [e for e in stock_errors if any(kw in e for kw in
                   ("AkShare", "Tushare", "network", "socket", "connect"))]
        if net_err:
            errors_by_stock[sym] = net_err

    assert not errors_by_stock, \
        f"offline 批量测试：以下股票出现网络错误：{errors_by_stock}"


# ══════════════════════════════════════════════════════════════════════════════
# API  研究接口单元测试（FastAPI TestClient）
# ══════════════════════════════════════════════════════════════════════════════

def test_api_research_start_and_state(monkeypatch):
    """API: POST /research/{symbol} 返回 completed 状态；mock 路径 GET state 精确返回 not_found。

    修复前：GET state 断言允许四种互相矛盾的状态（'not_found','completed','interrupted','failed'）。
    修复后：mock 路径不写入真实 checkpoint，所以 GET state 必须返回 not_found。
    """
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] 未安装")

    import agent_platform.finance.securities_graph as sg_mod
    import agent_platform.api.main as main_mod

    # 重置 API 层单例，避免跨测试污染
    main_mod._app_service = None

    # mock run_securities_analysis，返回完成状态（不写入真实 checkpoint）
    def _fake_run(symbol, request_id=None, graph=None, thread_id=None, data_mode="auto"):
        return {
            "symbol": symbol, "request_id": request_id or "x",
            "status": "preflight_done", "final_action": "execute",
            "confidence": 0.7, "errors": [], "trace": {}, "trace_entries": [],
            "technical_analysis": {**SAMPLE_TECH, "_markdown": ""},
            "fundamental_analysis": {**SAMPLE_FUND, "_markdown": ""},
            "industry_analysis": {**SAMPLE_IND, "_markdown": ""},
            "market_regime": {**SAMPLE_REGIME, "_markdown": ""},
            "synthesis": {**SAMPLE_SYNTH_HIGH, "_markdown": ""},
            "trade_signal": {**SAMPLE_TRADER, "_markdown": ""},
            "risk_result": {**SAMPLE_RISK, "_markdown": ""},
            "preflight_result": {"final_action": "execute", "_markdown": ""},
            "har_required": False, "har_detail": None,
        }

    monkeypatch.setattr(sg_mod, "run_securities_analysis", _fake_run)

    try:
        client = TestClient(main_mod.app)

        # POST /research/600519 — mock 路径不触发 interrupt，返回 completed
        resp = client.post("/research/600519")
        assert resp.status_code == 200, f"POST /research 失败：{resp.text}"
        body = resp.json()
        assert body["symbol"] == "600519"
        assert "thread_id" in body
        # 精确断言：mock 返回 final_action="execute"，无 interrupt，status 必须是 completed
        assert body["status"] == "completed", \
            f"mock 路径 final_action=execute 应返回 completed，实际：{body['status']}"
        # final_action 不能是 buy/sell/hold（Issue 3 修复验证）
        assert body.get("final_action") not in ("buy", "sell", "hold"), \
            f"final_action 不能是交易信号 buy/sell/hold，实际：{body.get('final_action')}"

        thread_id = body["thread_id"]

        # GET /research/{thread_id}/state — mock 路径不写入真实 checkpoint → not_found
        resp2 = client.get(f"/research/{thread_id}/state")
        assert resp2.status_code == 200
        state_body = resp2.json()
        assert state_body["thread_id"] == thread_id
        # 精确断言：mock 没有执行真实图，checkpoint 中无此 thread_id
        assert state_body["status"] == "not_found", \
            f"mock 路径 GET state 应返回 not_found，实际：{state_body['status']}"
    finally:
        if main_mod._app_service:
            main_mod._app_service.close()
        main_mod._app_service = None


def test_api_research_resume_invalid_thread(tmp_path):
    """API: POST /research/{thread_id}/resume 对不存在的 thread_id 返回 404。"""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] 未安装")

    import agent_platform.api.main as main_mod
    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    settings = Settings(sqlite_path=tmp_path / "test_404.db")
    main_mod._app_service = ApplicationService(settings=settings)

    try:
        client = TestClient(main_mod.app)
        resp = client.post("/research/nonexistent-thread-id/resume?decision=approve")
        assert resp.status_code == 404, f"无效 thread_id 应返回 404，实际：{resp.status_code}"
    finally:
        if main_mod._app_service:
            main_mod._app_service.close()
        main_mod._app_service = None


# ══════════════════════════════════════════════════════════════════════════════
# API-02  真实 SQLite checkpoint：interrupt → state → approve → completed
# ══════════════════════════════════════════════════════════════════════════════

def test_api_full_interrupt_approve_sqlite(monkeypatch, tmp_path):
    """API-02: 完整 HAR interrupt → approve 流程，使用真实 SQLite checkpoint。

    Issue 4 修复验证：
    - POST /research → status="interrupted"（不再硬编码 completed）
    - GET state → status="interrupted"，interrupt_payload 非空
    - POST resume approve → final_action="execute"
    - Re-GET state → status="completed"
    - SQLite checkpoint DB 存在
    - 已完成线程重复 resume → HTTP 409
    """
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] 未安装")

    import pathlib
    import agent_platform.api.main as main_mod
    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    # 用确定性 fake Agent 触发 HAR interrupt
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)   # 仓位 15% > 10%，触发 HAR
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    settings = Settings(sqlite_path=tmp_path / "api02.db")
    main_mod._app_service = ApplicationService(settings=settings)

    try:
        client = TestClient(main_mod.app)

        # ── 步骤1：启动分析，期望暂停在 HAR interrupt ────────────────────────
        resp1 = client.post("/research/600519")
        assert resp1.status_code == 200, f"POST /research 失败：{resp1.text}"
        body1 = resp1.json()
        assert body1["status"] == "interrupted", \
            f"HAR 触发后 POST /research 应返回 interrupted，实际：{body1['status']}"
        assert body1["final_action"] is None, \
            f"interrupt 时 final_action 必须为 None，实际：{body1['final_action']}"
        thread_id = body1["thread_id"]

        # ── 步骤2：查询状态，确认 interrupt_payload 含 trade_signal ────────────
        resp2 = client.get(f"/research/{thread_id}/state")
        assert resp2.status_code == 200
        state2 = resp2.json()
        assert state2["status"] == "interrupted", \
            f"GET state 应返回 interrupted，实际：{state2['status']}"
        assert state2["interrupt_payload"] is not None, \
            "GET state 的 interrupt_payload 不能为 None"

        # ── 步骤3：approve → 继续执行 risk_manager + harness ────────────────
        resp3 = client.post(f"/research/{thread_id}/resume?decision=approve")
        assert resp3.status_code == 200, f"approve 失败：{resp3.text}"
        resume3 = resp3.json()
        assert resume3["final_action"] == "execute", \
            f"approve 后 final_action 应为 execute，实际：{resume3['final_action']}"

        # ── 步骤4：重新查询状态，应为 completed ───────────────────────────────
        resp4 = client.get(f"/research/{thread_id}/state")
        state4 = resp4.json()
        assert state4["status"] == "completed", \
            f"approve 完成后 GET state 应为 completed，实际：{state4['status']}"
        assert state4["final_action"] == "execute"

        # ── 步骤5：SQLite checkpoint DB 必须存在 ──────────────────────────────
        lg_db = pathlib.Path(tmp_path) / "api02_lg_checkpoints.db"
        assert lg_db.exists(), f"SQLite checkpoint DB 应存在：{lg_db}"

        # ── 步骤6：已完成线程重复 resume → HTTP 409 ──────────────────────────
        resp5 = client.post(f"/research/{thread_id}/resume?decision=approve")
        assert resp5.status_code == 409, \
            f"已完成 thread 重复 resume 应返回 409，实际：{resp5.status_code}"
    finally:
        if main_mod._app_service:
            main_mod._app_service.close()
        main_mod._app_service = None


# ══════════════════════════════════════════════════════════════════════════════
# API-03  真实 SQLite checkpoint：interrupt → reject → block
# ══════════════════════════════════════════════════════════════════════════════

def test_api_full_interrupt_reject_sqlite(monkeypatch, tmp_path):
    """API-03: HAR interrupt → reject 流程，final_action="block"，重复 resume 返回 409。"""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] 未安装")

    import agent_platform.api.main as main_mod
    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    settings = Settings(sqlite_path=tmp_path / "api03.db")
    main_mod._app_service = ApplicationService(settings=settings)

    try:
        client = TestClient(main_mod.app)

        resp1 = client.post("/research/600519")
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert body1["status"] == "interrupted"
        thread_id = body1["thread_id"]

        # reject
        resp2 = client.post(f"/research/{thread_id}/resume?decision=reject")
        assert resp2.status_code == 200, f"reject 失败：{resp2.text}"
        resume2 = resp2.json()
        assert resume2["final_action"] == "block", \
            f"reject 后 final_action 应为 block，实际：{resume2['final_action']}"

        # 重复 resume → 409
        resp3 = client.post(f"/research/{thread_id}/resume?decision=reject")
        assert resp3.status_code == 409, \
            f"已 block 的 thread 重复 resume 应返回 409，实际：{resp3.status_code}"
    finally:
        if main_mod._app_service:
            main_mod._app_service.close()
        main_mod._app_service = None


# ══════════════════════════════════════════════════════════════════════════════
# API-04  进程内服务实例重建后仍能恢复 checkpoint
# ══════════════════════════════════════════════════════════════════════════════

def test_api_service_rebuild_recovers_checkpoint(monkeypatch, tmp_path):
    """API-04: 销毁 ApplicationService 实例后重建，仍能用同一 thread_id 查询 checkpoint。

    Issue 1 修复验证：使用 SQLite（不是 MemorySaver），checkpoint 写磁盘后跨实例可读。
    """
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService
    settings = Settings(sqlite_path=tmp_path / "rebuild.db")

    # ── 第一个服务实例：执行分析到 interrupt ─────────────────────────────────
    svc1 = ApplicationService(settings=settings)
    result1 = svc1.deep_research("600519")
    assert result1.status == "interrupted", \
        f"第一次 deep_research 应返回 interrupted，实际：{result1.status}"
    thread_id = result1.thread_id
    svc1.close()  # 显式关闭 SQLite 连接（避免 Windows 下文件占用；del 不等于确定关闭）

    # ── 第二个服务实例：从同一 SQLite 文件重建 ───────────────────────────────
    svc2 = ApplicationService(settings=settings)

    # 通过第二个实例的图查询 checkpoint
    snap = svc2._securities_graph.get_state({"configurable": {"thread_id": thread_id}})
    assert snap is not None, "第二个服务实例应能查询到 SQLite checkpoint"
    assert snap.values, "snapshot.values 不能为空"
    assert snap.values.get("symbol") == "600519"

    # 确认仍处于 interrupt 状态
    has_interrupt = any(
        hasattr(t, "interrupts") and t.interrupts
        for t in (snap.tasks or [])
    )
    assert has_interrupt, "第二个实例查询应显示仍处于 interrupt 状态"

    # 用第二个实例 resume → 应能继续执行
    from langgraph.types import Command as _Cmd
    final = svc2._securities_graph.invoke(
        _Cmd(resume="approve"),
        config={"configurable": {"thread_id": thread_id}},
    )
    assert final.get("final_action") == "execute", \
        f"第二个实例 approve 后 final_action 应为 execute，实际：{final.get('final_action')}"
    svc2.close()  # 显式关闭第二个实例的 SQLite 连接


# ══════════════════════════════════════════════════════════════════════════════
# API-05  关闭后重建服务仍能读取并恢复 checkpoint
# ══════════════════════════════════════════════════════════════════════════════

def test_api_service_close_and_reopen_recovers_checkpoint(monkeypatch, tmp_path):
    """API-05: svc.close() 后用同一 SQLite 路径重建 ApplicationService，仍能恢复 checkpoint。

    验证：
    - close() 显式关闭连接后 SQLite 文件未损坏
    - 重建后新连接可读取旧 checkpoint
    - 重建后可通过 svc2.close() 释放新连接
    """
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    settings = Settings(sqlite_path=tmp_path / "close_reopen.db")

    svc1 = ApplicationService(settings=settings)
    result1 = svc1.deep_research("600519")
    assert result1.status == "interrupted"
    thread_id = result1.thread_id

    svc1.close()  # 显式关闭连接
    assert svc1._langgraph_checkpoint_conn is None, "close() 后连接应置为 None"

    # 重建实例 — SQLite 文件仍完好
    svc2 = ApplicationService(settings=settings)
    snap = svc2._securities_graph.get_state({"configurable": {"thread_id": thread_id}})
    assert snap is not None and snap.values, "重建后应能查询到 checkpoint"
    assert snap.values.get("symbol") == "600519"

    svc2.close()
    assert svc2._langgraph_checkpoint_conn is None, "第二次 close() 后连接应置为 None"


# ══════════════════════════════════════════════════════════════════════════════
# STATUS-01  no_trade 状态一致性：POST start 与 GET state 返回相同 status
# ══════════════════════════════════════════════════════════════════════════════

def test_api_status_consistency_no_trade(monkeypatch, tmp_path):
    """STATUS-01: 低置信度 no_trade 路径：POST /research 和 GET /research/state 返回相同 status。

    需求：
    - POST /research 应返回 status="no_trade"
    - GET /research/{thread_id}/state 也应返回 status="no_trade"（之前错误返回 "completed"）
    """
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] 未安装")

    import agent_platform.api.main as main_mod
    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    # 使用低置信度（0.2）patch synthesis → 进入 no_trade 路径（不触发 trader）
    _patch_all_agents(monkeypatch)

    import agent_platform.finance.synthesis_agent as synth_mod

    def _fake_synth_low(symbol, **kw):
        class _SR:
            confidence = SAMPLE_SYNTH_LOW["confidence"]  # securities_graph.py 直接访问 result.confidence
            def to_dict(self):
                return {**SAMPLE_SYNTH_LOW, "_markdown": "low conf"}
            def to_markdown(self):
                return "low conf"
        return _SR()

    monkeypatch.setattr(synth_mod, "synthesize", _fake_synth_low)

    settings = Settings(sqlite_path=tmp_path / "no_trade_cons.db")
    main_mod._app_service = ApplicationService(settings=settings)

    try:
        client = TestClient(main_mod.app)

        resp_post = client.post("/research/600519")
        assert resp_post.status_code == 200, f"POST /research 失败：{resp_post.text}"
        post_body = resp_post.json()
        post_status = post_body["status"]
        thread_id = post_body["thread_id"]

        resp_get = client.get(f"/research/{thread_id}/state")
        assert resp_get.status_code == 200
        get_status = resp_get.json()["status"]

        assert post_status == "no_trade", \
            f"低置信度 POST /research 应返回 no_trade，实际：{post_status}"
        assert get_status == "no_trade", \
            f"GET state 应与 POST 一致返回 no_trade，实际：{get_status}"
        assert post_status == get_status, \
            f"POST status={post_status!r} 与 GET status={get_status!r} 不一致"
    finally:
        if main_mod._app_service:
            main_mod._app_service.close()
        main_mod._app_service = None


# ══════════════════════════════════════════════════════════════════════════════
# STATUS-02  approve 后 POST resume 与 GET state 返回相同 status
# ══════════════════════════════════════════════════════════════════════════════

def test_api_status_consistency_approve(monkeypatch, tmp_path):
    """STATUS-02: interrupt → approve 后，POST resume 和 GET state 返回相同 status="completed"。"""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] 未安装")

    import agent_platform.api.main as main_mod
    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    settings = Settings(sqlite_path=tmp_path / "approve_cons.db")
    main_mod._app_service = ApplicationService(settings=settings)

    try:
        client = TestClient(main_mod.app)

        resp1 = client.post("/research/600519")
        assert resp1.json()["status"] == "interrupted"
        thread_id = resp1.json()["thread_id"]

        resp_resume = client.post(f"/research/{thread_id}/resume?decision=approve")
        assert resp_resume.status_code == 200, f"approve 失败：{resp_resume.text}"
        resume_status = resp_resume.json()["status"]

        resp_get = client.get(f"/research/{thread_id}/state")
        get_status = resp_get.json()["status"]

        assert resume_status == "completed", \
            f"approve 后 POST resume 应返回 completed，实际：{resume_status}"
        assert get_status == "completed", \
            f"approve 后 GET state 应返回 completed，实际：{get_status}"
        assert resume_status == get_status, \
            f"POST resume status={resume_status!r} 与 GET status={get_status!r} 不一致"
    finally:
        if main_mod._app_service:
            main_mod._app_service.close()
        main_mod._app_service = None


# ══════════════════════════════════════════════════════════════════════════════
# STATUS-03  reject 后 POST resume 与 GET state 返回相同 status="blocked"
# ══════════════════════════════════════════════════════════════════════════════

def test_api_status_consistency_reject(monkeypatch, tmp_path):
    """STATUS-03: interrupt → reject 后，POST resume 和 GET state 返回相同 status="blocked"。"""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] 未安装")

    import agent_platform.api.main as main_mod
    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)
    _patch_trader(monkeypatch, raise_har=True)
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    settings = Settings(sqlite_path=tmp_path / "reject_cons.db")
    main_mod._app_service = ApplicationService(settings=settings)

    try:
        client = TestClient(main_mod.app)

        resp1 = client.post("/research/600519")
        assert resp1.json()["status"] == "interrupted"
        thread_id = resp1.json()["thread_id"]

        resp_resume = client.post(f"/research/{thread_id}/resume?decision=reject")
        assert resp_resume.status_code == 200, f"reject 失败：{resp_resume.text}"
        resume_status = resp_resume.json()["status"]
        resume_final  = resp_resume.json()["final_action"]

        resp_get = client.get(f"/research/{thread_id}/state")
        get_status = resp_get.json()["status"]
        get_final  = resp_get.json()["final_action"]

        assert resume_final == "block", \
            f"reject 后 final_action 应为 block，实际：{resume_final}"
        assert resume_status == "blocked", \
            f"reject 后 POST resume 应返回 blocked，实际：{resume_status}"
        assert get_status == "blocked", \
            f"reject 后 GET state 应返回 blocked，实际：{get_status}"
        assert resume_status == get_status, \
            f"POST resume status={resume_status!r} 与 GET status={get_status!r} 不一致"
        assert resume_final == get_final, \
            f"POST resume final_action={resume_final!r} 与 GET final_action={get_final!r} 不一致"
    finally:
        if main_mod._app_service:
            main_mod._app_service.close()
        main_mod._app_service = None


# ══════════════════════════════════════════════════════════════════════════════
# STATUS-04  failed 状态一致性：POST start 与 GET state 返回相同 status="failed"
# ══════════════════════════════════════════════════════════════════════════════

def test_api_status_consistency_failed(monkeypatch, tmp_path):
    """STATUS-04: 数据源错误导致 errors 非空时，POST /research 和 GET state 均返回 failed。"""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] 未安装")

    import agent_platform.api.main as main_mod
    import agent_platform.finance.fundamental_agent as fund_mod
    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    # 仅 fundamental_agent 失败（IOError → 写入 errors，图继续运行到 END）
    _patch_all_agents(monkeypatch)
    _patch_synthesis(monkeypatch)   # synthesis 正常执行，但置信度来自 patch
    _patch_trader(monkeypatch)      # trader 正常
    _patch_risk(monkeypatch)
    _patch_harness(monkeypatch, action="execute")

    def _fail_fundamental(*a, **kw):
        raise IOError("模拟数据源网络超时")

    monkeypatch.setattr(fund_mod, "analyze_fundamental", _fail_fundamental)

    # 让 synthesis 返回低置信度 → no_trade，使 final_action=None 同时 errors 非空 → failed
    import agent_platform.finance.synthesis_agent as synth_mod

    def _fake_synth_low(*a, **kw):
        class _SR:
            def to_dict(self):
                return {**SAMPLE_SYNTH_LOW, "_markdown": ""}
            def to_markdown(self):
                return ""
        return _SR()

    monkeypatch.setattr(synth_mod, "synthesize", _fake_synth_low)

    settings = Settings(sqlite_path=tmp_path / "failed_cons.db")
    main_mod._app_service = ApplicationService(settings=settings)

    try:
        client = TestClient(main_mod.app)

        resp_post = client.post("/research/600519")
        assert resp_post.status_code == 200, f"POST 失败：{resp_post.text}"
        post_status = resp_post.json()["status"]
        thread_id = resp_post.json()["thread_id"]
        post_errors = resp_post.json()["errors"]

        resp_get = client.get(f"/research/{thread_id}/state")
        get_status = resp_get.json()["status"]
        get_errors = resp_get.json()["errors"]

        assert post_errors, "errors 不能为空（fundamental_agent 应记录错误）"
        assert post_status == "failed", \
            f"有错误且无 final_action 时 POST /research 应返回 failed，实际：{post_status}"
        assert get_status == "failed", \
            f"GET state 应与 POST 一致返回 failed，实际：{get_status}"
        assert post_status == get_status, \
            f"POST status={post_status!r} 与 GET status={get_status!r} 不一致"
        assert get_errors, "GET state errors 不能为空"
    finally:
        if main_mod._app_service:
            main_mod._app_service.close()
        main_mod._app_service = None


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG-01  Settings.langgraph_use_memory_saver 环境变量解析
# ══════════════════════════════════════════════════════════════════════════════

def test_config_langgraph_memory_saver_env_var(monkeypatch):
    """CONFIG-01: LANGGRAPH_USE_MEMORY_SAVER 环境变量严格布尔解析。

    验证：
    - 默认值 false → False
    - "true"/"TRUE"/"1"/"yes"/"on" → True
    - "false"/"FALSE"/"0"/"no" → False
    - bool("false") 陷阱：字符串 "false" 不能被 bool() 解析为 False（此函数正确处理）
    """
    import agent_platform.config as cfg_mod

    def _reload_get_settings(env_val: str | None):
        """设置环境变量后重新调用 get_settings()（不需要 reload 模块，函数每次重读 os.environ）。"""
        if env_val is None:
            monkeypatch.delenv("LANGGRAPH_USE_MEMORY_SAVER", raising=False)
        else:
            monkeypatch.setenv("LANGGRAPH_USE_MEMORY_SAVER", env_val)
        return cfg_mod.get_settings()

    # 默认（环境变量不存在）→ False
    s = _reload_get_settings(None)
    assert s.langgraph_use_memory_saver is False, \
        "默认未设置环境变量时应为 False"

    # "false" → False（验证不会踩 bool('false')==True 陷阱）
    s = _reload_get_settings("false")
    assert s.langgraph_use_memory_saver is False, \
        "'false' 应解析为 False，不能用 bool(str) 直接转换"

    # "FALSE" → False
    s = _reload_get_settings("FALSE")
    assert s.langgraph_use_memory_saver is False

    # "0" → False
    s = _reload_get_settings("0")
    assert s.langgraph_use_memory_saver is False

    # "true" → True
    s = _reload_get_settings("true")
    assert s.langgraph_use_memory_saver is True, \
        "'true' 应解析为 True"

    # "TRUE" → True
    s = _reload_get_settings("TRUE")
    assert s.langgraph_use_memory_saver is True

    # "1" → True
    s = _reload_get_settings("1")
    assert s.langgraph_use_memory_saver is True

    # "yes" → True
    s = _reload_get_settings("yes")
    assert s.langgraph_use_memory_saver is True

    # "on" → True
    s = _reload_get_settings("on")
    assert s.langgraph_use_memory_saver is True
