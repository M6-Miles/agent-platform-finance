"""金融指标函数单元测试。"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from agent_platform.finance.indicators import (
    add_atr,
    add_bollinger_bands,
    add_cci,
    add_ema,
    add_kdj,
    add_macd,
    add_moving_average,
    add_rsi,
    add_volume_ma,
    annualized_volatility,
    max_drawdown,
    total_return,
)


# ── 测试数据工厂 ─────────────────────────────────────────────────────────────

def _df(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    data: dict[str, list] = {"close": closes}
    if volumes is not None:
        data["volume"] = volumes
    return pd.DataFrame(data)


# ── 原有指标 ─────────────────────────────────────────────────────────────────

def test_indicators_calculate_expected_values() -> None:
    data = _df([10.0, 11.0, 12.0, 9.0])
    with_ma = add_moving_average(data, window=2)
    assert with_ma["ma2"].round(2).tolist() == [10.0, 10.5, 11.5, 10.5]
    assert round(total_return(data), 4) == -0.1
    assert max_drawdown(data) == -0.25
    assert annualized_volatility(data) > 0


# ── add_moving_average ────────────────────────────────────────────────────────

class TestAddMovingAverage:
    def test_column_name(self) -> None:
        df = add_moving_average(_df([1.0, 2.0, 3.0]), window=3)
        assert "ma3" in df.columns

    def test_window_1_equals_close(self) -> None:
        df = add_moving_average(_df([5.0, 6.0, 7.0]), window=1)
        assert df["ma1"].tolist() == [5.0, 6.0, 7.0]

    def test_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError):
            add_moving_average(_df([1.0]), window=0)

    def test_original_unchanged(self) -> None:
        original = _df([1.0, 2.0, 3.0])
        add_moving_average(original, window=2)
        assert "ma2" not in original.columns


# ── add_ema ───────────────────────────────────────────────────────────────────

class TestAddEma:
    def test_column_name(self) -> None:
        df = add_ema(_df([1.0, 2.0, 3.0, 4.0, 5.0]), window=3)
        assert "ema3" in df.columns

    def test_length_preserved(self) -> None:
        data = _df([10.0, 11.0, 12.0, 9.0, 10.5])
        df = add_ema(data, window=3)
        assert len(df) == 5

    def test_ema_lags_sma_on_rising(self) -> None:
        # 线性上涨序列中，EMA 对近期数据权重更高，所以 EMA >= SMA
        closes = [100.0 + i for i in range(20)]
        data = _df(closes)
        df_ema = add_ema(data, window=5)
        df_sma = add_moving_average(data, window=5)
        assert df_ema["ema5"].iloc[-1] >= df_sma["ma5"].iloc[-1] - 1e-6

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            add_ema(pd.DataFrame({"close": []}), window=5)

    def test_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError):
            add_ema(_df([1.0]), window=0)


# ── add_macd ──────────────────────────────────────────────────────────────────

class TestAddMacd:
    def _data(self) -> pd.DataFrame:
        # 足够长才能让 MACD 稳定
        return _df([100.0 + i * 0.5 for i in range(40)])

    def test_columns_added(self) -> None:
        df = add_macd(self._data())
        for col in ("macd", "macd_signal", "macd_hist"):
            assert col in df.columns

    def test_hist_equals_dif_minus_dea_times_2(self) -> None:
        df = add_macd(self._data())
        expected = (df["macd"] - df["macd_signal"]) * 2
        pd.testing.assert_series_equal(df["macd_hist"], expected, check_names=False)

    def test_fast_must_be_less_than_slow(self) -> None:
        with pytest.raises(ValueError):
            add_macd(self._data(), fast=26, slow=12)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            add_macd(pd.DataFrame({"close": []}))

    def test_length_preserved(self) -> None:
        data = self._data()
        df = add_macd(data)
        assert len(df) == len(data)

    def test_rising_market_positive_macd(self) -> None:
        # 持续上涨时快线在慢线之上，DIF > 0
        data = _df([float(i) for i in range(1, 41)])
        df = add_macd(data)
        assert df["macd"].iloc[-1] > 0


# ── add_rsi ───────────────────────────────────────────────────────────────────

class TestAddRsi:
    def _data(self) -> pd.DataFrame:
        return _df([100.0 + i * 0.3 for i in range(20)])

    def test_column_added(self) -> None:
        df = add_rsi(self._data())
        assert "rsi" in df.columns

    def test_values_in_0_100(self) -> None:
        df = add_rsi(self._data())
        assert (df["rsi"] >= 0.0).all()
        assert (df["rsi"] <= 100.0).all()

    def test_overbought_on_strong_rally(self) -> None:
        # 连续大涨后 RSI 应进入超买区（>70）；用 30 条数据确保足够多的有效值
        data = _df([100.0 * (1.03 ** i) for i in range(30)])
        df = add_rsi(data, period=14)
        assert df["rsi"].iloc[-1] > 70

    def test_oversold_on_strong_drop(self) -> None:
        data = _df([100.0 * (0.97 ** i) for i in range(30)])
        df = add_rsi(data, period=14)
        assert df["rsi"].iloc[-1] < 30

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            add_rsi(pd.DataFrame({"close": []}))

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError):
            add_rsi(self._data(), period=0)

    def test_length_preserved(self) -> None:
        data = self._data()
        df = add_rsi(data)
        assert len(df) == len(data)


# ── add_bollinger_bands ───────────────────────────────────────────────────────

class TestAddBollingerBands:
    def _data(self) -> pd.DataFrame:
        return _df([100.0 + math.sin(i * 0.3) * 5 for i in range(30)])

    def test_columns_added(self) -> None:
        df = add_bollinger_bands(self._data())
        for col in ("bb_upper", "bb_middle", "bb_lower"):
            assert col in df.columns

    def test_upper_above_lower(self) -> None:
        df = add_bollinger_bands(self._data())
        assert (df["bb_upper"] >= df["bb_lower"]).all()

    def test_middle_between_upper_and_lower(self) -> None:
        df = add_bollinger_bands(self._data())
        assert (df["bb_middle"] <= df["bb_upper"] + 1e-9).all()
        assert (df["bb_middle"] >= df["bb_lower"] - 1e-9).all()

    def test_constant_price_no_band(self) -> None:
        # 价格恒定时上下轨相等
        df = add_bollinger_bands(_df([100.0] * 25))
        assert (df["bb_upper"] == df["bb_lower"]).all()

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            add_bollinger_bands(pd.DataFrame({"close": []}))

    def test_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError):
            add_bollinger_bands(self._data(), window=0)

    def test_length_preserved(self) -> None:
        data = self._data()
        df = add_bollinger_bands(data)
        assert len(df) == len(data)


# ── add_volume_ma ─────────────────────────────────────────────────────────────

class TestAddVolumeMa:
    def _data(self) -> pd.DataFrame:
        return _df([100.0, 101.0, 102.0], volumes=[1000.0, 2000.0, 3000.0])

    def test_column_added(self) -> None:
        df = add_volume_ma(self._data(), window=2)
        assert "volume_ma2" in df.columns

    def test_first_value_equals_first_volume(self) -> None:
        df = add_volume_ma(self._data(), window=3)
        assert df["volume_ma3"].iloc[0] == 1000.0

    def test_last_value_correct(self) -> None:
        df = add_volume_ma(self._data(), window=2)
        assert df["volume_ma2"].iloc[-1] == pytest.approx(2500.0)

    def test_missing_volume_column_raises(self) -> None:
        with pytest.raises(ValueError):
            add_volume_ma(_df([1.0, 2.0, 3.0]), window=2)

    def test_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError):
            add_volume_ma(self._data(), window=0)


# ── add_kdj ───────────────────────────────────────────────────────────────────

def _ohlc(n: int = 30) -> "pd.DataFrame":
    import pandas as pd
    import math
    closes = [100.0 + math.sin(i * 0.3) * 10 for i in range(n)]
    highs  = [c + 2.0 for c in closes]
    lows   = [c - 2.0 for c in closes]
    return pd.DataFrame({"close": closes, "high": highs, "low": lows})


class TestAddKdj:
    def test_columns_added(self) -> None:
        df = add_kdj(_ohlc())
        for col in ("kdj_k", "kdj_d", "kdj_j"):
            assert col in df.columns

    def test_length_preserved(self) -> None:
        data = _ohlc()
        df = add_kdj(data)
        assert len(df) == len(data)

    def test_j_equals_3k_minus_2d(self) -> None:
        df = add_kdj(_ohlc())
        expected_j = 3 * df["kdj_k"] - 2 * df["kdj_d"]
        import pandas as pd
        pd.testing.assert_series_equal(df["kdj_j"], expected_j, check_names=False)

    def test_k_d_in_0_100_roughly(self) -> None:
        df = add_kdj(_ohlc())
        assert (df["kdj_k"] >= 0).all() and (df["kdj_k"] <= 100).all()
        assert (df["kdj_d"] >= 0).all() and (df["kdj_d"] <= 100).all()

    def test_missing_column_raises(self) -> None:
        import pandas as pd
        with pytest.raises(ValueError):
            add_kdj(pd.DataFrame({"close": [1.0, 2.0], "high": [2.0, 3.0]}))

    def test_empty_raises(self) -> None:
        import pandas as pd
        with pytest.raises(ValueError):
            add_kdj(pd.DataFrame({"close": [], "high": [], "low": []}))

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError):
            add_kdj(_ohlc(), k=0)


# ── add_atr ───────────────────────────────────────────────────────────────────

class TestAddAtr:
    def test_column_added(self) -> None:
        df = add_atr(_ohlc())
        assert "atr" in df.columns

    def test_atr_positive(self) -> None:
        df = add_atr(_ohlc())
        assert (df["atr"] > 0).all()

    def test_length_preserved(self) -> None:
        data = _ohlc()
        df = add_atr(data)
        assert len(df) == len(data)

    def test_constant_range_gives_constant_atr(self) -> None:
        import pandas as pd
        data = pd.DataFrame({"close": [100.0]*20, "high": [102.0]*20, "low": [98.0]*20})
        df = add_atr(data, period=5)
        # With constant range=4, ATR converges to 4
        assert df["atr"].iloc[-1] == pytest.approx(4.0, abs=0.1)

    def test_missing_column_raises(self) -> None:
        with pytest.raises(ValueError):
            add_atr(_df([1.0, 2.0]))

    def test_empty_raises(self) -> None:
        import pandas as pd
        with pytest.raises(ValueError):
            add_atr(pd.DataFrame({"close": [], "high": [], "low": []}))

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError):
            add_atr(_ohlc(), period=0)


# ── add_cci ───────────────────────────────────────────────────────────────────

class TestAddCci:
    def test_column_added(self) -> None:
        df = add_cci(_ohlc())
        assert "cci" in df.columns

    def test_length_preserved(self) -> None:
        data = _ohlc()
        df = add_cci(data)
        assert len(df) == len(data)

    def test_overbought_on_strong_rally(self) -> None:
        import pandas as pd
        closes = [100.0 * (1.02 ** i) for i in range(30)]
        data = pd.DataFrame({"close": closes, "high": [c*1.01 for c in closes], "low": [c*0.99 for c in closes]})
        df = add_cci(data, period=20)
        assert df["cci"].iloc[-1] > 100

    def test_missing_column_raises(self) -> None:
        with pytest.raises(ValueError):
            add_cci(_df([1.0, 2.0]))

    def test_empty_raises(self) -> None:
        import pandas as pd
        with pytest.raises(ValueError):
            add_cci(pd.DataFrame({"close": [], "high": [], "low": []}))

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError):
            add_cci(_ohlc(), period=0)
