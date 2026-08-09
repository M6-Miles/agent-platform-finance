"""
模拟盘连续多交易日运行（Paper Trading Session）
================================================
说明书要求「补充多个连续交易日的模拟盘验收；真实行情不可用时必须明确标记
fallback / unavailable」。本模块是该要求的实现。

它做什么
--------
把 :class:`~agent_platform.finance.mock_broker.MockBroker` 从「单次 tick 撮合」
提升为「按交易日历连续运行 N 个交易日」：逐日喂入收盘价、撮合挂单、维护持仓、
记录每日资金曲线快照，最后给出组合层面的盈亏与逐日审计轨迹。

它绝不做什么
------------
1. **绝不连接真实券商**：撮合全部由本地 MockBroker 完成，本模块不 import 任何
   券商 SDK、不发出任何下单请求。这一点由 tests/test_paper_trading_session.py
   的模块依赖断言保证。
2. **绝不伪造行情**：某标的在某交易日没有真实 K 线时，本模块**不做前值填充、
   不插值、不用随机数补齐**，而是记录 ``missing_quote`` 并当日跳过该标的的撮合。
   前值填充会造出一个「该日确实有这个价格」的假象，属于伪造行情。
3. **绝不把降级说成实时**：取数经 :func:`fetch_price_history`，其四级
   ``data_status``（live / offline_sample / fallback / unavailable）原样透传并
   用 :func:`combine_statuses` 聚合 —— 任一标的降级则整体降级。
4. **绝不静默丢标的**：真实与样例数据都拿不到的标的进入 ``unavailable_symbols``
   并附失败原因，而不是从结果里消失。

未来信息（Lookahead）纪律
------------------------
策略只能看到「截至当日收盘（含当日）」的收盘价序列，撮合价为当日收盘价。
这是标准的「收盘价成交」假设：决策与成交同处当日收盘这一时点，
不使用次日及以后的任何数据，因此不存在未来信息泄漏。

⚠️  仅供研究参考，不构成投资建议。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_platform.finance.mock_broker import (
    SHARES_PER_LOT,
    MockBroker,
    OrderSide,
)

logger = logging.getLogger(__name__)

# 默认双均线参数（确定性、无随机）
_MA_SHORT = 5
_MA_LONG = 20

# 单一标的市值占组合上限（%）。模拟盘自身的仓位约束；
# 完整的风控与人工审批链路由 RiskManager / TradingHarness 负责，见其专项测试。
_DEFAULT_MAX_POSITION_PCT = 10.0


# ─────────────────────────────────────────────────────────────────────────────
# 策略接口
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StrategyContext:
    """策略可见的全部信息。刻意不含次日及以后数据，从类型上排除未来信息。"""

    day_index: int
    date: str
    symbol: str
    close: float
    closes: tuple[float, ...]      # 截至当日（含当日）的收盘价序列
    position_qty: int              # 当前持仓股数
    cash: float
    portfolio_value: float


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """策略意图。quantity=None 表示交由会话按仓位上限自动定量。"""

    side: str                      # "buy" / "sell"
    reason: str
    quantity: int | None = None


StrategyFn = Callable[[StrategyContext], TradeIntent | None]


def ma_crossover_strategy(ctx: StrategyContext) -> TradeIntent | None:
    """
    确定性双均线策略：MA5 上穿 MA20 买入，下穿清仓。

    仅用于验证模拟盘的多日连续运行能力，**不代表策略有效性**；
    策略质量与 Sharpe 的论证在回测模块，不在这里。
    """
    closes = ctx.closes
    if len(closes) < _MA_LONG + 1:
        return None

    def _ma(seq: tuple[float, ...], n: int) -> float:
        return sum(seq[-n:]) / n

    prev = closes[:-1]
    short_now, long_now = _ma(closes, _MA_SHORT), _ma(closes, _MA_LONG)
    short_prev, long_prev = _ma(prev, _MA_SHORT), _ma(prev, _MA_LONG)

    if short_prev <= long_prev and short_now > long_now:
        return TradeIntent(side="buy", reason=f"MA{_MA_SHORT} 上穿 MA{_MA_LONG}")
    if short_prev >= long_prev and short_now < long_now and ctx.position_qty > 0:
        return TradeIntent(side="sell", reason=f"MA{_MA_SHORT} 下穿 MA{_MA_LONG}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 结果结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class DailySnapshot:
    """单个交易日收盘后的组合快照，构成可审计的资金曲线。"""

    day_index: int
    date: str
    prices: dict[str, float]           # 当日参与撮合的标的收盘价
    missing_quote: tuple[str, ...]     # 当日无真实 K 线、已跳过撮合的标的
    filled_orders: tuple[dict[str, Any], ...]
    cash: float
    positions: dict[str, int]          # symbol → 持仓股数
    portfolio_value: float
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_index": self.day_index,
            "date": self.date,
            "prices": dict(self.prices),
            "missing_quote": list(self.missing_quote),
            "filled_orders": [dict(o) for o in self.filled_orders],
            "cash": round(self.cash, 2),
            "positions": dict(self.positions),
            "portfolio_value": round(self.portfolio_value, 2),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class PaperTradingResult:
    symbols: tuple[str, ...]
    trading_days: int
    initial_cash: float
    final_portfolio_value: float
    total_pnl: float
    total_pnl_pct: float
    snapshots: tuple[DailySnapshot, ...]
    total_trades: int
    data_status: str
    source: str
    updated_at: str
    fallback_reason: str | None
    unavailable_symbols: dict[str, str]      # symbol → 不可用原因
    per_symbol_status: dict[str, str]
    broker_kind: str
    disclaimer: str
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # 每标的在交易窗口之前用于预热指标的历史根数（严格为过去数据）
    warmup_bars: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "trading_days": self.trading_days,
            "initial_cash": round(self.initial_cash, 2),
            "final_portfolio_value": round(self.final_portfolio_value, 2),
            "total_pnl": round(self.total_pnl, 2),
            "total_pnl_pct": round(self.total_pnl_pct, 4),
            "total_trades": self.total_trades,
            "equity_curve": [
                {"date": s.date, "portfolio_value": round(s.portfolio_value, 2)}
                for s in self.snapshots
            ],
            "snapshots": [s.to_dict() for s in self.snapshots],
            "data_status": self.data_status,
            "source": self.source,
            "updated_at": self.updated_at,
            "fallback_reason": self.fallback_reason,
            "unavailable_symbols": dict(self.unavailable_symbols),
            "per_symbol_status": dict(self.per_symbol_status),
            "broker_kind": self.broker_kind,
            "disclaimer": self.disclaimer,
            "warnings": list(self.warnings),
            "warmup_bars": dict(self.warmup_bars),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 内部函数
# ─────────────────────────────────────────────────────────────────────────────

def _bars_by_date(frame: Any) -> dict[str, float]:
    """DataFrame → {日期: 收盘价}。收盘价缺失 / NaN / 非正一律丢弃，不填充。"""
    out: dict[str, float] = {}
    for rec in frame.to_dict(orient="records"):
        raw_date = str(rec.get("date") or "")[:10]
        if not raw_date:
            continue
        close = rec.get("close")
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if value != value or value <= 0:       # NaN 自身不等于自身
            continue
        out[raw_date] = value
    return out


def _size_buy(
    *, portfolio_value: float, close: float, position_qty: int, max_position_pct: float,
) -> int:
    """按单一标的仓位上限计算可买股数（向下取整到整手）。"""
    if close <= 0:
        return 0
    target_value = portfolio_value * max_position_pct / 100.0
    room_value = target_value - position_qty * close
    if room_value <= 0:
        return 0
    lots = int(room_value // (close * SHARES_PER_LOT))
    return lots * SHARES_PER_LOT


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def run_paper_trading_session(
    symbols: list[str] | tuple[str, ...],
    *,
    data_mode: str = "offline",
    days: int = 20,
    initial_cash: float = 1_000_000.0,
    strategy: StrategyFn = ma_crossover_strategy,
    max_position_pct: float = _DEFAULT_MAX_POSITION_PCT,
    provider: Any = None,
    fetcher: Any = None,
) -> PaperTradingResult:
    """
    在本地 MockBroker 上连续运行 ``days`` 个交易日的模拟盘。

    Parameters
    ----------
    data_mode : "offline" / "auto"
        offline 全程零网络；auto 允许真实数据源，失败自动降级并标记 fallback。
    days : int
        连续交易日数量，取数据日历中最后 ``days`` 个交易日。
    fetcher : 可注入的取数函数
        默认 :func:`fetch_price_history`。仅用于测试注入，生产不传。

    Raises
    ------
    ValueError
        symbols 为空或 days < 1（调用方编码错误，必须暴露而非静默返回空结果）。
    """
    from agent_platform.finance.constants import DISCLAIMER
    from agent_platform.finance.data_status import (
        STATUS_UNAVAILABLE,
        MarketDataAllSourcesFailed,
        combine_statuses,
        fetch_price_history,
        normalize_data_mode,
    )

    codes = tuple(str(s).strip() for s in symbols if str(s).strip())
    if not codes:
        raise ValueError("symbols 不能为空")
    if days < 1:
        raise ValueError(f"days 必须 >= 1，收到 {days}")

    mode = normalize_data_mode(data_mode)
    fetch = fetcher or fetch_price_history
    updated_at = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"

    # ── 1. 逐标的取数，四级状态原样保留 ──
    bars: dict[str, dict[str, float]] = {}
    per_symbol_status: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    sources: list[str] = []
    fallback_reasons: list[str] = []

    for code in codes:
        try:
            outcome = fetch(code, data_mode=mode, provider=provider)
        except MarketDataAllSourcesFailed as exc:
            # 真实与样例都失败 → 明确标 unavailable，不编造价格、不静默丢标的
            unavailable[code] = f"MarketDataAllSourcesFailed: {exc}"
            per_symbol_status[code] = STATUS_UNAVAILABLE
            logger.warning("[PaperTrading] %s 行情不可用: %s", code, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — 取数边界需兜住并标记，不得吞掉
            unavailable[code] = f"{type(exc).__name__}: {exc}"
            per_symbol_status[code] = STATUS_UNAVAILABLE
            logger.warning("[PaperTrading] %s 取数异常: %s", code, exc)
            continue

        series = _bars_by_date(outcome.frame)
        per_symbol_status[code] = outcome.data_status
        sources.append(outcome.source)
        if outcome.fallback_reason:
            fallback_reasons.append(f"{code}: {outcome.fallback_reason}")
        if not series:
            unavailable[code] = "取数成功但无任何有效收盘价（空表或收盘价全为空）"
            per_symbol_status[code] = STATUS_UNAVAILABLE
            continue
        bars[code] = series

    warnings: list[str] = [f"{c} 行情不可用：{r}" for c, r in unavailable.items()]

    # ── 2. 全部标的都不可用 → 明确 unavailable，不返回"跑了 0 天但成功"的假象 ──
    if not bars:
        return PaperTradingResult(
            symbols=codes, trading_days=0, initial_cash=initial_cash,
            final_portfolio_value=initial_cash, total_pnl=0.0, total_pnl_pct=0.0,
            snapshots=(), total_trades=0,
            data_status=STATUS_UNAVAILABLE,
            source="；".join(dict.fromkeys(sources)) or "无可用数据源",
            updated_at=updated_at,
            fallback_reason="全部标的行情不可用，模拟盘未运行",
            unavailable_symbols=unavailable, per_symbol_status=per_symbol_status,
            broker_kind="MockBroker(本地模拟撮合)", disclaimer=DISCLAIMER,
            warnings=tuple(warnings),
        )

    # ── 3. 交易日历：全部标的日期并集的最后 days 个交易日 ──
    all_dates = sorted({d for series in bars.values() for d in series})
    calendar = all_dates[-days:]
    if len(calendar) < days:
        warnings.append(
            f"数据仅覆盖 {len(calendar)} 个交易日，少于请求的 {days} 个，已按实际可用交易日运行"
        )

    # ── 4. 均线预热：用交易窗口**之前**的历史收盘价填充指标窗口 ──
    # 不预热的话，窗口第 1 天的 closes 为空，MA20 需要 21 根才出信号，
    # days=20 时策略一次都不会触发 —— 会跑出「20 天 0 笔成交」的空验收，
    # 什么都证明不了。预热只使用 calendar[0] 之前的数据，严格属于过去信息，
    # 不构成未来信息泄漏。每标的预热根数写入结果供审计。
    window_start = calendar[0]
    broker = MockBroker(initial_cash=initial_cash)
    history: dict[str, list[float]] = {}
    warmup_bars: dict[str, int] = {}
    for code, series in bars.items():
        prior = [series[d] for d in sorted(series) if d < window_start]
        history[code] = prior
        warmup_bars[code] = len(prior)
        if len(prior) < _MA_LONG + 1:
            warnings.append(
                f"{code} 窗口前历史仅 {len(prior)} 根（< {_MA_LONG + 1} 根），"
                f"默认双均线策略在本窗口内可能无信号"
            )
    snapshots: list[DailySnapshot] = []

    for day_index, date in enumerate(calendar):
        day_prices: dict[str, float] = {}
        missing: list[str] = []
        filled_today: list[dict[str, Any]] = []
        notes: list[str] = []

        for code, series in bars.items():
            close = series.get(date)
            if close is None:
                # 该标的当日无真实 K 线：跳过撮合。绝不前值填充 —— 那是伪造行情。
                missing.append(code)
                continue

            day_prices[code] = close
            history[code].append(close)

            positions = broker.get_positions()
            position_qty = positions[code].quantity if code in positions else 0

            intent = strategy(StrategyContext(
                day_index=day_index, date=date, symbol=code, close=close,
                closes=tuple(history[code]), position_qty=position_qty,
                cash=broker.cash, portfolio_value=broker.portfolio_value(),
            ))

            if intent is not None:
                qty = intent.quantity
                if intent.side == "buy":
                    if qty is None:
                        qty = _size_buy(
                            portfolio_value=broker.portfolio_value(), close=close,
                            position_qty=position_qty, max_position_pct=max_position_pct,
                        )
                    if qty and qty > 0:
                        broker.place_market_order(code, OrderSide.BUY, qty)
                        notes.append(f"{code} 买入意图 {qty} 股（{intent.reason}）")
                    else:
                        notes.append(f"{code} 买入意图被仓位上限拦截（{intent.reason}）")
                elif intent.side == "sell":
                    if qty is None:
                        qty = position_qty
                    if qty and qty > 0:
                        broker.place_market_order(code, OrderSide.SELL, qty)
                        notes.append(f"{code} 卖出意图 {qty} 股（{intent.reason}）")
                else:
                    raise ValueError(f"未知交易方向: {intent.side!r}")

            # 以当日收盘价撮合（收盘价成交假设，见模块文档）
            for order in broker.tick(code, close):
                filled_today.append({
                    "order_id": order.order_id, "symbol": order.symbol,
                    "side": order.side.value, "quantity": order.filled_quantity,
                    "price": round(order.filled_price or 0.0, 4),
                })

        snapshots.append(DailySnapshot(
            day_index=day_index, date=date, prices=day_prices,
            missing_quote=tuple(missing), filled_orders=tuple(filled_today),
            cash=broker.cash,
            positions={s: p.quantity for s, p in broker.get_positions().items()},
            portfolio_value=broker.portfolio_value(), notes=tuple(notes),
        ))

    summary = broker.summary()
    aggregate = combine_statuses([per_symbol_status[c] for c in bars])

    return PaperTradingResult(
        symbols=codes, trading_days=len(calendar), initial_cash=initial_cash,
        final_portfolio_value=broker.portfolio_value(),
        total_pnl=broker.total_pnl(), total_pnl_pct=broker.total_pnl_pct(),
        snapshots=tuple(snapshots), total_trades=int(summary["total_trades"]),
        data_status=aggregate,
        source="；".join(dict.fromkeys(sources)),
        updated_at=updated_at,
        fallback_reason="；".join(fallback_reasons) or None,
        unavailable_symbols=unavailable, per_symbol_status=per_symbol_status,
        broker_kind="MockBroker(本地模拟撮合)", disclaimer=DISCLAIMER,
        warnings=tuple(warnings),
        warmup_bars=warmup_bars,
    )
