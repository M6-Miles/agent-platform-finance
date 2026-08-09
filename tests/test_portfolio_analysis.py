"""多股票对比与组合分析模块单元测试。"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from agent_platform.finance.portfolio_analysis import (
    PortfolioComparisonResult,
    _compute_metrics,
    compare_securities,
)


# ── 测试数据工厂 ─────────────────────────────────────────────────────────────

def _price_df(symbol: str, closes: list[float], market: str = "上交所") -> pd.DataFrame:
    n = len(closes)
    dates = [date(2025, 1, 2 + i) for i in range(n)]
    return pd.DataFrame({
        "market": [market] * n,
        "symbol": [symbol] * n,
        "name": [f"{symbol}名称"] * n,
        "date": dates,
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "volume": [100_000] * n,
        "source": ["test"] * n,
        "updated_at": ["2025-01-02"] * n,
    })


def _mock_provider(price_map: dict[str, list[float]]) -> MagicMock:
    """返回按 symbol 返回不同 DataFrame 的 mock provider。"""
    provider = MagicMock()
    def get_price_history(symbol, start=None, end=None):
        key = symbol.strip().upper()
        if key not in price_map:
            raise ValueError(f"未找到证券代码 {symbol}")
        return _price_df(key, price_map[key])
    provider.get_price_history.side_effect = get_price_history
    return provider


# ── _compute_metrics ─────────────────────────────────────────────────────────

class TestComputeMetrics:
    def test_flat_price_zero_return(self) -> None:
        df = _price_df("X", [100.0] * 20)
        m = _compute_metrics("X", df)
        assert m.total_return_pct == pytest.approx(0.0)

    def test_rising_positive_return(self) -> None:
        df = _price_df("X", [100.0, 110.0, 121.0])
        m = _compute_metrics("X", df)
        assert m.total_return_pct == pytest.approx(21.0)

    def test_falling_negative_return(self) -> None:
        df = _price_df("X", [100.0, 90.0, 81.0])
        m = _compute_metrics("X", df)
        assert m.total_return_pct == pytest.approx(-19.0)

    def test_max_drawdown_correct(self) -> None:
        # Peak at 120, trough at 90 → drawdown = (90-120)/120 = -25%
        df = _price_df("X", [100.0, 120.0, 90.0])
        m = _compute_metrics("X", df)
        assert m.max_drawdown_pct == pytest.approx(-25.0)

    def test_volatility_zero_for_constant_price(self) -> None:
        df = _price_df("X", [100.0] * 20)
        m = _compute_metrics("X", df)
        assert m.annualized_volatility_pct == pytest.approx(0.0)

    def test_sharpe_positive_on_rising(self) -> None:
        # 有明显涨跌但整体向上，确保 std > 0 且 Sharpe > 0
        closes = [100.0, 103.0, 101.0, 105.0, 103.0, 108.0, 106.0, 111.0,
                  109.0, 114.0, 112.0, 117.0, 115.0, 120.0, 118.0, 123.0]
        df = _price_df("X", closes)
        m = _compute_metrics("X", df)
        assert m.sharpe_ratio > 0

    def test_trading_days_count(self) -> None:
        df = _price_df("X", [100.0] * 15)
        m = _compute_metrics("X", df)
        assert m.trading_days == 15

    def test_symbol_and_name(self) -> None:
        df = _price_df("ABC", [100.0, 101.0])
        m = _compute_metrics("ABC", df)
        assert m.symbol == "ABC"
        assert m.name == "ABC名称"


# ── compare_securities ────────────────────────────────────────────────────────

class TestCompareSecurities:
    def _provider_2(self) -> MagicMock:
        return _mock_provider({
            "DEMO001": [100.0, 105.0, 110.0, 108.0, 115.0],
            "DEMO002": [100.0, 98.0,  102.0, 100.0, 103.0],
        })

    def test_returns_result_object(self) -> None:
        result = compare_securities(["DEMO001", "DEMO002"], provider=self._provider_2())
        assert isinstance(result, PortfolioComparisonResult)

    def test_symbols_in_result(self) -> None:
        result = compare_securities(["DEMO001", "DEMO002"], provider=self._provider_2())
        assert set(result.symbols) == {"DEMO001", "DEMO002"}

    def test_normalized_returns_starts_at_100(self) -> None:
        result = compare_securities(["DEMO001", "DEMO002"], provider=self._provider_2())
        norm = result.normalized_returns
        for sym in result.symbols:
            assert sym in norm.columns
            first_valid = norm[sym].dropna().iloc[0]
            assert first_valid == pytest.approx(100.0)

    def test_correlation_matrix_shape(self) -> None:
        result = compare_securities(["DEMO001", "DEMO002"], provider=self._provider_2())
        corr = result.correlation_matrix
        assert corr.shape == (2, 2)

    def test_correlation_diagonal_is_1(self) -> None:
        result = compare_securities(["DEMO001", "DEMO002"], provider=self._provider_2())
        corr = result.correlation_matrix
        for sym in result.symbols:
            assert corr.loc[sym, sym] == pytest.approx(1.0)

    def test_metrics_count(self) -> None:
        result = compare_securities(["DEMO001", "DEMO002"], provider=self._provider_2())
        assert len(result.metrics) == 2

    def test_single_symbol_ok(self) -> None:
        provider = _mock_provider({"DEMO001": [100.0, 110.0, 105.0]})
        result = compare_securities(["DEMO001"], provider=provider)
        assert result.symbols == ["DEMO001"]

    def test_empty_symbols_raises(self) -> None:
        with pytest.raises(ValueError, match="至少需要"):
            compare_securities([], provider=self._provider_2())

    def test_too_many_symbols_raises(self) -> None:
        with pytest.raises(ValueError, match="最多"):
            compare_securities([f"S{i}" for i in range(11)], provider=self._provider_2())

    def test_all_symbols_fail_raises(self) -> None:
        provider = _mock_provider({})  # no valid symbols
        with pytest.raises(ValueError, match="所有证券"):
            compare_securities(["DEMO001"], provider=provider)

    def test_partial_failure_skips_bad_symbol(self) -> None:
        provider = _mock_provider({"DEMO001": [100.0, 105.0, 110.0]})
        # DEMO999 will fail, DEMO001 should succeed
        result = compare_securities(["DEMO001", "DEMO999"], provider=provider)
        assert result.symbols == ["DEMO001"]

    def test_metrics_dataframe_columns(self) -> None:
        result = compare_securities(["DEMO001", "DEMO002"], provider=self._provider_2())
        df = result.metrics_dataframe()
        for col in ("代码", "名称", "区间收益率(%)", "年化收益率(%)", "年化波动率(%)", "最大回撤(%)", "夏普比率"):
            assert col in df.columns

    def test_disclaimer_present(self) -> None:
        result = compare_securities(["DEMO001"], provider=_mock_provider({"DEMO001": [100.0, 101.0]}))
        assert len(result.disclaimer) > 0
