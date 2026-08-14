"""
Loop 记忆层测试
===============
验收要点：记忆层必须**真的持久化**，而不是名字里带 SQLite 就算过。
核心证明是「写入后换一个新实例重新打开同一文件，记录仍在」——
只在同一个对象里读回来，等于测了一个字典，证明不了持久化。
"""
from __future__ import annotations

import sqlite3

import pytest

from agent_platform.core.loop_memory import (
    InMemoryLoopMemory,
    LoopMemory,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    SQLiteLoopMemory,
)


# ─────────────────────────────────────────────────────────────────────────────
# 记忆类型常量
# ─────────────────────────────────────────────────────────────────────────────

class TestMemoryKind:
    def test_covers_five_loop_elements(self) -> None:
        """五要素每一项都必须有对应的记忆类型，缺一项就无法审计该要素。"""
        kinds = MemoryKind.all_kinds()
        for required in ("plan", "tool_call", "observation", "reflection", "decision"):
            assert required in kinds, f"五要素记忆类型缺失：{required}"

    def test_also_covers_goal_and_answer(self) -> None:
        kinds = MemoryKind.all_kinds()
        assert "goal" in kinds
        assert "answer" in kinds

    def test_constants_match_all_kinds(self) -> None:
        assert MemoryKind.PLAN in MemoryKind.all_kinds()
        assert MemoryKind.REFLECTION in MemoryKind.all_kinds()
        assert len(MemoryKind.all_kinds()) == len(set(MemoryKind.all_kinds()))


class TestMemoryRecord:
    def test_to_dict_exposes_all_fields(self) -> None:
        record = MemoryRecord(
            id="i1", session_id="s1", iteration=2, kind=MemoryKind.PLAN,
            content="先取行情", created_at="2026-01-01T00:00:00+00:00",
            meta={"k": "v"},
        )
        payload = record.to_dict()
        assert payload["id"] == "i1"
        assert payload["session_id"] == "s1"
        assert payload["iteration"] == 2
        assert payload["kind"] == "plan"
        assert payload["content"] == "先取行情"
        assert payload["meta"] == {"k": "v"}

    def test_meta_defaults_to_empty_dict(self) -> None:
        record = MemoryRecord(
            id="i", session_id="s", iteration=0, kind=MemoryKind.GOAL,
            content="g", created_at="2026-01-01T00:00:00+00:00",
        )
        assert record.meta == {}


# ─────────────────────────────────────────────────────────────────────────────
# 入参校验：脏数据必须当场拒绝
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path) -> LoopMemory:
    """两种实现跑同一套行为测试，保证接口语义一致。"""
    if request.param == "memory":
        return InMemoryLoopMemory()
    return SQLiteLoopMemory(tmp_path / "loop_memory.sqlite3")


class TestValidation:
    def test_empty_session_id_rejected(self, store: LoopMemory) -> None:
        with pytest.raises(ValueError, match="session_id"):
            store.append("", 1, MemoryKind.PLAN, "x")

    def test_blank_session_id_rejected(self, store: LoopMemory) -> None:
        with pytest.raises(ValueError, match="session_id"):
            store.append("   ", 1, MemoryKind.PLAN, "x")

    def test_negative_iteration_rejected(self, store: LoopMemory) -> None:
        with pytest.raises(ValueError, match="iteration"):
            store.append("s", -1, MemoryKind.PLAN, "x")

    def test_unknown_kind_rejected(self, store: LoopMemory) -> None:
        """拼错的 kind 必须报错。静默写入会造出永远查不到的孤儿记录。"""
        with pytest.raises(ValueError, match="未知记忆类型"):
            store.append("s", 1, "plannn", "x")

    def test_error_message_lists_legal_kinds(self, store: LoopMemory) -> None:
        with pytest.raises(ValueError) as exc:
            store.append("s", 1, "nope", "x")
        assert "plan" in str(exc.value)
        assert "reflection" in str(exc.value)

    def test_unknown_scope_rejected(self, store: LoopMemory) -> None:
        with pytest.raises(ValueError, match="未知记忆作用域"):
            store.append("s1", 1, MemoryKind.PLAN, "计划", scope="global")


class TestMemoryScope:
    def test_three_required_scopes_are_declared(self) -> None:
        assert MemoryScope.all_scopes() == ("working", "project", "organization")

    def test_records_filter_scopes(self, store: LoopMemory) -> None:
        for scope in MemoryScope.all_scopes():
            store.append("s1", 1, MemoryKind.PLAN, scope, scope=scope)

        assert [r.content for r in store.records("s1")] == [
            "working", "project", "organization"
        ]
        assert [r.content for r in store.records("s1", scope=MemoryScope.PROJECT)] == [
            "project"
        ]


# ─────────────────────────────────────────────────────────────────────────────
# 读写行为（两种实现共用）
# ─────────────────────────────────────────────────────────────────────────────

class TestReadWrite:
    def test_append_returns_record_with_content(self, store: LoopMemory) -> None:
        record = store.append("s1", 1, MemoryKind.PLAN, "先取行情")
        assert record.content == "先取行情"
        assert record.kind == MemoryKind.PLAN
        assert record.iteration == 1
        assert record.session_id == "s1"
        assert record.id

    def test_records_returns_in_insertion_order(self, store: LoopMemory) -> None:
        store.append("s1", 1, MemoryKind.PLAN, "第一")
        store.append("s1", 1, MemoryKind.OBSERVATION, "第二")
        store.append("s1", 2, MemoryKind.PLAN, "第三")
        contents = [r.content for r in store.records("s1")]
        assert contents == ["第一", "第二", "第三"]

    def test_records_filters_by_kind(self, store: LoopMemory) -> None:
        store.append("s1", 1, MemoryKind.PLAN, "P1")
        store.append("s1", 1, MemoryKind.OBSERVATION, "O1")
        store.append("s1", 2, MemoryKind.PLAN, "P2")
        plans = [r.content for r in store.records("s1", MemoryKind.PLAN)]
        assert plans == ["P1", "P2"]

    def test_sessions_are_isolated(self, store: LoopMemory) -> None:
        """会话隔离：A 的记忆绝不能出现在 B 的查询结果里。"""
        store.append("A", 1, MemoryKind.PLAN, "属于A")
        store.append("B", 1, MemoryKind.PLAN, "属于B")
        assert [r.content for r in store.records("A")] == ["属于A"]
        assert [r.content for r in store.records("B")] == ["属于B"]

    def test_latest_returns_most_recent_of_kind(self, store: LoopMemory) -> None:
        store.append("s1", 1, MemoryKind.REFLECTION, "旧反思")
        store.append("s1", 2, MemoryKind.REFLECTION, "新反思")
        latest = store.latest("s1", MemoryKind.REFLECTION)
        assert latest is not None
        assert latest.content == "新反思"

    def test_latest_returns_none_when_absent(self, store: LoopMemory) -> None:
        store.append("s1", 1, MemoryKind.PLAN, "P")
        assert store.latest("s1", MemoryKind.REFLECTION) is None

    def test_meta_round_trips(self, store: LoopMemory) -> None:
        """meta 必须原样取回。SQLite 侧要经 JSON 序列化，容易在这里丢字段。"""
        meta = {"tool": "get_quote", "ok": True, "n": 3, "args": {"symbol": "600519"}}
        store.append("s1", 1, MemoryKind.TOOL_CALL, "调用", meta=meta)
        got = store.records("s1")[0]
        assert got.meta == meta

    def test_meta_none_becomes_empty_dict(self, store: LoopMemory) -> None:
        store.append("s1", 1, MemoryKind.PLAN, "P", meta=None)
        assert store.records("s1")[0].meta == {}

    def test_clear_removes_only_target_session(self, store: LoopMemory) -> None:
        store.append("A", 1, MemoryKind.PLAN, "a1")
        store.append("A", 1, MemoryKind.PLAN, "a2")
        store.append("B", 1, MemoryKind.PLAN, "b1")
        removed = store.clear("A")
        assert removed == 2
        assert store.records("A") == []
        assert len(store.records("B")) == 1

    def test_clear_missing_session_returns_zero(self, store: LoopMemory) -> None:
        assert store.clear("never-existed") == 0

    def test_iteration_zero_allowed(self, store: LoopMemory) -> None:
        """目标本身记在 iteration=0，必须允许。"""
        record = store.append("s1", 0, MemoryKind.GOAL, "分析茅台")
        assert record.iteration == 0

    def test_created_at_is_populated(self, store: LoopMemory) -> None:
        record = store.append("s1", 1, MemoryKind.PLAN, "P")
        assert record.created_at
        assert "T" in record.created_at

    def test_satisfies_protocol(self, store: LoopMemory) -> None:
        assert isinstance(store, LoopMemory)


# ─────────────────────────────────────────────────────────────────────────────
# 持久化：本模块的核心验收点
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistence:
    def test_sqlite_survives_reopen(self, tmp_path) -> None:
        """
        关键证明：写入 → 丢弃实例 → 用新实例打开同一文件 → 记录仍在。
        只在同一实例内读回来无法区分「持久化」与「进程内字典」。
        """
        path = tmp_path / "persist.sqlite3"
        first = SQLiteLoopMemory(path)
        first.append("s1", 1, MemoryKind.PLAN, "跨实例应存活")
        first.append("s1", 1, MemoryKind.REFLECTION, "反思也应存活",
                     meta={"goal_met": False})
        del first

        second = SQLiteLoopMemory(path)
        contents = [r.content for r in second.records("s1")]
        assert contents == ["跨实例应存活", "反思也应存活"]
        reflection = second.latest("s1", MemoryKind.REFLECTION)
        assert reflection is not None
        assert reflection.meta == {"goal_met": False}

    def test_sqlite_file_actually_created(self, tmp_path) -> None:
        path = tmp_path / "nested" / "dir" / "created.sqlite3"
        store = SQLiteLoopMemory(path)
        store.append("s1", 1, MemoryKind.PLAN, "P")
        assert path.exists(), "SQLite 文件未落盘，谈不上持久化"
        assert path.stat().st_size > 0

    def test_sqlite_declares_persistent(self, tmp_path) -> None:
        assert SQLiteLoopMemory(tmp_path / "d.sqlite3").is_persistent() is True

    def test_inmemory_declares_not_persistent(self) -> None:
        """内存实现必须自陈不持久化，避免被当持久化实现误用去交验收。"""
        assert InMemoryLoopMemory().is_persistent() is False

    def test_inmemory_does_not_survive_new_instance(self) -> None:
        first = InMemoryLoopMemory()
        first.append("s1", 1, MemoryKind.PLAN, "只在本实例")
        assert InMemoryLoopMemory().records("s1") == []

    def test_initialize_is_idempotent(self, tmp_path) -> None:
        """重复 initialize 不得清库——启动即清空是最容易犯的持久化假象。"""
        path = tmp_path / "idem.sqlite3"
        store = SQLiteLoopMemory(path)
        store.append("s1", 1, MemoryKind.PLAN, "保留我")
        store.initialize()
        store.initialize()
        assert [r.content for r in store.records("s1")] == ["保留我"]

    def test_two_instances_share_one_file(self, tmp_path) -> None:
        path = tmp_path / "shared.sqlite3"
        a = SQLiteLoopMemory(path)
        b = SQLiteLoopMemory(path)
        a.append("s1", 1, MemoryKind.PLAN, "由A写入")
        assert [r.content for r in b.records("s1")] == ["由A写入"]

    def test_accepts_str_path(self, tmp_path) -> None:
        store = SQLiteLoopMemory(str(tmp_path / "strpath.sqlite3"))
        store.append("s1", 1, MemoryKind.PLAN, "P")
        assert len(store.records("s1")) == 1

    def test_scope_survives_reopen(self, tmp_path) -> None:
        path = tmp_path / "scopes.sqlite3"
        SQLiteLoopMemory(path).append(
            "s1", 1, MemoryKind.PLAN, "企业规则", scope=MemoryScope.ORGANIZATION
        )
        rows = SQLiteLoopMemory(path).records("s1", scope=MemoryScope.ORGANIZATION)
        assert len(rows) == 1
        assert rows[0].scope == MemoryScope.ORGANIZATION

    def test_legacy_table_is_migrated_with_working_default(self, tmp_path) -> None:
        path = tmp_path / "legacy.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE loop_memory (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    iteration INTEGER NOT NULL, kind TEXT NOT NULL,
                    content TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO loop_memory VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("id1", "s1", 1, "plan", "旧记录", "{}", "2026-01-01T00:00:00+00:00"),
            )

        rows = SQLiteLoopMemory(path).records("s1")
        assert rows[0].scope == MemoryScope.WORKING
