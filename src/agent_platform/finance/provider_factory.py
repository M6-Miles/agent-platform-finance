from __future__ import annotations

from agent_platform.config import Settings, get_settings
from agent_platform.finance.akshare_data_provider import AkShareMarketDataProvider
from agent_platform.finance.market_data_provider import MarketDataProvider
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider

SUPPORTED_MARKET_DATA_PROVIDERS = ("sample", "akshare")


def create_market_data_provider(
    name: str | None = None,
    settings: Settings | None = None,
) -> MarketDataProvider:
    current_settings = settings or get_settings()
    provider_name = (name or current_settings.market_data_provider).strip().lower()
    if provider_name == "sample":
        return SampleMarketDataProvider(current_settings.sample_prices_csv)
    if provider_name == "akshare":
        return AkShareMarketDataProvider()
    supported = ", ".join(SUPPORTED_MARKET_DATA_PROVIDERS)
    raise ValueError(f"不支持的行情数据源：{provider_name}。可选值：{supported}")
