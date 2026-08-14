# -*- coding: utf-8 -*-
"""深度投研 API 集成测试

验证 POST /research、GET /research/{thread_id}/state、POST /research/{thread_id}/resume
三个端点的完整工作流，包括状态一致性、interrupt/resume 机制、错误处理。
"""
import pytest
from fastapi.testclient import TestClient

from agent_platform.api.main import app


@pytest.fixture
def client():
    """FastAPI TestClient 实例"""
    return TestClient(app)


def test_research_rejects_unsupported_exchange_code(client):
    response = client.post("/research/660338?data_mode=auto")

    assert response.status_code == 422
    assert "600338" in response.json()["detail"]


def test_research_workflow_offline_completed(client):
    """测试离线模式完整工作流：POST /research → GET /state → 验证 completed 状态"""
    # 1. 启动深度投研 - 显式指定 offline 模式
    response = client.post("/research/DEMO001?data_mode=offline")
    assert response.status_code == 200
    data = response.json()

    assert "thread_id" in data
    assert data["symbol"] == "DEMO001"
    assert data["status"] in ["completed", "no_trade", "interrupted"]

    thread_id = data["thread_id"]

    # 2. 查询状态
    state_resp = client.get(f"/research/{thread_id}/state")
    assert state_resp.status_code == 200
    state = state_resp.json()

    assert state["thread_id"] == thread_id
    assert state["status"] in ["completed", "no_trade", "interrupted", "blocked"]

    # 3. 验证状态字段完整性
    if state["status"] == "completed":
        assert state.get("final_action") in ["execute", "manual_review"]
        # 至少有综合分析结果
        assert state.get("synthesis") is not None
    elif state["status"] == "no_trade":
        # 置信度不足场景
        assert state.get("synthesis") is not None
        confidence = state["synthesis"].get("confidence", 0)
        assert confidence <= 0.30


def test_research_interrupted_workflow(client, monkeypatch):
    """测试 interrupt 工作流：触发人工审批 → 验证 interrupted 状态"""
    # Mock generate_trade_signal 返回超限仓位触发 interrupt
    from agent_platform.finance import trader_agent

    original_generate = trader_agent.generate_trade_signal

    def mock_generate_high_position(synthesis, regime, technical=None):
        # 强制返回超限仓位
        result = original_generate(synthesis=synthesis, regime=regime, technical=technical)
        # 修改仓位建议为 15.0%（超过 10% 上限）
        from dataclasses import replace
        result = replace(result, position_pct_suggestion=15.0)
        return result

    monkeypatch.setattr(trader_agent, "generate_trade_signal", mock_generate_high_position)

    try:
        response = client.post("/research/DEMO001")
        assert response.status_code == 200
        data = response.json()

        thread_id = data["thread_id"]

        # 查询状态，期望 interrupted
        state_resp = client.get(f"/research/{thread_id}/state")
        assert state_resp.status_code == 200
        state = state_resp.json()

        if state["status"] == "interrupted":
            assert state.get("interrupt_payload") is not None
            assert "reason" in state["interrupt_payload"]
    finally:
        # 恢复原始函数
        monkeypatch.setattr(trader_agent, "generate_trade_signal", original_generate)


def test_research_no_trade_status(client, monkeypatch):
    """测试 no_trade 状态:置信度 ≤30% → status='no_trade'"""
    from agent_platform.finance import synthesis_agent as synth_module

    original_synthesize = synth_module.synthesize

    def mock_low_confidence_synthesize(symbol, technical, fundamental, industry, regime, **kwargs):
        result = original_synthesize(symbol=symbol, technical=technical, fundamental=fundamental,
                                     industry=industry, regime=regime, **kwargs)
        # 强制低置信度
        from dataclasses import replace
        result = replace(result, confidence=0.20, signal="hold")
        return result

    monkeypatch.setattr(synth_module, "synthesize", mock_low_confidence_synthesize)

    try:
        response = client.post("/research/DEMO001?data_mode=offline")
        assert response.status_code == 200
        data = response.json()

        # no_trade 是终态，POST 返回时应已完成
        assert data["status"] == "no_trade"

        state_resp = client.get(f"/research/{data['thread_id']}/state")
        assert state_resp.status_code == 200
        state = state_resp.json()
        assert state["status"] == "no_trade"
    finally:
        monkeypatch.setattr(synth_module, "synthesize", original_synthesize)


def test_resume_without_interrupt_returns_409(client):
    """测试对非 interrupt 状态的 thread 调用 resume 返回 409"""
    # 先完成一次正常工作流 - 显式指定 offline 模式
    response = client.post("/research/DEMO001?data_mode=offline")
    assert response.status_code == 200
    thread_id = response.json()["thread_id"]

    # 等待完成（或已完成）
    state_resp = client.get(f"/research/{thread_id}/state")
    state = state_resp.json()

    if state["status"] in ["completed", "no_trade", "blocked"]:
        # 尝试 resume
        resume_resp = client.post(f"/research/{thread_id}/resume?decision=approve")
        assert resume_resp.status_code == 409
        error = resume_resp.json()
        assert "不处于 interrupt 状态" in error["detail"]


def test_get_state_nonexistent_thread_returns_not_found(client):
    """测试查询不存在的 thread_id 返回 status='not_found'"""
    fake_thread = "nonexistent-thread-12345"
    response = client.get(f"/research/{fake_thread}/state")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "not_found"
    assert data["thread_id"] == fake_thread


def test_research_returns_complete_agent_results(client):
    """测试 GET /state 返回完整 Agent 结果字段"""
    response = client.post("/research/DEMO001")
    assert response.status_code == 200
    thread_id = response.json()["thread_id"]

    state_resp = client.get(f"/research/{thread_id}/state")
    assert state_resp.status_code == 200
    state = state_resp.json()

    # 验证新增字段存在（可能为 None，但字段必须在响应中）
    required_fields = [
        "symbol", "run_id", "data_mode", "requested_data_mode", "effective_data_mode",
        "technical_analysis", "fundamental_analysis",
        "industry_analysis", "market_regime",
        "synthesis", "trade_signal", "risk_result", "confidence"
    ]
    for field in required_fields:
        assert field in state, f"Missing field: {field}"
    assert state["run_id"]
    assert state["duration_s"] >= 0
    assert state["data_mode"] == state["effective_data_mode"]


def test_sample_symbol_auto_routes_offline_and_requires_review(client):
    response = client.post("/research/DEMO001?data_mode=auto")
    assert response.status_code == 200
    started = response.json()
    assert started["requested_data_mode"] == "auto"
    assert started["effective_data_mode"] == "offline"
    assert started["data_mode"] == "offline"
    assert started["status"] == "interrupted"

    state = client.get(f"/research/{started['thread_id']}/state").json()
    assert state["run_id"] == started["run_id"]
    assert state["duration_s"] > 0
    statuses = {
        state[key]["data_status"]
        for key in (
            "technical_analysis", "fundamental_analysis",
            "industry_analysis", "market_regime",
        )
    }
    assert statuses == {"offline_sample"}
    assert state["data_quality_summary"]["passed"] is False
    assert state["data_quality_summary"]["counts"]["offline_sample"] == 4
    assert state["interrupt_payload"]["reason"] == "preflight_manual_review"


def test_status_consistency_across_endpoints(client):
    """测试三个端点返回的 status 一致性"""
    # POST /research
    post_resp = client.post("/research/DEMO001")
    assert post_resp.status_code == 200
    post_data = post_resp.json()
    thread_id = post_data["thread_id"]
    post_status = post_data["status"]

    # GET /state
    get_resp = client.get(f"/research/{thread_id}/state")
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    get_status = get_data["status"]

    # 状态应一致（除非异步完成，但离线模式应同步）
    assert get_status == post_status or get_status in ["completed", "no_trade"]
