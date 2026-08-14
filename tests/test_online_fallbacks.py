from __future__ import annotations

import importlib

import pandas as pd
import pytest

from agent_platform.finance.data_status import (
    MarketDataAllSourcesFailed,
    fetch_price_history,
)
from agent_platform.mcp import akshare_tools


def test_explicit_environment_wins_over_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    import dotenv
    from agent_platform import config

    calls: list[bool] = []

    def fake_load_dotenv(_path, *, override: bool) -> None:
        calls.append(override)
        if override:
            monkeypatch.setenv("LLM_PROVIDER", "deepseek")

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(dotenv, "load_dotenv", fake_load_dotenv)
    reloaded = importlib.reload(config)

    assert calls == [False]
    assert reloaded.get_settings().llm_provider == "mock"


def test_online_acceptance_universe_contains_real_symbols() -> None:
    from Scripts.validate_deliverables import ONLINE_STOCK_UNIVERSE

    assert len(ONLINE_STOCK_UNIVERSE) == 20
    assert len(set(ONLINE_STOCK_UNIVERSE)) == 20
    assert all(len(symbol) == 6 and symbol.isdigit() for symbol in ONLINE_STOCK_UNIVERSE)


def test_real_symbol_failure_does_not_try_offline_sample_or_leak_url() -> None:
    class FailedProvider:
        def get_price_history(self, *_args, **_kwargs):
            raise ConnectionError(
                "HTTPSConnectionPool(host='private.example', url='/secret/path')"
            )

    with pytest.raises(MarketDataAllSourcesFailed) as caught:
        fetch_price_history(
            "000001", data_mode="auto", provider=FailedProvider()
        )

    message = str(caught.value)
    assert "真实行情源暂时无法访问" in message
    assert "未生成模拟价格" in message
    assert "DEMO001" not in message
    assert "private.example" not in message
    assert "/secret/path" not in message


class _FallbackAkShare:
    @staticmethod
    def stock_zh_a_hist(**_kwargs):
        raise ConnectionError("primary unavailable")

    @staticmethod
    def stock_zh_a_hist_tx(**_kwargs):
        return pd.DataFrame(
            [
                {
                    "date": "2026-08-07", "open": 10.0, "high": 10.5,
                    "low": 9.8, "close": 10.2, "volume": 100.0, "amount": 1020.0,
                },
                {
                    "date": "2026-08-10", "open": 10.3, "high": 10.8,
                    "low": 10.1, "close": 10.5, "volume": 120.0, "amount": 1260.0,
                },
            ]
        )

    @staticmethod
    def stock_zh_a_hist_min_em(**_kwargs):
        raise ConnectionError("primary unavailable")

    @staticmethod
    def stock_zh_a_minute(**_kwargs):
        return pd.DataFrame(
            [
                {
                    "day": "2026-08-10 09:31:00", "open": "10.30", "high": "10.40",
                    "low": "10.20", "close": "10.35", "volume": "10", "amount": "103.5",
                },
                {
                    "day": "2026-08-10 09:32:00", "open": "10.35", "high": "10.60",
                    "low": "10.30", "close": "10.50", "volume": "20", "amount": "210.0",
                },
            ]
        )

    @staticmethod
    def stock_zh_valuation_baidu(*, indicator: str, **_kwargs):
        values = {"市盈率(TTM)": 19.8, "市净率": 6.0, "总市值": 16366.32}
        return pd.DataFrame([{"date": "2026-08-09", "value": values[indicator]}])

    @staticmethod
    def stock_individual_info_em(**_kwargs):
        raise ConnectionError("primary unavailable")

    @staticmethod
    def stock_profile_cninfo(**_kwargs):
        return pd.DataFrame(
            [{
                "A股简称": "贵州茅台", "所属行业": "酒、饮料和精制茶制造业",
                "上市日期": "2001-08-27",
            }]
        )


def test_price_history_falls_back_to_tencent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(akshare_tools, "_load_akshare", lambda: _FallbackAkShare())

    result = akshare_tools.get_price_history(
        symbol="600519", start="2026-08-01", end="2026-08-10",
    )

    assert result["ok"] is True
    assert result["source"] == "akshare/stock_zh_a_hist_tx"
    assert result["data"]["rows"] == 2


def test_minute_bars_fall_back_to_tencent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(akshare_tools, "_load_akshare", lambda: _FallbackAkShare())

    result = akshare_tools.get_minute_bars(symbol="600519", period="1")

    assert result["ok"] is True
    assert result["source"] == "akshare/stock_zh_a_minute"
    assert result["data"]["rows"] == 2


def test_realtime_quote_falls_back_with_previous_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(akshare_tools, "_load_akshare", lambda: _FallbackAkShare())
    monkeypatch.setattr(
        akshare_tools, "_fetch_spot_row",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("primary unavailable")),
    )

    result = akshare_tools.get_realtime_quote(symbol="600519")

    assert result["ok"] is True
    assert result["source"] == "akshare/stock_zh_a_minute+stock_zh_a_hist_tx"
    assert result["data"]["price"] == 10.5
    assert result["data"]["prev_close"] == 10.2
    assert result["data"]["change_pct"] == pytest.approx(2.9412)
    assert result["data"]["volume"] == 30.0


def test_valuation_falls_back_to_baidu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(akshare_tools, "_load_akshare", lambda: _FallbackAkShare())
    monkeypatch.setattr(
        akshare_tools, "_fetch_spot_row",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("primary unavailable")),
    )

    result = akshare_tools.get_valuation_metrics(symbol="600519")

    assert result["ok"] is True
    assert result["source"] == "akshare/stock_zh_valuation_baidu"
    assert result["data"]["pe_ttm"] == 19.8
    assert result["data"]["pb"] == 6.0
    assert result["data"]["total_market_value_cny"] == pytest.approx(1_636_632_000_000)


def test_industry_falls_back_to_cninfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(akshare_tools, "_load_akshare", lambda: _FallbackAkShare())

    result = akshare_tools.get_stock_industry(symbol="600519")

    assert result["ok"] is True
    assert result["source"] == "akshare/stock_profile_cninfo"
    assert result["data"]["name"] == "贵州茅台"
    assert result["data"]["industry"] == "酒、饮料和精制茶制造业"


def test_industry_agent_keeps_live_identity_when_flow_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_platform.finance.industry_agent import analyze_industry
    from agent_platform.mcp import registry

    class FakeRegistry:
        def call(self, name: str, **_kwargs):
            if name == "get_stock_industry":
                return {
                    "ok": True,
                    "source": "akshare/stock_profile_cninfo",
                    "data": {"industry": "酒、饮料和精制茶制造业"},
                }
            return {
                "ok": False, "error_type": "UpstreamUnavailable",
                "error": "fund flow unavailable", "data": None,
            }

    monkeypatch.setattr(registry, "get_registry", lambda **_kwargs: FakeRegistry())

    result = analyze_industry("600519")

    assert result.data_status == "live"
    assert result.industry_name == "酒、饮料和精制茶制造业"
    assert result.fund_flow_3d_cny is None
    assert result.top_stocks == []
    assert "UpstreamUnavailable" in (result.fallback_reason or "")
