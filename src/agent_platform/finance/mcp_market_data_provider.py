"""把 MCP 工具信封适配为分析层使用的 MarketDataProvider 契约。"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from agent_platform.finance.akshare_data_provider import AkShareMarketDataProvider
from agent_platform.finance.errors import MarketDataUnavailableError
from agent_platform.finance.market_data_provider import SecurityInfo
from agent_platform.mcp import MCPToolRegistry, get_registry


class MCPMarketDataProvider:
    """默认业务行情入口；online/offline 都通过 MCP Registry 调用。"""

    def __init__(self, *, offline: bool, registry: MCPToolRegistry | None = None) -> None:
        self.offline = offline
        self.registry = registry or get_registry(offline=offline)

    def list_securities(self) -> list[SecurityInfo]:
        if self.offline:
            from agent_platform.finance.sample_data_provider import SampleMarketDataProvider

            return SampleMarketDataProvider().list_securities()
        return AkShareMarketDataProvider().list_securities()

    def get_price_history(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> pd.DataFrame:
        tool = "get_offline_price_history" if self.offline else "get_price_history"
        env = self.registry.call(
            tool,
            symbol=symbol,
            start=start.isoformat() if start else "",
            end=end.isoformat() if end else "",
        )
        if not env["ok"]:
            raise MarketDataUnavailableError(str(env["error"]))
        records = (env["data"] or {}).get("records") or []
        return self._normalize_history(records, symbol, env)

    def get_realtime_quote(self, symbol: str) -> dict[str, Any]:
        tool = "get_offline_realtime_quote" if self.offline else "get_realtime_quote"
        env = self.registry.call(tool, symbol=symbol)
        if not env["ok"]:
            raise MarketDataUnavailableError(str(env["error"]))
        data = dict(env["data"] or {})
        data.setdefault("source", env["source"])
        # 实时报价必须保留数据源时间；只有工具未提供时才使用调用完成时间。
        data.setdefault("updated_at", env["updated_at"])
        data["data_status"] = "offline_sample" if self.offline else "live"
        data["fallback_reason"] = None
        data.setdefault("market", AkShareMarketDataProvider.market_for_symbol(str(symbol)))
        return data

    @staticmethod
    def _normalize_history(
        records: list[dict[str, Any]], symbol: str, env: dict[str, Any]
    ) -> pd.DataFrame:
        if not records:
            raise MarketDataUnavailableError(f"MCP 未返回 {symbol} 的历史行情")
        frame = pd.DataFrame(records)
        aliases = {
            "date": ("date", "日期"),
            "open": ("open", "开盘"),
            "high": ("high", "最高"),
            "low": ("low", "最低"),
            "close": ("close", "收盘"),
            "volume": ("volume", "成交量"),
        }
        selected: dict[str, str] = {}
        for target, candidates in aliases.items():
            source = next((item for item in candidates if item in frame.columns), None)
            if source is None:
                raise MarketDataUnavailableError(f"MCP 历史行情缺少字段 {target}")
            selected[source] = target
        frame = frame.rename(columns=selected)
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
        if frame.empty:
            raise MarketDataUnavailableError(f"MCP 返回 {symbol} 的行情没有有效价格")
        frame["symbol"] = str(symbol).upper()
        frame["market"] = frame.get(
            "market", AkShareMarketDataProvider.market_for_symbol(str(symbol))
        )
        frame["name"] = frame.get("name", f"A股 {symbol}")
        frame["source"] = env["source"]
        frame["updated_at"] = env["updated_at"]
        columns = [
            "market", "symbol", "name", "date", "open", "high", "low", "close",
            "volume", "source", "updated_at",
        ]
        return frame[columns].sort_values("date").reset_index(drop=True)
