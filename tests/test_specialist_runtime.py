"""Specialist Agent 的 AgentLoop + Harness 适配层验收。"""
from __future__ import annotations

import pytest

from agent_platform.core.harness import GuardrailViolation
from agent_platform.finance.specialist_runtime import SpecialistRuntime


SCHEMA = {
    "type": "object",
    "required": ["symbol", "source", "updated_at"],
    "properties": {
        "symbol": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
    },
}


def _valid_output() -> dict:
    return {
        "symbol": "DEMO001",
        "source": "内置样例数据",
        "updated_at": "2026-08-09T00:00:00+00:00",
    }


def test_specialist_runs_all_five_loop_elements_without_llm() -> None:
    runtime = SpecialistRuntime(name="test_agent", schema=SCHEMA, analyzer=_valid_output)

    assert runtime.run({"session_id": "audit-1"}) == _valid_output()
    audit = runtime.last_audit
    assert audit["provider"] == "无（规则驱动）"
    assert audit["goal_met"] is True
    required = {"plan", "tool_call", "observation", "reflection", "decision"}
    assert required <= set(audit["memory_kinds"])
    assert required <= set(audit["events"])


def test_harness_blocks_missing_source() -> None:
    runtime = SpecialistRuntime(
        name="test_agent",
        schema=SCHEMA,
        analyzer=lambda: {"symbol": "DEMO001", "updated_at": "2026-08-09"},
    )

    with pytest.raises(GuardrailViolation):
        runtime.run({})


@pytest.mark.parametrize(
    "error", [TypeError("bad type"), AttributeError("bad attr"), NameError("bad name")]
)
def test_programming_errors_are_not_converted_to_business_results(error: Exception) -> None:
    def fail() -> dict:
        raise error

    runtime = SpecialistRuntime(name="test_agent", schema=SCHEMA, analyzer=fail)
    with pytest.raises(type(error), match="bad"):
        runtime.run({})


def test_analyzer_must_return_mapping() -> None:
    runtime = SpecialistRuntime(
        name="test_agent", schema=SCHEMA, analyzer=lambda: "bad"  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError, match="必须返回 dict"):
        runtime.run({})


# ─────────────────────────────────────────────────────────────────────────────
# ToolGuardLayer 逐-Action 边界治理集成测试（任务1补充）
# ─────────────────────────────────────────────────────────────────────────────

from agent_platform.finance.specialist_runtime import ActionAuditRecord, ToolGuardLayer  # noqa: E402


def _make_guard(**kwargs: object) -> tuple[ToolGuardLayer, list[ActionAuditRecord]]:
    """便捷构造 ToolGuardLayer，返回 (guard, audit_list)。"""
    audit: list[ActionAuditRecord] = []
    guard = ToolGuardLayer(audit=audit, **kwargs)  # type: ignore[arg-type]
    return guard, audit


def test_whitelist_blocks_unauthorized_tool() -> None:
    """越权调用必须抛 GuardrailViolation('ToolWhitelist') 并写入审计。"""
    guard, audit = _make_guard(allowed_tools={"only_this"})

    def forbidden(**kwargs: object) -> dict:  # pragma: no cover
        return {"ok": True, "source": "s", "updated_at": "t"}

    wrapped = guard.wrap("not_in_whitelist", forbidden)
    with pytest.raises(GuardrailViolation) as exc_info:
        wrapped()

    assert exc_info.value.rule_name == "ToolWhitelist"
    assert len(audit) == 1
    assert audit[0].allowed is False
    assert audit[0].blocked_reason is not None


def test_rate_limit_blocks_excess_network_calls() -> None:
    """网络工具超出每分钟限额后抛 GuardrailViolation('NetworkRateLimit')。"""
    guard, audit = _make_guard(
        network_tools={"net_tool"}, max_network_calls_per_minute=3
    )

    def net_fn(**kwargs: object) -> dict:
        return {"ok": True, "source": "s", "updated_at": "t"}

    wrapped = guard.wrap("net_tool", net_fn)

    for _ in range(3):          # 前3次正常
        wrapped()

    with pytest.raises(GuardrailViolation) as exc_info:
        wrapped()               # 第4次同分钟内触发限速

    assert exc_info.value.rule_name == "NetworkRateLimit"
    assert audit[-1].rate_limited is True


def test_local_tool_bypasses_network_rate_limit() -> None:
    """本地确定性工具（不在 network_tools 中）不受网络限速约束。"""
    guard, audit = _make_guard(
        network_tools=set(),            # local_tool 不在 network_tools 里
        max_network_calls_per_minute=1, # 极低限额——若被计入立刻封禁
    )

    def local_fn(**kwargs: object) -> dict:
        return {"ok": True, "source": "s", "updated_at": "t"}

    wrapped = guard.wrap("local_tool", local_fn)

    for _ in range(5):          # 5次必须全部通过
        wrapped()

    assert all(not r.rate_limited for r in audit)
    assert all(r.is_network_tool is False for r in audit)


def test_observation_envelope_missing_source_records_warning() -> None:
    """工具返回 ok=True 但缺少 source 字段时，审计记录 observation_valid=False 和警告。"""
    guard, audit = _make_guard()

    def tool_no_source(**kwargs: object) -> dict:
        return {"ok": True, "updated_at": "2026-08-10T00:00:00Z", "data": {}}

    wrapped = guard.wrap("missing_src_tool", tool_no_source)
    wrapped()           # 不应抛异常

    rec = audit[0]
    assert rec.ok is True
    assert rec.observation_valid is False
    assert any("source" in w for w in rec.observation_warnings)


def test_action_audit_present_and_serializable_in_last_audit() -> None:
    """SpecialistRuntime.last_audit['action_audit'] 可序列化为 JSON（LangGraph state 注入要求）。"""
    import json

    runtime = SpecialistRuntime(name="test_agent", schema=SCHEMA, analyzer=_valid_output)
    runtime.run({"session_id": "guard-audit-1"})

    audit_list = runtime.last_audit.get("action_audit")
    assert isinstance(audit_list, list) and len(audit_list) >= 1

    rec = audit_list[0]
    required_keys = (
        "tool", "allowed", "input_valid", "duration_s",
        "ok", "observation_valid", "observation_warnings",
        "is_network_tool", "rate_limited",
    )
    for key in required_keys:
        assert key in rec, f"action_audit 记录缺少字段 {key!r}"

    # 必须可序列化（LangGraph state 要求可持久化）
    json.dumps(audit_list)


def test_last_audit_has_blocked_and_invalid_counts() -> None:
    """last_audit 必须含 blocked_action_count 和 invalid_observation_count 两个计数字段。"""
    runtime = SpecialistRuntime(name="test_agent", schema=SCHEMA, analyzer=_valid_output)
    runtime.run({"session_id": "count-check"})

    audit = runtime.last_audit
    assert "blocked_action_count" in audit
    assert "invalid_observation_count" in audit
    assert isinstance(audit["blocked_action_count"], int)
    assert isinstance(audit["invalid_observation_count"], int)
    # 正常运行时：无阻断、无异常信封
    assert audit["blocked_action_count"] == 0
    assert audit["invalid_observation_count"] == 0


def test_langgraph_state_receives_specialist_audit() -> None:
    """
    主链集成测试：securities_graph 节点把 SpecialistRuntime.last_audit
    写入 LangGraph state['specialist_audit']，且审计含 action_audit/
    blocked_action_count/invalid_observation_count。
    """
    from agent_platform.finance.securities_graph import run_securities_analysis

    state = run_securities_analysis(symbol="DEMO001", data_mode="offline")

    specialist_audit = state.get("specialist_audit", [])
    assert isinstance(specialist_audit, list) and len(specialist_audit) >= 1, (
        "securities_graph state 中 specialist_audit 应为非空列表"
    )

    # 每条审计记录必须含必要键
    for record in specialist_audit:
        assert "agent" in record
        assert "action_audit" in record
        assert "blocked_action_count" in record
        assert "invalid_observation_count" in record
        assert "goal_met" in record
