from __future__ import annotations

from datetime import datetime

from agent_platform.finance.paper_broker_service import PaperBrokerService
from agent_platform.finance.paper_trading_monitor import PaperTradingMonitor


def test_daily_monitor_persists_complete_account_snapshot(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    monitor = PaperTradingMonitor(path, PaperBrokerService(path))
    job = monitor.create_job(["DEMO001"], data_mode="offline", run_time="00:00")

    result = monitor.run_job(job["id"], now=datetime(2026, 8, 10, 15, 10))

    assert result["status"] == "completed"
    snapshot = result["snapshot"]
    for field in (
        "quotes", "orders", "trades", "positions", "cash", "portfolio_value",
        "trading_date", "broker_kind",
    ):
        assert field in snapshot
    assert snapshot["quotes"]["DEMO001"]["data_status"] == "offline_sample"
    assert "MockBroker" in snapshot["broker_kind"]


def test_monitor_is_idempotent_per_job_and_date(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    monitor = PaperTradingMonitor(path, PaperBrokerService(path))
    job = monitor.create_job(["DEMO001"], data_mode="offline", run_time="00:00")
    now = datetime(2026, 8, 10, 15, 10)

    first = monitor.run_job(job["id"], now=now)
    second = monitor.run_job(job["id"], now=now)

    assert second["id"] == first["id"]
    assert len(monitor.list_runs(job["id"])) == 1


def test_monitor_jobs_and_runs_survive_service_rebuild(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    first = PaperTradingMonitor(path, PaperBrokerService(path))
    job = first.create_job(["DEMO001"], data_mode="offline", run_time="00:00")
    first.run_job(job["id"], now=datetime(2026, 8, 10, 15, 10))

    rebuilt = PaperTradingMonitor(path, PaperBrokerService(path))

    assert rebuilt.get_job(job["id"])["symbols"] == ["DEMO001"]
    assert rebuilt.list_runs(job["id"])[0]["trading_date"] == "2026-08-10"


def test_run_due_respects_schedule_and_enabled_flag(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    monitor = PaperTradingMonitor(path, PaperBrokerService(path))
    due = monitor.create_job(["DEMO001"], data_mode="offline", run_time="15:00")
    later = monitor.create_job(["DEMO001"], data_mode="offline", run_time="16:00")
    disabled = monitor.create_job(["DEMO001"], data_mode="offline", run_time="14:00")
    monitor.set_enabled(disabled["id"], False)

    results = monitor.run_due(now=datetime(2026, 8, 10, 15, 10))

    assert [result["job_id"] for result in results] == [due["id"]]
    assert monitor.list_runs(later["id"]) == []
    assert monitor.list_runs(disabled["id"]) == []


def test_duplicate_active_job_is_reused_without_orphan_account(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    broker = PaperBrokerService(path)
    monitor = PaperTradingMonitor(path, broker)

    first = monitor.create_job(["demo001", "DEMO001"], data_mode="offline")
    second = monitor.create_job(["DEMO001"], data_mode="offline")

    assert second["id"] == first["id"]
    assert second["deduplicated"] is True
    assert len(monitor.list_jobs()) == 1


def test_weekend_is_skipped_without_market_data_call(tmp_path, monkeypatch) -> None:
    path = tmp_path / "paper.sqlite3"
    broker = PaperBrokerService(path)
    monitor = PaperTradingMonitor(path, broker)
    job = monitor.create_job(["DEMO001"], data_mode="offline", run_time="00:00")
    monkeypatch.setattr(broker, "refresh", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("called")))

    result = monitor.run_job(job["id"], now=datetime(2026, 8, 8, 15, 10))

    assert result["status"] == "skipped_non_trading_day"
    assert result["snapshot"] is None


def test_summary_does_not_count_offline_run_as_real_evidence(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    monitor = PaperTradingMonitor(path, PaperBrokerService(path))
    job = monitor.create_job(["DEMO001"], data_mode="offline", run_time="00:00")
    monitor.run_job(job["id"], now=datetime(2026, 8, 10, 15, 10))

    summary = monitor.list_jobs()[0]["summary"]

    assert summary["completed_candidate_days"] == 1
    assert summary["valid_real_evidence_days"] == 0
    assert summary["evidence_status"] == "scheduler_disabled"


def test_real_evidence_requires_distinct_complete_trading_days(tmp_path, monkeypatch) -> None:
    path = tmp_path / "paper.sqlite3"
    broker = PaperBrokerService(path)
    monitor = PaperTradingMonitor(path, broker)
    job = monitor.create_job(["600519"], data_mode="auto", run_time="00:00")

    monkeypatch.setattr(broker, "refresh", lambda *_args, **_kwargs: {
        "cash": 1_000_000.0, "portfolio_value": 1_000_000.0,
        "positions": {}, "orders": [], "trades": [], "quote_errors": {},
        "broker_kind": "MockBroker(本地模拟撮合，无真实券商连接)",
        "quotes": {"600519": {
            "price": 1500.0, "source": "腾讯公开行情",
            "data_status": "live", "updated_at": "2026-08-03T15:10:00+08:00",
        }},
    })
    for day in (3, 4, 5, 6, 7, 10, 11):
        monitor.run_job(job["id"], now=datetime(2026, 8, day, 15, 10))

    summary = monitor.list_jobs()[0]["summary"]
    assert summary["valid_real_evidence_days"] == 7
    assert summary["first_valid_evidence_date"] == "2026-08-03"
    assert summary["last_valid_evidence_date"] == "2026-08-11"
    assert summary["calendar_span_days"] == 9
    assert summary["missing_candidate_dates"] == []
    assert summary["minimum_acceptance_met"] is True
    assert summary["full_target_met"] is False
    assert summary["evidence_status"] == "validated_7_days"


def test_real_evidence_reports_missing_candidate_day(tmp_path, monkeypatch) -> None:
    path = tmp_path / "paper.sqlite3"
    broker = PaperBrokerService(path)
    monitor = PaperTradingMonitor(path, broker)
    job = monitor.create_job(["600519"], data_mode="auto", run_time="00:00")
    monkeypatch.setattr(broker, "refresh", lambda *_args, **_kwargs: {
        "cash": 1.0, "portfolio_value": 1.0, "positions": {}, "orders": [],
        "trades": [], "quote_errors": {}, "broker_kind": "MockBroker",
        "quotes": {"600519": {"source": "腾讯公开行情", "data_status": "live"}},
    })
    for day in (3, 4, 5, 7, 10, 11, 12):
        monitor.run_job(job["id"], now=datetime(2026, 8, day, 15, 10))

    summary = monitor.list_jobs()[0]["summary"]
    assert summary["valid_real_evidence_days"] == 7
    assert summary["missing_candidate_dates"] == ["2026-08-06"]
    assert summary["minimum_acceptance_met"] is False


def test_scheduler_status_and_delete_job(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    monitor = PaperTradingMonitor(path, PaperBrokerService(path), configured_enabled=False)
    job = monitor.create_job(["DEMO001"], data_mode="offline")

    assert monitor.status()["running"] is False
    assert monitor.status()["configured_enabled"] is False

    monitor.delete_job(job["id"])
    assert monitor.list_jobs() == []


def test_scheduler_lease_allows_only_one_monitor_instance(tmp_path, monkeypatch) -> None:
    path = tmp_path / "paper.sqlite3"
    broker = PaperBrokerService(path)
    first = PaperTradingMonitor(path, broker, poll_interval_s=30)
    second = PaperTradingMonitor(path, broker, poll_interval_s=30)
    job = first.create_job(["DEMO001"], data_mode="offline", run_time="00:00")
    now = datetime(2026, 8, 10, 15, 10)

    first_results = first.run_due(now=now)
    second_results = second.run_due(now=now)

    assert [result["job_id"] for result in first_results] == [job["id"]]
    assert second_results == []
