"""
Loop 记忆层（可持久化）
======================
说明书要求 Loop 具备「规划、工具调用、观察、反思、继续规划/结束」五要素**以及
可持久化的记忆**。本模块是「记忆」这一项的实现。

与 chat_messages 的区别
-----------------------
``storage/sqlite_store.py`` 里的 ``chat_messages`` 存的是**对话消息**（user /
assistant / tool 的文本）。Loop 记忆存的是**推理产物**：第几轮的规划是什么、
观察到什么、反思结论是什么、为什么继续或结束。两者用途不同，因此本模块使用独立
的 ``loop_memory`` 表，**不改动既有 schema**，避免影响已有会话数据与测试。

持久化的验收标准
----------------
「可持久化」不等于「有个 dict 存着」。判定标准是：**换一个进程、换一个对象实例，
指向同一个文件，仍能把记录原样读回**。:class:`SQLiteLoopMemory` 按此标准实现，
对应测试用两个独立实例指向同一 db 文件来证明，而不是断言内存字典非空。

纪律
----
1. **不吞异常**：写入失败直接抛出。记忆静默丢失会让审计链断裂而无人知晓。
2. **顺序确定**：按 (iteration, rowid) 升序返回，同一轮内保持写入先后。
3. **meta 存 JSON**：结构化字段不塞进 content 字符串里拼接，便于机器校验。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MemoryKind:
    """
    记忆条目类型。与 Loop 五要素一一对应。

    用常量而非裸字符串：拼错 ``"reflaction"`` 这类错误在导入期就会 AttributeError，
    而不是在数据库里静静躺成一条永远查不出来的脏数据。
    """

    PLAN: Final[str] = "plan"                 # 要素 1：规划
    TOOL_CALL: Final[str] = "tool_call"       # 要素 2：工具调用
    OBSERVATION: Final[str] = "observation"   # 要素 3：观察
    REFLECTION: Final[str] = "reflection"     # 要素 4：反思
    DECISION: Final[str] = "decision"         # 要素 5：继续 / 结束
    GOAL: Final[str] = "goal"                 # 目标循环的目标本身
    ANSWER: Final[str] = "answer"             # 最终答案

    @classmethod
    def all_kinds(cls) -> tuple[str, ...]:
        return (
            cls.PLAN, cls.TOOL_CALL, cls.OBSERVATION, cls.REFLECTION,
            cls.DECISION, cls.GOAL, cls.ANSWER,
        )


class MemoryScope:
    """说明书要求的三级记忆作用域。"""

    WORKING: Final[str] = "working"
    PROJECT: Final[str] = "project"
    ORGANIZATION: Final[str] = "organization"

    @classmethod
    def all_scopes(cls) -> tuple[str, ...]:
        return (cls.WORKING, cls.PROJECT, cls.ORGANIZATION)


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """一条 Loop 记忆。``meta`` 为结构化附加字段（存库时序列化为 JSON）。"""

    id: str
    session_id: str
    iteration: int
    kind: str
    content: str
    created_at: str
    scope: str = MemoryScope.WORKING
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "iteration": self.iteration,
            "kind": self.kind,
            "content": self.content,
            "created_at": self.created_at,
            "scope": self.scope,
            "meta": dict(self.meta),
        }


@runtime_checkable
class LoopMemory(Protocol):
    """Loop 记忆接口。内存实现供快速测试，SQLite 实现供持久化验收。"""

    def append(
        self, session_id: str, iteration: int, kind: str, content: str,
        meta: dict[str, Any] | None = None, scope: str = MemoryScope.WORKING,
    ) -> MemoryRecord: ...

    def records(
        self, session_id: str, kind: str | None = None, scope: str | None = None,
    ) -> list[MemoryRecord]: ...

    def latest(self, session_id: str, kind: str) -> MemoryRecord | None: ...

    def clear(self, session_id: str) -> int: ...


def _validate(session_id: str, iteration: int, kind: str, scope: str) -> None:
    """入参校验。三者任一不合法都是调用方编码错误，必须立刻暴露而非写脏数据。"""
    if not str(session_id).strip():
        raise ValueError("session_id 不能为空")
    if iteration < 0:
        raise ValueError(f"iteration 不能为负，收到 {iteration}")
    if kind not in MemoryKind.all_kinds():
        raise ValueError(
            f"未知记忆类型 {kind!r}；合法值：{', '.join(MemoryKind.all_kinds())}"
        )
    if scope not in MemoryScope.all_scopes():
        raise ValueError(
            f"未知记忆作用域 {scope!r}；合法值：{', '.join(MemoryScope.all_scopes())}"
        )


class InMemoryLoopMemory:
    """进程内记忆。**不持久化**，仅供单元测试与一次性运行使用。"""

    def __init__(self) -> None:
        self._rows: list[MemoryRecord] = []

    def append(
        self, session_id: str, iteration: int, kind: str, content: str,
        meta: dict[str, Any] | None = None, scope: str = MemoryScope.WORKING,
    ) -> MemoryRecord:
        _validate(session_id, iteration, kind, scope)
        record = MemoryRecord(
            id=str(uuid4()), session_id=session_id, iteration=iteration,
            kind=kind, content=content, created_at=_utc_now(),
            scope=scope,
            meta=dict(meta or {}),
        )
        self._rows.append(record)
        return record

    def records(
        self, session_id: str, kind: str | None = None, scope: str | None = None,
    ) -> list[MemoryRecord]:
        return [
            r for r in self._rows
            if r.session_id == session_id
            and (kind is None or r.kind == kind)
            and (scope is None or r.scope == scope)
        ]

    def latest(self, session_id: str, kind: str) -> MemoryRecord | None:
        rows = self.records(session_id, kind)
        return rows[-1] if rows else None

    def clear(self, session_id: str) -> int:
        before = len(self._rows)
        self._rows = [r for r in self._rows if r.session_id != session_id]
        return before - len(self._rows)

    def is_persistent(self) -> bool:
        """显式声明自己不持久化，避免被当成持久化实现误用。"""
        return False


class SQLiteLoopMemory:
    """
    SQLite 持久化记忆。

    用法::

        mem = SQLiteLoopMemory(Path("data/loop.db"))
        mem.append("s1", 1, MemoryKind.PLAN, "先取行情再算指标")
        # 换一个实例（模拟进程重启）仍读得回：
        assert SQLiteLoopMemory(Path("data/loop.db")).records("s1")
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """
        连接上下文：``with connection`` 负责提交/回滚，``finally`` 负责**关闭**。

        为什么必须显式 close：``with sqlite3.connect(...)`` 只做事务提交，**不关闭
        连接**。连接对象要等垃圾回收才释放文件句柄，在 Windows 上会导致同一进程内
        「写完记忆后删不掉 db 文件」（WinError 32），长时间运行的 Loop 还会累积句柄。
        本封装在 Loop 演示脚本清理临时库失败时被发现，属真实缺陷而非风格问题。
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS loop_memory (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    meta TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'working'
                );

                CREATE INDEX IF NOT EXISTS idx_loop_memory_session
                ON loop_memory(session_id, iteration, kind);
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(loop_memory)")
            }
            if "scope" not in columns:
                connection.execute(
                    "ALTER TABLE loop_memory ADD COLUMN scope TEXT NOT NULL DEFAULT 'working'"
                )

    def append(
        self, session_id: str, iteration: int, kind: str, content: str,
        meta: dict[str, Any] | None = None, scope: str = MemoryScope.WORKING,
    ) -> MemoryRecord:
        _validate(session_id, iteration, kind, scope)
        record = MemoryRecord(
            id=str(uuid4()), session_id=session_id, iteration=iteration,
            kind=kind, content=content, created_at=_utc_now(),
            scope=scope,
            meta=dict(meta or {}),
        )
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO loop_memory(
                    id, session_id, iteration, kind, content, meta, created_at, scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id, record.session_id, record.iteration, record.kind,
                    record.content,
                    json.dumps(record.meta, ensure_ascii=False),
                    record.created_at, record.scope,
                ),
            )
        return record

    def records(
        self, session_id: str, kind: str | None = None, scope: str | None = None,
    ) -> list[MemoryRecord]:
        sql = (
            "SELECT id, session_id, iteration, kind, content, meta, created_at, scope"
            " FROM loop_memory WHERE session_id = ?"
        )
        params: list[Any] = [session_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if scope is not None:
            if scope not in MemoryScope.all_scopes():
                raise ValueError(f"未知记忆作用域 {scope!r}")
            sql += " AND scope = ?"
            params.append(scope)
        # rowid 兜底：同一轮同一秒写入多条时，靠 rowid 保持写入先后确定。
        sql += " ORDER BY iteration ASC, rowid ASC"
        with self._session() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def latest(self, session_id: str, kind: str) -> MemoryRecord | None:
        rows = self.records(session_id, kind)
        return rows[-1] if rows else None

    def clear(self, session_id: str) -> int:
        with self._session() as connection:
            cursor = connection.execute(
                "DELETE FROM loop_memory WHERE session_id = ?", (session_id,)
            )
            return cursor.rowcount

    def is_persistent(self) -> bool:
        return True

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        raw = row["meta"]
        try:
            meta = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            # 宁可把坏数据显式标出来，也不假装 meta 是空的。
            meta = {"_corrupt_meta": raw}
        return MemoryRecord(
            id=row["id"], session_id=row["session_id"], iteration=row["iteration"],
            kind=row["kind"], content=row["content"], created_at=row["created_at"],
            scope=row["scope"], meta=meta,
        )
