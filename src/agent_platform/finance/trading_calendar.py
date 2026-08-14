"""Trading-day abstraction; weekends are candidates, not exchange proof."""
from __future__ import annotations

from datetime import date
from typing import Protocol


class TradingCalendar(Protocol):
    name: str
    authoritative: bool

    def is_trading_day(self, value: date) -> bool: ...


class WeekdayCandidateCalendar:
    name = "weekday_candidate_only"
    authoritative = False

    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5
