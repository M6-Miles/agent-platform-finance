from __future__ import annotations

from datetime import date

from agent_platform.finance.sample_data_provider import SampleMarketDataProvider


def test_sample_provider_lists_and_loads_securities() -> None:
    provider = SampleMarketDataProvider()

    securities = provider.list_securities()
    symbols = {item.symbol for item in securities}
    # 样例数据现为 TEST001-TEST020 合成集
    assert len(symbols) >= 1
    first_sym = sorted(symbols)[0]
    history = provider.get_price_history(first_sym)

    assert not history.empty
    assert history.iloc[0]["symbol"] == first_sym


def test_sample_provider_filters_date_range() -> None:
    provider = SampleMarketDataProvider()

    # 合成数据起始日期为 2025-01-02，取前两周区间
    history = provider.get_price_history(
        "TEST001",
        start=date(2025, 1, 6),
        end=date(2025, 1, 17),
    )

    assert not history.empty
    assert history.iloc[0]["date"] >= date(2025, 1, 6)
    assert history.iloc[-1]["date"] <= date(2025, 1, 17)


def test_sample_provider_reports_unknown_symbol() -> None:
    provider = SampleMarketDataProvider()

    from agent_platform.finance.errors import InvalidSecuritySymbolError
    try:
        provider.get_price_history("UNKNOWN")
    except InvalidSecuritySymbolError as exc:
        assert "未找到证券代码" in str(exc)
    else:
        raise AssertionError("未知证券代码应抛出 InvalidSecuritySymbolError")
