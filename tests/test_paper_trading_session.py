"""
模拟盘多连续交易日验收测试
==========================
对应说明书第二节第 5 条：「补充多个连续交易日的模拟盘验收；真实行情不可用时
必须明确标记 fallback / unavailable」，以及「MockBroker 必须保持本地模拟撮合，
绝不接真实券商」。

本文件的断言分五组
------------------
1. 连续多交易日真的跑起来了（且**真的有成交**，不是 0 笔空验收）
2. 绝不伪造行情：缺 K 线跳过撮合，不前值填充、不插值
3. 数据状态诚实：fallback / unavailable 必须显式标记并给出原因
4. 绝不接真实券商：撮合方为本地 MockBroker，模块不依赖任何券商 SDK
5. 无未来信息：预热只用交易窗口之前的数据

所有测试零网络：默认 data_mode="offline"，注入式 fetcher 场景连数据层都不碰。
"""
from __future__ import annotations

import builtins
from typing import Any

import pandas as pd
import pytest

from agent_platform.finance.data_status import (
    STATUS_FALLBACK,
    STATUS_LIVE,
    STATUS_OFFLINE_SAMPLE,
    STATUS_UNAVAILABLE,
    FetchOutcome,
    MarketDataAllSourcesFailed,
)
from agent_platform.finance.paper_trading_session import (
    _MA_LONG,
    PaperTradingResult,
    StrategyContext,
    TradeIntent,
    _size_buy,
    ma_crossover_strategy,
    run_paper_trading_session,
)

# ═══════════════════════════════════════════════════════════════
#   测试脚手架
# ═══════════════════════════════════════════════════════════════


def _frame(dates: list[str], closes: list[float], *, symbol: str = "T001") -> pd.DataFrame:
    """构造最小可用日线表。列名与 SampleMarketDataProvider 一致。"""
    assert len(dates) == len(closes)
    return pd.DataFrame({
        "symbol": [symbol] * len(dates),
        "date": dates,
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [1_000_000] * len(dates),
    })


def _dates(n: int, *, start_day: int = 1) -> list[str]:
    """生成 n 个连续「交易日」字符串（用自然日模拟，日历连续性由数据定义）。"""
    return [f"2025-06-{start_day + i:02d}" for i in range(n)]


def _outcome(
    frame: pd.DataFrame,
    *,
    status: str = STATUS_OFFLINE_SAMPLE,
    source: str = "测试注入数据",
    reason: str | None = None,
) -> FetchOutcome:
    return FetchOutcome(
        frame=frame,
        data_status=status,
        source=source,
        updated_at="2025-06-30",
        fallback_reason=reason,
    )


class StubFetcher:
    """按 symbol 返回预置 FetchOutcome 或抛出预置异常。零网络。"""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def __call__(self, symbol: str, *, data_mode: str, provider: Any = None, **kw: Any):
        self.calls.append(symbol)
        result = self._mapping[symbol]
        if isinstance(result, Exception):
            raise result
        return result


def buy_on_day(day: int, qty: int = 100):
    """确定性测试策略：在指定 day_index 买入固定股数，其余不动。"""

    def _strategy(ctx: StrategyContext) -> TradeIntent | None:
        if ctx.day_index == day:
            return TradeIntent(side="buy", reason="测试策略", quantity=qty)
        return None

    return _strategy


def never_trade(ctx: StrategyContext) -> TradeIntent | None:
    return None


# ═══════════════════════════════════════════════════════════════
#   一、连续多交易日真的跑起来（且真的有成交）
# ═══════════════════════════════════════════════════════════════


class TestMultiDayContinuity:
    @pytest.fixture(scope="class")
    def result(self) -> PaperTradingResult:
        return run_paper_trading_session(
            ["DEMO001", "DEMO002"], data_mode="offline", days=20,
        )

    def test_runs_requested_number_of_trading_days(self, result):
        assert result.trading_days == 20
        assert len(result.snapshots) == 20

    def test_dates_are_strictly_increasing(self, result):
        dates = [s.date for s in result.snapshots]
        assert dates == sorted(dates)
        assert len(set(dates)) == len(dates), "交易日出现重复"

    def test_every_day_has_a_snapshot_with_portfolio_value(self, result):
        for snap in result.snapshots:
            assert snap.portfolio_value > 0
            assert snap.date
            assert snap.day_index >= 0

    def test_day_index_is_contiguous_from_zero(self, result):
        assert [s.day_index for s in result.snapshots] == list(range(20))

    def test_trades_actually_happen(self, result):
        """
        回归测试：曾出现「20 天 0 笔成交」的空验收。
        原因是均线预热缺失 —— MA20 需 21 根，窗口内永远凑不满。
        0 笔成交的模拟盘什么都证明不了，必须断言真的有成交。
        """
        assert result.total_trades > 0, "连续多日模拟盘一笔成交都没有，验收无意义"

    def test_equity_curve_actually_moves(self, result):
        values = [round(s.portfolio_value, 2) for s in result.snapshots]
        assert len(set(values)) > 1, "资金曲线全程不动，说明撮合未真正发生"

    def test_equity_curve_length_matches_days(self, result):
        assert len(result.to_dict()["equity_curve"]) == result.trading_days

    def test_fills_are_recorded_with_price_and_quantity(self, result):
        fills = [o for s in result.snapshots for o in s.filled_orders]
        assert fills, "没有任何成交记录"
        for order in fills:
            assert order["quantity"] > 0
            assert order["price"] > 0
            assert order["side"] in ("buy", "sell")

    def test_pnl_consistent_with_final_value(self, result):
        assert result.total_pnl == pytest.approx(
            result.final_portfolio_value - result.initial_cash, abs=1e-6,
        )

    def test_final_value_matches_last_snapshot(self, result):
        assert result.final_portfolio_value == pytest.approx(
            result.snapshots[-1].portfolio_value, abs=1e-6,
        )

    def test_deterministic_across_runs(self):
        a = run_paper_trading_session(["DEMO001"], data_mode="offline", days=20)
        b = run_paper_trading_session(["DEMO001"], data_mode="offline", days=20)
        assert a.total_trades == b.total_trades
        assert [s.date for s in a.snapshots] == [s.date for s in b.snapshots]
        assert [round(s.portfolio_value, 6) for s in a.snapshots] == [
            round(s.portfolio_value, 6) for s in b.snapshots
        ]

    def test_result_serializes(self, result):
        d = result.to_dict()
        for key in (
            "symbols", "trading_days", "total_trades", "equity_curve", "snapshots",
            "data_status", "source", "updated_at", "fallback_reason",
            "unavailable_symbols", "per_symbol_status", "broker_kind",
            "disclaimer", "warmup_bars",
        ):
            assert key in d, f"结果缺字段: {key}"

    def test_disclaimer_present(self, result):
        assert "不构成投资建议" in result.disclaimer


# ═══════════════════════════════════════════════════════════════
#   二、绝不伪造行情
# ═══════════════════════════════════════════════════════════════


class TestNeverFabricatesQuotes:
    def test_missing_bar_is_recorded_and_skipped(self):
        """
        B 标的在中间某日无 K 线 —— 该日必须记入 missing_quote，
        且**不得**出现在 prices 里（出现即意味着前值填充/插值，属伪造行情）。
        """
        dates = _dates(6)
        gap_date = dates[3]
        b_dates = [d for d in dates if d != gap_date]

        fetcher = StubFetcher({
            "AAA": _outcome(_frame(dates, [10.0] * 6, symbol="AAA")),
            "BBB": _outcome(_frame(b_dates, [20.0] * 5, symbol="BBB")),
        })
        r = run_paper_trading_session(
            ["AAA", "BBB"], data_mode="offline", days=6,
            strategy=never_trade, fetcher=fetcher,
        )

        gap = next(s for s in r.snapshots if s.date == gap_date)
        assert "BBB" in gap.missing_quote
        assert "BBB" not in gap.prices, "缺 K 线的标的出现了价格 —— 疑似前值填充"
        assert gap.prices["AAA"] == 10.0

    def test_other_days_unaffected_by_gap(self):
        dates = _dates(6)
        gap_date = dates[3]
        fetcher = StubFetcher({
            "AAA": _outcome(_frame(dates, [10.0] * 6, symbol="AAA")),
            "BBB": _outcome(
                _frame([d for d in dates if d != gap_date], [20.0] * 5, symbol="BBB"),
            ),
        })
        r = run_paper_trading_session(
            ["AAA", "BBB"], data_mode="offline", days=6,
            strategy=never_trade, fetcher=fetcher,
        )
        for snap in r.snapshots:
            if snap.date != gap_date:
                assert snap.prices.get("BBB") == 20.0
                assert "BBB" not in snap.missing_quote

    def test_nan_close_is_dropped_not_zero_filled(self):
        """收盘价 NaN 的行必须被丢弃，绝不当 0 或前值。"""
        dates = _dates(5)
        frame = _frame(dates, [10.0, 11.0, 12.0, 13.0, 14.0], symbol="AAA")
        frame.loc[2, "close"] = float("nan")

        fetcher = StubFetcher({"AAA": _outcome(frame)})
        r = run_paper_trading_session(
            ["AAA"], data_mode="offline", days=5,
            strategy=never_trade, fetcher=fetcher,
        )
        # NaN 那天没有有效收盘价 → 该日不进日历（日历由有效 K 线构成）
        assert dates[2] not in [s.date for s in r.snapshots]
        for snap in r.snapshots:
            assert all(p > 0 for p in snap.prices.values())

    def test_nonpositive_close_is_dropped(self):
        dates = _dates(4)
        frame = _frame(dates, [10.0, 0.0, 12.0, 13.0], symbol="AAA")
        fetcher = StubFetcher({"AAA": _outcome(frame)})
        r = run_paper_trading_session(
            ["AAA"], data_mode="offline", days=4,
            strategy=never_trade, fetcher=fetcher,
        )
        assert dates[1] not in [s.date for s in r.snapshots]


# ═══════════════════════════════════════════════════════════════
#   三、数据状态诚实：fallback / unavailable 必须显式标记
# ═══════════════════════════════════════════════════════════════


class TestDataStatusHonesty:
    def test_offline_run_marked_offline_sample(self):
        r = run_paper_trading_session(["DEMO001"], data_mode="offline", days=10)
        assert r.data_status == STATUS_OFFLINE_SAMPLE
        assert r.per_symbol_status["DEMO001"] == STATUS_OFFLINE_SAMPLE
        assert r.fallback_reason is None

    def test_fallback_status_and_reason_surfaced(self):
        """真实数据源失败降级到样例 → 必须标 fallback 并带原因，不得说成 live。"""
        dates = _dates(5)
        fetcher = StubFetcher({
            "AAA": _outcome(
                _frame(dates, [10.0] * 5, symbol="AAA"),
                status=STATUS_FALLBACK,
                source="内置样例数据（真实数据源降级）",
                reason="真实数据源失败（ConnectionError）：连接超时",
            ),
        })
        r = run_paper_trading_session(
            ["AAA"], data_mode="auto", days=5,
            strategy=never_trade, fetcher=fetcher,
        )
        assert r.data_status == STATUS_FALLBACK
        assert r.fallback_reason is not None
        assert "ConnectionError" in r.fallback_reason
        assert "AAA" in r.fallback_reason

    def test_unavailable_symbol_is_marked_not_dropped(self):
        """两个标的都拿不到数据 → 整体 unavailable，且原因逐标的可见。"""
        fetcher = StubFetcher({
            "AAA": MarketDataAllSourcesFailed("真实数据源失败；样例数据同样不可用"),
            "BBB": MarketDataAllSourcesFailed("真实数据源失败；样例数据同样不可用"),
        })
        r = run_paper_trading_session(
            ["AAA", "BBB"], data_mode="auto", days=5,
            strategy=never_trade, fetcher=fetcher,
        )
        assert r.data_status == STATUS_UNAVAILABLE
        assert set(r.unavailable_symbols) == {"AAA", "BBB"}
        assert r.trading_days == 0
        assert r.total_trades == 0
        assert r.fallback_reason is not None
        for reason in r.unavailable_symbols.values():
            assert "MarketDataAllSourcesFailed" in reason

    def test_unavailable_run_does_not_claim_success(self):
        """全部不可用时不得返回「跑完了、盈亏 0」的假成功。"""
        fetcher = StubFetcher({"AAA": MarketDataAllSourcesFailed("全失败")})
        r = run_paper_trading_session(
            ["AAA"], data_mode="auto", days=5, strategy=never_trade, fetcher=fetcher,
        )
        assert r.data_status == STATUS_UNAVAILABLE
        assert r.snapshots == ()
        assert "未运行" in (r.fallback_reason or "")
        assert r.warnings, "不可用标的必须进 warnings 显式提示"

    def test_partial_unavailable_keeps_running_and_marks_it(self):
        """一个可用、一个不可用 → 继续跑可用的，但不可用的必须被标记。"""
        dates = _dates(5)
        fetcher = StubFetcher({
            "AAA": _outcome(_frame(dates, [10.0] * 5, symbol="AAA")),
            "BBB": MarketDataAllSourcesFailed("全失败"),
        })
        r = run_paper_trading_session(
            ["AAA", "BBB"], data_mode="auto", days=5,
            strategy=never_trade, fetcher=fetcher,
        )
        assert r.trading_days == 5
        assert "BBB" in r.unavailable_symbols
        assert r.per_symbol_status["BBB"] == STATUS_UNAVAILABLE
        assert any("BBB" in w for w in r.warnings)

    def test_generic_fetch_exception_marked_unavailable(self):
        """非 MarketDataAllSourcesFailed 的异常也必须标记，不得吞掉。"""
        fetcher = StubFetcher({"AAA": RuntimeError("上游 500")})
        r = run_paper_trading_session(
            ["AAA"], data_mode="auto", days=5, strategy=never_trade, fetcher=fetcher,
        )
        assert r.data_status == STATUS_UNAVAILABLE
        assert "RuntimeError" in r.unavailable_symbols["AAA"]

    def test_empty_frame_marked_unavailable(self):
        """取数「成功」但表里没有任何有效收盘价 → unavailable，不算成功。"""
        fetcher = StubFetcher({"AAA": _outcome(_frame([], [], symbol="AAA"))})
        r = run_paper_trading_session(
            ["AAA"], data_mode="offline", days=5, strategy=never_trade, fetcher=fetcher,
        )
        assert r.data_status == STATUS_UNAVAILABLE
        assert "AAA" in r.unavailable_symbols

    def test_live_status_preserved(self):
        dates = _dates(5)
        fetcher = StubFetcher({
            "AAA": _outcome(
                _frame(dates, [10.0] * 5, symbol="AAA"),
                status=STATUS_LIVE, source="AkShare 公开数据",
            ),
        })
        r = run_paper_trading_session(
            ["AAA"], data_mode="auto", days=5, strategy=never_trade, fetcher=fetcher,
        )
        assert r.data_status == STATUS_LIVE
        assert r.fallback_reason is None

    def test_source_is_never_empty_when_data_present(self):
        r = run_paper_trading_session(["DEMO001"], data_mode="offline", days=5)
        assert r.source
        assert "样例" in r.source, "离线运行的 source 必须自证是样例数据"


# ═══════════════════════════════════════════════════════════════
#   四、绝不接真实券商
# ═══════════════════════════════════════════════════════════════


class TestNeverRealBroker:
    def test_broker_kind_declares_local_mock(self):
        r = run_paper_trading_session(["DEMO001"], data_mode="offline", days=5)
        assert "MockBroker" in r.broker_kind
        assert "模拟" in r.broker_kind

    def test_module_imports_mock_broker_only(self):
        """
        源码级断言：本模块只依赖本地 MockBroker，不出现任何券商 SDK / 下单 API。
        """
        import inspect

        from agent_platform.finance import paper_trading_session as mod

        src = inspect.getsource(mod)
        assert "from agent_platform.finance.mock_broker import" in src

        forbidden = (
            "easytrader", "tushare_trade", "ths_trader", "xtquant", "vnpy",
            "ctp", "securities_api", "real_broker", "live_trade",
            "place_real_order", "requests.post", "httpx.post",
        )
        for token in forbidden:
            assert token not in src, f"模拟盘模块出现疑似真实交易依赖: {token}"

    def test_offline_session_imports_no_akshare(self, monkeypatch):
        """离线模拟盘必须零网络：akshare 一旦被 import 就失败。"""
        real_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name == "akshare":
                raise AssertionError("离线模拟盘尝试 import akshare —— 违反零网络要求")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guard)
        r = run_paper_trading_session(["DEMO001"], data_mode="offline", days=10)
        assert r.trading_days == 10
        assert r.data_status == STATUS_OFFLINE_SAMPLE

    def test_no_position_without_a_fill(self):
        """没有成交就不该有持仓 —— 排除「凭空记账」。"""
        r = run_paper_trading_session(
            ["DEMO001"], data_mode="offline", days=10, strategy=never_trade,
        )
        assert r.total_trades == 0
        for snap in r.snapshots:
            assert snap.positions == {}
            assert snap.cash == pytest.approx(r.initial_cash)

    def test_cash_decreases_after_buy(self):
        dates = _dates(6)
        fetcher = StubFetcher({"AAA": _outcome(_frame(dates, [10.0] * 6, symbol="AAA"))})
        r = run_paper_trading_session(
            ["AAA"], data_mode="offline", days=6,
            strategy=buy_on_day(1, qty=1000), fetcher=fetcher, initial_cash=100_000.0,
        )
        assert r.total_trades == 1
        assert r.snapshots[0].cash == pytest.approx(100_000.0)
        assert r.snapshots[1].cash < 100_000.0
        assert r.snapshots[1].positions["AAA"] == 1000


# ═══════════════════════════════════════════════════════════════
#   五、无未来信息 + 仓位上限
# ═══════════════════════════════════════════════════════════════


class TestNoLookaheadAndSizing:
    def test_warmup_uses_only_prior_bars(self):
        """
        预热根数必须等于「交易窗口首日之前」的有效 K 线数，
        多一根就意味着把窗口内（含当日之后）的数据当成了历史。
        """
        r = run_paper_trading_session(["DEMO001"], data_mode="offline", days=20)
        first_day = r.snapshots[0].date

        from agent_platform.finance.data_status import fetch_price_history

        frame = fetch_price_history("DEMO001", data_mode="offline").frame
        prior = [
            rec for rec in frame.to_dict(orient="records")
            if str(rec["date"])[:10] < first_day and float(rec["close"]) > 0
        ]
        assert r.warmup_bars["DEMO001"] == len(prior)

    def test_strategy_never_sees_future_closes(self):
        """策略每日收到的 closes 末位必须等于当日收盘价，长度单调递增。"""
        seen: list[tuple[int, int, float]] = []

        def spy(ctx: StrategyContext) -> TradeIntent | None:
            seen.append((ctx.day_index, len(ctx.closes), ctx.closes[-1]))
            assert ctx.closes[-1] == ctx.close, "closes 末位不是当日收盘价"
            return None

        dates = _dates(6)
        closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        fetcher = StubFetcher({"AAA": _outcome(_frame(dates, closes, symbol="AAA"))})
        run_paper_trading_session(
            ["AAA"], data_mode="offline", days=6, strategy=spy, fetcher=fetcher,
        )
        lengths = [n for _, n, _ in seen]
        assert lengths == sorted(lengths)
        assert [c for _, _, c in seen] == closes

    def test_warmup_shortfall_produces_warning(self):
        """窗口前历史不足时必须给出警告，而不是静默跑出 0 信号。"""
        dates = _dates(5)
        fetcher = StubFetcher({"AAA": _outcome(_frame(dates, [10.0] * 5, symbol="AAA"))})
        r = run_paper_trading_session(
            ["AAA"], data_mode="offline", days=5, fetcher=fetcher,
        )
        assert r.warmup_bars["AAA"] == 0
        assert any("预热" in w or "无信号" in w for w in r.warnings)

    def test_size_buy_respects_cap(self):
        qty = _size_buy(
            portfolio_value=1_000_000.0, close=100.0,
            position_qty=0, max_position_pct=10.0,
        )
        assert qty == 1000                      # 10% = 100,000 元 / 100 元 = 1000 股
        assert qty % 100 == 0, "必须为整手"

    def test_size_buy_accounts_for_existing_position(self):
        qty = _size_buy(
            portfolio_value=1_000_000.0, close=100.0,
            position_qty=900, max_position_pct=10.0,
        )
        assert qty == 100

    def test_size_buy_returns_zero_when_cap_reached(self):
        qty = _size_buy(
            portfolio_value=1_000_000.0, close=100.0,
            position_qty=1000, max_position_pct=10.0,
        )
        assert qty == 0

    def test_size_buy_zero_on_bad_price(self):
        assert _size_buy(
            portfolio_value=1_000_000.0, close=0.0,
            position_qty=0, max_position_pct=10.0,
        ) == 0

    def test_smaller_cap_invests_less(self):
        big = run_paper_trading_session(
            ["DEMO001"], data_mode="offline", days=20, max_position_pct=10.0,
        )
        small = run_paper_trading_session(
            ["DEMO001"], data_mode="offline", days=20, max_position_pct=1.0,
        )
        big_qty = sum(
            o["quantity"] for s in big.snapshots for o in s.filled_orders
            if o["side"] == "buy"
        )
        small_qty = sum(
            o["quantity"] for s in small.snapshots for o in s.filled_orders
            if o["side"] == "buy"
        )
        assert big_qty > 0
        assert small_qty < big_qty, "仓位上限收紧后买入量未减少，说明上限未生效"


# ═══════════════════════════════════════════════════════════════
#   六、默认策略与入参校验
# ═══════════════════════════════════════════════════════════════


class TestStrategyAndValidation:
    def test_ma_strategy_silent_before_warmup(self):
        ctx = StrategyContext(
            day_index=0, date="2025-06-01", symbol="AAA", close=10.0,
            closes=tuple([10.0] * _MA_LONG), position_qty=0,
            cash=1e6, portfolio_value=1e6,
        )
        assert ma_crossover_strategy(ctx) is None

    def test_ma_strategy_golden_cross_buys(self):
        closes = tuple([10.0] * _MA_LONG + [40.0])
        ctx = StrategyContext(
            day_index=30, date="2025-06-30", symbol="AAA", close=40.0,
            closes=closes, position_qty=0, cash=1e6, portfolio_value=1e6,
        )
        intent = ma_crossover_strategy(ctx)
        assert intent is not None and intent.side == "buy"

    def test_ma_strategy_does_not_sell_without_position(self):
        closes = tuple([40.0] * _MA_LONG + [1.0])
        ctx = StrategyContext(
            day_index=30, date="2025-06-30", symbol="AAA", close=1.0,
            closes=closes, position_qty=0, cash=1e6, portfolio_value=1e6,
        )
        intent = ma_crossover_strategy(ctx)
        assert intent is None, "无持仓时不应产生卖出意图"

    def test_empty_symbols_raises(self):
        with pytest.raises(ValueError, match="symbols"):
            run_paper_trading_session([], data_mode="offline")

    def test_zero_days_raises(self):
        with pytest.raises(ValueError, match="days"):
            run_paper_trading_session(["DEMO001"], data_mode="offline", days=0)

    def test_invalid_data_mode_raises(self):
        with pytest.raises(ValueError):
            run_paper_trading_session(["DEMO001"], data_mode="akshare", days=5)

    def test_unknown_side_raises(self):
        def bad(ctx: StrategyContext) -> TradeIntent:
            return TradeIntent(side="short", reason="非法方向", quantity=100)

        dates = _dates(5)
        fetcher = StubFetcher({"AAA": _outcome(_frame(dates, [10.0] * 5, symbol="AAA"))})
        with pytest.raises(ValueError, match="未知交易方向"):
            run_paper_trading_session(
                ["AAA"], data_mode="offline", days=5, strategy=bad, fetcher=fetcher,
            )

    def test_fewer_days_available_than_requested_warns(self):
        dates = _dates(5)
        fetcher = StubFetcher({"AAA": _outcome(_frame(dates, [10.0] * 5, symbol="AAA"))})
        r = run_paper_trading_session(
            ["AAA"], data_mode="offline", days=60,
            strategy=never_trade, fetcher=fetcher,
        )
        assert r.trading_days == 5
        assert any("交易日" in w for w in r.warnings)
