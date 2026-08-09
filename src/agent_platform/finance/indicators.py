from __future__ import annotations

import math

import pandas as pd


# ── 移动均线 ────────────────────────────────────────────────────────────────

def add_moving_average(data: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """简单移动均线（SMA），就地追加列 f"ma{window}"。"""
    if window < 1:
        raise ValueError("均线窗口必须大于 0")
    result = data.copy()
    result[f"ma{window}"] = result["close"].rolling(window=window, min_periods=1).mean()
    return result


def add_ema(data: pd.DataFrame, window: int = 12) -> pd.DataFrame:
    """指数移动均线（EMA），就地追加列 f"ema{window}"。"""
    if window < 1:
        raise ValueError("EMA 窗口必须大于 0")
    if data.empty:
        raise ValueError("行情数据不能为空")
    result = data.copy()
    result[f"ema{window}"] = (
        result["close"].ewm(span=window, adjust=False, min_periods=1).mean()
    )
    return result


# ── MACD ────────────────────────────────────────────────────────────────────

def add_macd(
    data: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD 指标，追加三列：
    - macd       : DIF（快线 EMA - 慢线 EMA）
    - macd_signal: DEA（DIF 的 EMA，即信号线）
    - macd_hist  : 柱状图（DIF - DEA），乘以 2 匹配常见显示惯例
    """
    if data.empty:
        raise ValueError("行情数据不能为空")
    if fast >= slow:
        raise ValueError("MACD 快线周期必须小于慢线周期")
    result = data.copy()
    close = result["close"].astype(float)
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=1).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=1).mean()
    result["macd"] = ema_fast - ema_slow
    result["macd_signal"] = result["macd"].ewm(span=signal, adjust=False, min_periods=1).mean()
    result["macd_hist"] = (result["macd"] - result["macd_signal"]) * 2
    return result


# ── RSI ─────────────────────────────────────────────────────────────────────

def add_rsi(data: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """相对强弱指数（RSI），追加列 "rsi"。
    使用 Wilder 平滑法（ewm com=period-1）。
    边界情况：
    - 初始数据不足 period 时填充中性值 50；
    - avg_loss=0 且 avg_gain>0（纯上涨）→ RSI=100；
    - avg_loss=0 且 avg_gain=0（横盘）→ RSI=50。
    """
    if data.empty:
        raise ValueError("行情数据不能为空")
    if period < 1:
        raise ValueError("RSI 周期必须大于 0")
    result = data.copy()
    delta = result["close"].astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    # 默认中性值（数据不足或横盘）
    rsi = pd.Series(50.0, index=result.index, dtype=float)
    valid = avg_gain.notna() & avg_loss.notna()
    has_loss = valid & (avg_loss > 0)
    pure_gain = valid & (avg_loss == 0) & (avg_gain > 0)

    rs = avg_gain[has_loss] / avg_loss[has_loss]
    rsi[has_loss] = 100.0 - (100.0 / (1.0 + rs))
    rsi[pure_gain] = 100.0
    result["rsi"] = rsi
    return result


# ── 布林带 ──────────────────────────────────────────────────────────────────

def add_bollinger_bands(
    data: pd.DataFrame,
    window: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """布林带，追加三列：bb_upper、bb_middle、bb_lower。
    中轨 = SMA(window)，上下轨 = 中轨 ± num_std * 滚动标准差。
    """
    if data.empty:
        raise ValueError("行情数据不能为空")
    if window < 1:
        raise ValueError("布林带窗口必须大于 0")
    result = data.copy()
    close = result["close"].astype(float)
    rolling = close.rolling(window=window, min_periods=1)
    middle = rolling.mean()
    std = rolling.std(ddof=0).fillna(0.0)
    result["bb_middle"] = middle
    result["bb_upper"] = middle + num_std * std
    result["bb_lower"] = middle - num_std * std
    return result


# ── 成交量均线 ───────────────────────────────────────────────────────────────

def add_volume_ma(data: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """成交量移动均线，追加列 f"volume_ma{window}"。"""
    if "volume" not in data.columns:
        raise ValueError("数据中缺少 volume 列")
    if window < 1:
        raise ValueError("成交量均线窗口必须大于 0")
    result = data.copy()
    result[f"volume_ma{window}"] = (
        result["volume"].astype(float).rolling(window=window, min_periods=1).mean()
    )
    return result


# ── KDJ ─────────────────────────────────────────────────────────────────────

def add_kdj(
    data: pd.DataFrame,
    k: int = 9,
    d: int = 3,
    j_weight: int = 3,
) -> pd.DataFrame:
    """KDJ 随机指标，追加三列：kdj_k、kdj_d、kdj_j。
    需要 high、low、close 列。
    - RSV = (close - low_N) / (high_N - low_N) * 100
    - K   = EWM(RSV, com=d-1)      → 1/d 平滑
    - D   = EWM(K,   com=d-1)
    - J   = j_weight*K - (j_weight-1)*D
    价格区间为零（high==low）时 RSV 填充中性值 50。
    """
    for col in ("high", "low", "close"):
        if col not in data.columns:
            raise ValueError(f"KDJ 需要 {col} 列")
    if data.empty:
        raise ValueError("行情数据不能为空")
    if k < 1 or d < 1:
        raise ValueError("KDJ 周期必须大于 0")

    result = data.copy()
    high_k = result["high"].astype(float).rolling(window=k, min_periods=1).max()
    low_k = result["low"].astype(float).rolling(window=k, min_periods=1).min()
    hl_range = (high_k - low_k).replace(0.0, float("nan"))
    rsv = ((result["close"].astype(float) - low_k) / hl_range) * 100
    rsv = rsv.fillna(50.0)

    result["kdj_k"] = rsv.ewm(com=d - 1, min_periods=1, adjust=False).mean()
    result["kdj_d"] = result["kdj_k"].ewm(com=d - 1, min_periods=1, adjust=False).mean()
    result["kdj_j"] = j_weight * result["kdj_k"] - (j_weight - 1) * result["kdj_d"]
    return result


# ── ATR ──────────────────────────────────────────────────────────────────────

def add_atr(data: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """平均真实波幅（ATR），追加列 "atr"。
    需要 high、low、close 列。
    true_range = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = Wilder EWM of TR (ewm com=period-1, min_periods=1)
    """
    for col in ("high", "low", "close"):
        if col not in data.columns:
            raise ValueError(f"ATR 需要 {col} 列")
    if data.empty:
        raise ValueError("行情数据不能为空")
    if period < 1:
        raise ValueError("ATR 周期必须大于 0")

    result = data.copy()
    high = result["high"].astype(float)
    low = result["low"].astype(float)
    close = result["close"].astype(float)
    prev_close = close.shift(1).fillna(close)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    result["atr"] = tr.ewm(com=period - 1, min_periods=1, adjust=False).mean()
    return result


# ── CCI ──────────────────────────────────────────────────────────────────────

def add_cci(data: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """商品通道指数（CCI），追加列 "cci"。
    需要 high、low、close 列。
    typical = (high + low + close) / 3
    CCI = (typical - SMA) / (0.015 * mean_abs_deviation)
    价格波动为零时 CCI 填充 0。
    """
    for col in ("high", "low", "close"):
        if col not in data.columns:
            raise ValueError(f"CCI 需要 {col} 列")
    if data.empty:
        raise ValueError("行情数据不能为空")
    if period < 1:
        raise ValueError("CCI 周期必须大于 0")

    result = data.copy()
    typical = (
        result["high"].astype(float)
        + result["low"].astype(float)
        + result["close"].astype(float)
    ) / 3
    rolling_mean = typical.rolling(period, min_periods=1).mean()
    mean_dev = typical.rolling(period, min_periods=1).apply(
        lambda x: float((abs(x - x.mean())).mean()), raw=True
    )
    denom = (0.015 * mean_dev).replace(0.0, float("nan"))
    result["cci"] = ((typical - rolling_mean) / denom).fillna(0.0)
    return result


# ── 汇总统计函数 ─────────────────────────────────────────────────────────────

def total_return(data: pd.DataFrame) -> float:
    if data.empty:
        raise ValueError("行情数据不能为空")
    first = float(data.iloc[0]["close"])
    last = float(data.iloc[-1]["close"])
    if first == 0:
        raise ValueError("首日收盘价不能为 0")
    return last / first - 1


def daily_returns(data: pd.DataFrame) -> pd.Series:
    if data.empty:
        raise ValueError("行情数据不能为空")
    return data["close"].astype(float).pct_change().dropna()


def annualized_volatility(data: pd.DataFrame, trading_days: int = 252) -> float:
    returns = daily_returns(data)
    if returns.empty:
        return 0.0
    return float(returns.std(ddof=0) * math.sqrt(trading_days))


def max_drawdown(data: pd.DataFrame) -> float:
    if data.empty:
        raise ValueError("行情数据不能为空")
    close = data["close"].astype(float)
    running_max = close.cummax()
    drawdown = close / running_max - 1
    return float(drawdown.min())
