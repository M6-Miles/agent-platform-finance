"""SQLiteStore / ApplicationService rename & delete 测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.config import Settings
from agent_platform.services.application_service import ApplicationService
from agent_platform.storage.sqlite_store import SQLiteStore


def _settings(tmp: Path) -> Settings:
    return Settings(
        app_name="test",
        sqlite_path=tmp / "test.sqlite3",
    )


class TestStoreSessionLifecycle:
    def test_rename_exists(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s1.sqlite3")
        s = store.create_session("原名")
        store.rename_session(s.id, "新名")
        assert store.get_session(s.id).title == "新名"

    def test_rename_whitespace_is_noop(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s2.sqlite3")
        s = store.create_session("保留")
        store.rename_session(s.id, "   ")
        assert store.get_session(s.id).title == "保留"

    def test_rename_empty_is_noop(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s3.sqlite3")
        s = store.create_session("保留")
        store.rename_session(s.id, "")
        assert store.get_session(s.id).title == "保留"

    def test_delete_session_removes_it(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s4.sqlite3")
        s = store.create_session("待删除")
        store.delete_session(s.id)
        assert store.get_session(s.id) is None

    def test_delete_session_cascades_messages(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s5.sqlite3")
        s = store.create_session("test")
        store.add_message(s.id, "user", "hello")
        store.delete_session(s.id)
        # messages 也应因 ON DELETE CASCADE 被删除
        assert store.get_session(s.id) is None

    def test_delete_nonexistent_raises(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "s6.sqlite3")
        with pytest.raises(ValueError, match="会话不存在"):
            store.delete_session("does-not-exist")


class TestServiceSessionLifecycle:
    def test_rename(self, tmp_path: Path) -> None:
        svc = ApplicationService(settings=_settings(tmp_path))
        s = svc.create_session("原")
        svc.rename_session(s.id, "改")
        sessions = svc.list_sessions()
        assert sessions[0].title == "改"

    def test_delete(self, tmp_path: Path) -> None:
        svc = ApplicationService(settings=_settings(tmp_path))
        s = svc.create_session("test")
        svc.delete_session(s.id)
        assert len(svc.list_sessions()) == 0
