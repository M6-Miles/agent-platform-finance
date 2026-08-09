from __future__ import annotations

from agent_platform.finance.analysis import analyze_security
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
from agent_platform.storage.sqlite_store import SQLiteStore


def test_sqlite_store_persists_session_messages_and_analysis(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "test.sqlite3")
    session = store.create_session("测试会话")

    store.add_message(session.id, "user", "请分析 DEMO001")
    store.add_message(session.id, "assistant", "分析完成", provider="mock")

    # 显式使用 SampleMarketDataProvider
    provider = SampleMarketDataProvider()
    store.add_analysis(analyze_security("DEMO001", provider=provider), "direct", session.id)

    assert store.get_session(session.id) is not None
    assert [item.role for item in store.list_messages(session.id)] == [
        "user",
        "assistant",
    ]
    history = store.list_analyses()
    assert len(history) == 1
    assert history[0].session_id == session.id
    assert history[0].symbol == "DEMO001"
