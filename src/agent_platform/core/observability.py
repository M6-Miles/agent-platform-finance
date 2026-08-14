"""
可观测性面板（Observability Panel）
=====================================
收集 AgentHarness / LangGraph 工作流的运行指标：
  - Token 消耗（输入/输出）
  - 延迟（P50/P95）
  - 失败率与 Guardrail 触发率
  - CircuitBreaker 状态
"""
from __future__ import annotations

import time
import logging
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentCallRecord:
    """单次 Agent 调用记录。"""
    agent_name: str
    task: str
    started_at: float
    duration_s: float
    success: bool
    input_tokens: int
    output_tokens: int
    guardrail_violations: list[str]
    retries: int


@dataclass
class ObservabilityPanel:
    """
    可观测性面板：汇总所有 Agent 调用指标。
    使用方法：
        panel = ObservabilityPanel()
        with panel.record("technical_agent", task="analyze") as ctx:
            result = agent.run(task)
            ctx.set_tokens(input=500, output=200)
    """

    storage_path: Path | str | None = None
    max_records: int = 10_000
    _records: list[AgentCallRecord] = field(default_factory=list, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.storage_path is None:
            return
        self.storage_path = Path(self.storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS observability_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT NOT NULL,
                    task TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    duration_s REAL NOT NULL,
                    success INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    guardrail_violations TEXT NOT NULL DEFAULT '',
                    retries INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_observability_started_at
                ON observability_records(started_at DESC);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        if self.storage_path is None:
            raise RuntimeError("未配置可观测性持久化路径")
        connection = sqlite3.connect(self.storage_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def record_call(
        self,
        agent_name: str,
        task: str,
        duration_s: float,
        success: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        guardrail_violations: list[str] | None = None,
        retries: int = 0,
    ) -> None:
        record = AgentCallRecord(
            agent_name=agent_name,
            task=task,
            started_at=time.time(),
            duration_s=duration_s,
            success=success,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            guardrail_violations=list(guardrail_violations or []),
            retries=retries,
        )
        with self._lock:
            if self.storage_path is None:
                self._records.append(record)
                if len(self._records) > self.max_records:
                    del self._records[:-self.max_records]
                return
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO observability_records(
                        agent_name, task, started_at, duration_s, success,
                        input_tokens, output_tokens, guardrail_violations, retries
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.agent_name, record.task, record.started_at,
                        record.duration_s, int(record.success), record.input_tokens,
                        record.output_tokens, "\n".join(record.guardrail_violations),
                        record.retries,
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM observability_records
                    WHERE id NOT IN (
                        SELECT id FROM observability_records
                        ORDER BY started_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (self.max_records,),
                )

    def records(self, limit: int | None = None) -> list[AgentCallRecord]:
        """Return records oldest-to-newest for stable percentile calculations."""
        with self._lock:
            if self.storage_path is None:
                values = list(self._records)
                return values[-limit:] if limit is not None else values
            sql = """
                SELECT agent_name, task, started_at, duration_s, success,
                       input_tokens, output_tokens, guardrail_violations, retries
                FROM observability_records ORDER BY started_at DESC, id DESC
            """
            params: tuple[Any, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (limit,)
            with self._connect() as connection:
                rows = connection.execute(sql, params).fetchall()
        return [
            AgentCallRecord(
                agent_name=row["agent_name"], task=row["task"],
                started_at=float(row["started_at"]), duration_s=float(row["duration_s"]),
                success=bool(row["success"]), input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                guardrail_violations=[
                    value for value in str(row["guardrail_violations"]).splitlines() if value
                ],
                retries=int(row["retries"]),
            )
            for row in reversed(rows)
        ]

    def get_summary(self) -> dict[str, Any]:
        """返回汇总统计字典。"""
        records = self.records()
        if not records:
            return {
                "total_calls": 0,
                "success_rate_pct": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "latency_p50_s": 0.0,
                "latency_p95_s": 0.0,
                "guardrail_violation_count": 0,
                "avg_retries": 0.0,
                "per_agent": {},
                "recent_calls": [],
            }

        durations = [r.duration_s for r in records]
        durations_sorted = sorted(durations)
        n = len(durations_sorted)

        def percentile(data: list[float], p: float) -> float:
            idx = max(0, int(math.ceil(p / 100.0 * n)) - 1)
            return data[min(idx, len(data) - 1)]

        import math
        success_count = sum(1 for r in records if r.success)
        total_violations = sum(len(r.guardrail_violations) for r in records)

        # Per-agent breakdown
        per_agent: dict[str, Any] = defaultdict(lambda: {"calls": 0, "successes": 0, "total_duration": 0.0, "total_input_tokens": 0, "total_output_tokens": 0})
        for r in records:
            per_agent[r.agent_name]["calls"] += 1
            per_agent[r.agent_name]["successes"] += int(r.success)
            per_agent[r.agent_name]["total_duration"] += r.duration_s
            per_agent[r.agent_name]["total_input_tokens"] += r.input_tokens
            per_agent[r.agent_name]["total_output_tokens"] += r.output_tokens

        return {
            "total_calls": len(records),
            "success_rate_pct": round(success_count / len(records) * 100, 1),
            "total_input_tokens": sum(r.input_tokens for r in records),
            "total_output_tokens": sum(r.output_tokens for r in records),
            "latency_p50_s": round(percentile(durations_sorted, 50), 3),
            "latency_p95_s": round(percentile(durations_sorted, 95), 3),
            "guardrail_violation_count": total_violations,
            "avg_retries": round(sum(r.retries for r in records) / len(records), 2),
            "per_agent": {
                name: {
                    "calls": v["calls"],
                    "success_rate_pct": round(v["successes"] / v["calls"] * 100, 1) if v["calls"] > 0 else 0.0,
                    "avg_duration_s": round(v["total_duration"] / v["calls"], 3) if v["calls"] > 0 else 0.0,
                    "total_input_tokens": v["total_input_tokens"],
                    "total_output_tokens": v["total_output_tokens"],
                }
                for name, v in per_agent.items()
            },
            "recent_calls": [
                {
                    "id": f"CALL-{index:04d}",
                    "agent_name": record.agent_name,
                    "task": record.task,
                    "started_at": record.started_at,
                    "duration_s": round(record.duration_s, 6),
                    "success": record.success,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "guardrail_violations": list(record.guardrail_violations),
                    "retries": record.retries,
                }
                for index, record in enumerate(reversed(records[-100:]), start=1)
            ],
        }

    def to_markdown(self) -> str:
        s = self.get_summary()
        lines = [
            "## 可观测性面板",
            f"- 总调用次数：{s['total_calls']}",
            f"- 成功率：{s['success_rate_pct']}%",
            f"- 总输入 Token：{s['total_input_tokens']:,}",
            f"- 总输出 Token：{s['total_output_tokens']:,}",
            f"- 延迟 P50：{s['latency_p50_s']}s  P95：{s['latency_p95_s']}s",
            f"- Guardrail 触发次数：{s['guardrail_violation_count']}",
            f"- 平均重试次数：{s['avg_retries']}",
            "",
            "### 各 Agent 详情",
        ]
        for agent, v in s["per_agent"].items():
            lines.append(
                f"- **{agent}**：{v['calls']} 次，成功率 {v['success_rate_pct']}%，"
                f"均延迟 {v['avg_duration_s']}s，输入 {v['total_input_tokens']} tok"
            )
        return "\n".join(lines)

    def reset(self) -> None:
        with self._lock:
            if self.storage_path is None:
                self._records.clear()
            else:
                with self._connect() as connection:
                    connection.execute("DELETE FROM observability_records")
