#!/usr/bin/env python
"""
Scripts/run_agent_loop_demo.py
===============================
Loop 五要素 + 目标循环 + 心跳 + 事件钩子 + 持久化记忆 的**可运行验收入口**。

为什么需要这个脚本
------------------
说明书要求「心跳/定时、事件钩子、目标循环必须是项目内最小可靠可测实现，不是
文档或假接口」，并且「不得留下没有任何主链调用的工具文件」。单元测试证明了模块
本身正确，但那是测试代码在调用；本脚本让这四个模块被一条**真实主链**驱动一次：

    真实 MCP 注册表（offline=True 硬阻断） → ToolRegistry → AgentLoop
      → 五要素逐轮产出 → SQLite 持久化记忆 → 事件总线审计 → 心跳计数

用法
----
    python Scripts/run_agent_loop_demo.py                    # 默认 DEMO001，离线零网络
    python Scripts/run_agent_loop_demo.py --symbol DEMO002
    python Scripts/run_agent_loop_demo.py --keep-db          # 保留 SQLite 便于人工核查

退出码
------
0 = 全部断言通过；1 = 有任一断言失败（脚本会逐条打印失败原因，不吞错）。

纪律
----
* 全程 ``offline=True``，**零网络调用**，脚本末尾对此做硬断言。
* 所有数据来自内置样例，输出明确标注 ``offline_sample``，绝不冒充实时行情。
* 目标未达成时如实打印「未达成」与缺口，不美化结论。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

# 强制 UTF-8 输出（Windows GBK 终端下中文/符号会抛 UnicodeEncodeError）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 将 src/ 加入模块搜索路径（脚本从项目根目录执行）
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent_platform.core.agent_loop import (  # noqa: E402
    AgentLoop,
    KeywordReflector,
    RuleBasedPlanner,
    StopReason,
)
from agent_platform.core.event_hooks import EventBus, HookContext, LoopEvent  # noqa: E402
from agent_platform.core.loop_memory import MemoryKind, SQLiteLoopMemory  # noqa: E402
from agent_platform.core.scheduler import HeartbeatScheduler, ManualClock  # noqa: E402
from agent_platform.core.tools import RegisteredTool, ToolRegistry  # noqa: E402
from agent_platform.mcp import build_default_registry  # noqa: E402

DISCLAIMER = "仅供研究参考，不构成投资建议。"

#: 目标达成的必需证据关键词。第 1 轮拿前两项，第 2 轮拿第三项 —— 证明目标循环
#: 真的跑了多轮，而不是一轮就撞上「碰巧齐了」。
REQUIRED_EVIDENCE = ("pe_ttm", "industry_name", "regime")


def build_tool_registry(mcp) -> ToolRegistry:
    """
    把**真实 MCP 离线工具**包装进 Loop 的 ToolRegistry。

    这里不造任何假数据：三个 handler 都实打实走 ``mcp.call``，返回完整信封的
    JSON 文本（含 source / updated_at / ok），因此 Loop 的「观察」是可溯源的。
    """

    def _fundamental(symbol: str) -> str:
        env = mcp.call("get_offline_fundamental", symbol=symbol)
        return json.dumps(env, ensure_ascii=False, default=str)

    def _industry(symbol: str) -> str:
        env = mcp.call("get_offline_industry", symbol=symbol)
        return json.dumps(env, ensure_ascii=False, default=str)

    def _regime(index_code: str = "sh000001") -> str:
        env = mcp.call("get_offline_market_regime", index_code=index_code)
        return json.dumps(env, ensure_ascii=False, default=str)

    tools = ToolRegistry()
    tools.register(RegisteredTool(
        name="mcp_offline_fundamental",
        description="离线基本面（PE/PB/ROE/资产负债率），经 MCP 注册表，零网络",
        handler=_fundamental,
    ))
    tools.register(RegisteredTool(
        name="mcp_offline_industry",
        description="离线行业（行业名/景气/资金流/龙头），经 MCP 注册表，零网络",
        handler=_industry,
    ))
    tools.register(RegisteredTool(
        name="mcp_offline_market_regime",
        description="离线市场状态（Regime/指数/风险偏好），经 MCP 注册表，零网络",
        handler=_regime,
    ))
    return tools


def make_tool_plan(symbol: str):
    """
    确定性工具编排：第 1 轮取基本面 + 行业，第 2 轮取市场状态。

    刻意分两轮，这样「继续规划下一轮」这个要素有真实的触发场景可验收。
    """

    def _plan(goal: str, iteration: int, observations) -> list[tuple[str, dict]]:
        if iteration == 1:
            return [
                ("mcp_offline_fundamental", {"symbol": symbol}),
                ("mcp_offline_industry", {"symbol": symbol}),
            ]
        if iteration == 2:
            return [("mcp_offline_market_regime", {"index_code": "sh000001"})]
        return []

    return _plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Loop 五要素 / 目标循环 / 心跳 / 事件钩子 / 持久化记忆 离线验收",
    )
    parser.add_argument("--symbol", default="DEMO001", help="样例标的代码（默认 DEMO001）")
    parser.add_argument("--max-iterations", type=int, default=4, help="迭代硬上限（默认 4）")
    parser.add_argument(
        "--db",
        default="data/loop_memory_demo.sqlite3",
        help="记忆 SQLite 路径（默认 data/loop_memory_demo.sqlite3，已被 .gitignore 忽略）",
    )
    parser.add_argument("--keep-db", action="store_true", help="运行后保留 SQLite 文件")
    args = parser.parse_args()

    root = Path(__file__).parent.parent
    db_path = (root / args.db) if not Path(args.db).is_absolute() else Path(args.db)

    print("=" * 70)
    print("Loop 五要素 + 目标循环 离线验收（零网络）")
    print("=" * 70)
    print(f"标的：{args.symbol}    迭代上限：{args.max_iterations}")
    print(f"记忆库：{db_path}")
    print(f"必需证据：{', '.join(REQUIRED_EVIDENCE)}")
    print()

    # ── 1. 真实 MCP 注册表，硬离线 ──────────────────────────────────────────
    mcp = build_default_registry(offline=True)

    # ── 2. 事件钩子：把五要素实时打出来 ─────────────────────────────────────
    bus = EventBus()

    def _printer(ctx: HookContext) -> None:
        payload = ctx.payload
        if ctx.event == LoopEvent.PLAN:
            print(f"  [规划] 第{payload['iteration']}轮 {payload['plan']}")
        elif ctx.event == LoopEvent.TOOL_CALL:
            print(f"  [工具] {payload['tool']} 参数={payload['arguments']}")
        elif ctx.event == LoopEvent.OBSERVATION:
            flag = "成功" if payload["ok"] else "失败"
            print(f"  [观察] {payload['tool']} {flag} 长度={len(payload['output'])}")
        elif ctx.event == LoopEvent.REFLECTION:
            miss = payload.get("missing") or ()
            print(f"  [反思] {payload['assessment']}"
                  + (f" 缺口={list(miss)}" if miss else ""))
        elif ctx.event == LoopEvent.DECISION:
            print(f"  [决策] {payload['decision']}")
        elif ctx.event == LoopEvent.GOAL_REACHED:
            print(f"  [目标] 第{payload['iteration']}轮达成")

    bus.register(LoopEvent.PLAN, _printer, name="打印规划")
    bus.register(LoopEvent.TOOL_CALL, _printer, name="打印工具调用")
    bus.register(LoopEvent.OBSERVATION, _printer, name="打印观察")
    bus.register(LoopEvent.REFLECTION, _printer, name="打印反思")
    bus.register(LoopEvent.DECISION, _printer, name="打印决策")
    bus.register(LoopEvent.GOAL_REACHED, _printer, name="打印目标达成")

    # 静默记录钩子：注册到**全部**事件上。
    # EventBus.history 只记录「钩子被调用」这件事 —— 没有监听者的事件即使被
    # emit 过也不会留痕。若只在上面 6 个verbose 事件上挂钩子，审计就会把
    # loop_start / iteration_end / loop_end「查无记录」误报成「未触发」。
    # 首次运行确实这样误报了 3 项，这里补齐监听而不是放宽审计。
    for _event in LoopEvent.all_events():
        bus.register(_event, lambda ctx: None, name=f"记录-{_event}", priority=10)

    # ── 3. 心跳：手动时钟保证确定性（真实挂钟请用 scheduler.run_for）─────────
    clock = ManualClock()
    scheduler = HeartbeatScheduler(clock=clock)
    beats: list[int] = []
    scheduler.register("每轮巡检", interval_s=1.0, callback=lambda: beats.append(1))
    # DECISION 钩子在 scheduler.poll() 之前触发，借它把时钟推进 1 秒，
    # 于是每轮迭代恰好产生一次心跳，可确定性断言。
    bus.register(LoopEvent.DECISION, lambda ctx: clock.advance(1.0), name="推进时钟",
                 priority=200)

    # ── 4. 持久化记忆 ───────────────────────────────────────────────────────
    memory = SQLiteLoopMemory(db_path)
    session_id = f"demo-{args.symbol}"
    memory.clear(session_id)  # 重复运行时先清掉上一轮，避免计数混淆

    # ── 5. 组装并运行 ───────────────────────────────────────────────────────
    goal = f"评估 {args.symbol} 的基本面、所属行业景气与当前市场状态"
    loop = AgentLoop(
        tools=build_tool_registry(mcp),
        provider=None,                     # 离线：完全不依赖 LLM
        memory=memory,
        bus=bus,
        scheduler=scheduler,
        planner=RuleBasedPlanner(),
        reflector=KeywordReflector(required=REQUIRED_EVIDENCE),
        tool_plan=make_tool_plan(args.symbol),
        max_iterations=args.max_iterations,
    )

    print("运行中：")
    result = loop.run(goal, session_id=session_id)
    print()

    # ── 6. 逐项审计 ─────────────────────────────────────────────────────────
    print("-" * 70)
    print("审计")
    print("-" * 70)
    print(f"目标达成：{result.goal_met}    结束原因：{result.stop_reason}"
          f"    迭代数：{result.iterations}")
    print(f"Provider：{result.provider}")
    print(f"答案：{result.answer}")
    print()

    kinds = {k: len(memory.records(session_id, k)) for k in MemoryKind.all_kinds()}
    print("记忆记录数（按类型）：")
    for kind, count in kinds.items():
        print(f"  {kind:<12} {count}")
    print()

    events_seen = sorted({r.event for r in bus.history})
    print(f"事件已触发：{', '.join(events_seen)}")
    print(f"钩子失败数：{result.hook_failures}    心跳次数：{result.heartbeats}")
    print()

    stats = mcp.stats()
    print("MCP 调用统计：")
    print(f"  总调用 {stats['total']}    成功 {stats['ok']}    失败 {stats['failed']}"
          f"    离线阻断 {stats['blocked_offline']}")
    print(f"  offline 模式：{stats['offline']}")
    called = sorted(stats["by_tool"])
    print(f"  被调用工具：{', '.join(called)}")
    network_tools = [n for n in called if mcp.spec(n).requires_network]
    print(f"  其中需要网络的：{network_tools or '无'}")
    print()

    # 持久化验证：换一个实例（模拟进程重启）仍能读回
    reopened = SQLiteLoopMemory(db_path)
    persisted = reopened.records(session_id)
    print(f"重开 SQLite 读回记录数：{len(persisted)}（模拟进程重启后记忆仍在）")
    print()

    # ── 7. 硬断言 ───────────────────────────────────────────────────────────
    failures: list[str] = []

    if not result.goal_met:
        failures.append(f"目标未达成（stop_reason={result.stop_reason}，缺口={list(result.missing)}）")
    if result.stop_reason != StopReason.GOAL_MET:
        failures.append(f"结束原因应为 {StopReason.GOAL_MET}，实际 {result.stop_reason}")
    if result.iterations < 2:
        failures.append(f"目标循环应至少跑 2 轮才证明多轮能力，实际 {result.iterations} 轮")

    # 五要素必须条条落库
    for kind in (MemoryKind.PLAN, MemoryKind.TOOL_CALL, MemoryKind.OBSERVATION,
                 MemoryKind.REFLECTION, MemoryKind.DECISION):
        if kinds.get(kind, 0) <= 0:
            failures.append(f"五要素缺失：记忆中没有 {kind} 记录")
    if kinds.get(MemoryKind.GOAL, 0) != 1:
        failures.append(f"目标记录应恰好 1 条，实际 {kinds.get(MemoryKind.GOAL, 0)}")
    if kinds.get(MemoryKind.ANSWER, 0) != 1:
        failures.append(f"答案记录应恰好 1 条，实际 {kinds.get(MemoryKind.ANSWER, 0)}")

    # 事件钩子必须真的被触发
    for event in (LoopEvent.LOOP_START, LoopEvent.PLAN, LoopEvent.TOOL_CALL,
                  LoopEvent.OBSERVATION, LoopEvent.REFLECTION, LoopEvent.DECISION,
                  LoopEvent.ITERATION_END, LoopEvent.GOAL_REACHED, LoopEvent.LOOP_END):
        if event not in events_seen:
            failures.append(f"事件钩子未触发：{event}")
    if result.hook_failures != 0:
        failures.append(f"钩子失败数应为 0，实际 {result.hook_failures}")

    # 心跳必须真的跳了，且与迭代数一致
    if result.heartbeats != result.iterations:
        failures.append(f"心跳次数应等于迭代数 {result.iterations}，实际 {result.heartbeats}")
    if len(beats) != result.heartbeats:
        failures.append(f"心跳回调执行次数 {len(beats)} 与记录 {result.heartbeats} 不一致")

    # 零网络：一次网络工具都不该被调用，也不该有被阻断的尝试
    if network_tools:
        failures.append(f"离线验收却调用了需要网络的工具：{network_tools}")
    if stats["blocked_offline"] != 0:
        failures.append(f"出现 {stats['blocked_offline']} 次离线阻断，说明主链尝试过外发请求")
    if stats["failed"] != 0:
        failures.append(f"MCP 调用有 {stats['failed']} 次失败")
    if stats["offline"] is not True:
        failures.append("MCP 注册表不在 offline 模式")

    # 持久化：读回条数必须与写入一致
    expected_total = sum(kinds.values())
    if len(persisted) != expected_total:
        failures.append(f"持久化条数不符：写入 {expected_total}，重开读回 {len(persisted)}")
    if not persisted:
        failures.append("重开 SQLite 未读到任何记录，记忆并未真正持久化")

    if not args.keep_db:
        try:
            db_path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"（提示）清理 SQLite 失败，可手动删除 {db_path}：{exc}")

    print("=" * 70)
    if failures:
        print(f"验收未通过：{len(failures)} 项失败")
        for i, item in enumerate(failures, 1):
            print(f"  {i}. {item}")
        print("=" * 70)
        return 1

    print("验收通过：五要素齐备、目标循环多轮达成、心跳与钩子均已触发、"
          "记忆已持久化、零网络调用")
    print(f"数据来源：内置样例（offline_sample）。{DISCLAIMER}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
