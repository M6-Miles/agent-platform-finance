"""SQLite 持久化的本地 MockBroker 会话服务。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_platform.finance.mock_broker import MockBroker, OrderSide
from agent_platform.finance.data_status import resolve_effective_data_mode
from agent_platform.finance.quote_tool import get_latest_quote
from agent_platform.finance.quote_tool import QuoteToolError

if False:  # pragma: no cover - typing only, avoids a runtime dependency cycle
    from agent_platform.core.provider_health import ProviderHealthRegistry


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class PaperBrokerService:
    """账户事实均落 SQLite；服务对象重建后可恢复，不连接真实券商。"""

    LIVE_QUOTE_TTL_SECONDS = 2.0
    OFFLINE_QUOTE_TTL_SECONDS = 300.0
    STALE_QUOTE_MAX_AGE_SECONDS = 900.0

    def __init__(
        self,
        path: Path | str,
        *,
        provider_health: "ProviderHealthRegistry | None" = None,
    ) -> None:
        self.path = Path(path)
        self.provider_health = provider_health
        self._quote_cache: dict[tuple[str, str], tuple[float, object]] = {}
        self._quote_cache_lock = threading.Lock()
        self._order_lock = threading.RLock()
        self.initialize()

    def _get_quote(self, symbol: str, data_mode: str, *, force: bool = False):
        """短时复用服务端已验证报价，避免刷新后下单再次访问公共行情源。"""
        normalized_symbol = symbol.strip().upper()
        effective_mode = resolve_effective_data_mode(normalized_symbol, data_mode)
        key = (normalized_symbol, effective_mode)
        ttl = (
            self.OFFLINE_QUOTE_TTL_SECONDS
            if key[1] == "offline"
            else self.LIVE_QUOTE_TTL_SECONDS
        )
        now = time.monotonic()
        with self._quote_cache_lock:
            cached = self._quote_cache.get(key)
        if not force:
            if cached is not None and now - cached[0] <= ttl:
                if self.provider_health is not None and effective_mode == "auto":
                    self.provider_health.mark_cache_hit(
                        "market_quote", source=cached[1].source,
                        data_at=cached[1].updated_at,
                    )
                return cached[1], True, round(now - cached[0], 3)

        if (
            effective_mode == "auto"
            and self.provider_health is not None
            and not self.provider_health.can_attempt("market_quote")
        ):
            if cached is not None and now - cached[0] <= self.STALE_QUOTE_MAX_AGE_SECONDS:
                stale = replace(
                    cached[1], data_status="delayed",
                    source=f"{cached[1].source}（最近成功缓存）",
                    fallback_reason="实时行情源处于指数退避期，暂用最近一次成功报价",
                )
                self.provider_health.mark_cache_hit(
                    "market_quote", source=stale.source, data_at=stale.updated_at,
                )
                return stale, True, round(now - cached[0], 3)
            raise QuoteToolError("行情源连续失败，正在指数退避，请稍后重试")

        started = time.perf_counter()
        try:
            quote = get_latest_quote(key[0], data_mode=key[1])
        except Exception as exc:
            if self.provider_health is not None and effective_mode == "auto":
                self.provider_health.record_failure(
                    "market_quote", (time.perf_counter() - started) * 1000, exc
                )
            if cached is not None and now - cached[0] <= self.STALE_QUOTE_MAX_AGE_SECONDS:
                stale = replace(
                    cached[1], data_status="delayed",
                    source=f"{cached[1].source}（最近成功缓存）",
                    fallback_reason=f"实时行情获取失败（{type(exc).__name__}），暂用最近成功报价",
                )
                if self.provider_health is not None:
                    self.provider_health.mark_cache_hit(
                        "market_quote", source=stale.source, data_at=stale.updated_at,
                    )
                return stale, True, round(now - cached[0], 3)
            raise
        stored_at = time.monotonic()
        with self._quote_cache_lock:
            self._quote_cache[key] = (stored_at, quote)
        if self.provider_health is not None and effective_mode == "auto":
            status = "real_time" if quote.data_status == "live" else "delayed"
            self.provider_health.record_success(
                "market_quote", (time.perf_counter() - started) * 1000,
                status=status, source=quote.source, data_at=quote.updated_at,
            )
        return quote, False, 0.0

    def get_quote(
        self, symbol: str, data_mode: str = "auto", *, force_refresh: bool = False,
    ) -> dict:
        quote, cache_hit, quote_age_s = self._get_quote(
            symbol, data_mode, force=force_refresh,
        )
        result = quote.to_dict()
        result.update({
            "quote_cache_hit": cache_hit,
            "quote_age_s": quote_age_s,
            "delivery_status": (
                "cached" if cache_hit and quote.data_status == "delayed"
                else "cache" if cache_hit else
                "real_time" if quote.data_status == "live" else
                "offline_sample" if quote.data_status == "offline_sample" else "degraded"
            ),
            "cache_time": quote.updated_at if cache_hit else None,
        })
        return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS paper_broker_accounts (
                    id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_trading_runs (
                    id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_order_requests (
                    account_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, request_id)
                );"""
            )

    def create_account(self, initial_cash: float = 1_000_000.0) -> dict:
        if initial_cash <= 0:
            raise ValueError("initial_cash 必须为正")
        account_id = str(uuid4())
        broker = MockBroker(initial_cash=initial_cash)
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO paper_broker_accounts VALUES (?, ?, ?, ?)",
                (account_id, json.dumps(broker.export_state()), timestamp, timestamp),
            )
        return self._view(account_id, broker, timestamp)

    def get_account(self, account_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json, updated_at FROM paper_broker_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"模拟盘账户不存在: {account_id}")
        return self._view(account_id, MockBroker.from_state(json.loads(row["state_json"])), row["updated_at"])

    def _place_order_legacy(
        self,
        account_id: str,
        *,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: float | None,
        data_mode: str,
        request_id: str | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> dict:
        normalized_request_id = (request_id or "").strip() or None
        with self._order_lock:
            if normalized_request_id:
                replay = self._load_order_response(account_id, normalized_request_id)
                if replay is not None:
                    replay["idempotent_replay"] = True
                    return replay

            broker = self._load(account_id)
            quote, cache_hit, quote_age_s = self._get_quote(symbol, data_mode)
            direction = OrderSide(side)
            if order_type == "market":
                order = broker.place_market_order(symbol, direction, quantity)
            elif order_type == "limit":
                if limit_price is None:
                    raise ValueError("限价单必须提供 limit_price")
                order = broker.place_limit_order(symbol, direction, quantity, limit_price)
            else:
                raise ValueError(f"未知 order_type: {order_type}")
            broker.tick(symbol, quote.price)
            if direction == OrderSide.BUY and order.status.value == "filled" and (
                stop_loss_price is not None or take_profit_price is not None
            ):
                if stop_loss_price is None or take_profit_price is None:
                    raise ValueError("止损价和止盈价必须同时提供")
                broker.set_position_protection(
                    symbol,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                )
            view = self._save(account_id, broker)
            view["submitted_order_id"] = order.order_id
            view["submitted_order_status"] = order.status.value
            view["submitted_order_reject_reason"] = order.reject_reason
            view["submitted_order_filled_price"] = order.filled_price
            view["submitted_order_filled_quantity"] = order.filled_quantity
            view["quote"] = quote.to_dict()
            view["quote_cache_hit"] = cache_hit
            view["quote_age_s"] = quote_age_s
            view["idempotent_replay"] = False
            if normalized_request_id:
                self._save_order_response(
                    account_id, normalized_request_id, view
                )
            return view

    def place_order(
        self,
        account_id: str,
        *,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: float | None,
        data_mode: str,
        request_id: str | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> dict:
        """Atomically apply an idempotent paper order across processes."""
        normalized_request_id = (request_id or "").strip() or None
        quote, cache_hit, quote_age_s = self._get_quote(symbol, data_mode)
        with self._order_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_request_id:
                replay_row = connection.execute(
                    "SELECT response_json FROM paper_order_requests "
                    "WHERE account_id = ? AND request_id = ?",
                    (account_id, normalized_request_id),
                ).fetchone()
                if replay_row is not None:
                    replay = json.loads(replay_row["response_json"])
                    replay["idempotent_replay"] = True
                    return replay

            row = connection.execute(
                "SELECT state_json FROM paper_broker_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"模拟盘账户不存在: {account_id}")
            broker = MockBroker.from_state(json.loads(row["state_json"]))
            direction = OrderSide(side)
            if order_type == "market":
                order = broker.place_market_order(symbol, direction, quantity)
            elif order_type == "limit":
                if limit_price is None:
                    raise ValueError("限价单必须提供 limit_price")
                order = broker.place_limit_order(symbol, direction, quantity, limit_price)
            else:
                raise ValueError(f"未知 order_type: {order_type}")
            broker.tick(symbol, quote.price)
            if direction == OrderSide.BUY and order.status.value == "filled" and (
                stop_loss_price is not None or take_profit_price is not None
            ):
                if stop_loss_price is None or take_profit_price is None:
                    raise ValueError("止损价和止盈价必须同时提供")
                broker.set_position_protection(
                    symbol,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                )
            timestamp = _now()
            connection.execute(
                "UPDATE paper_broker_accounts SET state_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(broker.export_state()), timestamp, account_id),
            )
            view = self._view(account_id, broker, timestamp)
            view.update({
                "submitted_order_id": order.order_id,
                "submitted_order_status": order.status.value,
                "submitted_order_reject_reason": order.reject_reason,
                "submitted_order_filled_price": order.filled_price,
                "submitted_order_filled_quantity": order.filled_quantity,
                "quote": quote.to_dict(),
                "quote_cache_hit": cache_hit,
                "quote_age_s": quote_age_s,
                "idempotent_replay": False,
            })
            if normalized_request_id:
                connection.execute(
                    "INSERT INTO paper_order_requests VALUES (?, ?, ?, ?)",
                    (account_id, normalized_request_id, json.dumps(view, ensure_ascii=False), timestamp),
                )
            return view

    def _load_order_response(self, account_id: str, request_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT response_json FROM paper_order_requests "
                "WHERE account_id = ? AND request_id = ?",
                (account_id, request_id),
            ).fetchone()
        return json.loads(row["response_json"]) if row is not None else None

    def _save_order_response(
        self, account_id: str, request_id: str, response: dict
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO paper_order_requests VALUES (?, ?, ?, ?)",
                (
                    account_id,
                    request_id,
                    json.dumps(response, ensure_ascii=False),
                    _now(),
                ),
            )

    def refresh(
        self,
        account_id: str,
        *,
        symbols: list[str],
        data_mode: str,
        force_refresh: bool = False,
    ) -> dict:
        broker = self._load(account_id)
        targets = sorted(
            {symbol.strip().upper() for symbol in symbols if symbol.strip()}
            | set(broker.get_positions())
        )
        quotes: dict[str, dict] = {}
        errors: dict[str, str] = {}
        fetched: dict[str, object] = {}

        def fetch(symbol: str):
            quote, cache_hit, quote_age_s = self._get_quote(
                symbol, data_mode, force=force_refresh
            )
            return quote, cache_hit, quote_age_s

        # Public quote providers are independent per symbol. Bound concurrency to
        # avoid serial latency without flooding an upstream source.
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as executor:
            futures = {executor.submit(fetch, symbol): symbol for symbol in targets}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    fetched[symbol] = future.result()
                except Exception as exc:  # noqa: BLE001 - isolate each symbol
                    errors[symbol] = f"{type(exc).__name__}: {exc}"

        # MockBroker is deliberately mutated on one thread, then persisted once.
        for symbol in targets:
            if symbol not in fetched:
                continue
            try:
                quote, cache_hit, quote_age_s = fetched[symbol]
                broker.tick(symbol, quote.price)
                quotes[symbol] = {
                    **quote.to_dict(),
                    "quote_cache_hit": cache_hit,
                    "quote_age_s": quote_age_s,
                    "delivery_status": (
                        "cached" if cache_hit and quote.data_status == "delayed"
                        else "cache" if cache_hit else
                        "real_time" if quote.data_status == "live" else
                        "offline_sample" if quote.data_status == "offline_sample" else "degraded"
                    ),
                    "cache_time": quote.updated_at if cache_hit else None,
                }
            except Exception as exc:  # noqa: BLE001 - 每个标的独立报告，不伪造价格
                errors[symbol] = f"{type(exc).__name__}: {exc}"
        view = self._save(account_id, broker)
        view.update({
            "quotes": quotes,
            "quote_errors": errors,
            "force_refresh": force_refresh,
        })
        return view

    def run_continuous(self, **kwargs) -> dict:
        from agent_platform.finance.paper_trading_session import run_paper_trading_session

        result = run_paper_trading_session(**kwargs).to_dict()
        run_id = str(uuid4())
        result["run_id"] = run_id
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO paper_trading_runs VALUES (?, ?, ?)",
                (run_id, json.dumps(result, ensure_ascii=False), _now()),
            )
        return result

    def get_run(self, run_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM paper_trading_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"连续模拟盘运行不存在: {run_id}")
        return json.loads(row["result_json"])

    def _load(self, account_id: str) -> MockBroker:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM paper_broker_accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"模拟盘账户不存在: {account_id}")
        return MockBroker.from_state(json.loads(row["state_json"]))

    def _save(self, account_id: str, broker: MockBroker) -> dict:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE paper_broker_accounts SET state_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(broker.export_state()), timestamp, account_id),
            )
        return self._view(account_id, broker, timestamp)

    @staticmethod
    def _view(account_id: str, broker: MockBroker, updated_at: str) -> dict:
        state = broker.export_state()
        return {
            "account_id": account_id,
            **broker.summary(),
            "positions": state["positions"],
            "orders": state["orders"],
            "trades": state["trade_history"],
            "updated_at": updated_at,
            "broker_kind": "MockBroker(本地模拟撮合，无券商连接)",
        }
