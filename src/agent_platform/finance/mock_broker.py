"""
MockBroker — 模拟撮合引擎
==========================
提供本地纸面交易（Paper Trading）环境，绝不连接真实券商。
支持：限价单、市价单、撮合成交、持仓 / 盈亏统计。

数量单位约定（全模块唯一口径）
------------------------------
本模块所有 ``quantity`` / ``filled_quantity`` / ``Position.quantity`` 字段
**一律以「股」（shares）为单位**，不使用「手」。
A 股 1 手 = 100 股（``SHARES_PER_LOT``）；若上层 UI 以“手”输入，
必须先调用 :func:`lots_to_shares` 换算后再传入本模块。
成交金额 = 成交价 × 股数，因此价格与数量单位严格匹配。

⚠️  仅供研究参考，不构成投资建议。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

DISCLAIMER = "仅供研究参考，不构成投资建议"

#: A 股每手股数；本模块内部一律按「股」计价与记账
SHARES_PER_LOT = 100

#: 数量单位标识，供 API / 前端读取展示，避免“手/股”歧义
QUANTITY_UNIT = "shares"


def lots_to_shares(lots: int) -> int:
    """手 → 股（1 手 = 100 股）。"""
    if lots <= 0:
        raise ValueError(f"lots 必须为正整数，收到 {lots}")
    return int(lots) * SHARES_PER_LOT


def shares_to_lots(shares: int) -> float:
    """股 → 手（可能为小数，A 股零股不足 1 手）。"""
    if shares < 0:
        raise ValueError(f"shares 不能为负，收到 {shares}")
    return shares / SHARES_PER_LOT


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass
class Order:
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: Optional[float]
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_quantity: int = 0
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    filled_at: Optional[str] = None
    reject_reason: Optional[str] = None
    trigger_reason: Optional[str] = None


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float = 0.0
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_cost) * self.quantity

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.avg_cost <= 0:
            return 0.0
        return (self.current_price / self.avg_cost - 1) * 100


class MockBroker:
    """
    模拟经纪商 / 纸面交易撮合引擎。

    Parameters
    ----------
    initial_cash : float
        初始资金（人民币元），默认 100 万。
    commission_pct : float
        佣金率，默认 0.03%（双边）。
    slippage_pct : float
        滑点，默认 0.1%（单边）。
    """

    def __init__(
        self,
        initial_cash: float = 1_000_000.0,
        commission_pct: float = 0.03,
        slippage_pct: float = 0.1,
    ) -> None:
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.commission_pct = commission_pct / 100
        self.slippage_pct = slippage_pct / 100

        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._order_counter = 0
        self._trade_history: list[dict] = []

    # ── 下单 ─────────────────────────────────────────────────────────────────

    def _new_order_id(self) -> str:
        self._order_counter += 1
        return f"ORD{self._order_counter:06d}"

    def place_market_order(self, symbol: str, side: OrderSide, quantity: int) -> Order:
        """提交市价单；立即以 0 价格暂挂（需调用 tick() 撮合）。

        ``quantity`` 单位为「股」（shares），不是「手」。
        """
        if quantity <= 0:
            raise ValueError(f"quantity 必须为正整数（单位：股），收到 {quantity}")
        order = Order(
            order_id=self._new_order_id(),
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            limit_price=None,
        )
        self._orders[order.order_id] = order
        logger.debug("place_market_order: %s", order)
        return order

    def place_limit_order(
        self, symbol: str, side: OrderSide, quantity: int, limit_price: float
    ) -> Order:
        """提交限价单；tick() 时若价格满足则撮合。

        ``quantity`` 单位为「股」（shares），不是「手」。
        """
        if quantity <= 0:
            raise ValueError(f"quantity 必须为正整数（单位：股），收到 {quantity}")
        if limit_price <= 0:
            raise ValueError(f"limit_price 必须为正，收到 {limit_price}")
        order = Order(
            order_id=self._new_order_id(),
            symbol=symbol,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=limit_price,
        )
        self._orders[order.order_id] = order
        logger.debug("place_limit_order: %s", order)
        return order

    # ── 撮合（Tick）─────────────────────────────────────────────────────────

    def tick(self, symbol: str, market_price: float) -> list[Order]:
        """
        接受行情报价，撮合该标的所有挂单。
        返回本次成交的订单列表。
        """
        if market_price <= 0:
            raise ValueError(f"market_price 必须为正，收到 {market_price}")

        # 更新持仓现价
        if symbol in self._positions:
            self._positions[symbol].current_price = market_price

        filled: list[Order] = []
        for order in list(self._orders.values()):
            if order.symbol != symbol or order.status != OrderStatus.PENDING:
                continue
            self._try_fill(order, market_price)
            if order.status == OrderStatus.FILLED:
                filled.append(order)
        protective = self._trigger_protective_exit(symbol, market_price)
        if protective is not None:
            filled.append(protective)
        return filled

    def set_position_protection(
        self, symbol: str, *, stop_loss_price: float, take_profit_price: float
    ) -> None:
        """为本地模拟持仓设置保护价，不连接任何真实交易系统。"""
        position = self._positions.get(symbol)
        if position is None:
            raise ValueError(f"无法为无持仓证券设置保护价: {symbol}")
        if not (0 < stop_loss_price < position.avg_cost < take_profit_price):
            raise ValueError("保护价必须满足 0 < 止损价 < 持仓均价 < 止盈价")
        position.stop_loss_price = float(stop_loss_price)
        position.take_profit_price = float(take_profit_price)

    def _trigger_protective_exit(self, symbol: str, market_price: float) -> Order | None:
        position = self._positions.get(symbol)
        if position is None:
            return None
        reason: str | None = None
        if position.stop_loss_price is not None and market_price <= position.stop_loss_price:
            reason = "stop_loss"
        elif position.take_profit_price is not None and market_price >= position.take_profit_price:
            reason = "take_profit"
        if reason is None:
            return None
        order = self.place_market_order(symbol, OrderSide.SELL, position.quantity)
        order.trigger_reason = reason
        self._try_fill(order, market_price)
        return order if order.status == OrderStatus.FILLED else None

    def _try_fill(self, order: Order, market_price: float) -> None:
        """尝试撮合单笔订单。"""
        # 判断限价单是否满足条件
        if order.order_type == OrderType.LIMIT:
            if order.side == OrderSide.BUY and market_price > order.limit_price:  # type: ignore[operator]
                return  # 市价高于买入限价，不成交
            if order.side == OrderSide.SELL and market_price < order.limit_price:  # type: ignore[operator]
                return  # 市价低于卖出限价，不成交

        # 滑点
        if order.side == OrderSide.BUY:
            exec_price = market_price * (1 + self.slippage_pct)
        else:
            exec_price = market_price * (1 - self.slippage_pct)

        # 成本 & 资金检查
        trade_value = exec_price * order.quantity
        commission = trade_value * self.commission_pct

        if order.side == OrderSide.BUY:
            total_cost = trade_value + commission
            if total_cost > self.cash:
                order.status = OrderStatus.REJECTED
                order.reject_reason = f"资金不足：需 {total_cost:.2f} 元，可用 {self.cash:.2f} 元"
                logger.warning("Order rejected (insufficient cash): %s", order.order_id)
                return
            self.cash -= total_cost
            self._update_position_buy(order.symbol, order.quantity, exec_price)

        else:  # SELL
            pos = self._positions.get(order.symbol)
            if pos is None or pos.quantity < order.quantity:
                order.status = OrderStatus.REJECTED
                order.reject_reason = f"持仓不足：需 {order.quantity} 股，持 {pos.quantity if pos else 0} 股"
                logger.warning("Order rejected (insufficient position): %s", order.order_id)
                return
            self.cash += trade_value - commission
            self._update_position_sell(order.symbol, order.quantity)

        order.status = OrderStatus.FILLED
        order.filled_price = exec_price
        order.filled_quantity = order.quantity
        order.filled_at = datetime.utcnow().isoformat() + "Z"

        self._trade_history.append({
            "order_id": order.order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "quantity_unit": QUANTITY_UNIT,
            "exec_price": exec_price,
            "commission": commission,
            "trigger_reason": order.trigger_reason,
            "timestamp": order.filled_at,
        })
        logger.info("Order filled %s @ %.2f × %d", order.order_id, exec_price, order.quantity)

    # ── 持仓维护 ─────────────────────────────────────────────────────────────

    def _update_position_buy(self, symbol: str, qty: int, price: float) -> None:
        if symbol in self._positions:
            pos = self._positions[symbol]
            total_cost = pos.avg_cost * pos.quantity + price * qty
            pos.quantity += qty
            pos.avg_cost = total_cost / pos.quantity
            pos.current_price = price
            # 加仓会改变成本基准，旧保护价不再可靠；调用方可在成交后重新设置。
            pos.stop_loss_price = None
            pos.take_profit_price = None
        else:
            self._positions[symbol] = Position(
                symbol=symbol, quantity=qty, avg_cost=price, current_price=price
            )

    def _update_position_sell(self, symbol: str, qty: int) -> None:
        pos = self._positions[symbol]
        pos.quantity -= qty
        if pos.quantity == 0:
            del self._positions[symbol]

    # ── 查询接口 ─────────────────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order and order.status == OrderStatus.PENDING:
            order.status = OrderStatus.CANCELLED
            return True
        return False

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def portfolio_value(self) -> float:
        """总资产 = 现金 + 持仓市值。"""
        mkt = sum(p.market_value for p in self._positions.values())
        return self.cash + mkt

    def total_pnl(self) -> float:
        return self.portfolio_value() - self.initial_cash

    def total_pnl_pct(self) -> float:
        return self.total_pnl() / self.initial_cash * 100

    def summary(self) -> dict:
        return {
            "cash": round(self.cash, 2),
            "portfolio_value": round(self.portfolio_value(), 2),
            "total_pnl": round(self.total_pnl(), 2),
            "total_pnl_pct": round(self.total_pnl_pct(), 4),
            "open_positions": len(self._positions),
            "total_trades": len(self._trade_history),
            "disclaimer": DISCLAIMER,
        }

    def export_state(self) -> dict:
        """导出可 JSON 持久化快照；不包含任何真实券商连接信息。"""
        return {
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "commission_pct": self.commission_pct * 100,
            "slippage_pct": self.slippage_pct * 100,
            "order_counter": self._order_counter,
            "orders": [
                {
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "order_type": order.order_type.value,
                    "quantity": order.quantity,
                    "limit_price": order.limit_price,
                    "status": order.status.value,
                    "filled_price": order.filled_price,
                    "filled_quantity": order.filled_quantity,
                    "created_at": order.created_at,
                    "filled_at": order.filled_at,
                    "reject_reason": order.reject_reason,
                    "trigger_reason": order.trigger_reason,
                }
                for order in self._orders.values()
            ],
            "positions": [
                {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "avg_cost": position.avg_cost,
                    "current_price": position.current_price,
                    "stop_loss_price": position.stop_loss_price,
                    "take_profit_price": position.take_profit_price,
                }
                for position in self._positions.values()
            ],
            "trade_history": [dict(item) for item in self._trade_history],
        }

    @classmethod
    def from_state(cls, state: dict) -> "MockBroker":
        """从持久化快照恢复，供服务重启后继续模拟撮合。"""
        broker = cls(
            initial_cash=float(state["initial_cash"]),
            commission_pct=float(state.get("commission_pct", 0.03)),
            slippage_pct=float(state.get("slippage_pct", 0.1)),
        )
        broker.cash = float(state["cash"])
        broker._order_counter = int(state.get("order_counter", 0))
        for raw in state.get("orders", []):
            order = Order(
                order_id=str(raw["order_id"]),
                symbol=str(raw["symbol"]),
                side=OrderSide(raw["side"]),
                order_type=OrderType(raw["order_type"]),
                quantity=int(raw["quantity"]),
                limit_price=raw.get("limit_price"),
                status=OrderStatus(raw["status"]),
                filled_price=raw.get("filled_price"),
                filled_quantity=int(raw.get("filled_quantity", 0)),
                created_at=str(raw["created_at"]),
                filled_at=raw.get("filled_at"),
                reject_reason=raw.get("reject_reason"),
                trigger_reason=raw.get("trigger_reason"),
            )
            broker._orders[order.order_id] = order
        for raw in state.get("positions", []):
            position = Position(
                symbol=str(raw["symbol"]),
                quantity=int(raw["quantity"]),
                avg_cost=float(raw["avg_cost"]),
                current_price=float(raw.get("current_price", 0.0)),
                stop_loss_price=raw.get("stop_loss_price"),
                take_profit_price=raw.get("take_profit_price"),
            )
            broker._positions[position.symbol] = position
        broker._trade_history = [dict(item) for item in state.get("trade_history", [])]
        return broker
