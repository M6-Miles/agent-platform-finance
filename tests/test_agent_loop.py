"""
Loop 五要素 + 目标循环测试
==========================
说明书要求 Loop 必须具备「规划、工具调用、观察、反思、继续规划/结束」五要素，
且心跳/事件钩子/目标循环是「项目内最小可靠可测实现，不是文档或假接口」。

本文件的验收立场
----------------
1. **不测「字段存在」，测「行为发生」**：不满足于断言 ``step.plan`` 这个属性有值，
   而是断言规划文本随轮次和缺口变化、断言五要素都真的落进了记忆与事件总线。
2. **失败必须可见**：工具失败要产出 ERROR 事件、要记进观察、且**不得**因为错误
   文本里恰好含有关键词就被判成目标达成。
3. **未达成必须诚实**：触及迭代上限时 ``goal_met`` 必须为 False、答案里必须写明
   未达成与缺口，不允许用模糊措辞混过去。
4. **持久化要跨实例验证**：写完后重新打开同一个数据库文件再读，才算真持久化。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from agent_platform.core.agent_loop import (
    AgentLoop,
    AgentLoopResult,
    KeywordReflector,
    LoopStep,
    Observation,
    Reflection,
    RuleBasedPlanner,
    StopReason,
)
from agent_platform.core.event_hooks import EventBus, HookContext, LoopEvent
from agent_platform.core.llm_provider import ChatMessage, ModelReply, ToolCall
from agent_platform.core.loop_memory import (
    InMemoryLoopMemory,
    MemoryKind,
    SQLiteLoopMemory,
)
from agent_platform.core.scheduler import HeartbeatScheduler, ManualClock
from agent_platform.core.tools import RegisteredTool, ToolRegistry

# ─────────────────────────────────────────────────────────────────────────────
# 测试夹具
# ─────────────────────────────────────────────────────────────────────────────


def _registry(**handlers: Any) -> ToolRegistry:
    """用 name=handler 快速搭一个工具注册表。"""
    reg = ToolRegistry()
    for name, handler in handlers.items():
        reg.register(RegisteredTool(name=name, description=f"测试工具 {name}", handler=handler))
    return reg


def _plan_calls(*sequence: Sequence[tuple[str, dict[str, Any]]]):
    """
    构造一个按轮次返回不同调用列表的 tool_plan。

    第 N 轮返回 ``sequence[N-1]``；超出长度后返回最后一项，便于测试上限行为。
    """

    def tool_plan(goal: str, iteration: int, observations: Sequence[Observation]):
        if not sequence:
            return ()
        index = min(iteration, len(sequence)) - 1
        return sequence[index]

    return tool_plan


class StubProvider:
    """按队列返回 ModelReply 的 Provider 替身，实现 LLMProvider 协议。"""

    def __init__(self, replies: Sequence[ModelReply], name: str = "stub") -> None:
        self._replies = list(replies)
        self._name = name
        self.calls: list[list[ChatMessage]] = []
        self.tools_seen: list[Any] = []

    @property
    def name(self) -> str:
        return self._name

    def generate(self, messages, tools) -> ModelReply:
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools))
        if not self._replies:
            return ModelReply(text="队列已空", tool_calls=())
        return self._replies.pop(0)


# ─────────────────────────────────────────────────────────────────────────────
# 五要素齐备
# ─────────────────────────────────────────────────────────────────────────────


class TestFiveElementsAllPresent:
    """五要素必须每一项都真的产出，而不是只有字段占位。"""

    def test_all_five_elements_land_in_memory(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        result = loop.run("查收盘价", session_id="s1")
        assert result.goal_met is True  # 前提：本轮确实达成，否则下面的齐备性无意义

        kinds = {r.kind for r in loop.memory.records("s1")}
        # 规划、工具调用、观察、反思、决策，加上目标与最终答案，七类齐备。
        assert kinds == {
            MemoryKind.GOAL, MemoryKind.PLAN, MemoryKind.TOOL_CALL,
            MemoryKind.OBSERVATION, MemoryKind.REFLECTION,
            MemoryKind.DECISION, MemoryKind.ANSWER,
        }

    def test_all_five_elements_emit_events(self):
        bus = EventBus()
        seen: list[str] = []
        for event in LoopEvent.all_events():
            bus.register(event, lambda ctx: seen.append(ctx.event), name=f"记录_{event}")

        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            bus=bus,
        )
        loop.run("查收盘价")

        for element in (LoopEvent.PLAN, LoopEvent.TOOL_CALL, LoopEvent.OBSERVATION,
                        LoopEvent.REFLECTION, LoopEvent.DECISION):
            assert element in seen, f"要素事件 {element} 未触发"
        assert LoopEvent.LOOP_START in seen
        assert LoopEvent.LOOP_END in seen

    def test_step_carries_every_element(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        step = loop.run("查收盘价").steps[0]

        assert isinstance(step, LoopStep)
        assert step.plan.strip()                       # 要素 1
        assert step.tool_names == ("quote",)           # 要素 2
        assert step.observations[0].output == "收盘价 10.5"   # 要素 3
        assert step.reflection.assessment.strip()      # 要素 4
        assert step.decision.strip()                   # 要素 5

    def test_element_order_within_iteration(self):
        """一轮之内五要素的顺序必须是 规划→调用→观察→反思→决策。"""
        bus = EventBus()
        order: list[str] = []
        tracked = (LoopEvent.PLAN, LoopEvent.TOOL_CALL, LoopEvent.OBSERVATION,
                   LoopEvent.REFLECTION, LoopEvent.DECISION)
        for event in tracked:
            bus.register(event, lambda ctx: order.append(ctx.event), name=f"o_{event}")

        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            bus=bus,
        )
        loop.run("查收盘价")
        assert order == list(tracked)


# ─────────────────────────────────────────────────────────────────────────────
# 要素 1：规划真的随状态变化
# ─────────────────────────────────────────────────────────────────────────────


class TestPlanningIsRealNotStatic:
    """规划必须是「基于当前状态推导」，不是每轮复制同一句话。"""

    def test_plan_text_differs_across_iterations(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价", "成交量")),
            max_iterations=3,
        )
        plans = [s.plan for s in loop.run("查行情").steps]
        assert len(plans) == 3
        assert plans[0] != plans[1], "第 2 轮规划与第 1 轮一字不差，等于没有规划"

    def test_plan_mentions_missing_evidence(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价", "成交量")),
            max_iterations=2,
        )
        steps = loop.run("查行情").steps
        # 第 1 轮反思指出缺「成交量」，第 2 轮规划必须把这个缺口写进去。
        assert "成交量" in steps[1].plan

    def test_planner_receives_accumulated_observations(self):
        seen_counts: list[int] = []

        def spy_planner(*, goal, iteration, observations, missing):
            seen_counts.append(len(observations))
            return f"第 {iteration} 轮"

        loop = AgentLoop(
            tools=_registry(quote=lambda: "部分数据"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("永不出现",)),
            planner=spy_planner,
            max_iterations=3,
        )
        loop.run("查行情")
        # 观察是累计的：第 1 轮 0 条，第 2 轮 1 条，第 3 轮 2 条。
        assert seen_counts == [0, 1, 2]

    def test_planner_receives_missing_from_prior_reflection(self):
        seen_missing: list[tuple[str, ...]] = []

        def spy_planner(*, goal, iteration, observations, missing):
            seen_missing.append(tuple(missing))
            return "规划"

        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价", "成交量")),
            planner=spy_planner,
            max_iterations=2,
        )
        loop.run("查行情")
        assert seen_missing[0] == ()
        assert seen_missing[1] == ("成交量",)

    def test_rule_based_planner_first_round_quotes_goal(self):
        plan = RuleBasedPlanner()(goal="查茅台", iteration=1, observations=(), missing=())
        assert "查茅台" in plan

    def test_rule_based_planner_notes_failed_tools(self):
        failed = Observation(tool="quote", arguments={}, output="炸了", ok=False, error="炸了")
        plan = RuleBasedPlanner()(
            goal="查行情", iteration=2, observations=(failed,), missing=(),
        )
        assert "quote" in plan


# ─────────────────────────────────────────────────────────────────────────────
# 要素 2 + 3：工具调用与观察
# ─────────────────────────────────────────────────────────────────────────────


class TestToolCallAndObservation:
    def test_tool_actually_invoked(self):
        hits: list[dict[str, Any]] = []

        def quote(**kwargs):
            hits.append(kwargs)
            return "收盘价 10.5"

        loop = AgentLoop(
            tools=_registry(quote=quote),
            tool_plan=_plan_calls([("quote", {"symbol": "600519"})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        loop.run("查收盘价")
        assert hits == [{"symbol": "600519"}], "工具没被真正调用"

    def test_arguments_recorded_in_memory(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda symbol: f"{symbol} 收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {"symbol": "600519"})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        loop.run("查收盘价", session_id="s2")
        call = loop.memory.latest("s2", MemoryKind.TOOL_CALL)
        assert call is not None
        assert call.meta["arguments"] == {"symbol": "600519"}

    def test_multiple_tools_in_one_iteration(self):
        loop = AgentLoop(
            tools=_registry(
                quote=lambda: "收盘价 10.5",
                volume=lambda: "成交量 12345",
            ),
            tool_plan=_plan_calls([("quote", {}), ("volume", {})]),
            reflector=KeywordReflector(required=("收盘价", "成交量")),
        )
        result = loop.run("查行情")
        assert result.steps[0].tool_names == ("quote", "volume")
        assert result.goal_met is True
        assert result.iterations == 1

    def test_observation_captures_output_verbatim(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5 元"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        obs = loop.run("查收盘价").steps[0].observations[0]
        assert obs.output == "收盘价 10.5 元"
        assert obs.ok is True
        assert obs.error is None


# ─────────────────────────────────────────────────────────────────────────────
# 失败可见性
# ─────────────────────────────────────────────────────────────────────────────


class TestFailuresAreVisible:
    """工具失败绝不能被吞掉或伪装成成功。"""

    def test_tool_exception_marked_not_ok(self):
        def boom():
            raise RuntimeError("上游超时")

        loop = AgentLoop(
            tools=_registry(quote=boom),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            max_iterations=1,
        )
        obs = loop.run("查收盘价").steps[0].observations[0]
        assert obs.ok is False
        assert obs.error is not None
        assert "上游超时" in obs.error

    def test_failure_emits_error_event(self):
        def boom():
            raise RuntimeError("上游超时")

        bus = EventBus()
        errors: list[dict[str, Any]] = []
        bus.register(LoopEvent.ERROR, lambda ctx: errors.append(ctx.payload), name="收集")

        loop = AgentLoop(
            tools=_registry(quote=boom),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            bus=bus, max_iterations=1,
        )
        loop.run("查收盘价")
        assert len(errors) == 1
        assert errors[0]["tool"] == "quote"
        assert "上游超时" in errors[0]["error"]

    def test_missing_tool_is_a_visible_failure(self):
        loop = AgentLoop(
            tools=_registry(),
            tool_plan=_plan_calls([("不存在的工具", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            max_iterations=1,
        )
        obs = loop.run("查收盘价").steps[0].observations[0]
        assert obs.ok is False

    def test_failed_output_containing_keyword_does_not_meet_goal(self):
        """
        关键红线：失败工具的报错文本里恰好含有必需关键词时，
        绝不能因此把目标判成达成。
        """
        def boom():
            raise RuntimeError("获取收盘价失败")

        loop = AgentLoop(
            tools=_registry(quote=boom),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            max_iterations=1,
        )
        result = loop.run("查收盘价")
        assert result.goal_met is False, "把失败报错当成证据，等于伪造达成"
        assert "收盘价" in result.missing

    def test_failure_recorded_in_memory_with_ok_flag(self):
        def boom():
            raise RuntimeError("炸了")

        loop = AgentLoop(
            tools=_registry(quote=boom),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            max_iterations=1,
        )
        loop.run("查收盘价", session_id="s3")
        obs = loop.memory.latest("s3", MemoryKind.OBSERVATION)
        assert obs is not None
        assert obs.meta["ok"] is False

    def test_one_tool_failing_does_not_stop_others(self):
        def boom():
            raise RuntimeError("炸了")

        loop = AgentLoop(
            tools=_registry(bad=boom, good=lambda: "成交量 999"),
            tool_plan=_plan_calls([("bad", {}), ("good", {})]),
            reflector=KeywordReflector(required=("成交量",)),
            max_iterations=1,
        )
        result = loop.run("查行情")
        assert len(result.steps[0].observations) == 2
        assert result.goal_met is True, "前一个工具失败不应阻断后一个"


# ─────────────────────────────────────────────────────────────────────────────
# 要素 5：继续 / 结束 与目标循环
# ─────────────────────────────────────────────────────────────────────────────


class TestGoalLoopTermination:
    def test_stops_immediately_when_goal_met(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            max_iterations=5,
        )
        result = loop.run("查收盘价")
        assert result.goal_met is True
        assert result.stop_reason == StopReason.GOAL_MET
        assert result.iterations == 1, "目标已达成却继续刷轮次，是无谓消耗"

    def test_loops_until_evidence_complete(self):
        """分两轮才拿全证据时，循环必须真的跑到第 2 轮再结束。"""
        def tool_plan(goal, iteration, observations):
            return [("quote", {})] if iteration == 1 else [("volume", {})]

        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5", volume=lambda: "成交量 999"),
            tool_plan=tool_plan,
            reflector=KeywordReflector(required=("收盘价", "成交量")),
            max_iterations=5,
        )
        result = loop.run("查行情")
        assert result.iterations == 2
        assert result.goal_met is True
        assert result.stop_reason == StopReason.GOAL_MET

    def test_max_iterations_reports_failure_honestly(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("永不出现的证据",)),
            max_iterations=3,
        )
        result = loop.run("查行情")
        assert result.iterations == 3
        assert result.goal_met is False
        assert result.stop_reason == StopReason.MAX_ITERATIONS
        assert "未达成" in result.answer, "触上限却不写明未达成，属于谎报"
        assert "永不出现的证据" in result.answer

    def test_reflector_stop_is_honored(self):
        def stopper(*, goal, iteration, observations, all_observations):
            return Reflection(
                assessment="无法推进", goal_met=False, should_continue=False,
                missing=("外部数据",), reason="上游不可用",
            )

        loop = AgentLoop(
            tools=_registry(quote=lambda: "x"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=stopper, max_iterations=9,
        )
        result = loop.run("查行情")
        assert result.iterations == 1
        assert result.goal_met is False
        assert result.stop_reason == StopReason.REFLECTOR_STOP

    def test_repeated_tool_failures_stop_the_loop(self):
        """连续失败达到上限应诚实停止，而不是刷满迭代假装努力。"""
        def boom():
            raise RuntimeError("上游持续不可用")

        loop = AgentLoop(
            tools=_registry(quote=boom),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",), max_failures=2),
            max_iterations=10,
        )
        result = loop.run("查收盘价")
        assert result.iterations == 2
        assert result.stop_reason == StopReason.REFLECTOR_STOP
        assert result.goal_met is False

    def test_decision_text_says_continue_then_end(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("成交量",)),
            max_iterations=2,
        )
        decisions = [s.decision for s in loop.run("查行情").steps]
        assert "继续" in decisions[0]
        assert "结束" in decisions[1]

    def test_goal_reached_event_only_when_met(self):
        bus = EventBus()
        reached: list[dict[str, Any]] = []
        bus.register(LoopEvent.GOAL_REACHED, lambda ctx: reached.append(ctx.payload), name="r")

        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("成交量",)),
            bus=bus, max_iterations=2,
        )
        loop.run("查行情")
        assert reached == [], "目标未达成却广播 goal_reached，是虚假信号"

    def test_iterations_are_numbered_from_one(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "x"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("无",)),
            max_iterations=3,
        )
        assert [s.iteration for s in loop.run("查行情").steps] == [1, 2, 3]

    def test_empty_tool_plan_does_not_spin_forever(self):
        """无工具可调时不能死循环，必须在上限处停下并标注未达成。"""
        loop = AgentLoop(
            tools=_registry(),
            tool_plan=lambda goal, iteration, observations: (),
            reflector=KeywordReflector(required=("收盘价",)),
            max_iterations=3,
        )
        result = loop.run("查收盘价")
        assert result.iterations == 3
        assert result.goal_met is False
        assert result.stop_reason == StopReason.MAX_ITERATIONS


# ─────────────────────────────────────────────────────────────────────────────
# 记忆持久化
# ─────────────────────────────────────────────────────────────────────────────


class TestLoopMemoryPersistence:
    def test_run_with_sqlite_memory_survives_reopen(self, tmp_path):
        db = tmp_path / "loop.db"
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            memory=SQLiteLoopMemory(db),
        )
        loop.run("查收盘价", session_id="持久化会话")

        # 全新实例、同一个文件：读到的必须是刚才写的五要素。
        reopened = SQLiteLoopMemory(db)
        kinds = {r.kind for r in reopened.records("持久化会话")}
        assert MemoryKind.PLAN in kinds
        assert MemoryKind.REFLECTION in kinds
        assert MemoryKind.DECISION in kinds

    def test_two_sessions_do_not_mix(self):
        memory = InMemoryLoopMemory()
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            memory=memory,
        )
        loop.run("查收盘价", session_id="甲")
        loop.run("查收盘价", session_id="乙")
        assert all(r.session_id == "甲" for r in memory.records("甲"))
        assert len(memory.records("乙")) > 0

    def test_answer_written_to_memory(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        result = loop.run("查收盘价", session_id="s4")
        stored = loop.memory.latest("s4", MemoryKind.ANSWER)
        assert stored is not None
        assert stored.content == result.answer

    def test_goal_recorded_at_iteration_zero(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 1"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        loop.run("查收盘价", session_id="s5")
        goal = loop.memory.latest("s5", MemoryKind.GOAL)
        assert goal is not None
        assert goal.iteration == 0
        assert goal.content == "查收盘价"

    def test_memory_iterations_match_step_numbers(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("成交量",)),
            max_iterations=2,
        )
        loop.run("查行情", session_id="s6")
        plan_iters = [r.iteration for r in loop.memory.records("s6", MemoryKind.PLAN)]
        assert plan_iters == [1, 2]


# ─────────────────────────────────────────────────────────────────────────────
# 心跳接入
# ─────────────────────────────────────────────────────────────────────────────


class TestHeartbeatIntegration:
    def test_heartbeat_fires_during_loop(self):
        clock = ManualClock()
        sched = HeartbeatScheduler(clock=clock)
        beats: list[int] = []
        sched.register("巡检", interval_s=5.0, callback=lambda: beats.append(1))

        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("成交量",)),
            scheduler=sched, max_iterations=3,
        )
        result = loop.run("查行情")
        assert len(beats) >= 1, "接了调度器却一次都没触发，等于假接口"
        assert result.heartbeats == len(beats)

    def test_heartbeat_fires_each_interval_as_clock_advances(self):
        clock = ManualClock()
        sched = HeartbeatScheduler(clock=clock)
        beats: list[float] = []
        sched.register("巡检", interval_s=5.0, callback=lambda: beats.append(clock.now))

        # 工具每次被调用就推进 5 秒，于是每轮迭代末都应恰好触发一次心跳。
        def advancing_quote():
            clock.advance(5.0)
            return "只有收盘价"

        loop = AgentLoop(
            tools=_registry(quote=advancing_quote),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("成交量",)),
            scheduler=sched, max_iterations=3,
        )
        result = loop.run("查行情")
        assert result.heartbeats == 3
        assert beats == [5.0, 10.0, 15.0]

    def test_heartbeat_failure_does_not_break_loop(self):
        clock = ManualClock()
        sched = HeartbeatScheduler(clock=clock)

        def bad():
            raise RuntimeError("心跳回调炸了")

        sched.register("坏定时器", interval_s=1.0, callback=bad)

        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            scheduler=sched,
        )
        result = loop.run("查收盘价")
        assert result.goal_met is True, "心跳回调异常拖垮了主循环"
        assert sched.stats()["failed"] == 1, "心跳失败被吞掉了"

    def test_no_scheduler_means_zero_heartbeats(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        assert loop.run("查收盘价").heartbeats == 0


# ─────────────────────────────────────────────────────────────────────────────
# 事件钩子接入
# ─────────────────────────────────────────────────────────────────────────────


class TestHookIntegration:
    def test_hook_receives_real_payload(self):
        bus = EventBus()
        payloads: list[dict[str, Any]] = []
        bus.register(LoopEvent.OBSERVATION, lambda ctx: payloads.append(ctx.payload), name="p")

        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            bus=bus,
        )
        loop.run("查收盘价")
        assert payloads[0]["tool"] == "quote"
        assert payloads[0]["output"] == "收盘价 10.5"
        assert payloads[0]["ok"] is True

    def test_hook_failure_counted_but_loop_completes(self):
        bus = EventBus()

        def bad(ctx: HookContext):
            raise RuntimeError("钩子炸了")

        bus.register(LoopEvent.PLAN, bad, name="坏钩子")

        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
            bus=bus,
        )
        result = loop.run("查收盘价")
        assert result.goal_met is True, "钩子异常拖垮了主循环"
        assert result.hook_failures == 1, "钩子失败没有被计数，等于静默吞掉"

    def test_reflection_payload_carries_missing(self):
        bus = EventBus()
        payloads: list[dict[str, Any]] = []
        bus.register(LoopEvent.REFLECTION, lambda ctx: payloads.append(ctx.payload), name="p")

        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("成交量",)),
            bus=bus, max_iterations=1,
        )
        loop.run("查行情")
        assert payloads[0]["missing"] == ["成交量"] or payloads[0]["missing"] == ("成交量",)

    def test_loop_end_payload_reports_stop_reason(self):
        bus = EventBus()
        payloads: list[dict[str, Any]] = []
        bus.register(LoopEvent.LOOP_END, lambda ctx: payloads.append(ctx.payload), name="p")

        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("成交量",)),
            bus=bus, max_iterations=2,
        )
        loop.run("查行情")
        assert payloads[0]["stop_reason"] == StopReason.MAX_ITERATIONS
        assert payloads[0]["goal_met"] is False
        assert payloads[0]["iterations"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# LLM 驱动路径
# ─────────────────────────────────────────────────────────────────────────────


class TestProviderDrivenPath:
    def test_provider_tool_calls_are_executed(self):
        provider = StubProvider([
            ModelReply(text="我先查行情", tool_calls=(ToolCall(name="quote", arguments={"symbol": "600519"}),)),
        ])
        loop = AgentLoop(
            tools=_registry(quote=lambda symbol: f"{symbol} 收盘价 10.5"),
            provider=provider,
            reflector=KeywordReflector(required=("收盘价",)),
        )
        result = loop.run("查收盘价")
        assert result.goal_met is True
        assert result.steps[0].observations[0].output == "600519 收盘价 10.5"
        assert result.steps[0].assistant_text == "我先查行情"

    def test_provider_receives_tool_descriptions(self):
        provider = StubProvider([ModelReply(text="", tool_calls=())])
        loop = AgentLoop(
            tools=_registry(quote=lambda: "x"),
            provider=provider,
            reflector=KeywordReflector(required=("永不",)),
            max_iterations=1,
        )
        loop.run("查行情")
        names = [t.name for t in provider.tools_seen[0]]
        assert names == ["quote"]

    def test_provider_sees_observations_in_next_round(self):
        provider = StubProvider([
            ModelReply(text="", tool_calls=(ToolCall(name="quote", arguments={}),)),
            ModelReply(text="", tool_calls=(ToolCall(name="quote", arguments={}),)),
        ])
        loop = AgentLoop(
            tools=_registry(quote=lambda: "只有收盘价"),
            provider=provider,
            reflector=KeywordReflector(required=("成交量",)),
            max_iterations=2,
        )
        loop.run("查行情")
        # 第 2 次调用时，历史里必须已经带上第 1 轮的工具结果。
        second_call_roles = [m.role for m in provider.calls[1]]
        assert "tool" in second_call_roles

    def test_provider_name_reported(self):
        provider = StubProvider([ModelReply(text="", tool_calls=())], name="stub-llm")
        loop = AgentLoop(
            tools=_registry(quote=lambda: "x"), provider=provider,
            reflector=KeywordReflector(required=("永不",)), max_iterations=1,
        )
        assert loop.run("查行情").provider == "stub-llm"

    def test_no_provider_reports_rule_driven(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 1"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        assert "规则" in loop.run("查行情").provider

    def test_history_is_prepended(self):
        provider = StubProvider([ModelReply(text="", tool_calls=())])
        loop = AgentLoop(
            tools=_registry(quote=lambda: "x"), provider=provider,
            reflector=KeywordReflector(required=("永不",)), max_iterations=1,
        )
        loop.run("查行情", history=[ChatMessage(role="user", content="之前聊过的内容")])
        contents = [m.content for m in provider.calls[0]]
        assert contents[0] == "之前聊过的内容"
        assert contents[-1] == "查行情"


# ─────────────────────────────────────────────────────────────────────────────
# 入参校验与数据结构
# ─────────────────────────────────────────────────────────────────────────────


class TestValidationAndSerialization:
    @pytest.mark.parametrize("bad", [0, -1, -10])
    def test_max_iterations_must_be_positive(self, bad):
        with pytest.raises(ValueError, match="max_iterations"):
            AgentLoop(tools=_registry(), max_iterations=bad)

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_empty_goal_rejected(self, bad):
        loop = AgentLoop(tools=_registry())
        with pytest.raises(ValueError, match="goal"):
            loop.run(bad)

    def test_result_to_dict_is_json_ready(self):
        import json

        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 10.5"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        payload = loop.run("查收盘价").to_dict()
        assert json.loads(json.dumps(payload, ensure_ascii=False))["goal_met"] is True

    def test_observation_to_dict_round_trip(self):
        obs = Observation(tool="quote", arguments={"a": 1}, output="out", ok=False, error="e")
        assert obs.to_dict()["ok"] is False
        assert obs.to_dict()["error"] == "e"

    def test_reflection_to_dict_includes_missing(self):
        r = Reflection(assessment="a", goal_met=False, should_continue=True,
                       missing=("x",), reason="r")
        assert list(r.to_dict()["missing"]) == ["x"]

    def test_stop_reason_constants_unique(self):
        reasons = StopReason.all_reasons()
        assert len(reasons) == len(set(reasons))

    def test_result_is_frozen(self):
        loop = AgentLoop(
            tools=_registry(quote=lambda: "收盘价 1"),
            tool_plan=_plan_calls([("quote", {})]),
            reflector=KeywordReflector(required=("收盘价",)),
        )
        result = loop.run("查行情")
        assert isinstance(result, AgentLoopResult)
        with pytest.raises(Exception):
            result.goal_met = False  # type: ignore[misc]

    def test_default_reflector_stops_on_first_success(self):
        """未声明必需证据时，拿到有效观察即算达成，不无谓多跑。"""
        loop = AgentLoop(
            tools=_registry(quote=lambda: "任意输出"),
            tool_plan=_plan_calls([("quote", {})]),
            max_iterations=5,
        )
        result = loop.run("随便查点东西")
        assert result.goal_met is True
        assert result.iterations == 1
