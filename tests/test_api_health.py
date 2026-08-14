from __future__ import annotations

from fastapi.testclient import TestClient

from agent_platform.api import main
from agent_platform.config import Settings
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
from agent_platform.services.application_service import ApplicationService
from agent_platform.storage.sqlite_store import SQLiteStore


def build_client(tmp_path, monkeypatch) -> TestClient:
    market_data = SampleMarketDataProvider()
    settings = Settings(
        sample_prices_csv=market_data.csv_path,
        sqlite_path=tmp_path / "api.sqlite3",
    )
    service = ApplicationService(
        settings=settings,
        store=SQLiteStore(settings.sqlite_path),
        market_data=market_data,
    )
    monkeypatch.setattr(main, "get_application_service", lambda: service)
    return TestClient(main.app)


def test_health_endpoint(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["storage"] == "sqlite"


def test_observability_api_is_persistent_and_resettable(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    service = main.get_application_service()
    service.observability.record_call(
        "technical_agent", "DEMO001", 0.125, True,
        input_tokens=20, output_tokens=10,
    )

    response = client.get("/observability")
    assert response.status_code == 200
    assert response.json()["total_calls"] == 1
    assert response.json()["total_input_tokens"] == 20
    assert response.json()["recent_calls"][0]["agent_name"] == "technical_agent"

    reset = client.delete("/observability")
    assert reset.status_code == 200
    assert client.get("/observability").json()["total_calls"] == 0


def test_paper_monitor_api_daily_record_is_idempotent(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    created = client.post(
        "/paper-trading/monitor/jobs",
        json={"symbols": ["DEMO001"], "data_mode": "offline", "run_time": "00:00"},
    )
    assert created.status_code == 200
    job_id = created.json()["id"]

    first = client.post(f"/paper-trading/monitor/jobs/{job_id}/run")
    second = client.post(f"/paper-trading/monitor/jobs/{job_id}/run")
    history = client.get(f"/paper-trading/monitor/jobs/{job_id}/runs")

    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    assert first.json()["deduplicated"] is False
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["deduplicated"] is True
    assert len(history.json()) == 1


def test_paper_monitor_api_exposes_scheduler_and_task_controls(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    created = client.post(
        "/paper-trading/monitor/jobs",
        json={"symbols": ["DEMO001"], "data_mode": "offline", "run_time": "15:10"},
    ).json()

    status = client.get("/paper-trading/monitor/status")
    paused = client.patch(
        f"/paper-trading/monitor/jobs/{created['id']}", json={"enabled": False}
    )
    deleted = client.delete(f"/paper-trading/monitor/jobs/{created['id']}")

    assert status.status_code == 200
    assert {"configured_enabled", "running", "thread_alive"} <= status.json().keys()
    assert paused.json()["enabled"] is False
    assert deleted.json() == {"deleted": True}
    assert client.get("/paper-trading/monitor/jobs").json() == []


def test_chat_endpoint_reuses_session(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)

    first = client.post("/chat", json={"message": "请分析 DEMO002"})
    session_id = first.json()["session_id"]
    second = client.post(
        "/chat",
        json={"message": "谢谢", "session_id": session_id},
    )
    messages = client.get(f"/sessions/{session_id}/messages")

    assert first.status_code == 200
    assert first.json()["provider"]  # provider 取决于 .env 配置，不硬编码
    assert first.json()["tool_steps"]
    assert second.status_code == 200
    assert second.json()["session_id"] == session_id
    assert len(messages.json()) == 4


def test_analysis_date_range_and_history(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)

    # 合成数据为 TEST001-TEST020，日期区间从 2025-01-02 开始
    response = client.get(
        "/analysis/TEST001",
        params={"start": "2025-03-10", "end": "2025-03-20"},
    )
    history = client.get("/analysis-history")

    assert response.status_code == 200
    assert response.json()["start_date"] == "2025-03-10"
    assert response.json()["end_date"] == "2025-03-20"
    assert history.json()[0]["trigger"] == "direct"


def test_list_securities(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)

    response = client.get("/securities")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    for item in data:
        for key in ("market", "symbol", "name", "source", "updated_at"):
            assert key in item


def test_create_and_list_sessions(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)

    # 创建两个会话
    create1 = client.post("/sessions", json={"title": "会话A"})
    create2 = client.post("/sessions", json={"title": "会话B"})
    assert create1.status_code == 200
    assert create2.status_code == 200
    id1, id2 = create1.json()["id"], create2.json()["id"]
    assert id1 != id2

    # 列表应包含两个新建会话
    list_resp = client.get("/sessions")
    assert list_resp.status_code == 200
    ids = [s["id"] for s in list_resp.json()]
    assert id1 in ids
    assert id2 in ids


def test_list_messages_unknown_session_404(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    response = client.get("/sessions/nonexistent-id-xxxx/messages")
    assert response.status_code == 404


def test_delete_nonexistent_session_404(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    response = client.delete("/sessions/ghost-session-id")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_rename_nonexistent_session_404(tmp_path, monkeypatch) -> None:
    client = build_client(tmp_path, monkeypatch)
    response = client.patch(
        "/sessions/ghost-session-id",
        params={"title": "新标题"},
    )
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]
