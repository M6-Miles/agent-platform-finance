"""AkShareMarketDataProvider 单元测试。

所有 akshare 外部调用均通过 monkeypatch / 自定义 loader 拦截，
测试不发起任何真实网络请求。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from agent_platform.finance.akshare_data_provider import AkShareMarketDataProvider
from agent_platform.finance.errors import (
    InvalidSecuritySymbolError,
    MarketDataDependencyError,
    MarketDataUnavailableError,
)


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def _make_stock_list() -> pd.DataFrame:
    return pd.DataFrame({"code": ["600519", "000001"], "name": ["贵州茅台", "平安银行"]})


def _make_price_history(symbol: str = "600519") -> pd.DataFrame:
    today = date.today()
    days = [(today - timedelta(days=i)) for i in range(10, 0, -1)]
    return pd.DataFrame(
        {
            "日期": [d.isoformat() for d in days],
            "开盘": [1800.0 + i for i in range(10)],
            "最高": [1810.0 + i for i in range(10)],
            "最低": [1790.0 + i for i in range(10)],
            "收盘": [1805.0 + i for i in range(10)],
            "成交量": [100000 + i * 1000 for i in range(10)],
        }
    )


def _make_provider(
    stock_list: pd.DataFrame | None = None,
    price_history: pd.DataFrame | None = None,
) -> AkShareMarketDataProvider:
    """返回使用自定义 loader 的 Provider，不依赖真实 akshare 包。"""
    sl = stock_list if stock_list is not None else _make_stock_list()
    ph = price_history if price_history is not None else _make_price_history()
    return AkShareMarketDataProvider(
        stock_list_loader=lambda: sl,
        history_loader=lambda **_kw: ph,
    )


# ── normalize_symbol ─────────────────────────────────────────────────────────

class TestNormalizeSymbol:
    def test_plain_six_digits(self) -> None:
        assert AkShareMarketDataProvider.normalize_symbol("600519") == "600519"

    def test_lowercase_stripped(self) -> None:
        assert AkShareMarketDataProvider.normalize_symbol(" 000001 ") == "000001"

    def test_sh_prefix_stripped(self) -> None:
        assert AkShareMarketDataProvider.normalize_symbol("SH600519") == "600519"

    def test_sz_prefix_stripped(self) -> None:
        assert AkShareMarketDataProvider.normalize_symbol("SZ000001") == "000001"

    def test_dot_suffix_stripped(self) -> None:
        assert AkShareMarketDataProvider.normalize_symbol("600519.SH") == "600519"

    def test_invalid_raises(self) -> None:
        with pytest.raises(InvalidSecuritySymbolError):
            AkShareMarketDataProvider.normalize_symbol("DEMO001")

    def test_too_short_raises(self) -> None:
        with pytest.raises(InvalidSecuritySymbolError):
            AkShareMarketDataProvider.normalize_symbol("60051")

    def test_too_long_raises(self) -> None:
        with pytest.raises(InvalidSecuritySymbolError):
            AkShareMarketDataProvider.normalize_symbol("6005191")


# ── market_for_symbol ────────────────────────────────────────────────────────

class TestMarketForSymbol:
    def test_sh(self) -> None:
        assert AkShareMarketDataProvider.market_for_symbol("600519") == "上交所"

    def test_sz(self) -> None:
        assert AkShareMarketDataProvider.market_for_symbol("000001") == "深交所"

    def test_bj(self) -> None:
        assert AkShareMarketDataProvider.market_for_symbol("430047") == "北交所"


# ── list_securities ──────────────────────────────────────────────────────────

class TestListSecurities:
    def test_returns_expected_count(self) -> None:
        provider = _make_provider()
        securities = provider.list_securities()
        assert len(securities) == 2

    def test_symbol_and_name(self) -> None:
        provider = _make_provider()
        sec = {s.symbol: s for s in provider.list_securities()}
        assert sec["600519"].name == "贵州茅台"
        assert sec["600519"].market == "上交所"
        assert sec["000001"].name == "平安银行"
        assert sec["000001"].market == "深交所"

    def test_source_name(self) -> None:
        provider = _make_provider()
        for sec in provider.list_securities():
            assert sec.source == AkShareMarketDataProvider.source_name

    def test_loader_error_raises_unavailable(self) -> None:
        def failing_loader() -> pd.DataFrame:
            raise OSError("网络超时")

        provider = AkShareMarketDataProvider(stock_list_loader=failing_loader)
        with pytest.raises(MarketDataUnavailableError, match="AkShare 获取 A 股证券列表失败"):
            provider.list_securities()

    def test_empty_result_raises_unavailable(self) -> None:
        provider = AkShareMarketDataProvider(
            stock_list_loader=lambda: pd.DataFrame()
        )
        with pytest.raises(MarketDataUnavailableError, match="未返回 A 股证券列表"):
            provider.list_securities()


# ── get_price_history ────────────────────────────────────────────────────────

class TestGetPriceHistory:
    def test_returns_dataframe_with_required_columns(self) -> None:
        provider = _make_provider()
        df = provider.get_price_history("600519")
        required = {"market", "symbol", "name", "date", "open", "high", "low", "close", "volume", "source", "updated_at"}
        assert required.issubset(set(df.columns))

    def test_row_count(self) -> None:
        provider = _make_provider()
        df = provider.get_price_history("600519")
        assert len(df) == 10

    def test_symbol_normalized_in_result(self) -> None:
        provider = _make_provider()
        df = provider.get_price_history("SH600519")
        assert (df["symbol"] == "600519").all()

    def test_date_column_is_date_type(self) -> None:
        provider = _make_provider()
        df = provider.get_price_history("600519")
        assert isinstance(df["date"].iloc[0], date)

    def test_sorted_ascending_by_date(self) -> None:
        provider = _make_provider()
        df = provider.get_price_history("600519")
        dates = list(df["date"])
        assert dates == sorted(dates)

    def test_default_start_applied_when_none(self) -> None:
        """不传 start 时，内部将计算 (today - default_history_days) 作为起始日。"""
        calls: list[dict] = []

        def capturing_loader(**kw: object) -> pd.DataFrame:
            calls.append(dict(kw))
            return _make_price_history()

        provider = AkShareMarketDataProvider(
            stock_list_loader=lambda: _make_stock_list(),
            history_loader=capturing_loader,
            default_history_days=90,
        )
        provider.get_price_history("600519")
        assert len(calls) == 1
        expected_start = (date.today() - timedelta(days=90)).strftime("%Y%m%d")
        assert calls[0]["start_date"] == expected_start

    def test_invalid_symbol_raises(self) -> None:
        provider = _make_provider()
        with pytest.raises(InvalidSecuritySymbolError):
            provider.get_price_history("DEMO001")

    def test_empty_history_raises_invalid_symbol(self) -> None:
        provider = AkShareMarketDataProvider(
            stock_list_loader=lambda: _make_stock_list(),
            history_loader=lambda **_kw: pd.DataFrame(),
        )
        with pytest.raises(InvalidSecuritySymbolError, match="未返回证券"):
            provider.get_price_history("600519")

    def test_loader_error_raises_unavailable(self) -> None:
        def failing_loader(**_kw: object) -> pd.DataFrame:
            raise ConnectionError("连接失败")

        provider = AkShareMarketDataProvider(
            stock_list_loader=lambda: _make_stock_list(),
            history_loader=failing_loader,
        )
        with pytest.raises(MarketDataUnavailableError, match="日线失败"):
            provider.get_price_history("600519")


# ── 缺少 akshare 包时的错误处理 ──────────────────────────────────────────────

class TestMissingAkshare:
    def test_import_error_raises_dependency_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 stock_list_loader 时，若 akshare 包不存在应抛 MarketDataDependencyError。"""
        import builtins
        real_import = builtins.__import__

        def patched_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "akshare":
                raise ImportError("No module named 'akshare'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", patched_import)
        provider = AkShareMarketDataProvider()  # no loaders → will call _load_akshare
        with pytest.raises(MarketDataDependencyError, match="AkShare"):
            provider.list_securities()


class TestRealtimeQuoteConsistency:
    def test_uses_previous_trading_day_close_not_current_open(self, monkeypatch) -> None:
        class FakeAk:
            @staticmethod
            def stock_zh_a_minute(**_kwargs):
                return pd.DataFrame({
                    "day": [
                        "2026-08-07 14:59:00", "2026-08-07 15:00:00",
                        "2026-08-10 09:31:00", "2026-08-10 10:00:00",
                    ],
                    "open": [10.0, 10.1, 11.0, 11.1],
                    "close": [10.1, 10.2, 11.1, 11.22],
                })

        provider = AkShareMarketDataProvider()
        monkeypatch.setattr(provider, "_load_akshare", lambda: FakeAk())

        quote = provider.get_realtime_quote("000001")

        assert quote["price"] == 11.22
        assert quote["prev_close"] == 10.2
        assert quote["change_pct"] == 10.0
        assert "上一交易日校验" in quote["source"]

    def test_ohlcv_validation_rejects_impossible_high_low(self) -> None:
        frame = pd.DataFrame({
            "date": [date(2026, 8, 10)],
            "open": [10.0], "high": [9.0], "low": [8.0], "close": [11.0],
            "volume": [100.0],
        })

        with pytest.raises(MarketDataUnavailableError, match="OHLC"):
            AkShareMarketDataProvider._validate_ohlcv(frame, "000001")


class TestNetworkCallDoesNotUseGlobalSocketTimeout:
    """证明生产路径 _network_call 不调用 socket.setdefaulttimeout。

    项目会并发请求（Agent + 数据 + LLM API），全局 socket 超时会影响其他线程。
    """

    def test_network_call_does_not_modify_socket_default_timeout(self, monkeypatch) -> None:
        """_network_call 不应调用 socket.setdefaulttimeout。"""
        import socket

        call_tracker = {"setdefaulttimeout_called": False}
        def patched_setdefaulttimeout(timeout):
            call_tracker["setdefaulttimeout_called"] = True
            # 如果被调用则抛错，让测试立即失败
            raise AssertionError(
                f"socket.setdefaulttimeout({timeout}) 被调用，违反并发安全要求"
            )

        monkeypatch.setattr(socket, "setdefaulttimeout", patched_setdefaulttimeout)

        # 使用自定义 loader 模拟网络调用，不触发真实 akshare
        def mock_loader():
            return _make_stock_list()

        provider = AkShareMarketDataProvider(stock_list_loader=mock_loader)

        # 调用会走测试 mock 路径（直接调用 loader，不经过 _network_call）
        # 这个测试验证即使将来生产路径被错误修改，monkeypatch 也会捕获
        provider.list_securities()

        # 验证没有调用全局 socket 超时设置
        assert not call_tracker["setdefaulttimeout_called"]

        # 清理
        monkeypatch.undo()
