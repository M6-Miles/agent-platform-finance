"""
事件钩子测试
============
覆盖：注册校验、执行顺序确定性、单钩子异常隔离、once 语义、审计统计。

刻意不测什么
------------
不测「钩子内部业务逻辑对不对」——那是各业务模块自己的测试。这里只保证
**机制本身可靠**：该触发的触发了、顺序稳定、有人炸了别人照跑且故障可见。
"""
from __future__ import annotations

import pytest

from agent_platform.core.event_hooks import (
    EventBus,
    HookContext,
    HookRecord,
    LoopEvent,
    RecordingHook,
)


# ─────────────────────────────────────────────────────────────────────────────
# 事件名常量
# ─────────────────────────────────────────────────────────────────────────────

class TestLoopEventNames:
    def test_all_events_covers_five_loop_elements(self):
        """Loop 五要素必须各有对应事件，缺一个就说明钩子覆盖不全。"""
        events = LoopEvent.all_events()
        for required in (
            LoopEvent.PLAN,          # 规划
            LoopEvent.TOOL_CALL,     # 工具调用
            LoopEvent.OBSERVATION,   # 观察
            LoopEvent.REFLECTION,    # 反思
            LoopEvent.DECISION,      # 继续规划 / 结束
        ):
            assert required in events

    def test_event_names_are_unique(self):
        events = LoopEvent.all_events()
        assert len(events) == len(set(events))

    def test_lifecycle_events_present(self):
        events = LoopEvent.all_events()
        assert LoopEvent.LOOP_START in events
        assert LoopEvent.LOOP_END in events
        assert LoopEvent.ERROR in events
        assert LoopEvent.GOAL_REACHED in events


# ─────────────────────────────────────────────────────────────────────────────
# 注册校验
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistrationValidation:
    def test_unknown_event_rejected_in_strict_mode(self):
        """拼错事件名必须立即报错，否则会变成永远不触发的死钩子。"""
        bus = EventBus()
        with pytest.raises(ValueError, match="未知事件名"):
            bus.register("relfection", lambda ctx: None)

    def test_unknown_event_allowed_when_not_strict(self):
        bus = EventBus(strict_events=False)
        bus.register("my_custom_event", lambda ctx: None, name="h")
        assert bus.handler_names("my_custom_event") == ["h"]

    def test_non_callable_rejected(self):
        bus = EventBus()
        with pytest.raises(TypeError, match="必须可调用"):
            bus.register(LoopEvent.PLAN, "not a function")  # type: ignore[arg-type]

    def test_register_returns_display_name(self):
        bus = EventBus()

        def my_handler(ctx):
            return None

        assert bus.register(LoopEvent.PLAN, my_handler) == "my_handler"
        assert bus.register(LoopEvent.PLAN, my_handler, name="别名") == "别名"

    def test_lambda_gets_a_usable_display_name(self):
        bus = EventBus()
        name = bus.register(LoopEvent.PLAN, lambda ctx: None)
        assert isinstance(name, str) and name


# ─────────────────────────────────────────────────────────────────────────────
# 基本触发
# ─────────────────────────────────────────────────────────────────────────────

class TestEmit:
    def test_handler_receives_event_and_payload(self):
        bus = EventBus()
        rec = RecordingHook()
        bus.register(LoopEvent.PLAN, rec, name="rec")

        bus.emit(LoopEvent.PLAN, {"plan": "先查行情"})

        assert rec.events == [LoopEvent.PLAN]
        assert rec.contexts[0].payload == {"plan": "先查行情"}

    def test_emit_with_no_handlers_returns_empty_and_does_not_raise(self):
        """没人监听是正常状态，不该报错。"""
        bus = EventBus()
        assert bus.emit(LoopEvent.LOOP_END) == []

    def test_emit_returns_one_record_per_handler(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="a")
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="b")

        records = bus.emit(LoopEvent.PLAN)

        assert [r.handler_name for r in records] == ["a", "b"]
        assert all(r.ok for r in records)
        assert all(isinstance(r, HookRecord) for r in records)

    def test_payload_defaults_to_empty_dict(self):
        bus = EventBus()
        rec = RecordingHook()
        bus.register(LoopEvent.LOOP_START, rec, name="rec")

        bus.emit(LoopEvent.LOOP_START)

        assert rec.contexts[0].payload == {}

    def test_payload_is_copied_not_aliased(self):
        """调用方事后改自己的 dict 不该篡改钩子已收到的上下文。"""
        bus = EventBus()
        rec = RecordingHook()
        bus.register(LoopEvent.PLAN, rec, name="rec")

        payload = {"plan": "原始"}
        bus.emit(LoopEvent.PLAN, payload)
        payload["plan"] = "被改了"

        assert rec.contexts[0].payload == {"plan": "原始"}

    def test_sequence_increases_across_emits(self):
        bus = EventBus()
        rec = RecordingHook()
        bus.register(LoopEvent.PLAN, rec, name="rec")
        bus.register(LoopEvent.DECISION, rec, name="rec2")

        bus.emit(LoopEvent.PLAN)
        bus.emit(LoopEvent.DECISION)

        seqs = [c.sequence for c in rec.contexts]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 2

    def test_only_matching_event_handlers_fire(self):
        bus = EventBus()
        plan_rec, decision_rec = RecordingHook(), RecordingHook()
        bus.register(LoopEvent.PLAN, plan_rec, name="p")
        bus.register(LoopEvent.DECISION, decision_rec, name="d")

        bus.emit(LoopEvent.PLAN)

        assert plan_rec.events == [LoopEvent.PLAN]
        assert decision_rec.events == []


# ─────────────────────────────────────────────────────────────────────────────
# 执行顺序
# ─────────────────────────────────────────────────────────────────────────────

class TestOrdering:
    def test_lower_priority_runs_first(self):
        bus = EventBus()
        calls: list[str] = []
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append("late"), name="late", priority=200)
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append("early"), name="early", priority=1)

        bus.emit(LoopEvent.PLAN)

        assert calls == ["early", "late"]

    def test_same_priority_keeps_registration_order(self):
        bus = EventBus()
        calls: list[str] = []
        for tag in ("first", "second", "third"):
            bus.register(
                LoopEvent.PLAN,
                lambda ctx, t=tag: calls.append(t),
                name=tag,
                priority=50,
            )

        bus.emit(LoopEvent.PLAN)

        assert calls == ["first", "second", "third"]

    def test_handler_names_reflects_execution_order(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="b", priority=10)
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="a", priority=5)

        assert bus.handler_names(LoopEvent.PLAN) == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────────────
# 异常隔离（核心可靠性要求）
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureIsolation:
    def test_failing_handler_does_not_stop_others(self):
        bus = EventBus()
        calls: list[str] = []

        def boom(ctx):
            raise RuntimeError("钩子内部炸了")

        bus.register(LoopEvent.PLAN, boom, name="boom", priority=1)
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append("still ran"), name="ok", priority=2)

        records = bus.emit(LoopEvent.PLAN)

        assert calls == ["still ran"], "前一个钩子异常不得阻断后续钩子"
        assert [r.ok for r in records] == [False, True]

    def test_emit_itself_does_not_raise(self):
        """监听者故障绝不外泄到主业务流程。"""
        bus = EventBus()
        bus.register(LoopEvent.ERROR, lambda ctx: 1 / 0, name="div0")

        records = bus.emit(LoopEvent.ERROR)  # 不应抛异常

        assert records[0].ok is False

    def test_error_message_records_type_and_text(self):
        bus = EventBus()

        def boom(ctx):
            raise ValueError("具体原因")

        bus.register(LoopEvent.PLAN, boom, name="boom")

        record = bus.emit(LoopEvent.PLAN)[0]

        assert record.error is not None
        assert "ValueError" in record.error
        assert "具体原因" in record.error

    def test_failures_are_visible_not_swallowed(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: 1 / 0, name="bad")
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="good")

        bus.emit(LoopEvent.PLAN)

        failures = bus.failures()
        assert len(failures) == 1
        assert failures[0].handler_name == "bad"
        assert bus.stats()["failed"] == 1
        assert bus.stats()["ok"] == 1

    def test_failing_handler_keeps_firing_on_later_emits(self):
        """失败不等于自动摘除；是否摘除由调用方依据审计记录决定。"""
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: 1 / 0, name="bad")

        bus.emit(LoopEvent.PLAN)
        bus.emit(LoopEvent.PLAN)

        assert len(bus.failures()) == 2


# ─────────────────────────────────────────────────────────────────────────────
# once 语义
# ─────────────────────────────────────────────────────────────────────────────

class TestOnce:
    def test_once_handler_fires_exactly_once(self):
        bus = EventBus()
        calls: list[int] = []
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append(1), name="one", once=True)

        bus.emit(LoopEvent.PLAN)
        bus.emit(LoopEvent.PLAN)
        bus.emit(LoopEvent.PLAN)

        assert calls == [1]

    def test_once_handler_removed_after_firing(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="one", once=True)

        bus.emit(LoopEvent.PLAN)

        assert bus.handler_names(LoopEvent.PLAN) == []

    def test_once_removal_does_not_disturb_persistent_handlers(self):
        bus = EventBus()
        calls: list[str] = []
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append("once"), name="once", once=True)
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append("always"), name="always")

        bus.emit(LoopEvent.PLAN)
        bus.emit(LoopEvent.PLAN)

        assert calls == ["once", "always", "always"]
        assert bus.handler_names(LoopEvent.PLAN) == ["always"]

    def test_failing_once_handler_is_still_removed(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: 1 / 0, name="bad_once", once=True)

        bus.emit(LoopEvent.PLAN)
        bus.emit(LoopEvent.PLAN)

        assert bus.handler_names(LoopEvent.PLAN) == []
        assert len(bus.failures()) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 注销
# ─────────────────────────────────────────────────────────────────────────────

class TestUnregister:
    def test_unregister_removes_handler(self):
        bus = EventBus()
        calls: list[int] = []
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append(1), name="h")

        assert bus.unregister(LoopEvent.PLAN, "h") is True
        bus.emit(LoopEvent.PLAN)

        assert calls == []

    def test_unregister_unknown_returns_false(self):
        bus = EventBus()
        assert bus.unregister(LoopEvent.PLAN, "nobody") is False

    def test_unregister_leaves_siblings_intact(self):
        bus = EventBus()
        calls: list[str] = []
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append("a"), name="a")
        bus.register(LoopEvent.PLAN, lambda ctx: calls.append("b"), name="b")

        bus.unregister(LoopEvent.PLAN, "a")
        bus.emit(LoopEvent.PLAN)

        assert calls == ["b"]

    def test_event_key_dropped_when_last_handler_removed(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="a")
        bus.unregister(LoopEvent.PLAN, "a")

        assert bus.registered_events() == []


# ─────────────────────────────────────────────────────────────────────────────
# 审计
# ─────────────────────────────────────────────────────────────────────────────

class TestAudit:
    def test_events_seen_collapses_consecutive_duplicates(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="a")
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="b")
        bus.register(LoopEvent.DECISION, lambda ctx: None, name="c")

        bus.emit(LoopEvent.PLAN)      # 两个钩子 → 两条记录、同一事件
        bus.emit(LoopEvent.DECISION)

        assert bus.events_seen() == [LoopEvent.PLAN, LoopEvent.DECISION]

    def test_stats_counts_handlers_and_invocations(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="a")
        bus.register(LoopEvent.DECISION, lambda ctx: None, name="b")

        bus.emit(LoopEvent.PLAN)
        bus.emit(LoopEvent.DECISION)

        stats = bus.stats()
        assert stats["handlers"] == 2
        assert stats["events_registered"] == 2
        assert stats["invocations"] == 2
        assert stats["failed"] == 0

    def test_reset_history_keeps_registrations(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="a")
        bus.emit(LoopEvent.PLAN)

        bus.reset_history()

        assert bus.history == []
        assert bus.handler_names(LoopEvent.PLAN) == ["a"]

    def test_record_to_dict_exposes_audit_fields(self):
        bus = EventBus()
        bus.register(LoopEvent.PLAN, lambda ctx: None, name="a")

        d = bus.emit(LoopEvent.PLAN)[0].to_dict()

        assert set(d) == {"event", "handler_name", "sequence", "ok", "error"}
        assert d["event"] == LoopEvent.PLAN
        assert d["ok"] is True

    def test_context_to_dict_exposes_fields(self):
        ctx = HookContext(event=LoopEvent.PLAN, payload={"k": 1}, sequence=3)
        assert ctx.to_dict() == {"event": "plan", "payload": {"k": 1}, "sequence": 3}


# ─────────────────────────────────────────────────────────────────────────────
# RecordingHook 辅助类
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordingHook:
    def test_collects_multiple_events_in_order(self):
        bus = EventBus()
        rec = RecordingHook()
        for event in (LoopEvent.PLAN, LoopEvent.OBSERVATION, LoopEvent.REFLECTION):
            bus.register(event, rec, name=f"rec_{event}")

        bus.emit(LoopEvent.PLAN, {"i": 1})
        bus.emit(LoopEvent.OBSERVATION, {"i": 2})
        bus.emit(LoopEvent.REFLECTION, {"i": 3})

        assert rec.events == [LoopEvent.PLAN, LoopEvent.OBSERVATION, LoopEvent.REFLECTION]

    def test_payloads_for_filters_by_event(self):
        bus = EventBus()
        rec = RecordingHook()
        bus.register(LoopEvent.PLAN, rec, name="p")
        bus.register(LoopEvent.DECISION, rec, name="d")

        bus.emit(LoopEvent.PLAN, {"n": 1})
        bus.emit(LoopEvent.DECISION, {"n": 2})
        bus.emit(LoopEvent.PLAN, {"n": 3})

        assert rec.payloads_for(LoopEvent.PLAN) == [{"n": 1}, {"n": 3}]
        assert rec.payloads_for(LoopEvent.DECISION) == [{"n": 2}]
