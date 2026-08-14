"""长任务目录隔离和 Echo 五要素闭环测试。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent_platform.core.loop_memory import MemoryKind, SQLiteLoopMemory
from agent_platform.core.task_state import TaskStateNamespace


def _load_echo_module():
    path = Path(__file__).parents[1] / "examples" / "echo_agent" / "run_demo.py"
    spec = importlib.util.spec_from_file_location("echo_agent_demo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parallel_tasks_use_separate_directories(tmp_path) -> None:
    namespace = TaskStateNamespace(tmp_path / "tasks")
    namespace.save_state("task-a", {"value": "A"})
    namespace.save_state("task-b", {"value": "B"})

    assert namespace.task_dir("task-a") != namespace.task_dir("task-b")
    assert namespace.load_state("task-a") == {"value": "A"}
    assert namespace.load_state("task-b") == {"value": "B"}


@pytest.mark.parametrize("task_id", ["../escape", "..", ".", "a/b", "a\\b", ""])
def test_task_id_rejects_path_traversal(tmp_path, task_id: str) -> None:
    with pytest.raises(ValueError, match="非法 task_id"):
        TaskStateNamespace(tmp_path / "tasks").task_dir(task_id)


def test_echo_demo_persists_all_five_loop_elements(tmp_path) -> None:
    module = _load_echo_module()
    result = module.run_echo("hello", task_id="echo-1", state_root=tmp_path / "tasks")

    assert result["goal_met"] is True
    memory = SQLiteLoopMemory(result["memory_path"])
    kinds = {record.kind for record in memory.records("echo-1")}
    assert {
        MemoryKind.PLAN,
        MemoryKind.TOOL_CALL,
        MemoryKind.OBSERVATION,
        MemoryKind.REFLECTION,
        MemoryKind.DECISION,
    } <= kinds
    assert TaskStateNamespace(tmp_path / "tasks").load_state("echo-1")["goal_met"] is True
