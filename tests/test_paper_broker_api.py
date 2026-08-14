"""持久化 MockBroker 服务和 API 契约测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.finance.paper_broker_service import PaperBrokerService
from agent_platform.finance.quote_tool import QuotePayload


def _quote(symbol: str = "DEMO001") -> QuotePayload:
    return QuotePayload(
        symbol=symbol,
        name="测试证券",
        price=100.0,
        prev_close=99.0,
        change_pct=1.01,
        market="测试市场",
        source="测试可信行情",
        updated_at="2026-08-12T10:00:00Z",
        data_status="live",
        fallback_reason=None,
    )


def test_service_recovers_account_after_rebuild(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    service = PaperBrokerService(path)
    account = service.create_account(100_000)
    account_id = account["account_id"]
    service.place_order(
        account_id,
        symbol="DEMO001",
        side="buy",
        quantity=100,
        order_type="market",
        limit_price=None,
        data_mode="offline",
    )

    recovered = PaperBrokerService(path).get_account(account_id)
    assert recovered["positions"][0]["symbol"] == "DEMO001"
    assert recovered["positions"][0]["quantity"] == 100
    assert recovered["cash"] < 100_000


def test_api_account_order_and_recovery(monkeypatch, tmp_path) -> None:
    from agent_platform.api import main as api_main
    from agent_platform.config import Settings
    from agent_platform.services.application_service import ApplicationService

    settings = Settings(
        sqlite_path=tmp_path / "api.sqlite3",
        market_data_provider="sample",
        llm_provider="mock",
        langgraph_use_memory_saver=True,
    )
    service = ApplicationService(settings=settings)
    monkeypatch.setattr(api_main, "_app_service", service)
    client = TestClient(api_main.app)

    created = client.post("/paper-trading/accounts", json={"initial_cash": 100000}).json()
    account_id = created["account_id"]
    response = client.post(
        f"/paper-trading/accounts/{account_id}/orders",
        json={
            "symbol": "DEMO001",
            "side": "buy",
            "quantity": 100,
            "order_type": "market",
            "data_mode": "offline",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["broker_kind"].startswith("MockBroker")
    assert body["positions"][0]["quantity"] == 100
    assert body["quote"]["data_status"] == "offline_sample"

    recovered = client.get(f"/paper-trading/accounts/{account_id}")
    assert recovered.status_code == 200
    assert recovered.json()["positions"][0]["quantity"] == 100
    service.close()


def test_continuous_run_is_persisted(tmp_path) -> None:
    service = PaperBrokerService(tmp_path / "runs.sqlite3")
    result = service.run_continuous(
        symbols=["DEMO001"], data_mode="offline", days=5, initial_cash=100_000
    )
    recovered = PaperBrokerService(tmp_path / "runs.sqlite3").get_run(result["run_id"])
    assert recovered["trading_days"] == 5
    assert recovered["broker_kind"].startswith("MockBroker")
    assert len(recovered["equity_curve"]) == 5


def test_recent_server_quote_is_reused_for_order(monkeypatch, tmp_path) -> None:
    from agent_platform.finance import paper_broker_service as module

    calls: list[str] = []

    def fake_quote(symbol: str, *, data_mode: str):
        calls.append(symbol)
        return _quote(symbol)

    monkeypatch.setattr(module, "get_latest_quote", fake_quote)
    service = PaperBrokerService(tmp_path / "cache.sqlite3")
    account_id = service.create_account(100_000)["account_id"]

    first = service.get_quote("DEMO001", "auto")
    order = service.place_order(
        account_id,
        symbol="DEMO001",
        side="buy",
        quantity=100,
        order_type="market",
        limit_price=None,
        data_mode="auto",
        request_id="request-cache",
    )

    assert first["quote_cache_hit"] is False
    assert order["quote_cache_hit"] is True
    assert calls == ["DEMO001"]


def test_service_refresh_executes_persisted_take_profit(monkeypatch, tmp_path) -> None:
    from agent_platform.finance import paper_broker_service as module

    price = {"value": 100.0}

    def fake_quote(symbol: str, *, data_mode: str):
        quote = _quote(symbol)
        return QuotePayload(
            symbol=quote.symbol, name=quote.name, price=price["value"],
            prev_close=quote.prev_close, change_pct=quote.change_pct,
            market=quote.market, source=quote.source, updated_at=quote.updated_at,
            data_status=quote.data_status, fallback_reason=quote.fallback_reason,
        )

    monkeypatch.setattr(module, "get_latest_quote", fake_quote)
    service = PaperBrokerService(tmp_path / "take-profit.sqlite3")
    account_id = service.create_account(100_000)["account_id"]
    service.place_order(
        account_id, symbol="DEMO001", side="buy", quantity=100,
        order_type="market", limit_price=None, data_mode="auto",
        request_id="protected-buy", stop_loss_price=90.0,
        take_profit_price=110.0,
    )

    price["value"] = 111.0
    service.refresh(
        account_id, symbols=["DEMO001"], data_mode="auto", force_refresh=True
    )
    recovered = PaperBrokerService(tmp_path / "take-profit.sqlite3").get_account(account_id)

    assert recovered["positions"] == []
    assert recovered["trades"][-1]["trigger_reason"] == "take_profit"


def test_force_quote_refresh_bypasses_live_cache(monkeypatch, tmp_path) -> None:
    from agent_platform.finance import paper_broker_service as module

    calls: list[str] = []

    def fake_quote(symbol: str, *, data_mode: str):
        calls.append(symbol)
        return _quote(symbol)

    monkeypatch.setattr(module, "get_latest_quote", fake_quote)
    service = PaperBrokerService(tmp_path / "force-quote.sqlite3")
    first = service.get_quote("600519", "auto")
    forced = service.get_quote("600519", "auto", force_refresh=True)

    assert first["quote_cache_hit"] is False
    assert forced["quote_cache_hit"] is False
    assert calls == ["600519", "600519"]


def test_failed_live_refresh_uses_explicit_stale_cache_and_keeps_backoff(monkeypatch, tmp_path) -> None:
    from agent_platform.core.provider_health import ProviderHealthRegistry
    from agent_platform.finance import paper_broker_service as module

    registry = ProviderHealthRegistry()
    monkeypatch.setattr(module, "get_latest_quote", lambda symbol, *, data_mode: _quote(symbol))
    service = PaperBrokerService(
        tmp_path / "stale-cache.sqlite3", provider_health=registry
    )
    service.get_quote("600519", "auto")
    monkeypatch.setattr(
        module, "get_latest_quote",
        lambda symbol, *, data_mode: (_ for _ in ()).throw(ConnectionError("down")),
    )

    cached = service.get_quote("600519", "auto", force_refresh=True)

    assert cached["delivery_status"] == "cached"
    assert cached["data_status"] == "delayed"
    assert cached["cache_time"]
    assert cached["fallback_reason"]
    assert registry.can_attempt("market_quote") is False


def test_order_request_id_is_idempotent(monkeypatch, tmp_path) -> None:
    from agent_platform.finance import paper_broker_service as module

    monkeypatch.setattr(
        module, "get_latest_quote", lambda symbol, *, data_mode: _quote(symbol)
    )
    service = PaperBrokerService(tmp_path / "idempotent.sqlite3")
    account_id = service.create_account(100_000)["account_id"]
    kwargs = dict(
        symbol="DEMO001",
        side="buy",
        quantity=100,
        order_type="market",
        limit_price=None,
        data_mode="auto",
        request_id="same-request",
    )

    first = service.place_order(account_id, **kwargs)
    replay = service.place_order(account_id, **kwargs)

    assert replay["idempotent_replay"] is True
    assert replay["submitted_order_id"] == first["submitted_order_id"]
    account = service.get_account(account_id)
    assert account["total_trades"] == 1
    assert account["positions"][0]["quantity"] == 100


def test_concurrent_orders_do_not_lose_account_updates(monkeypatch, tmp_path) -> None:
    from agent_platform.finance import paper_broker_service as module

    monkeypatch.setattr(
        module, "get_latest_quote", lambda symbol, *, data_mode: _quote(symbol)
    )
    path = tmp_path / "concurrent.sqlite3"
    account_id = PaperBrokerService(path).create_account(100_000)["account_id"]

    def submit(index: int) -> dict:
        return PaperBrokerService(path).place_order(
            account_id, symbol="DEMO001", side="buy", quantity=100,
            order_type="market", limit_price=None, data_mode="auto",
            request_id=f"concurrent-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, range(2)))

    assert all(result["submitted_order_status"] == "filled" for result in results)
    account = PaperBrokerService(path).get_account(account_id)
    assert account["positions"][0]["quantity"] == 200
    assert account["total_trades"] == 2


def test_insufficient_cash_returns_rejected_status(monkeypatch, tmp_path) -> None:
    from agent_platform.finance import paper_broker_service as module

    monkeypatch.setattr(
        module, "get_latest_quote", lambda symbol, *, data_mode: _quote(symbol)
    )
    service = PaperBrokerService(tmp_path / "rejected.sqlite3")
    account_id = service.create_account(100_000)["account_id"]

    result = service.place_order(
        account_id,
        symbol="DEMO001",
        side="buy",
        quantity=1000,
        order_type="market",
        limit_price=None,
        data_mode="auto",
        request_id="too-expensive",
    )

    assert result["submitted_order_status"] == "rejected"
    assert "资金不足" in result["submitted_order_reject_reason"]
    assert result["submitted_order_filled_quantity"] == 0
    assert result["positions"] == []
    assert result["total_trades"] == 0


def test_auto_refresh_reuses_cache_but_manual_refresh_forces_quote(monkeypatch, tmp_path) -> None:
    from agent_platform.finance import paper_broker_service as module

    calls: list[str] = []

    def fake_quote(symbol: str, *, data_mode: str):
        calls.append(f"{symbol}:{data_mode}")
        return _quote(symbol)

    monkeypatch.setattr(module, "get_latest_quote", fake_quote)
    service = PaperBrokerService(tmp_path / "refresh-cache.sqlite3")
    account_id = service.create_account(100_000)["account_id"]

    first = service.refresh(
        account_id, symbols=["DEMO001"], data_mode="auto", force_refresh=False
    )
    cached = service.refresh(
        account_id, symbols=["DEMO001"], data_mode="offline", force_refresh=False
    )
    forced = service.refresh(
        account_id, symbols=["DEMO001"], data_mode="auto", force_refresh=True
    )

    assert first["quotes"]["DEMO001"]["quote_cache_hit"] is False
    assert cached["quotes"]["DEMO001"]["quote_cache_hit"] is True
    assert forced["quotes"]["DEMO001"]["quote_cache_hit"] is False
    assert calls == ["DEMO001:offline", "DEMO001:offline"]


def test_multi_symbol_refresh_fetches_concurrently(monkeypatch, tmp_path) -> None:
    from agent_platform.finance import paper_broker_service as module

    barrier = threading.Barrier(2, timeout=2)

    def fake_quote(symbol: str, *, data_mode: str):
        barrier.wait()
        return _quote(symbol)

    monkeypatch.setattr(module, "get_latest_quote", fake_quote)
    service = PaperBrokerService(tmp_path / "refresh-concurrent.sqlite3")
    account_id = service.create_account(100_000)["account_id"]
    result = service.refresh(
        account_id,
        symbols=["DEMO001", "DEMO002"],
        data_mode="offline",
        force_refresh=True,
    )
    assert set(result["quotes"]) == {"DEMO001", "DEMO002"}
    assert result["quote_errors"] == {}


def test_frontend_uses_completion_based_broker_refresh_scheduler() -> None:
    html = Path("frontend_prototype.html").read_text(encoding="utf-8")
    assert 'id="broker-auto-refresh"' in html
    assert 'id="broker-refresh-interval"' in html
    assert "setTimeout(async () =>" in html
    assert "pushTick({ manual: false })" in html
    assert "force_refresh:false" in html
    assert "brokerAutoRefreshTimer = setInterval" not in html
    assert "获取单股实时报价" in html
    assert "force_refresh=${forceRefresh}" in html
    assert "AbortSignal.timeout(5000)" in html
    assert "公共行情源 5 秒内未响应，本次获取已停止" in html
    assert "stopBrokerAutoRefresh('正在获取单股报价')" in html
    assert "不允许离线样例价和联网报价跨模式复用" in html
    assert "broker.tickers = {}" in html
    assert "尚未加载数据，请设置参数后点击" in html
    assert "推送行情" not in html
