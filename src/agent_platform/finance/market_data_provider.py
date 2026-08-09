from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

import pandas as pd


@dataclass(frozen=True, slots=True)
class SecurityInfo:
    market: str
    symbol: str
    name: str
    source: str
    updated_at: str


class MarketDataProvider(Protocol):
    def list_securities(self) -> list[SecurityInfo]: ...

    def get_price_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame: ...
