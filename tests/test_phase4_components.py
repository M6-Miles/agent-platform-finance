"""
Phase 4 组件测试：
  - Backtesting Engine
  - Evaluator Agent
  - Observability Panel
  - MockBroker
"""
from __future__ import annotations

import pandas as pd
from datetime import date

from agent_platform.finance.backtesting import run_backtest
from agent_platform.core.evaluator_agent import evaluate
from agent_platform.core.observability import ObservabilityPanel
from agent_platform.finance.mock_broker import (
    MockBroker, OrderSide, OrderStatus, OrderType
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Backtesting Engine Tests
# ═══════════════════════════════════════════════════════════════════════════

def test_backtest_simple_buy_hold():
    """简单买入持有策略。"""
    dates = [date(2024, 1, i) for i in range(1, 11)]
    prices = [100.0 + i * 2 for i in range(10)]  # 100 → 118
    df = pd.DataFrame({"date": dates, "close": prices})

    signals = [("2024-01-02", "buy"), ("2024-01-09", "sell")]
    result = run_backtest("TEST", df, signals, initial_capital=100_000.0)

    assert result.total_return_pct > 0
    assert result.total_trades == 1  # 1 buy+sell round trip = 1 trade
    assert result.sharpe_ratio is not None


def test_backtest_multiple_trades():
    """多次买卖。"""
    dates = [date(2024, 1, i) for i in range(1, 21)]
    prices = [100 + (i % 5) * 3 for i in range(20)]
    df = pd.DataFrame({"date": dates, "close": prices})

    signals = [
        ("2024-01-02", "buy"),
        ("2024-01-05", "sell"),
        ("2024-01-10", "buy"),
        ("2024-01-15", "sell"),
    ]
    result = run_backtest("TEST", df, signals)
    assert result.total_trades == 2  # 2 buy+sell round trips = 2 trades


def test_backtest_no_trades():
    """无交易信号。"""
    dates = [date(2024, 1, i) for i in range(1, 6)]
    prices = [100.0] * 5
    df = pd.DataFrame({"date": dates, "close": prices})
    result = run_backtest("TEST", df, [])

    assert result.total_trades == 0
    assert result.total_return_pct == 0.0
    assert result.sharpe_ratio == 0.0


def test_backtest_max_drawdown():
    """回撤计算。"""
    dates = [date(2024, 1, i) for i in range(1, 11)]
    prices = [100, 110, 105, 95, 90, 100, 110, 105, 95, 100]
    df = pd.DataFrame({"date": dates, "close": prices})

    signals = [("2024-01-02", "buy")]
    result = run_backtest("TEST", df, signals, initial_capital=100_000.0)

    # 从 110 跌到 90 = -18.2% 回撤
    assert result.max_drawdown_pct > 15.0
    assert result.max_drawdown_pct < 20.0


def test_backtest_win_rate():
    """胜率计算。"""
    dates = [date(2024, 1, i) for i in range(1, 21)]
    prices = [100, 105, 110, 108, 112, 115, 113, 118, 120, 119,
              122, 125, 123, 128, 130, 128, 132, 135, 133, 138]
    df = pd.DataFrame({"date": dates, "close": prices})

    signals = [
        ("2024-01-02", "buy"), ("2024-01-06", "sell"),  # profit
        ("2024-01-10", "buy"), ("2024-01-14", "sell"),  # profit
        ("2024-01-16", "buy"), ("2024-01-20", "sell"),  # profit
    ]
    result = run_backtest("TEST", df, signals)
    assert result.win_rate_pct > 90.0  # 3/3 wins


def test_backtest_equity_curve():
    """权益曲线正常性。"""
    dates = [date(2024, 1, i) for i in range(1, 11)]
    prices = [100 + i for i in range(10)]
    df = pd.DataFrame({"date": dates, "close": prices})

    signals = [("2024-01-02", "buy"), ("2024-01-09", "sell")]
    result = run_backtest("TEST", df, signals)

    assert len(result.equity_curve) >= 1
    assert result.equity_curve[0] == 1.0  # normalized start
    assert result.equity_curve[-1] > 1.0  # profit


# ═══════════════════════════════════════════════════════════════════════════
# 2. Evaluator Agent Tests
# ═══════════════════════════════════════════════════════════════════════════

def test_evaluator_synthesis_perfect():
    """完美输出：100 分。"""
    output = {
        "signal": "buy",
        "confidence": 0.75,
        "source": "synthesis_agent",
        "updated_at": "2024-01-01T00:00:00Z",
        "disclaimer": "仅供研究参考，不构成投资建议",
    }
    result = evaluate("synthesis", output)
    assert result.overall_score == 100.0
    assert len(result.issues) == 0


def test_evaluator_synthesis_missing_source():
    """缺少 source 字段。"""
    output = {
        "signal": "buy",
        "confidence": 0.75,
        "updated_at": "2024-01-01T00:00:00Z",
        "disclaimer": "仅供研究参考，不构成投资建议",
    }
    result = evaluate("synthesis", output)
    assert result.overall_score < 100.0
    assert any("source" in issue for issue in result.issues)


def test_evaluator_synthesis_logic_conflict():
    """高置信度但 sell 信号 → 逻辑矛盾。"""
    output = {
        "signal": "sell",
        "confidence": 0.75,
        "source": "synthesis_agent",
        "updated_at": "2024-01-01T00:00:00Z",
        "disclaimer": "仅供研究参考，不构成投资建议",
    }
    result = evaluate("synthesis", output)
    assert result.overall_score < 100.0
    assert any("矛盾" in issue for issue in result.issues)


def test_evaluator_synthesis_confidence_out_of_range():
    """置信度超出 [0,1] 范围。"""
    output = {
        "signal": "buy",
        "confidence": 1.5,
        "source": "synthesis_agent",
        "updated_at": "2024-01-01T00:00:00Z",
        "disclaimer": "仅供研究参考，不构成投资建议",
    }
    result = evaluate("synthesis", output)
    assert result.overall_score < 100.0
    assert any("超出" in issue for issue in result.issues)


def test_evaluator_forbidden_keyword():
    """包含违禁词。"""
    output = {
        "signal": "buy",
        "confidence": 0.8,
        "source": "synthesis_agent",
        "updated_at": "2024-01-01T00:00:00Z",
        "disclaimer": "绝对稳赚不赔",
    }
    result = evaluate("synthesis", output)
    assert result.overall_score < 100.0
    assert any("违禁词" in issue for issue in result.issues)


def test_evaluator_trader_perfect():
    """Trader 完美输出。"""
    output = {
        "signal": "buy",
        "position_pct_suggestion": 5.0,
        "source": "trader_agent",
        "updated_at": "2024-01-01T00:00:00Z",
        "disclaimer": "仅供研究参考，不构成投资建议",
    }
    result = evaluate("trader", output)
    assert result.overall_score == 100.0


def test_evaluator_trader_logic_conflict():
    """buy 信号但仓位为 0。"""
    output = {
        "signal": "buy",
        "position_pct_suggestion": 0.0,
        "source": "trader_agent",
        "updated_at": "2024-01-01T00:00:00Z",
        "disclaimer": "仅供研究参考，不构成投资建议",
    }
    result = evaluate("trader", output)
    assert result.overall_score < 100.0


# ═══════════════════════════════════════════════════════════════════════════
# 3. Observability Panel Tests
# ═══════════════════════════════════════════════════════════════════════════

def test_observability_empty():
    """空面板。"""
    panel = ObservabilityPanel()
    summary = panel.get_summary()
    assert summary["total_calls"] == 0
    assert summary["success_rate_pct"] == 0.0


def test_observability_single_call():
    """单次成功调用。"""
    panel = ObservabilityPanel()
    panel.record_call(
        agent_name="test_agent",
        task="test_task",
        duration_s=0.5,
        success=True,
        input_tokens=100,
        output_tokens=50,
    )
    summary = panel.get_summary()
    assert summary["total_calls"] == 1
    assert summary["success_rate_pct"] == 100.0
    assert summary["total_input_tokens"] == 100
    assert summary["total_output_tokens"] == 50


def test_observability_multiple_calls():
    """多次调用统计。"""
    panel = ObservabilityPanel()
    for i in range(10):
        panel.record_call(
            agent_name=f"agent_{i % 3}",
            task="task",
            duration_s=0.1 + i * 0.01,
            success=i % 4 != 0,  # 75% 成功率
            input_tokens=100,
            output_tokens=50,
        )
    summary = panel.get_summary()
    assert summary["total_calls"] == 10
    assert 70.0 <= summary["success_rate_pct"] <= 80.0


def test_observability_latency_percentiles():
    """延迟分位数。"""
    panel = ObservabilityPanel()
    durations = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    for d in durations:
        panel.record_call("agent", "task", d, True)

    summary = panel.get_summary()
    assert 0.4 <= summary["latency_p50_s"] <= 0.6
    assert 0.9 <= summary["latency_p95_s"] <= 1.0


def test_observability_guardrail_violations():
    """Guardrail 触发统计。"""
    panel = ObservabilityPanel()
    panel.record_call("agent1", "task", 0.5, True, guardrail_violations=["keyword_blocker"])
    panel.record_call("agent2", "task", 0.5, False, guardrail_violations=["rate_limiter", "schema_validator"])

    summary = panel.get_summary()
    assert summary["guardrail_violation_count"] == 3


def test_observability_per_agent_breakdown():
    """各 Agent 细分统计。"""
    panel = ObservabilityPanel()
    panel.record_call("agent_a", "task1", 0.5, True, input_tokens=100, output_tokens=50)
    panel.record_call("agent_a", "task2", 0.3, True, input_tokens=200, output_tokens=100)
    panel.record_call("agent_b", "task3", 1.0, False, input_tokens=50, output_tokens=25)

    summary = panel.get_summary()
    assert "agent_a" in summary["per_agent"]
    assert summary["per_agent"]["agent_a"]["calls"] == 2
    assert summary["per_agent"]["agent_a"]["success_rate_pct"] == 100.0
    assert summary["per_agent"]["agent_b"]["success_rate_pct"] == 0.0


def test_observability_sqlite_persists_across_instances(tmp_path):
    path = tmp_path / "observability.sqlite3"
    first = ObservabilityPanel(path)
    first.record_call(
        "agent_a", "task", 0.25, True,
        input_tokens=12, output_tokens=8,
        guardrail_violations=["test_guardrail"], retries=1,
    )

    second = ObservabilityPanel(path)
    summary = second.get_summary()

    assert summary["total_calls"] == 1
    assert summary["total_input_tokens"] == 12
    assert summary["recent_calls"][0]["agent_name"] == "agent_a"
    assert summary["recent_calls"][0]["guardrail_violations"] == ["test_guardrail"]


def test_observability_sqlite_reset_is_persistent(tmp_path):
    path = tmp_path / "observability.sqlite3"
    panel = ObservabilityPanel(path)
    panel.record_call("agent", "task", 0.1, True)
    panel.reset()

    assert ObservabilityPanel(path).get_summary()["total_calls"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 4. MockBroker Tests
# ═══════════════════════════════════════════════════════════════════════════

def test_mockbroker_initial_state():
    """初始化状态。"""
    broker = MockBroker(initial_cash=100_000.0)
    assert broker.cash == 100_000.0
    assert broker.portfolio_value() == 100_000.0
    assert len(broker.get_positions()) == 0


def test_mockbroker_market_order_buy():
    """市价买入。"""
    broker = MockBroker(initial_cash=100_000.0)
    order = broker.place_market_order("TEST", OrderSide.BUY, 100)

    assert order.status == OrderStatus.PENDING
    assert order.order_type == OrderType.MARKET

    filled = broker.tick("TEST", 50.0)
    assert len(filled) == 1
    assert order.status == OrderStatus.FILLED
    assert order.filled_price is not None
    assert order.filled_quantity == 100


def test_mockbroker_limit_order_buy_filled():
    """限价买入成交。"""
    broker = MockBroker(initial_cash=100_000.0)
    order = broker.place_limit_order("TEST", OrderSide.BUY, 100, limit_price=51.0)

    # 市价 50 < 限价 51 → 成交
    filled = broker.tick("TEST", 50.0)
    assert len(filled) == 1
    assert order.status == OrderStatus.FILLED


def test_mockbroker_limit_order_buy_not_filled():
    """限价买入未成交。"""
    broker = MockBroker(initial_cash=100_000.0)
    order = broker.place_limit_order("TEST", OrderSide.BUY, 100, limit_price=49.0)

    # 市价 50 > 限价 49 → 不成交
    filled = broker.tick("TEST", 50.0)
    assert len(filled) == 0
    assert order.status == OrderStatus.PENDING


def test_mockbroker_sell_with_position():
    """卖出持仓。"""
    broker = MockBroker(initial_cash=100_000.0)

    # 先买入
    buy_order = broker.place_market_order("TEST", OrderSide.BUY, 100)
    broker.tick("TEST", 50.0)
    assert buy_order.status == OrderStatus.FILLED

    # 再卖出
    sell_order = broker.place_market_order("TEST", OrderSide.SELL, 100)
    broker.tick("TEST", 55.0)
    assert sell_order.status == OrderStatus.FILLED
    assert len(broker.get_positions()) == 0


def test_mockbroker_sell_insufficient_position():
    """卖出持仓不足。"""
    broker = MockBroker(initial_cash=100_000.0)

    # 买入 50 股
    broker.place_market_order("TEST", OrderSide.BUY, 50)
    broker.tick("TEST", 50.0)

    # 尝试卖出 100 股
    sell_order = broker.place_market_order("TEST", OrderSide.SELL, 100)
    broker.tick("TEST", 55.0)

    assert sell_order.status == OrderStatus.REJECTED
    assert "持仓不足" in sell_order.reject_reason


def test_mockbroker_buy_insufficient_cash():
    """资金不足拒绝。"""
    broker = MockBroker(initial_cash=1_000.0)
    order = broker.place_market_order("TEST", OrderSide.BUY, 100)
    broker.tick("TEST", 50.0)  # 需要 ~5015 元（含滑点 + 佣金）

    assert order.status == OrderStatus.REJECTED
    assert "资金不足" in order.reject_reason


def test_mockbroker_position_pnl():
    """持仓盈亏计算。"""
    broker = MockBroker(initial_cash=100_000.0)
    broker.place_market_order("TEST", OrderSide.BUY, 100)
    broker.tick("TEST", 50.0)

    positions = broker.get_positions()
    pos = positions["TEST"]

    # 更新市价
    broker.tick("TEST", 60.0)
    assert pos.current_price == 60.0
    assert pos.unrealized_pnl > 0
    assert pos.unrealized_pnl_pct > 0


def test_mockbroker_portfolio_value():
    """组合总资产。"""
    broker = MockBroker(initial_cash=100_000.0)
    broker.place_market_order("TEST", OrderSide.BUY, 100)
    broker.tick("TEST", 50.0)

    # 市价上涨到 60
    broker.tick("TEST", 60.0)

    portfolio = broker.portfolio_value()
    assert portfolio > 100_000.0  # 盈利


def test_mockbroker_cancel_order():
    """撤单。"""
    broker = MockBroker(initial_cash=100_000.0)
    order = broker.place_limit_order("TEST", OrderSide.BUY, 100, limit_price=45.0)

    assert broker.cancel_order(order.order_id)
    assert order.status == OrderStatus.CANCELLED

    # 再次撤单失败
    assert not broker.cancel_order(order.order_id)


def test_mockbroker_summary():
    """汇总信息。"""
    broker = MockBroker(initial_cash=100_000.0)
    broker.place_market_order("TEST", OrderSide.BUY, 100)
    broker.tick("TEST", 50.0)

    summary = broker.summary()
    assert "cash" in summary
    assert "portfolio_value" in summary
    assert "total_pnl" in summary
    assert "disclaimer" in summary
    assert summary["total_trades"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 5. Preflight Status Classification Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPreflightStatusClassification:
    """验证 run_e2e_batch() 的 preflight 状态分类逻辑：
    仅 execute 计为完全通过，manual_review / block 各有独立标记，互不混淆。
    """

    # ── 内联复制 validate_deliverables.py 的分类/计数逻辑 ─────────────────

    def _classify(self, action: str) -> str:
        """与 run_e2e_batch() 保持一致的状态映射。"""
        if action == "execute":
            return "✅ execute"
        elif action == "manual_review":
            return "⚠️ manual_review"
        else:  # "block"
            return "⛔ block"

    def _count_summary(self, results: list[dict]) -> dict:
        """与 run_e2e_batch() 保持一致的汇总计数逻辑。"""
        n_execute = sum(1 for r in results if r.get("preflight") == "execute")
        n_review  = sum(1 for r in results if r.get("preflight") in ("manual_review", "需人工审批"))
        n_block   = sum(1 for r in results if r.get("preflight") == "block")
        n_har_req = sum(1 for r in results if "审批" in r.get("status", "") and "preflight" not in r)
        n_error   = sum(1 for r in results if r.get("preflight") == "error")
        n_review += n_har_req
        return {"execute": n_execute, "manual_review": n_review, "block": n_block, "error": n_error}

    # ── 单条状态映射 ─────────────────────────────────────────────────────

    def test_execute_gets_checkmark_status(self):
        """final_action == 'execute' → status 以 ✅ 开头。"""
        status = self._classify("execute")
        assert status.startswith("✅"), f"期望 ✅，实际: {status}"
        assert "execute" in status

    def test_manual_review_gets_warning_status(self):
        """final_action == 'manual_review' → status 以 ⚠️ 开头，不以 ✅ 开头。"""
        status = self._classify("manual_review")
        assert status.startswith("⚠️"), f"期望 ⚠️，实际: {status}"
        assert not status.startswith("✅"), "manual_review 不应被标记为 ✅"

    def test_block_gets_blocked_status(self):
        """final_action == 'block' → status 以 ⛔ 开头，不以 ✅ 开头。"""
        status = self._classify("block")
        assert status.startswith("⛔"), f"期望 ⛔，实际: {status}"
        assert not status.startswith("✅"), "block 不应被标记为 ✅"

    # ── 汇总计数 ─────────────────────────────────────────────────────────

    def test_execute_count_excludes_manual_review(self):
        """n_execute 不包含 manual_review 条目。"""
        results = [
            {"symbol": "A", "preflight": "execute",       "status": "✅ execute"},
            {"symbol": "B", "preflight": "manual_review", "status": "⚠️ manual_review"},
            {"symbol": "C", "preflight": "manual_review", "status": "⚠️ manual_review"},
            {"symbol": "D", "preflight": "block",         "status": "⛔ block"},
        ]
        counts = self._count_summary(results)
        assert counts["execute"] == 1,       f"期望 execute=1，实际={counts['execute']}"
        assert counts["manual_review"] == 2, f"期望 manual_review=2，实际={counts['manual_review']}"
        assert counts["block"] == 1,         f"期望 block=1，实际={counts['block']}"
        assert counts["error"] == 0

    def test_manual_review_counted_in_review_not_execute(self):
        """manual_review 统计到 n_review，绝不算入 n_execute。"""
        results = [
            {"symbol": "X", "preflight": "manual_review", "status": "⚠️ manual_review"},
        ]
        counts = self._count_summary(results)
        assert counts["execute"] == 0
        assert counts["manual_review"] == 1

    def test_error_results_counted_separately(self):
        """异常结果计入 n_error，不污染其他计数。"""
        results = [
            {"symbol": "ERR", "preflight": "error", "status": "❌ some error"},
        ]
        counts = self._count_summary(results)
        assert counts["error"] == 1
        assert counts["execute"] == 0
        assert counts["manual_review"] == 0
        assert counts["block"] == 0

    def test_har_approval_required_counted_as_review(self):
        """HumanApprovalRequired 触发的条目（无 preflight 键，status 含"审批"）归入 n_review。"""
        results = [
            {"symbol": "HAR", "status": "⚠️ 需人工审批", "note": "position > 10%"},
        ]
        counts = self._count_summary(results)
        assert counts["manual_review"] == 1
        assert counts["execute"] == 0

    def test_mixed_batch_summary(self):
        """混合批次：execute/manual_review/block/error 各一条，计数完全正确。"""
        results = [
            {"symbol": "E", "preflight": "execute",       "status": "✅ execute"},
            {"symbol": "M", "preflight": "manual_review", "status": "⚠️ manual_review"},
            {"symbol": "B", "preflight": "block",         "status": "⛔ block"},
            {"symbol": "X", "preflight": "error",         "status": "❌ timeout"},
        ]
        counts = self._count_summary(results)
        assert counts == {"execute": 1, "manual_review": 1, "block": 1, "error": 1}
