"""
Agent Loop：五要素 + 目标循环
==============================
说明书要求 Loop 具备五要素：**规划 → 工具调用 → 观察 → 反思 → 继续规划/结束**，
并要求「目标循环」是项目内最小可靠可测实现。本模块是这两项的实现。

与 ``agent_runtime.py`` 的关系
------------------------------
``AgentRuntime`` 是一个 69 行的裸 ReAct 循环：它有工具调用和观察，但**没有显式的
规划产物，也没有显式的反思产物**，停止条件只是「模型不再要求调用工具」或「步数
用尽」。它够用，且已有大量测试依赖它，因此**保留不动**。

本模块新增的是被审计出缺失的部分：

===============  ================================  ==============================
Loop 要素         AgentRuntime                      AgentLoop（本模块）
===============  ================================  ==============================
1 规划            无（隐式在模型输出里）             ``LoopStep.plan``，落记忆落钩子
2 工具调用        有                                有，并逐条记录成功/失败
3 观察            有（tool 消息）                    ``LoopStep.observations``
4 反思            无                                ``LoopStep.reflection`` 显式产物
5 继续/结束       隐式（无 tool_calls 即停）          ``LoopStep.decision`` + 停止原因
记忆              由上层 chat 会话代管               直接写 :mod:`loop_memory`
目标循环          无                                ``run(goal=...)`` 直到目标达成
===============  ================================  ==============================

为什么默认 planner / reflector 不用 LLM
---------------------------------------
若规划与反思只能靠 LLM 产出，那么离线（禁网）环境下这两个要素就无法验收，测试也会
变成「打桩打出一个假答案再断言它等于自己」。因此本模块的默认 :class:`RuleBasedPlanner`
与 :class:`KeywordReflector` 是**确定性代码实现**：规划由目标与上一轮观察推导，
目标达成由「必需证据关键词是否已在观察中出现」判定。LLM 可作为可选增强注入，
但不构成五要素可运行的前提。

纪律
----
1. **工具失败不伪装成成功**：``ToolExecutionResult.is_error`` 为真时，该次观察
   ``ok=False``、``error`` 保留原文，并触发 ``LoopEvent.ERROR``，绝不当成正常结果。
2. **不无限循环**：``max_iterations`` 为硬上限，达到即以 ``max_iterations``
   停止原因结束，并且 ``goal_met=False`` —— 不把「跑完了」说成「做到了」。
3. **钩子异常不影响主循环**：由 :class:`EventBus` 隔离，见该模块纪律。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from agent_platform.core.event_hooks import EventBus, LoopEvent
from agent_platform.core.llm_provider import ChatMessage, LLMProvider, ToolDescription
from agent_platform.core.loop_memory import InMemoryLoopMemory, LoopMemory, MemoryKind
from agent_platform.core.scheduler import HeartbeatScheduler
from agent_platform.core.tools import ToolRegistry

logger = logging.getLogger(__name__)


class StopReason:
    """循环结束原因。"""

    GOAL_MET: Final[str] = "goal_met"                 # 反思判定目标达成
    MAX_ITERATIONS: Final[str] = "max_iterations"     # 触及硬上限（目标未达成）
    REFLECTOR_STOP: Final[str] = "reflector_stop"     # 反思要求停止（如判定无法推进）
    NO_PROGRESS: Final[str] = "no_progress"           # 连续多轮无新观察

    @classmethod
    def all_reasons(cls) -> tuple[str, ...]:
        return (cls.GOAL_MET, cls.MAX_ITERATIONS, cls.REFLECTOR_STOP, cls.NO_PROGRESS)


# ─────────────────────────────────────────────────────────────────────────────
# 观察 / 反思 / 步骤 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Observation:
    """要素 3：一次工具调用的观察结果。失败也是观察，必须留痕。"""

    tool: str
    arguments: dict[str, Any]
    output: str
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "output": self.output,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class Reflection:
    """
    要素 4：反思产物。

    ``goal_met`` 与 ``should_continue`` 是两个独立判断：目标达成必然停止，但目标
    未达成也可能因为「已确认无法推进」而停止（``should_continue=False``）。把两者
    合并成一个布尔量会导致「没做到」和「做完了」在结果里长得一样。
    """

    assessment: str
    goal_met: bool
    should_continue: bool
    missing: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment": self.assessment,
            "goal_met": self.goal_met,
            "should_continue": self.should_continue,
            "missing": list(self.missing),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class LoopStep:
    """一轮循环的完整五要素记录。"""

    iteration: int
    plan: str
    observations: tuple[Observation, ...]
    reflection: Reflection
    decision: str
    assistant_text: str = ""

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(o.tool for o in self.observations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "plan": self.plan,
            "observations": [o.to_dict() for o in self.observations],
            "reflection": self.reflection.to_dict(),
            "decision": self.decision,
            "assistant_text": self.assistant_text,
        }


@dataclass(frozen=True, slots=True)
class AgentLoopResult:
    """循环运行结果。"""

    session_id: str
    goal: str
    answer: str
    steps: tuple[LoopStep, ...]
    goal_met: bool
    stop_reason: str
    provider: str
    missing: tuple[str, ...] = ()
    heartbeats: int = 0
    hook_failures: int = 0

    @property
    def iterations(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "goal": self.goal,
            "answer": self.answer,
            "iterations": self.iterations,
            "steps": [s.to_dict() for s in self.steps],
            "goal_met": self.goal_met,
            "stop_reason": self.stop_reason,
            "provider": self.provider,
            "missing": list(self.missing),
            "heartbeats": self.heartbeats,
            "hook_failures": self.hook_failures,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 要素 1：规划器
# ─────────────────────────────────────────────────────────────────────────────

class Planner(Protocol):
    """规划器接口。返回本轮的规划文本。"""

    def __call__(
        self, *, goal: str, iteration: int, observations: Sequence[Observation],
        missing: Sequence[str],
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class RuleBasedPlanner:
    """
    确定性规划器：由目标 + 尚缺证据推导下一步，不依赖 LLM，离线可跑可测。

    首轮规划目标本身；后续轮次针对上一轮反思给出的 ``missing`` 项规划补齐动作。
    """

    def __call__(
        self, *, goal: str, iteration: int, observations: Sequence[Observation],
        missing: Sequence[str],
    ) -> str:
        if iteration <= 1:
            return f"第 1 轮规划：拆解目标「{goal}」，调用工具收集所需证据。"
        if missing:
            return (
                f"第 {iteration} 轮规划：已获得 {len(observations)} 条观察，"
                f"仍缺 {', '.join(missing)}，本轮针对缺口补齐。"
            )
        failed = [o.tool for o in observations if not o.ok]
        if failed:
            return (
                f"第 {iteration} 轮规划：工具 {', '.join(sorted(set(failed)))} 上一轮失败，"
                f"本轮重试或改用替代路径。"
            )
        return f"第 {iteration} 轮规划：复核已有证据是否足以回答「{goal}」。"


# ─────────────────────────────────────────────────────────────────────────────
# 要素 4：反思器
# ─────────────────────────────────────────────────────────────────────────────

class Reflector(Protocol):
    """反思器接口。基于累计观察判断目标是否达成、是否继续。"""

    def __call__(
        self, *, goal: str, iteration: int, observations: Sequence[Observation],
        all_observations: Sequence[Observation],
    ) -> Reflection: ...


@dataclass(frozen=True, slots=True)
class KeywordReflector:
    """
    确定性反思器：目标达成 = 所有 ``required`` 关键词都已在**成功**观察里出现。

    只认成功观察：失败工具的错误信息里可能恰好含有关键词（例如报错文本里带着
    「收盘价」二字），若把失败输出也算作证据，就会把一次失败判成目标达成。
    """

    required: tuple[str, ...] = ()
    max_failures: int = 3

    def __call__(
        self, *, goal: str, iteration: int, observations: Sequence[Observation],
        all_observations: Sequence[Observation],
    ) -> Reflection:
        ok_text = "\n".join(o.output for o in all_observations if o.ok)
        missing = tuple(kw for kw in self.required if kw not in ok_text)
        failures = sum(1 for o in all_observations if not o.ok)

        if not self.required:
            # 未声明必需证据：以「本轮拿到至少一条成功观察」为达成标准。
            got = any(o.ok for o in observations)
            return Reflection(
                assessment=f"本轮成功观察 {sum(1 for o in observations if o.ok)} 条",
                goal_met=got, should_continue=not got, missing=(),
                reason="已取得有效观察" if got else "本轮无有效观察，继续",
            )

        if not missing:
            return Reflection(
                assessment=f"必需证据 {len(self.required)} 项全部到位",
                goal_met=True, should_continue=False, missing=(),
                reason="证据齐备，目标达成",
            )

        if failures >= self.max_failures:
            # 连续失败到阈值：诚实地停下并标注未达成，而不是继续刷轮次假装努力。
            return Reflection(
                assessment=f"累计工具失败 {failures} 次，达到上限 {self.max_failures}",
                goal_met=False, should_continue=False, missing=missing,
                reason=f"工具连续失败，无法补齐 {', '.join(missing)}",
            )

        return Reflection(
            assessment=f"仍缺 {len(missing)} 项证据",
            goal_met=False, should_continue=True, missing=missing,
            reason=f"缺口：{', '.join(missing)}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 主循环
# ─────────────────────────────────────────────────────────────────────────────

ToolPlan = Callable[[str, int, Sequence[Observation]], Sequence[tuple[str, dict[str, Any]]]]


class AgentLoop:
    """
    五要素 Loop + 目标循环。

    Parameters
    ----------
    tools
        工具注册表，要素 2 的执行者。
    provider
        可选 LLM Provider。为 ``None`` 时循环完全靠 ``tool_plan`` 驱动，
        离线禁网环境下依然可运行可验收。
    memory
        记忆层。默认进程内实现；传入 :class:`SQLiteLoopMemory` 即获得持久化。
    bus
        事件总线。默认自建一个，运行后可从 ``bus.history`` 审计五要素是否都触发过。
    scheduler
        可选心跳调度器。每轮迭代末 ``poll()`` 一次，用于「循环内定时任务」。
    max_iterations
        硬上限。必须为正。
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        provider: LLMProvider | None = None,
        memory: LoopMemory | None = None,
        bus: EventBus | None = None,
        scheduler: HeartbeatScheduler | None = None,
        planner: Planner | None = None,
        reflector: Reflector | None = None,
        tool_plan: ToolPlan | None = None,
        max_iterations: int = 6,
    ) -> None:
        if max_iterations <= 0:
            raise ValueError(f"max_iterations 必须为正，收到 {max_iterations}")
        self.tools = tools
        self.provider = provider
        self.memory: LoopMemory = memory or InMemoryLoopMemory()
        self.bus = bus or EventBus()
        self.scheduler = scheduler
        self.planner: Planner = planner or RuleBasedPlanner()
        self.reflector: Reflector = reflector or KeywordReflector()
        self.tool_plan = tool_plan
        self.max_iterations = int(max_iterations)

    # ── 要素 2：工具调用 ────────────────────────────────────────────────────

    def _decide_tool_calls(
        self, goal: str, iteration: int, observations: Sequence[Observation],
        messages: list[ChatMessage],
    ) -> tuple[list[tuple[str, dict[str, Any]]], str]:
        """
        决定本轮调用哪些工具。返回 (调用列表, assistant 文本)。

        优先使用显式注入的 ``tool_plan``（确定性，离线可测）；否则交给 LLM。
        两者都没有时返回空列表 —— 空转一轮由反思去判定停止，不静默死循环。
        """
        if self.tool_plan is not None:
            calls = [(name, dict(args)) for name, args in self.tool_plan(goal, iteration, observations)]
            return calls, ""

        if self.provider is None:
            return [], ""

        descriptions = [
            ToolDescription(name=t.name, description=t.description)
            for t in self.tools.descriptions()
        ] if _descriptions_are_tools(self.tools) else list(self.tools.descriptions())

        reply = self.provider.generate(messages, descriptions)
        calls = [(c.name, dict(c.arguments)) for c in reply.tool_calls]
        return calls, reply.text or ""

    def _execute(
        self, calls: Sequence[tuple[str, dict[str, Any]]], iteration: int, session_id: str,
    ) -> list[Observation]:
        observations: list[Observation] = []
        for name, arguments in calls:
            self.memory.append(
                session_id, iteration, MemoryKind.TOOL_CALL,
                f"调用 {name}", meta={"tool": name, "arguments": arguments},
            )
            self.bus.emit(LoopEvent.TOOL_CALL, {
                "iteration": iteration, "tool": name, "arguments": arguments,
            })

            result = self.tools.execute(name, arguments)
            observation = Observation(
                tool=name, arguments=dict(arguments), output=result.output,
                ok=not result.is_error,
                error=result.output if result.is_error else None,
            )
            observations.append(observation)

            self.memory.append(
                session_id, iteration, MemoryKind.OBSERVATION,
                observation.output, meta={"tool": name, "ok": observation.ok},
            )
            self.bus.emit(LoopEvent.OBSERVATION, {
                "iteration": iteration, "tool": name, "ok": observation.ok,
                "output": observation.output,
            })
            if not observation.ok:
                # 失败必须显式广播，不能只躺在 observation 里等人翻。
                self.bus.emit(LoopEvent.ERROR, {
                    "iteration": iteration, "tool": name, "error": observation.error,
                })
        return observations

    # ── 目标循环 ────────────────────────────────────────────────────────────

    def run(self, goal: str, *, session_id: str = "loop", history: Sequence[ChatMessage] = ()) -> AgentLoopResult:
        """
        运行目标循环，直到目标达成、反思要求停止或触及硬上限。

        每轮依次产出五要素并全部写入记忆与事件总线，运行后可逐项审计。
        """
        if not str(goal).strip():
            raise ValueError("goal 不能为空")

        self.memory.append(session_id, 0, MemoryKind.GOAL, goal)
        self.bus.emit(LoopEvent.LOOP_START, {"goal": goal, "session_id": session_id,
                                             "max_iterations": self.max_iterations})

        messages: list[ChatMessage] = [*history, ChatMessage(role="user", content=goal)]
        steps: list[LoopStep] = []
        all_observations: list[Observation] = []
        missing: tuple[str, ...] = ()
        stop_reason = StopReason.MAX_ITERATIONS
        goal_met = False
        heartbeats = 0

        for iteration in range(1, self.max_iterations + 1):
            # ── 要素 1：规划 ──
            plan = self.planner(
                goal=goal, iteration=iteration,
                observations=tuple(all_observations), missing=missing,
            )
            self.memory.append(session_id, iteration, MemoryKind.PLAN, plan)
            self.bus.emit(LoopEvent.PLAN, {"iteration": iteration, "plan": plan})

            # ── 要素 2 + 3：工具调用与观察 ──
            calls, assistant_text = self._decide_tool_calls(
                goal, iteration, tuple(all_observations), messages,
            )
            observations = self._execute(calls, iteration, session_id)
            all_observations.extend(observations)

            if assistant_text:
                messages.append(ChatMessage(role="assistant", content=assistant_text))
            for observation in observations:
                messages.append(ChatMessage(role="tool", content=observation.output))

            # ── 要素 4：反思 ──
            reflection = self.reflector(
                goal=goal, iteration=iteration, observations=tuple(observations),
                all_observations=tuple(all_observations),
            )
            missing = reflection.missing
            self.memory.append(
                session_id, iteration, MemoryKind.REFLECTION,
                reflection.assessment, meta=reflection.to_dict(),
            )
            self.bus.emit(LoopEvent.REFLECTION, {
                "iteration": iteration, **reflection.to_dict(),
            })

            # ── 要素 5：继续规划 / 结束 ──
            if reflection.goal_met:
                goal_met, stop_reason = True, StopReason.GOAL_MET
                decision = "结束：目标达成"
            elif not reflection.should_continue:
                stop_reason = StopReason.REFLECTOR_STOP
                decision = f"结束：{reflection.reason}"
            elif iteration >= self.max_iterations:
                stop_reason = StopReason.MAX_ITERATIONS
                decision = f"结束：达到迭代上限 {self.max_iterations}，目标未达成"
            else:
                decision = "继续规划下一轮"

            self.memory.append(
                session_id, iteration, MemoryKind.DECISION, decision,
                meta={"goal_met": reflection.goal_met,
                      "should_continue": reflection.should_continue},
            )
            self.bus.emit(LoopEvent.DECISION, {
                "iteration": iteration, "decision": decision,
                "goal_met": reflection.goal_met,
            })

            steps.append(LoopStep(
                iteration=iteration, plan=plan,
                observations=tuple(observations), reflection=reflection,
                decision=decision, assistant_text=assistant_text,
            ))

            # 循环内定时任务：放在迭代末，保证每轮至少一次心跳机会。
            if self.scheduler is not None:
                heartbeats += len(self.scheduler.poll())

            self.bus.emit(LoopEvent.ITERATION_END, {
                "iteration": iteration, "decision": decision,
                "observations": len(observations),
            })

            if reflection.goal_met:
                self.bus.emit(LoopEvent.GOAL_REACHED, {
                    "iteration": iteration, "goal": goal,
                })
                break
            if not reflection.should_continue:
                break

        answer = self._compose_answer(goal, goal_met, all_observations, missing, stop_reason)
        self.memory.append(session_id, len(steps), MemoryKind.ANSWER, answer,
                           meta={"goal_met": goal_met, "stop_reason": stop_reason})
        self.bus.emit(LoopEvent.LOOP_END, {
            "goal": goal, "iterations": len(steps), "goal_met": goal_met,
            "stop_reason": stop_reason,
        })

        return AgentLoopResult(
            session_id=session_id, goal=goal, answer=answer, steps=tuple(steps),
            goal_met=goal_met, stop_reason=stop_reason,
            provider=self.provider.name if self.provider is not None else "无（规则驱动）",
            missing=missing, heartbeats=heartbeats,
            hook_failures=len(self.bus.failures()),
        )

    @staticmethod
    def _compose_answer(
        goal: str, goal_met: bool, observations: Sequence[Observation],
        missing: Sequence[str], stop_reason: str,
    ) -> str:
        """
        汇总答案。目标未达成时**明确写出未达成与缺口**，不用含糊措辞掩盖。
        """
        ok_n = sum(1 for o in observations if o.ok)
        fail_n = len(observations) - ok_n
        head = f"目标「{goal}」" + ("已达成" if goal_met else "未达成")
        detail = f"有效观察 {ok_n} 条，失败 {fail_n} 条，结束原因：{stop_reason}"
        if not goal_met and missing:
            detail += f"；缺口：{', '.join(missing)}"
        return f"{head}。{detail}。"


def _descriptions_are_tools(tools: ToolRegistry) -> bool:
    """
    兼容判断：``ToolRegistry.descriptions()`` 返回的是 ``RegisteredTool`` 还是
    ``ToolDescription``。两种形态都能喂给 Provider，避免因上游结构调整而炸掉。
    """
    items = tools.descriptions()
    if not items:
        return False
    return not isinstance(items[0], ToolDescription)
