"""最小 Echo Agent：展示规划、工具、观察、反思、决策与持久化记忆。"""
from __future__ import annotations

from pathlib import Path

from agent_platform.core.agent_loop import AgentLoop, KeywordReflector
from agent_platform.core.loop_memory import SQLiteLoopMemory
from agent_platform.core.task_state import TaskStateNamespace
from agent_platform.core.tools import RegisteredTool, ToolRegistry


def run_echo(message: str, *, task_id: str, state_root: Path | str) -> dict:
    namespace = TaskStateNamespace(state_root)
    tools = ToolRegistry()
    tools.register(
        RegisteredTool(
            name="echo",
            description="原样返回用户输入",
            handler=lambda text: f"ECHO:{text}",
        )
    )
    memory = SQLiteLoopMemory(namespace.loop_memory_path(task_id))
    loop = AgentLoop(
        tools=tools,
        provider=None,
        memory=memory,
        reflector=KeywordReflector(required=(f"ECHO:{message}",)),
        tool_plan=lambda _goal, iteration, _observations: (
            [("echo", {"text": message})] if iteration == 1 else []
        ),
        max_iterations=2,
    )
    result = loop.run(f"回显消息：{message}", session_id=task_id)
    payload = result.to_dict()
    payload["memory_path"] = str(namespace.loop_memory_path(task_id))
    namespace.save_state(task_id, payload)
    return payload


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[2]
    output = run_echo("hello", task_id="echo-demo", state_root=project_root / "data" / "tasks")
    print(output["answer"])
