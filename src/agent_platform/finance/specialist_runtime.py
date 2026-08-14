"""Specialist Agent 的 Loop + Harness 统一运行适配层（含逐 Action 边界治理）。

边界治理层（ToolGuardLayer）在每次工具执行前后各插入一道检查：

执行前（Pre-Action）：
  1. 白名单校验 —— 该 Specialist 只能调用自身允许的工具；越权调用拒绝并写 trace。
  2. 输入类型/Schema 校验 —— 参数类型不符立即拒绝，不调上游。
  3. 网络工具限流 —— 仅对 requires_network=True 的工具计滑动窗口限流；
     纯本地确定性函数（requires_network=False）不施加无意义的网络限流。

执行后（Post-Observation）：
  4. MCP 信封字段校验 —— 工具返回 dict 时检查 ok/source/updated_at；
     若工具返回 dict 且 ok=True 但缺 data_status，记 warning 而非阻断，
     因为并非所有工具都返回 MCP 标准信封。

所有违规、拒绝、重试、阻断、耗时均进入 `action_audit` 列表，可从
`SpecialistRuntime.last_audit["action_audit"]` 读取，并对 LangGraph state 可见。
"""
from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from agent_platform.core.agent_loop import AgentLoop, KeywordReflector
from agent_platform.core.event_hooks import EventBus, LoopEvent
from agent_platform.core.harness import (
    AgentHarness,
    CrossValidator,
    GuardrailViolation,
    JSONSchemaValidator,
    KeywordBlocker,
    SourceAttributionFilter,
)
from agent_platform.core.loop_memory import InMemoryLoopMemory
from agent_platform.core.tools import RegisteredTool, ToolRegistry


TECHNICAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["symbol", "source", "updated_at"],
    "properties": {
        "symbol": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
        "data_status": {"type": "string"},
        "fallback_reason": {},
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# 逐 Action 边界治理层
# ─────────────────────────────────────────────────────────────────────────────

class ActionAuditRecord:
    """单次工具调用的审计记录，对 LangGraph state 可见。"""

    __slots__ = (
        "tool", "allowed", "blocked_reason",
        "input_valid", "input_error",
        "duration_s", "ok", "observation_valid", "observation_warnings",
        "is_network_tool", "rate_limited",
    )

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.allowed: bool = True
        self.blocked_reason: str | None = None
        self.input_valid: bool = True
        self.input_error: str | None = None
        self.duration_s: float = 0.0
        self.ok: bool = False
        self.observation_valid: bool = True
        self.observation_warnings: list[str] = []
        self.is_network_tool: bool = False
        self.rate_limited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "allowed": self.allowed,
            "blocked_reason": self.blocked_reason,
            "input_valid": self.input_valid,
            "input_error": self.input_error,
            "duration_s": round(self.duration_s, 6),
            "ok": self.ok,
            "observation_valid": self.observation_valid,
            "observation_warnings": self.observation_warnings,
            "is_network_tool": self.is_network_tool,
            "rate_limited": self.rate_limited,
        }


class ToolGuardLayer:
    """
    在 SpecialistRuntime 中包装每个工具 handler，实现逐 Action 边界治理。

    Parameters
    ----------
    allowed_tools : set[str] | None
        允许调用的工具名集合。None 表示不限制（向后兼容默认行为）。
    input_schemas : dict[str, dict] | None
        工具名 → JSON Schema，执行前校验入参。
    network_tools : set[str] | None
        需要网络的工具名集合（施加限流）；本地确定性工具不限流。
    max_network_calls_per_minute : int
        网络工具每分钟调用上限（滑动窗口）。
    audit : list[ActionAuditRecord]
        外部传入的审计列表，每次调用追加记录（可从 last_audit 访问）。
    """

    def __init__(
        self,
        *,
        allowed_tools: set[str] | None = None,
        input_schemas: dict[str, dict] | None = None,
        network_tools: set[str] | None = None,
        max_network_calls_per_minute: int = 30,
        audit: list[ActionAuditRecord],
    ) -> None:
        self._allowed = allowed_tools
        self._input_schemas = input_schemas or {}
        self._network_tools = network_tools or set()
        self._max_net = max_network_calls_per_minute
        self._net_window: deque[float] = deque()
        self.audit = audit

    def wrap(self, tool_name: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        """返回包裹了边界治理的 handler。"""

        guard = self

        def guarded(**kwargs: Any) -> Any:
            rec = ActionAuditRecord(tool_name)
            guard.audit.append(rec)
            rec.is_network_tool = tool_name in guard._network_tools

            # ── 1. 白名单校验 ──────────────────────────────────────────────────
            if guard._allowed is not None and tool_name not in guard._allowed:
                rec.allowed = False
                rec.blocked_reason = (
                    f"越权调用：Specialist 不允许使用工具 {tool_name!r}；"
                    f"允许列表={sorted(guard._allowed)}"
                )
                raise GuardrailViolation("ToolWhitelist", rec.blocked_reason)

            # ── 2. 输入 Schema 校验 ────────────────────────────────────────────
            schema = guard._input_schemas.get(tool_name)
            if schema:
                errs = _validate_schema(kwargs, schema)
                if errs:
                    rec.input_valid = False
                    rec.input_error = "; ".join(errs)
                    raise GuardrailViolation(
                        "ToolInputSchema",
                        f"工具 {tool_name!r} 输入校验失败: {rec.input_error}",
                    )

            # ── 3. 网络工具限流（仅对 requires_network=True 工具） ────────────
            if rec.is_network_tool:
                now = time.monotonic()
                while guard._net_window and now - guard._net_window[0] > 60.0:
                    guard._net_window.popleft()
                if len(guard._net_window) >= guard._max_net:
                    wait = 60.0 - (now - guard._net_window[0])
                    rec.rate_limited = True
                    rec.blocked_reason = (
                        f"网络工具 {tool_name!r} 超出限流（{guard._max_net} 次/min），"
                        f"请等待 {wait:.1f}s"
                    )
                    raise GuardrailViolation("NetworkRateLimit", rec.blocked_reason)
                guard._net_window.append(now)

            # ── 4. 执行工具 ────────────────────────────────────────────────────
            t0 = time.perf_counter()
            try:
                result = handler(**kwargs)
                rec.duration_s = time.perf_counter() - t0
                rec.ok = True
            except GuardrailViolation:
                rec.duration_s = time.perf_counter() - t0
                raise
            except Exception:
                rec.duration_s = time.perf_counter() - t0
                raise

            # ── 5. Observation 信封校验（仅 dict 类型结果） ──────────────────
            if isinstance(result, dict):
                _check_observation_envelope(result, tool_name, rec)

            return result

        return guarded


def _validate_schema(data: Any, schema: dict[str, Any]) -> list[str]:
    """用 jsonschema 或退化必填字段检查校验输入。返回错误消息列表。"""
    errors: list[str] = []
    try:
        import jsonschema
        validator = jsonschema.Draft7Validator(schema)
        errors = [e.message for e in validator.iter_errors(data)]
    except ImportError:
        for field in schema.get("required", []):
            if not isinstance(data, dict) or field not in data:
                errors.append(f"缺少必填参数: {field}")
    return errors


def _check_observation_envelope(
    result: dict[str, Any],
    tool_name: str,
    rec: ActionAuditRecord,
) -> None:
    """
    校验 MCP 信封必要字段。

    ok=True 时检查 source / updated_at（这两个是 ok_envelope 标准字段）。
    data_status 缺失只警告不阻断（非所有工具都有四级状态）。
    fallback_reason 缺失只警告（仅降级路径必须提供）。
    """
    warns = rec.observation_warnings

    # ok 字段本身
    if "ok" not in result:
        warns.append("信封缺少 'ok' 字段，无法判断成功/失败")
        return

    if not result.get("ok"):
        # 失败信封：检查 error_type 字段
        if not result.get("error_type"):
            warns.append("失败信封缺少 'error_type' 字段")
        return  # 失败信封不强求 source/updated_at

    # 成功信封
    if not result.get("source"):
        rec.observation_valid = False
        warns.append(f"工具 {tool_name!r} 成功信封缺少 'source' 字段")
    if not result.get("updated_at"):
        rec.observation_valid = False
        warns.append(f"工具 {tool_name!r} 成功信封缺少 'updated_at' 字段")

    data = result.get("data") or {}
    if isinstance(data, dict):
        if "data_status" not in data and "data_status" not in result:
            warns.append(
                f"工具 {tool_name!r} 返回成功但 data 中无 'data_status'（可选字段，仅警告）"
            )


# ─────────────────────────────────────────────────────────────────────────────
# SpecialistRuntime
# ─────────────────────────────────────────────────────────────────────────────

class SpecialistRuntime:
    """把确定性 Specialist 工具放入五要素 Loop，并以 Harness 校验输出。

    新增参数
    --------
    allowed_tools : set[str] | None
        允许该 Specialist 调用的工具名白名单。None 不限制。
    tool_input_schemas : dict[str, dict] | None
        工具名 → JSON Schema，用于逐 Action 输入校验。
    network_tools : set[str] | None
        标记为需要网络的工具名（施加限流）；纯本地确定性工具不限流。
    max_network_calls_per_minute : int
        网络工具每分钟上限。默认 30（宽松，避免阻断正常运行）。
    """

    def __init__(
        self,
        *,
        name: str,
        schema: dict[str, Any],
        analyzer: Callable[[], dict[str, Any]],
        technical_cross_validation: bool = False,
        allowed_tools: set[str] | None = None,
        tool_input_schemas: dict[str, dict] | None = None,
        network_tools: set[str] | None = None,
        max_network_calls_per_minute: int = 30,
    ) -> None:
        self.name = name
        self.schema = schema
        self.analyzer = analyzer
        self.technical_cross_validation = technical_cross_validation
        self._allowed_tools = allowed_tools
        self._tool_input_schemas = tool_input_schemas or {}
        self._network_tools = network_tools or set()
        self._max_network_calls_per_minute = max_network_calls_per_minute
        self.last_audit: dict[str, Any] = {}

    def run(self, task: Any) -> dict[str, Any]:
        holder: dict[str, Any] = {}
        action_audit: list[ActionAuditRecord] = []

        # 逐 Action 边界治理层
        guard = ToolGuardLayer(
            allowed_tools=self._allowed_tools,
            input_schemas=self._tool_input_schemas,
            network_tools=self._network_tools,
            max_network_calls_per_minute=self._max_network_calls_per_minute,
            audit=action_audit,
        )

        tools = ToolRegistry()

        def analyze() -> str:
            try:
                output = self.analyzer()
            except (TypeError, AttributeError, NameError) as exc:
                holder["programming_error"] = exc
                raise
            if not isinstance(output, dict):
                error = TypeError(f"{self.name} 分析工具必须返回 dict")
                holder["programming_error"] = error
                raise error
            holder["output"] = output
            return json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)

        tool_name = f"run_{self.name}"
        # 工具白名单：若未指定 allowed_tools，默认允许自身的分析工具
        effective_allowed = self._allowed_tools
        if effective_allowed is not None and tool_name not in effective_allowed:
            effective_allowed = effective_allowed | {tool_name}

        # 将 analyze 包裹在边界治理层中
        wrapped_analyze = guard.wrap(tool_name, analyze)

        tools.register(RegisteredTool(
            name=tool_name,
            description=f"运行 {self.name} 确定性分析并返回结构化证据",
            handler=wrapped_analyze,
        ))
        memory = InMemoryLoopMemory()
        bus = EventBus()
        for event_name in LoopEvent.all_events():
            bus.register(event_name, lambda _context: None, name=f"audit_{event_name}")
        loop = AgentLoop(
            tools=tools,
            provider=None,
            memory=memory,
            bus=bus,
            reflector=KeywordReflector(),
            tool_plan=lambda _goal, iteration, _observations: (
                [(tool_name, {})] if iteration == 1 else []
            ),
            max_iterations=2,
        )

        cross = CrossValidator()

        def run_loop(_task: Any) -> dict[str, Any]:
            result = loop.run(
                f"完成 {self.name} 并取得可追溯结构化输出",
                session_id=str(task.get("session_id", self.name))
                if isinstance(task, dict) else self.name,
            )
            programming_error = holder.get("programming_error")
            if isinstance(programming_error, (TypeError, AttributeError, NameError)):
                raise programming_error
            if not result.goal_met or "output" not in holder:
                raise RuntimeError(
                    f"{self.name} Loop 未达成目标：{result.stop_reason}; "
                    f"missing={list(result.missing)}"
                )
            output = holder["output"]
            if self.technical_cross_validation:
                cross.set_ground_truth({
                    key: float(output[key])
                    for key in ("latest_rsi", "latest_macd", "latest_ma5", "latest_ma20")
                    if output.get(key) is not None
                })
            # 汇总审计：含逐 Action 记录
            blocked_actions = [r for r in action_audit if not r.allowed or r.rate_limited]
            invalid_observations = [r for r in action_audit if not r.observation_valid]
            self.last_audit = {
                "agent": self.name,
                "goal_met": result.goal_met,
                "iterations": len(result.steps),
                "memory_kinds": [record.kind for record in memory.records(result.session_id)],
                "events": [event.event for event in bus.history],
                "provider": result.provider,
                # 逐 Action 审计（可对 LangGraph state 注入）
                "action_audit": [r.to_dict() for r in action_audit],
                "blocked_action_count": len(blocked_actions),
                "invalid_observation_count": len(invalid_observations),
                "total_duration_s": sum(r.duration_s for r in action_audit),
            }
            return output

        guardrails = [
            JSONSchemaValidator(self.schema),
            SourceAttributionFilter(),
            KeywordBlocker(),
        ]
        if self.technical_cross_validation:
            guardrails.append(cross)
        return AgentHarness(
            agent=run_loop,
            guardrails=guardrails,
            max_retries=0,
        ).run(task)
