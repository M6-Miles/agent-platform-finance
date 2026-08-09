from __future__ import annotations

from agent_platform.config import Settings
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
from agent_platform.services.application_service import ApplicationService
from agent_platform.storage.sqlite_store import SQLiteStore


def build_service(tmp_path) -> ApplicationService:
    market_data = SampleMarketDataProvider()
    settings = Settings(
        sample_prices_csv=market_data.csv_path,
        sqlite_path=tmp_path / "service.sqlite3",
    )
    return ApplicationService(
        settings=settings,
        store=SQLiteStore(settings.sqlite_path),
        market_data=market_data,
    )


def test_direct_analysis_is_persisted(tmp_path) -> None:
    service = build_service(tmp_path)

    result = service.analyze_security("TEST001")

    assert result.symbol == "TEST001"
    assert service.list_analysis_history()[0].trigger == "direct"


def test_chat_reuses_session_and_persists_agent_analysis(tmp_path) -> None:
    service = build_service(tmp_path)

    first = service.chat("请分析 TEST002")
    second = service.chat("谢谢", session_id=first.session_id)

    assert second.session_id == first.session_id
    messages = service.list_messages(first.session_id)
    assert [item.role for item in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert service.list_analysis_history()[0].trigger == "agent_tool"
