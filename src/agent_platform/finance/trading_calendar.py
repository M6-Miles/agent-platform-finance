"""Versioned A-share trading calendar with an explicit coverage boundary."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Protocol

from agent_platform.config import PROJECT_ROOT


class TradingCalendar(Protocol):
    name: str
    authoritative: bool

    def is_trading_day(self, value: date) -> bool: ...

    def is_authoritative_for(self, value: date) -> bool: ...


class WeekdayCandidateCalendar:
    name = "weekday_candidate_only"
    authoritative = False

    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5

    def is_authoritative_for(self, value: date) -> bool:
        return False


class ChinaAStockCalendar:
    """Local exchange-calendar snapshot; unknown dates remain conservative candidates."""

    name = "china_a_exchange_calendar"

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or PROJECT_ROOT / "data" / "reference" / "china_a_market_calendar.json")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.covered_from = date.fromisoformat(payload["covered_from"])
        self.covered_to = date.fromisoformat(payload["covered_to"])
        self.closed_dates = {
            date.fromisoformat(value)
            for value in payload.get("closed_weekdays", []) + payload.get("temporary_closures", [])
        }
        self.source = str(payload["source"])
        self.authoritative = True

    def is_authoritative_for(self, value: date) -> bool:
        return self.covered_from <= value <= self.covered_to

    def is_trading_day(self, value: date) -> bool:
        if value.weekday() >= 5:
            return False
        if self.is_authoritative_for(value):
            return value not in self.closed_dates
        return True

    def metadata(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "authoritative": self.authoritative,
            "covered_from": self.covered_from.isoformat(),
            "covered_to": self.covered_to.isoformat(),
            "source": self.source,
        }
