"""
心跳 / 定时器测试
=================
验收目标（说明书 二.1）：心跳/定时必须是项目内**可测试的最小可靠实现**。

本文件刻意不使用 ``time.sleep`` 来等待触发（除 :class:`TestRunForRealClock`
中一个 10 毫秒级的冒烟用例），而是注入手动时钟，确定性断言
「第 N 秒应该触发第几次」。靠真实等待的定时器测试既慢又随机失败，等于没测。
"""
from __future__ import annotations

import pytest

from agent_platform.core.scheduler import (
    Heartbeat,
    HeartbeatRecord,
    HeartbeatScheduler,
    ManualClock,
)


# ─────────────────────────────────────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────────────────────────────────────

class Counter:
    """可调用计数器，记录被触发的次数。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class Boom:
    """总是抛异常的回调，用于验证故障隔离。"""

    def __init__(self, message: str = "定时器内部故障") -> None:
        self.message = message
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        raise RuntimeError(self.message)


def _sched() -> tuple[HeartbeatScheduler, ManualClock]:
    clock = ManualClock()
    return HeartbeatScheduler(clock=clock), clock


# ─────────────────────────────────────────────────────────────────────────────
# 注册校验
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistration:
    def test_register_returns_timer(self) -> None:
        sched, _ = _sched()
        timer = sched.register("tick", interval_s=5.0, callback=Counter())
        assert isinstance(timer, Heartbeat)
        assert timer.name == "tick"
        assert timer.interval_s == 5.0
        assert timer.beats == 0

    def test_empty_name_rejected(self) -> None:
        sched, _ = _sched()
        with pytest.raises(ValueError, match="名称不能为空"):
            sched.register("   ", interval_s=1.0, callback=Counter())

    def test_duplicate_name_rejected(self) -> None:
        """重复注册是调用方编码错误，必须立即暴露而不是静默覆盖。"""
        sched, _ = _sched()
        sched.register("tick", interval_s=1.0, callback=Counter())
        with pytest.raises(ValueError, match="重复注册"):
            sched.register("tick", interval_s=2.0, callback=Counter())

    @pytest.mark.parametrize("bad", [0.0, -1.0, -0.001])
    def test_non_positive_interval_rejected(self, bad: float) -> None:
        sched, _ = _sched()
        with pytest.raises(ValueError, match="interval_s"):
            sched.register("tick", interval_s=bad, callback=Counter())

    def test_has_and_names_and_unregister(self) -> None:
        sched, _ = _sched()
        sched.register("b", interval_s=1.0, callback=Counter())
        sched.register("a", interval_s=1.0, callback=Counter())
        assert sched.has("a") is True
        assert sched.timer_names() == ["a", "b"]

        assert sched.unregister("a") is True
        assert sched.has("a") is False
        assert sched.timer_names() == ["b"]

    def test_unregister_missing_returns_false(self) -> None:
        sched, _ = _sched()
        assert sched.unregister("nope") is False

    def test_unregistered_timer_stops_firing(self) -> None:
        sched, clock = _sched()
        counter = Counter()
        sched.register("tick", interval_s=5.0, callback=counter)
        sched.poll()
        assert counter.calls == 1

        sched.unregister("tick")
        clock.advance(100.0)
        assert sched.poll() == []
        assert counter.calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# 触发时序
# ─────────────────────────────────────────────────────────────────────────────

class TestFiringSchedule:
    def test_first_poll_fires_immediately(self) -> None:
        """从未触发过的定时器立即到期，避免启动后要空等一个周期。"""
        sched, _ = _sched()
        counter = Counter()
        sched.register("tick", interval_s=5.0, callback=counter)

        fired = sched.poll()
        assert [r.name for r in fired] == ["tick"]
        assert fired[0].beat_index == 1
        assert counter.calls == 1

    def test_does_not_refire_before_interval(self) -> None:
        sched, clock = _sched()
        counter = Counter()
        sched.register("tick", interval_s=5.0, callback=counter)
        sched.poll()

        clock.advance(4.999)
        assert sched.poll() == []
        assert counter.calls == 1

    def test_fires_exactly_at_interval_boundary(self) -> None:
        sched, clock = _sched()
        counter = Counter()
        sched.register("tick", interval_s=5.0, callback=counter)
        sched.poll()

        clock.advance(5.0)
        fired = sched.poll()
        assert len(fired) == 1
        assert fired[0].beat_index == 2
        assert counter.calls == 2

    def test_beat_count_over_many_intervals(self) -> None:
        """走 5 个周期应恰好触发 6 次（含启动那次），不多不少。"""
        sched, clock = _sched()
        counter = Counter()
        sched.register("tick", interval_s=2.0, callback=counter)

        sched.poll()
        for _ in range(5):
            clock.advance(2.0)
            sched.poll()

        assert counter.calls == 6
        assert sched.stats()["by_timer"]["tick"]["beats"] == 6

    def test_independent_intervals(self) -> None:
        """快慢两个定时器互不干扰，各按自己的周期走。"""
        sched, clock = _sched()
        fast, slow = Counter(), Counter()
        sched.register("fast", interval_s=1.0, callback=fast)
        sched.register("slow", interval_s=10.0, callback=slow)

        sched.poll()                      # 两个都首发
        for _ in range(10):
            clock.advance(1.0)
            sched.poll()

        assert fast.calls == 11            # 首发 + 10 次
        assert slow.calls == 2             # 首发 + t=10 一次

    def test_fire_order_is_deterministic_by_name(self) -> None:
        """同一时刻多个定时器按名称排序触发，保证可复现。"""
        sched, _ = _sched()
        for name in ("zulu", "alpha", "mike"):
            sched.register(name, interval_s=1.0, callback=Counter())

        fired = sched.poll()
        assert [r.name for r in fired] == ["alpha", "mike", "zulu"]

    def test_explicit_now_overrides_clock(self) -> None:
        sched, clock = _sched()
        counter = Counter()
        sched.register("tick", interval_s=5.0, callback=counter)
        sched.poll(now=0.0)

        # 时钟没动，但显式传入已越过周期的时刻
        fired = sched.poll(now=5.0)
        assert len(fired) == 1
        assert fired[0].fired_at == 5.0
        assert clock.now == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 阻塞补偿纪律：只补一次，且把错过的周期数暴露出来
# ─────────────────────────────────────────────────────────────────────────────

class TestSkippedBeats:
    def test_blocked_gap_fires_once_not_burst(self) -> None:
        """卡住 16 秒（周期 5 秒）只触发 1 次，不得连发 3 次造成风暴。"""
        sched, clock = _sched()
        counter = Counter()
        sched.register("tick", interval_s=5.0, callback=counter)
        sched.poll()
        assert counter.calls == 1

        clock.advance(16.0)
        fired = sched.poll()

        assert len(fired) == 1
        assert counter.calls == 2

    def test_skipped_beats_recorded(self) -> None:
        """错过的周期数必须可见 —— 掩盖「曾经卡住」等于让故障隐身。"""
        sched, clock = _sched()
        sched.register("tick", interval_s=5.0, callback=Counter())
        sched.poll()

        clock.advance(16.0)
        fired = sched.poll()
        assert fired[0].skipped_beats == 2

    def test_on_time_beat_reports_zero_skipped(self) -> None:
        sched, clock = _sched()
        sched.register("tick", interval_s=5.0, callback=Counter())
        first = sched.poll()[0]
        clock.advance(5.0)
        second = sched.poll()[0]

        assert first.skipped_beats == 0
        assert second.skipped_beats == 0

    def test_stats_aggregates_skipped(self) -> None:
        sched, clock = _sched()
        sched.register("tick", interval_s=1.0, callback=Counter())
        sched.poll()
        clock.advance(4.0)
        sched.poll()

        assert sched.stats()["skipped_beats"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# 故障隔离：一个回调炸了不能拖垮调度器
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureIsolation:
    def test_failing_callback_does_not_block_others(self) -> None:
        sched, _ = _sched()
        boom = Boom()
        survivor = Counter()
        # 名称保证 boom 先跑（按名称排序）
        sched.register("aaa_boom", interval_s=1.0, callback=boom)
        sched.register("zzz_ok", interval_s=1.0, callback=survivor)

        fired = sched.poll()

        assert boom.calls == 1
        assert survivor.calls == 1, "前一个定时器抛异常后，后续定时器仍必须被触发"
        assert [r.ok for r in fired] == [False, True]

    def test_failure_is_recorded_not_swallowed(self) -> None:
        sched, _ = _sched()
        sched.register("tick", interval_s=1.0, callback=Boom("磁盘满了"))

        record = sched.poll()[0]
        assert record.ok is False
        assert record.error is not None
        assert "RuntimeError" in record.error
        assert "磁盘满了" in record.error

    def test_failure_counted_in_stats(self) -> None:
        sched, clock = _sched()
        sched.register("bad", interval_s=1.0, callback=Boom())
        sched.register("good", interval_s=1.0, callback=Counter())
        sched.poll()
        clock.advance(1.0)
        sched.poll()

        stats = sched.stats()
        assert stats["total_beats"] == 4
        assert stats["ok"] == 2
        assert stats["failed"] == 2
        assert stats["by_timer"]["bad"]["failures"] == 2
        assert stats["by_timer"]["good"]["failures"] == 0

    def test_failing_timer_keeps_its_schedule(self) -> None:
        """失败不影响后续排程：下个周期照常再试。"""
        sched, clock = _sched()
        boom = Boom()
        sched.register("tick", interval_s=5.0, callback=boom)
        sched.poll()
        clock.advance(5.0)
        sched.poll()
        clock.advance(5.0)
        sched.poll()

        assert boom.calls == 3

    def test_poll_never_raises_from_callback(self) -> None:
        sched, _ = _sched()
        sched.register("tick", interval_s=1.0, callback=Boom())
        sched.poll()  # 不抛即通过


# ─────────────────────────────────────────────────────────────────────────────
# 审计记录
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryAndRecords:
    def test_history_accumulates_all_beats(self) -> None:
        sched, clock = _sched()
        sched.register("tick", interval_s=1.0, callback=Counter())
        for _ in range(3):
            sched.poll()
            clock.advance(1.0)

        assert len(sched.history) == 3
        assert [r.beat_index for r in sched.history] == [1, 2, 3]

    def test_reset_history_keeps_timers(self) -> None:
        sched, _ = _sched()
        sched.register("tick", interval_s=1.0, callback=Counter())
        sched.poll()
        sched.reset_history()

        assert sched.history == []
        assert sched.has("tick") is True

    def test_record_to_dict_shape(self) -> None:
        record = HeartbeatRecord(
            name="tick", fired_at=1.2345678, beat_index=2,
            ok=False, error="RuntimeError: x", skipped_beats=1,
        )
        assert record.to_dict() == {
            "name": "tick",
            "fired_at": 1.234568,
            "beat_index": 2,
            "ok": False,
            "error": "RuntimeError: x",
            "skipped_beats": 1,
        }

    def test_record_is_immutable(self) -> None:
        record = HeartbeatRecord(name="t", fired_at=0.0, beat_index=1, ok=True)
        with pytest.raises(Exception):
            record.name = "changed"  # type: ignore[misc]

    def test_stats_on_empty_scheduler(self) -> None:
        sched, _ = _sched()
        assert sched.stats() == {
            "timers": 0, "total_beats": 0, "ok": 0, "failed": 0,
            "skipped_beats": 0, "by_timer": {},
        }


# ─────────────────────────────────────────────────────────────────────────────
# 手动时钟
# ─────────────────────────────────────────────────────────────────────────────

class TestManualClock:
    def test_starts_at_zero_and_advances(self) -> None:
        clock = ManualClock()
        assert clock() == 0.0
        clock.advance(2.5)
        assert clock() == 2.5
        clock.advance(2.5)
        assert clock() == 5.0

    def test_time_cannot_flow_backwards(self) -> None:
        clock = ManualClock()
        with pytest.raises(ValueError, match="时间不能倒流"):
            clock.advance(-1.0)

    def test_advance_returns_new_now(self) -> None:
        clock = ManualClock(now=10.0)
        assert clock.advance(1.0) == 11.0


# ─────────────────────────────────────────────────────────────────────────────
# 真实挂钟入口（生产路径冒烟，毫秒级）
# ─────────────────────────────────────────────────────────────────────────────

class TestRunForRealClock:
    def test_zero_duration_does_not_fire(self) -> None:
        sched = HeartbeatScheduler()
        counter = Counter()
        sched.register("tick", interval_s=1.0, callback=counter)

        assert sched.run_for(0.0) == []
        assert counter.calls == 0

    def test_run_for_polls_with_real_clock(self) -> None:
        """验证 run_for 真的会轮询（间隔设得远大于运行时长，只应首发一次）。"""
        sched = HeartbeatScheduler()
        counter = Counter()
        sched.register("tick", interval_s=100.0, callback=counter)

        fired = sched.run_for(0.01, sleep_s=0.001)

        assert counter.calls == 1
        assert [r.beat_index for r in fired] == [1]

    def test_negative_duration_rejected(self) -> None:
        sched = HeartbeatScheduler()
        with pytest.raises(ValueError, match="duration_s"):
            sched.run_for(-1.0)

    @pytest.mark.parametrize("bad", [0.0, -0.5])
    def test_non_positive_sleep_rejected(self, bad: float) -> None:
        sched = HeartbeatScheduler()
        with pytest.raises(ValueError, match="sleep_s"):
            sched.run_for(1.0, sleep_s=bad)

    def test_default_clock_is_monotonic(self) -> None:
        import time as _time

        assert HeartbeatScheduler().clock is _time.monotonic
