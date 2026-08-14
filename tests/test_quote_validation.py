"""报价工具校验测试：symbol、price、昨收、状态、来源。"""
from __future__ import annotations

import pytest

from agent_platform.finance.quote_tool import (
    QuoteToolError,
    _is_valid_symbol_format,
    get_latest_quote,
)


class TestSymbolValidation:
    """证券代码格式校验。"""

    def test_empty_symbol_raises_error(self) -> None:
        with pytest.raises(QuoteToolError, match="缺少证券代码"):
            get_latest_quote("")

    def test_none_symbol_raises_error(self) -> None:
        with pytest.raises(QuoteToolError, match="缺少证券代码"):
            get_latest_quote(None)  # type: ignore

    def test_invalid_format_raises_error(self) -> None:
        with pytest.raises(QuoteToolError, match="证券代码格式无效"):
            get_latest_quote("ABC")

    def test_valid_6_digit_symbol(self) -> None:
        assert _is_valid_symbol_format("600519") is True
        assert _is_valid_symbol_format("000001") is True

    def test_valid_demo_symbol(self) -> None:
        assert _is_valid_symbol_format("DEMO001") is True
        assert _is_valid_symbol_format("TEST020") is True

    def test_invalid_demo_suffix(self) -> None:
        assert _is_valid_symbol_format("DEMO1") is False
        assert _is_valid_symbol_format("DEMO00") is False
        assert _is_valid_symbol_format("DEMOABC") is False

    def test_invalid_length(self) -> None:
        assert _is_valid_symbol_format("60051") is False
        assert _is_valid_symbol_format("6005199") is False


class TestQuoteFieldValidation:
    """报价字段完整性校验。"""

    def test_missing_symbol_field(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        def mock_provider_for_mode(mode):
            class MockProvider:
                def get_realtime_quote(self, symbol):
                    return {
                        "price": 100.0,
                        "prev_close": 99.0,
                        "source": "测试",
                        "updated_at": "2026-08-13",
                    }
            return MockProvider()

        monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

        with pytest.raises(QuoteToolError, match="缺少 symbol 字段"):
            get_latest_quote("DEMO001", data_mode="offline")

    def test_missing_source_field(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        def mock_provider_for_mode(mode):
            class MockProvider:
                def get_realtime_quote(self, symbol):
                    return {
                        "symbol": "DEMO001",
                        "price": 100.0,
                        "prev_close": 99.0,
                        "updated_at": "2026-08-13",
                    }
            return MockProvider()

        monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

        with pytest.raises(QuoteToolError, match="缺少 source 字段"):
            get_latest_quote("DEMO001", data_mode="offline")

    def test_missing_updated_at_field(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        def mock_provider_for_mode(mode):
            class MockProvider:
                def get_realtime_quote(self, symbol):
                    return {
                        "symbol": "DEMO001",
                        "price": 100.0,
                        "prev_close": 99.0,
                        "source": "测试",
                    }
            return MockProvider()

        monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

        with pytest.raises(QuoteToolError, match="缺少 updated_at 字段"):
            get_latest_quote("DEMO001", data_mode="offline")


class TestPriceValidation:
    """价格有效性校验。"""

    def test_negative_price_raises_error(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        def mock_provider_for_mode(mode):
            class MockProvider:
                def get_realtime_quote(self, symbol):
                    return {
                        "symbol": "DEMO001",
                        "price": -10.0,
                        "prev_close": 99.0,
                        "source": "测试",
                        "updated_at": "2026-08-13",
                    }
            return MockProvider()

        monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

        with pytest.raises(QuoteToolError, match="价格非正数"):
            get_latest_quote("DEMO001", data_mode="offline")

    def test_zero_price_raises_error(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        def mock_provider_for_mode(mode):
            class MockProvider:
                def get_realtime_quote(self, symbol):
                    return {
                        "symbol": "DEMO001",
                        "price": 0.0,
                        "prev_close": 99.0,
                        "source": "测试",
                        "updated_at": "2026-08-13",
                    }
            return MockProvider()

        monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

        with pytest.raises(QuoteToolError, match="价格非正数"):
            get_latest_quote("DEMO001", data_mode="offline")

    def test_negative_prev_close_raises_error(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        def mock_provider_for_mode(mode):
            class MockProvider:
                def get_realtime_quote(self, symbol):
                    return {
                        "symbol": "DEMO001",
                        "price": 100.0,
                        "prev_close": -99.0,
                        "source": "测试",
                        "updated_at": "2026-08-13",
                    }
            return MockProvider()

        monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

        with pytest.raises(QuoteToolError, match="昨收价非正数"):
            get_latest_quote("DEMO001", data_mode="offline")

    def test_unparseable_price_raises_error(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        def mock_provider_for_mode(mode):
            class MockProvider:
                def get_realtime_quote(self, symbol):
                    return {
                        "symbol": "DEMO001",
                        "price": "not_a_number",
                        "prev_close": 99.0,
                        "source": "测试",
                        "updated_at": "2026-08-13",
                    }
            return MockProvider()

        monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

        with pytest.raises(QuoteToolError, match="价格无法解析"):
            get_latest_quote("DEMO001", data_mode="offline")


class TestDataStatusValidation:
    """数据状态校验。"""

    def test_invalid_data_status_raises_error(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        def mock_provider_for_mode(mode):
            class MockProvider:
                def get_realtime_quote(self, symbol):
                    return {
                        "symbol": "DEMO001",
                        "price": 100.0,
                        "prev_close": 99.0,
                        "source": "测试",
                        "updated_at": "2026-08-13",
                        "data_status": "invalid_status",
                    }
            return MockProvider()

        monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

        with pytest.raises(QuoteToolError, match="data_status 无效"):
            get_latest_quote("DEMO001", data_mode="offline")

    def test_valid_data_statuses(self, monkeypatch) -> None:
        from agent_platform.finance import quote_tool as module

        valid_statuses = ["live", "offline_sample", "fallback", "delayed", "historical"]

        for status in valid_statuses:
            def mock_provider_for_mode(mode):
                class MockProvider:
                    def get_realtime_quote(self, symbol):
                        return {
                            "symbol": "DEMO001",
                            "price": 100.0,
                            "prev_close": 99.0,
                            "source": "测试",
                            "updated_at": "2026-08-13",
                            "data_status": status,
                        }
                return MockProvider()

            monkeypatch.setattr(module, "provider_for_mode", mock_provider_for_mode)

            quote = get_latest_quote("DEMO001", data_mode="offline")
            assert quote.data_status == status
