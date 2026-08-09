"""
心跳 / 定时器（Heartbeat & Timer）
==================================
说明书要求「心跳/定时、事件钩子、目标循环必须是项目内可测试的最小可靠实现，
不是文档或假接口」。本模块是「心跳/定时」这一项的实现。

为什么要可注入时钟
------------------
定时器最容易写成「靠 time.sleep 等一会儿看看有没有触发」的测试 —— 那种测试
既慢又随机失败，等于没测。本模块把取时函数抽成 ``clock`` 参数，测试可注入一个
手动推进的假时钟，从而**确定性地**断言「第 N 秒该触发第几次」。
:meth:`HeartbeatScheduler.run_for` 提供真实挂钟运行入口，供生产使用。

可靠性纪律
----------
1. **一个定时器的回调抛异常，不得影响其他定时器**：异常被捕获并写入
   :class:`HeartbeatRecord` 的 ``error``，调度器继续跑完本轮其余定时器。
2. **异常不得静默**：失败记录进 ``history`` 且 ``stats()['failed']`` 计数，
   调用方可断言。吞掉异常等于让故障隐身。
3. **不做补偿式连发**：若因阻塞错过了多个周期，只触发一次，并把错过的周期数
   记进 ``skipped_beats``。连发会在恢复瞬间产生风暴，且掩盖了「曾经卡住」这一事实。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 取时函数。默认单调时钟，避免系统时间被改动导致定时器错乱。
Clock = Callable[[], float]

HeartbeatCallback = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class HeartbeatRecord:
    """单次心跳触发的审计记录。"""

    name: str
    fired_at: float
    beat_index: int          # 该定时器的第几次触发（从 1 开始）
    ok: bool
    error: str | None = None
    skipped_beats: int = 0   # 因阻塞错过的周期数（0 表示准时）

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fired_at": round(self.fired_at, 6),
            "beat_index": self.beat_index,
            "ok": self.ok,
            "error": self.error,
            "skipped_beats": self.skipped_beats,
        }


@dataclass
class Heartbeat:
    """一个命名定时器。``interval_s`` 秒触发一次 ``callback``。"""

    name: str
    interval_s: float
    callback: HeartbeatCallback
    last_fired_at: float | None = None
    beats: int = 0
    failures: int = 0

    def due(self, now: float) -> bool:
        """当前时刻是否应触发。首次调用（从未触发过）立即到期。"""
        if self.last_fired_at is None:
            return True
        return (now - self.last_fired_at) >= self.interval_s

    def fire(self, now: float) -> HeartbeatRecord:
        """
        触发一次。回调异常被捕获并记录，绝不外泄到调度循环。
        """
        skipped = 0
        if self.last_fired_at is not None and self.interval_s > 0:
            elapsed = now - self.last_fired_at
            # 错过的完整周期数（超出本次应触发的那一个之外的部分）
            skipped = max(0, int(elapsed // self.interval_s) - 1)

        self.beats += 1
        self.last_fired_at = now

        try:
            self.callback()
        except Exception as exc:  # noqa: BLE001 — 单个定时器故障不得拖垮调度器
            self.failures += 1
            logger.warning("[Heartbeat] %s 回调失败: %s", self.name, exc)
            return HeartbeatRecord(
                name=self.name, fired_at=now, beat_index=self.beats,
                ok=False, error=f"{type(exc).__name__}: {exc}",
                skipped_beats=skipped,
            )

        return HeartbeatRecord(
            name=self.name, fired_at=now, beat_index=self.beats,
            ok=True, error=None, skipped_beats=skipped,
        )


class HeartbeatScheduler:
    """
    最小可靠心跳调度器。

    用法（确定性测试）::

        t = {"now": 0.0}
        sched = HeartbeatScheduler(clock=lambda: t["now"])
        sched.register("tick", interval_s=5.0, callback=my_fn)
        sched.poll()          # 首次立即触发
        t["now"] = 5.0
        sched.poll()          # 第 2 次触发

    用法（生产挂钟）::

        sched.run_for(duration_s=30.0, sleep_s=0.5)
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or time.monotonic
        self._timers: dict[str, Heartbeat] = {}
        self.history: list[HeartbeatRecord] = []

    # ── 注册 ────────────────────────────────────────────────────────────────

    def register(
        self, name: str, *, interval_s: float, callback: HeartbeatCallback,
    ) -> Heartbeat:
        """
        注册定时器。

        Raises
        ------
        ValueError
            名称重复或 interval_s <= 0。两者都是调用方编码错误，必须立即暴露。
        """
        if not str(name).strip():
            raise ValueError("定时器名称不能为空")
        if name in self._timers:
            raise ValueError(f"定时器重复注册: {name}")
        if interval_s <= 0:
            raise ValueError(f"interval_s 必须为正，收到 {interval_s}")
        timer = Heartbeat(name=name, interval_s=float(interval_s), callback=callback)
        self._timers[name] = timer
        return timer

    def unregister(self, name: str) -> bool:
        """注销定时器。返回是否确实存在过。"""
        return self._timers.pop(name, None) is not None

    def has(self, name: str) -> bool:
        return name in self._timers

    def timer_names(self) -> list[str]:
        return sorted(self._timers)

    # ── 轮询 ────────────────────────────────────────────────────────────────

    def poll(self, now: float | None = None) -> list[HeartbeatRecord]:
        """
        触发当前所有到期的定时器，返回本轮触发记录。

        按名称排序遍历，保证同一时刻多个定时器的触发顺序确定可复现。
        """
        moment = self.clock() if now is None else float(now)
        fired: list[HeartbeatRecord] = []
        for name in sorted(self._timers):
            timer = self._timers[name]
            if timer.due(moment):
                record = timer.fire(moment)
                fired.append(record)
                self.history.append(record)
        return fired

    def run_for(self, duration_s: float, *, sleep_s: float = 0.1) -> list[HeartbeatRecord]:
        """
        真实挂钟运行 ``duration_s`` 秒，每 ``sleep_s`` 秒轮询一次。

        供生产使用；测试请用注入假时钟 + :meth:`poll`，不要靠真实等待。
        """
        if duration_s < 0:
            raise ValueError(f"duration_s 不能为负，收到 {duration_s}")
        if sleep_s <= 0:
            raise ValueError(f"sleep_s 必须为正，收到 {sleep_s}")

        started = self.clock()
        fired: list[HeartbeatRecord] = []
        while (self.clock() - started) < duration_s:
            fired.extend(self.poll())
            time.sleep(sleep_s)
        return fired

    # ── 审计 ────────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        total = len(self.history)
        ok_n = sum(1 for r in self.history if r.ok)
        return {
            "timers": len(self._timers),
            "total_beats": total,
            "ok": ok_n,
            "failed": total - ok_n,
            "skipped_beats": sum(r.skipped_beats for r in self.history),
            "by_timer": {
                name: {"beats": t.beats, "failures": t.failures}
                for name, t in sorted(self._timers.items())
            },
        }

    def reset_history(self) -> None:
        self.history.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 便捷构造：手动时钟（供测试与演示）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ManualClock:
    """
    可手动推进的时钟。让定时器测试确定性化，无需 time.sleep。

    用法::

        clk = ManualClock()
        sched = HeartbeatScheduler(clock=clk)
        sched.poll()
        clk.advance(5.0)
        sched.poll()
    """

    now: float = 0.0
    _history: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError(f"时间不能倒流，收到 {seconds}")
        self.now += float(seconds)
        self._history.append(self.now)
        return self.now
