"""长任务状态目录隔离与安全路径管理。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TaskStateNamespace:
    """在固定根目录下为每个任务提供独立、不可穿越的状态目录。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        if not _SAFE_TASK_ID.fullmatch(str(task_id)) or task_id in {".", ".."}:
            raise ValueError(f"非法 task_id: {task_id!r}")
        target = (self.root / task_id).resolve()
        if target.parent != self.root:
            raise ValueError(f"任务目录越界: {task_id!r}")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def loop_memory_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "loop_memory.sqlite3"

    def state_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "state.json"

    def save_state(self, task_id: str, state: dict[str, Any]) -> Path:
        path = self.state_path(task_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def load_state(self, task_id: str) -> dict[str, Any] | None:
        path = self.state_path(task_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"任务状态必须是 JSON object: {path}")
        return payload
