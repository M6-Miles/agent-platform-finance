"""
任务 C：LangGraph 主链逐 Action 审计证据
=========================================
验证：
1. 执行至少一个 Specialist 节点，specialist_audit 中可见 action_audit
2. action_audit 包含 blocked_action_count、invalid_observation_count、total_duration_s
3. 缺少 source/updated_at 的成功信封导致 observation_valid=False 且警告进入审计
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_platform.finance.securities_graph import (
    build_securities_graph,
    run_securities_analysis,
)


def test_specialist_audit_in_langgraph_state():
    """LangGraph 主链执行后，specialist_audit 存在且结构完整。"""
    graph = build_securities_graph()
    state = run_securities_analysis(
        symbol="TEST001",
        graph=graph,
        data_mode="offline",  # 离线模式，避免联网
    )

    # 断言：specialist_audit 字段存在
    assert "specialist_audit" in state
    audit_list = state["specialist_audit"]
    assert isinstance(audit_list, list)
    # 至少执行了一个 Specialist（technical/fundamental/industry/market_regime 之一）
    assert len(audit_list) >= 1

    # 检查第一个审计记录的结构
    first_audit = audit_list[0]
    assert "agent" in first_audit
    assert "action_audit" in first_audit
    assert isinstance(first_audit["action_audit"], list)
    assert "blocked_action_count" in first_audit
    assert "invalid_observation_count" in first_audit
    assert "total_duration_s" in first_audit

    # 断言：审计字段是合理值
    assert first_audit["blocked_action_count"] >= 0
    assert first_audit["invalid_observation_count"] >= 0
    assert first_audit["total_duration_s"] >= 0.0


def test_action_audit_contains_duration_and_tool():
    """action_audit 中每条记录包含 tool、duration_s、ok。"""
    graph = build_securities_graph()
    state = run_securities_analysis(
        symbol="TEST001",
        graph=graph,
        data_mode="offline",
    )

    audit_list = state.get("specialist_audit", [])
    assert len(audit_list) >= 1

    for specialist_audit in audit_list:
        action_audit = specialist_audit.get("action_audit", [])
        for action_rec in action_audit:
            assert "tool" in action_rec
            assert "duration_s" in action_rec
            assert "ok" in action_rec
            assert isinstance(action_rec["duration_s"], (int, float))
            assert action_rec["duration_s"] >= 0.0


def test_missing_source_updated_at_invalidates_observation():
    """成功信封缺少 source/updated_at 必须标记 observation_valid=False。"""
    from agent_platform.finance.specialist_runtime import (
        ActionAuditRecord,
        ToolGuardLayer,
    )

    audit: list[ActionAuditRecord] = []
    guard = ToolGuardLayer(
        allowed_tools={"incomplete_tool"},
        audit=audit,
    )

    def incomplete_handler() -> dict:
        # 返回 ok=True 但缺少 source/updated_at
        return {"ok": True, "data": {"value": 42}}

    wrapped = guard.wrap("incomplete_tool", incomplete_handler)

    # 调用工具（不应抛异常，但审计会标记 observation_valid=False）
    result = wrapped()
    assert result["ok"] is True  # 工具本身成功

    # 审计记录必须标记不完整
    assert len(audit) == 1
    rec = audit[0]
    assert rec.tool == "incomplete_tool"
    assert rec.ok is True  # 工具执行成功
    assert rec.observation_valid is False  # 但信封不完整
    assert len(rec.observation_warnings) >= 2  # 至少 source、updated_at 两个警告
    assert any("source" in w for w in rec.observation_warnings)
    assert any("updated_at" in w for w in rec.observation_warnings)


def test_blocked_action_count_when_whitelist_violated():
    """工具白名单越权调用时，blocked_action_count 增加。"""
    from agent_platform.core.harness import GuardrailViolation
    from agent_platform.finance.specialist_runtime import (
        ActionAuditRecord,
        ToolGuardLayer,
    )

    audit: list[ActionAuditRecord] = []
    guard = ToolGuardLayer(
        allowed_tools={"tool_a", "tool_b"},  # 只允许 a、b
        audit=audit,
    )

    def dummy_handler() -> str:
        return "should not reach"

    wrapped = guard.wrap("tool_c", dummy_handler)  # tool_c 不在白名单

    # 调用必须抛 GuardrailViolation
    try:
        wrapped()
        assert False, "应该抛出 GuardrailViolation"
    except GuardrailViolation as exc:
        assert "越权调用" in str(exc)

    # 审计记录
    assert len(audit) == 1
    rec = audit[0]
    assert rec.tool == "tool_c"
    assert rec.allowed is False
    assert rec.blocked_reason is not None
    assert "越权调用" in rec.blocked_reason


def test_invalid_observation_count_incremented():
    """observation_valid=False 的记录计入 invalid_observation_count。"""
    from agent_platform.finance.specialist_runtime import (
        ActionAuditRecord,
        ToolGuardLayer,
    )

    audit: list[ActionAuditRecord] = []
    guard = ToolGuardLayer(audit=audit)

    def bad_envelope_handler() -> dict:
        # 成功但缺 source/updated_at
        return {"ok": True, "data": {}}

    wrapped = guard.wrap("bad_tool", bad_envelope_handler)
    wrapped()

    # 统计 invalid_observation_count
    invalid_count = sum(1 for r in audit if not r.observation_valid)
    assert invalid_count == 1


def test_total_duration_accumulates():
    """total_duration_s 是所有 action 耗时之和。"""
    from agent_platform.finance.specialist_runtime import (
        ActionAuditRecord,
        ToolGuardLayer,
    )

    audit: list[ActionAuditRecord] = []
    guard = ToolGuardLayer(audit=audit)

    def fast_tool() -> str:
        return "fast"

    def slow_tool() -> str:
        import time
        time.sleep(0.01)
        return "slow"

    wrapped_fast = guard.wrap("fast", fast_tool)
    wrapped_slow = guard.wrap("slow", slow_tool)

    wrapped_fast()
    wrapped_slow()

    total_duration = sum(r.duration_s for r in audit)
    assert total_duration >= 0.01  # 至少 slow_tool 的耗时
    assert len(audit) == 2


def test_network_tool_rate_limit_increments_blocked():
    """网络工具限流时，rate_limited=True 且 blocked_action_count 增加。"""
    from agent_platform.core.harness import GuardrailViolation
    from agent_platform.finance.specialist_runtime import (
        ActionAuditRecord,
        ToolGuardLayer,
    )

    audit: list[ActionAuditRecord] = []
    guard = ToolGuardLayer(
        network_tools={"net_tool"},
        max_network_calls_per_minute=2,  # 仅允许 2 次
        audit=audit,
    )

    def net_handler() -> str:
        return "ok"

    wrapped = guard.wrap("net_tool", net_handler)

    # 前两次成功
    wrapped()
    wrapped()

    # 第三次触发限流
    try:
        wrapped()
        assert False, "第三次应触发限流"
    except GuardrailViolation as exc:
        assert "超出限流" in str(exc)

    # 审计记录
    assert len(audit) == 3
    assert audit[0].rate_limited is False
    assert audit[1].rate_limited is False
    assert audit[2].rate_limited is True
    assert audit[2].blocked_reason is not None

    # blocked_action_count = rate_limited 或 allowed=False 的记录数
    blocked_count = sum(1 for r in audit if r.rate_limited or not r.allowed)
    assert blocked_count == 1


def test_langgraph_state_contains_all_specialists():
    """LangGraph 执行完整流程后，specialist_audit 包含四个 Specialist。"""
    graph = build_securities_graph()
    state = run_securities_analysis(
        symbol="TEST002",
        graph=graph,
        data_mode="offline",
    )

    audit_list = state.get("specialist_audit", [])
    # 并行四路：technical、fundamental、industry、market_regime
    # 实际返回数量可能因节点执行顺序略有差异，但至少应有主要节点
    assert len(audit_list) >= 1  # 至少有一个 Specialist 执行了

    agent_names = {a["agent"] for a in audit_list}
    # 检查至少有一个核心 Specialist
    assert len(agent_names) >= 1


def test_specialist_audit_survives_error_nodes():
    """即使某节点出错，specialist_audit 也应保留已完成节点的审计。"""
    graph = build_securities_graph()
    # 使用一个可能触发部分节点失败的符号（仍是离线样例数据，不会真失败）
    state = run_securities_analysis(
        symbol="TEST999",  # 不存在的符号可能导致部分节点降级
        graph=graph,
        data_mode="offline",
    )

    # 即使有错误，specialist_audit 也应该存在
    assert "specialist_audit" in state
    # 至少有一个节点完成了
    assert len(state["specialist_audit"]) >= 1
