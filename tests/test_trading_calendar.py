from __future__ import annotations

from datetime import date

from agent_platform.finance.trading_calendar import ChinaAStockCalendar


def test_china_calendar_distinguishes_exchange_holiday_from_workday() -> None:
    calendar = ChinaAStockCalendar()

    assert calendar.is_trading_day(date(2026, 2, 18)) is False
    assert calendar.is_trading_day(date(2026, 8, 14)) is True
    assert calendar.is_trading_day(date(2026, 8, 15)) is False
    assert calendar.is_authoritative_for(date(2026, 8, 14)) is True


def test_china_calendar_exposes_versioned_source_and_coverage() -> None:
    metadata = ChinaAStockCalendar().metadata()

    assert metadata["covered_from"] == "2025-01-01"
    assert metadata["covered_to"] == "2026-12-31"
    assert "交易所" in str(metadata["source"])
