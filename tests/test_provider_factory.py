"""create_market_data_provider 单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.config import Settings
from agent_platform.finance.provider_factory import create_market_data_provider
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider


def test_create_sample_returns_sample_provider() -> None:
    settings = Settings(sample_prices_csv=Path("data/sample/prices.csv"))
    provider = create_market_data_provider("sample", settings)
    assert isinstance(provider, SampleMarketDataProvider)

def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="不支持"):
        create_market_data_provider("nonexistent", Settings())

def test_normalize_whitespace_and_case() -> None:
    settings = Settings(sample_prices_csv=Path("data/sample/prices.csv"))
    provider = create_market_data_provider("  SAMPLE  ", settings)
    assert isinstance(provider, SampleMarketDataProvider)
