"""
事件钩子（Event Hooks）
=======================
说明书要求「心跳/定时、事件钩子、目标循环必须是项目内可测试的最小可靠实现，
不是文档或假接口」。本模块是「事件钩子」这一项的实现。

设计取舍
--------
钩子机制最容易写成「留一个 ``on_event`` 属性、谁也不调用」的假接口。本模块的
钩子由 :mod:`agent_platform.core.agent_loop` 在 Loop 五要素的每个阶段真实
``emit``，事件名集中在 :class:`LoopEvent`，测试可直接断言「跑一轮 Loop 收到了
哪些事件、顺序如何」。

可靠性纪律
----------
1. **一个钩子抛异常，不得中断主流程，也不得影响其他钩子**：异常被捕获写入
   :class:`HookRecord` 的 ``error``，其余钩子照常执行。业务主链路不因监听者
   失败而失败。
2. **异常不得静默**：失败写入 ``history``，``stats()['failed']`` 计数，
   :meth:`EventBus.failures` 可取出明细。吞掉异常等于让故障隐身。
3. **顺序确定可复现**：同一事件的多个钩子按 (priority 升序, 注册序号升序)
   执行。不依赖 dict 插入顺序之外的隐式行为，便于断言。
4. **未知事件名不静默通过**：``emit`` 一个从未注册过监听者的事件是合法的
   （返回空列表），但 :meth:`EventBus.register` 一个不在 :class:`LoopEvent`
   已知集合内的事件名时，若开启 ``strict_events`` 会直接报错，避免把
   ``"relfection"`` 这类拼写错误变成「钩子永远不触发」的静默失效。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)


class LoopEvent:
    """
    Loop 生命周期标准事件名。

    与 Loop 五要素（规划、工具调用、观察、反思、继续/结束）一一对应，
    另加起止与异常事件。用常量而非裸字符串，拼写错误在导入期就暴露。
    """

    LOOP_START: Final[str] = "loop_start"
    PLAN: Final[str] = "plan"                    # 要素 1：规划
    TOOL_CALL: Final[str] = "tool_call"          # 要素 2：工具调用
    OBSERVATION: Final[str] = "observation"      # 要素 3：观察
    REFLECTION: Final[str] = "reflection"        # 要素 4：反思
    DECISION: Final[str] = "decision"            # 要素 5：继续规划 / 结束
    ITERATION_END: Final[str] = "iteration_end"
    GOAL_REACHED: Final[str] = "goal_reached"
    LOOP_END: Final[str] = "loop_end"
    ERROR: Final[str] = "error"

    @classmethod
    def all_events(cls) -> tuple[str, ...]:
        """已知事件名全集，供 ``strict_events`` 校验与文档使用。"""
        return (
            cls.LOOP_START, cls.PLAN, cls.TOOL_CALL, cls.OBSERVATION,
            cls.REFLECTION, cls.DECISION, cls.ITERATION_END,
            cls.GOAL_REACHED, cls.LOOP_END, cls.ERROR,
        )


@dataclass(frozen=True, slots=True)
class HookContext:
    """
    传给钩子的上下文。

    ``payload`` 为事件相关数据（如 plan 事件带 ``{"plan": ...}``）。
    ``sequence`` 是全局递增的 emit 序号，便于在乱序日志里还原真实时序。
    """

    event: str
    payload: dict[str, Any]
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "payload": self.payload, "sequence": self.sequence}


HookHandler = Callable[[HookContext], Any]


@dataclass(frozen=True, slots=True)
class HookRecord:
    """单个钩子被调用一次的审计记录。"""

    event: str
    handler_name: str
    sequence: int
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "handler_name": self.handler_name,
            "sequence": self.sequence,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class _Registration:
    """内部注册项。``order`` 为同优先级下的注册先后，保证顺序确定。"""

    event: str
    name: str
    handler: HookHandler
    priority: int
    order: int
    once: bool


class EventBus:
    """
    最小可靠事件总线。

    用法::

        bus = EventBus()
        seen: list[str] = []
        bus.register(LoopEvent.PLAN, lambda ctx: seen.append(ctx.event), name="记录")
        bus.emit(LoopEvent.PLAN, {"plan": "先查行情"})
        assert seen == ["plan"]
    """

    def __init__(self, *, strict_events: bool = True) -> None:
        self.strict_events = strict_events
        self._handlers: dict[str, list[_Registration]] = {}
        self._counter = 0            # 注册序号，保证同优先级稳定排序
        self._sequence = 0           # emit 序号
        self.history: list[HookRecord] = []

    # ── 注册 ────────────────────────────────────────────────────────────────

    def register(
        self,
        event: str,
        handler: HookHandler,
        *,
        name: str | None = None,
        priority: int = 100,
        once: bool = False,
    ) -> str:
        """
        注册一个钩子，返回其显示名（用于 :meth:`unregister` 与审计）。

        Parameters
        ----------
        priority
            升序执行，数值小的先跑。默认 100，留出前后插入空间。
        once
            为真时触发一次后自动注销。

        Raises
        ------
        ValueError
            ``strict_events`` 开启且事件名不在 :meth:`LoopEvent.all_events` 内。
            这样 ``"relfection"`` 之类拼写错误会立即报错，而不是变成一个
            永远不触发的死钩子。
        TypeError
            handler 不可调用。
        """
        if self.strict_events and event not in LoopEvent.all_events():
            raise ValueError(
                f"未知事件名 {event!r}；已知事件为 {LoopEvent.all_events()}。"
                "若确需自定义事件，请用 EventBus(strict_events=False)。"
            )
        if not callable(handler):
            raise TypeError(f"钩子必须可调用，收到 {type(handler).__name__}")

        display = name or getattr(handler, "__name__", None) or repr(handler)
        self._counter += 1
        reg = _Registration(
            event=event, name=display, handler=handler,
            priority=int(priority), order=self._counter, once=bool(once),
        )
        self._handlers.setdefault(event, []).append(reg)
        return display

    def unregister(self, event: str, name: str) -> bool:
        """按显示名注销钩子。返回是否确实移除了至少一个。"""
        regs = self._handlers.get(event)
        if not regs:
            return False
        kept = [r for r in regs if r.name != name]
        removed = len(kept) != len(regs)
        if kept:
            self._handlers[event] = kept
        else:
            self._handlers.pop(event, None)
        return removed

    def handler_names(self, event: str) -> list[str]:
        """返回该事件的钩子显示名，按实际执行顺序。"""
        return [r.name for r in self._ordered(event)]

    def registered_events(self) -> list[str]:
        return sorted(self._handlers)

    def _ordered(self, event: str) -> list[_Registration]:
        return sorted(self._handlers.get(event, []), key=lambda r: (r.priority, r.order))

    # ── 触发 ────────────────────────────────────────────────────────────────

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> list[HookRecord]:
        """
        触发事件，按顺序调用全部钩子，返回本次调用记录。

        钩子异常被捕获记录，**绝不外泄**：主业务流程不因监听者失败而中断。
        无监听者时返回空列表（不报错）——「没人关心这个事件」是正常状态。
        """
        self._sequence += 1
        seq = self._sequence
        ctx = HookContext(event=event, payload=dict(payload or {}), sequence=seq)

        records: list[HookRecord] = []
        fired_once: list[_Registration] = []
        for reg in self._ordered(event):
            try:
                reg.handler(ctx)
            except Exception as exc:  # noqa: BLE001 — 监听者故障不得拖垮主流程
                logger.warning("[EventBus] 钩子 %s 处理 %s 失败: %s", reg.name, event, exc)
                record = HookRecord(
                    event=event, handler_name=reg.name, sequence=seq,
                    ok=False, error=f"{type(exc).__name__}: {exc}",
                )
            else:
                record = HookRecord(
                    event=event, handler_name=reg.name, sequence=seq, ok=True,
                )
            records.append(record)
            self.history.append(record)
            if reg.once:
                fired_once.append(reg)

        for reg in fired_once:
            self.unregister(reg.event, reg.name)

        return records

    # ── 审计 ────────────────────────────────────────────────────────────────

    def events_seen(self) -> list[str]:
        """按调用时序去重前的事件名序列，用于断言 Loop 阶段顺序。"""
        seen: list[str] = []
        for record in self.history:
            if not seen or seen[-1] != record.event:
                seen.append(record.event)
        return seen

    def failures(self) -> list[HookRecord]:
        return [r for r in self.history if not r.ok]

    def stats(self) -> dict[str, Any]:
        total = len(self.history)
        failed = len(self.failures())
        return {
            "handlers": sum(len(v) for v in self._handlers.values()),
            "events_registered": len(self._handlers),
            "invocations": total,
            "ok": total - failed,
            "failed": failed,
        }

    def reset_history(self) -> None:
        self.history.clear()


@dataclass
class RecordingHook:
    """
    把收到的上下文全部存下来的钩子，供测试与调试使用。

    用法::

        rec = RecordingHook()
        bus.register(LoopEvent.PLAN, rec, name="rec")
        ...
        assert [c.event for c in rec.contexts] == ["plan"]
    """

    contexts: list[HookContext] = field(default_factory=list)

    def __call__(self, ctx: HookContext) -> None:
        self.contexts.append(ctx)

    @property
    def events(self) -> list[str]:
        return [c.event for c in self.contexts]

    def payloads_for(self, event: str) -> list[dict[str, Any]]:
        return [c.payload for c in self.contexts if c.event == event]
