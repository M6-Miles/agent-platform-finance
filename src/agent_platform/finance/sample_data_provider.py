from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from agent_platform.config import get_settings
from agent_platform.finance.errors import InvalidSecuritySymbolError
from agent_platform.finance.market_data_provider import SecurityInfo


class SampleMarketDataProvider:
    def __init__(self, csv_path: Path | None = None) -> None:
        self.csv_path = csv_path or get_settings().sample_prices_csv

    def _load(self) -> pd.DataFrame:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"找不到样例行情文件：{self.csv_path}")
        data = pd.read_csv(self.csv_path)
        data["date"] = pd.to_datetime(data["date"]).dt.date
        return data.sort_values(["symbol", "date"]).reset_index(drop=True)

    def list_securities(self) -> list[SecurityInfo]:
        data = self._load()
        rows = (
            data.groupby("symbol", as_index=False)
            .agg(
                market=("market", "first"),
                name=("name", "first"),
                source=("source", "first"),
                updated_at=("updated_at", "max"),
            )
            .sort_values("symbol")
        )
        return [
            SecurityInfo(
                market=str(row.market),
                symbol=str(row.symbol),
                name=str(row.name),
                source=str(row.source),
                updated_at=str(row.updated_at),
            )
            for row in rows.itertuples(index=False)
        ]

    def get_price_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        normalized_symbol = symbol.strip().upper()
        data = self._load()
        filtered = data[data["symbol"].str.upper() == normalized_symbol].copy()
        if filtered.empty:
            available = ", ".join(info.symbol for info in self.list_securities())
            raise InvalidSecuritySymbolError(f"未找到证券代码 {symbol}。可选样例：{available}")
        if start is not None:
            filtered = filtered[filtered["date"] >= start]
        if end is not None:
            filtered = filtered[filtered["date"] <= end]
        if filtered.empty:
            raise InvalidSecuritySymbolError("选定日期范围内没有样例行情数据")
        return filtered.reset_index(drop=True)

    def get_realtime_quote(self, symbol: str) -> dict:
        """用样例数据的最后一个收盘价模拟实时报价（离线模式）。"""
        df = self.get_price_history(symbol)
        last = df.iloc[-1]
        price = float(last["close"])
        prev_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else price
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0
        return {
            "symbol": str(last["symbol"]),
            "name": str(last.get("name", symbol)),
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "market": str(last.get("market", "")),
            "source": "样例数据（离线）",
            "data_status": "offline_sample",
            "fallback_reason": None,
        }
