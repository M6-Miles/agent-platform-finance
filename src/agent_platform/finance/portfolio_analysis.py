"""多股票对比与组合分析模块。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass
from datetime import date

import pandas as pd

from agent_platform.finance.constants import DISCLAIMER
from agent_platform.finance.indicators import (
    annualized_volatility,
    max_drawdown,
    total_return,
)
from agent_platform.finance.market_data_provider import MarketDataProvider
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider


# ── 单只股票汇总指标 ──────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SecurityMetrics:
    symbol: str
    name: str
    market: str
    total_return_pct: float
    annualized_return_pct: float
    annualized_volatility_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float          # 简化 Sharpe：年化收益 / 年化波动（无风险利率=0）
    trading_days: int


# ── 对比结果容器 ───────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PortfolioComparisonResult:
    symbols: list[str]
    names: list[str]
    start_date: str
    end_date: str
    source: str
    normalized_returns: pd.DataFrame   # columns: date + 每只股票代码（起点=100）
    correlation_matrix: pd.DataFrame   # 日收益率相关性矩阵
    metrics: list[SecurityMetrics]
    disclaimer: str

    def metrics_dataframe(self) -> pd.DataFrame:
        """把 metrics 转为可展示的 DataFrame。"""
        return pd.DataFrame(
            [
                {
                    "代码": m.symbol,
                    "名称": m.name,
                    "市场": m.market,
                    "区间收益率(%)": round(m.total_return_pct, 2),
                    "年化收益率(%)": round(m.annualized_return_pct, 2),
                    "年化波动率(%)": round(m.annualized_volatility_pct, 2),
                    "最大回撤(%)": round(m.max_drawdown_pct, 2),
                    "夏普比率": round(m.sharpe_ratio, 3),
                    "交易日数": m.trading_days,
                }
                for m in self.metrics
            ]
        )


# ── 核心分析函数 ──────────────────────────────────────────────────────────────

def _compute_metrics(symbol: str, df: pd.DataFrame) -> SecurityMetrics:
    """计算单只股票的汇总指标，复用 indicators.py 的纯函数。"""
    n = len(df)

    # 区间收益率 — 复用 indicators.py
    tr = total_return(df)

    # 年化收益率（连续复利近似：按 252 交易日/年换算）
    annual_factor = 252 / n if n > 0 else 1
    annualized_return = (1 + tr) ** annual_factor - 1

    # 年化波动率 — 复用 indicators.py
    ann_vol = annualized_volatility(df)

    # 最大回撤 — 复用 indicators.py
    max_dd = max_drawdown(df)

    # 简化 Sharpe（无风险利率 = 0）
    sharpe = (annualized_return / ann_vol) if ann_vol > 1e-9 else 0.0

    return SecurityMetrics(
        symbol=symbol,
        name=str(df["name"].iloc[-1]),
        market=str(df["market"].iloc[-1]),
        total_return_pct=tr * 100,
        annualized_return_pct=annualized_return * 100,
        annualized_volatility_pct=ann_vol * 100,
        max_drawdown_pct=max_dd * 100,
        sharpe_ratio=sharpe,
        trading_days=n,
    )


def compare_securities(
    symbols: list[str],
    start: date | None = None,
    end: date | None = None,
    provider: MarketDataProvider | None = None,
) -> PortfolioComparisonResult:
    """拉取多只股票的日线数据并计算对比指标。

    Args:
        symbols: 证券代码列表（2-10 只）
        start: 分析开始日期（None=数据最早）
        end: 分析结束日期（None=今天）
        provider: MarketDataProvider 实例（None=使用样例数据）

    Returns:
        PortfolioComparisonResult 对象

    Raises:
        ValueError: symbols 为空或长度超过 10
    """
    if not symbols:
        raise ValueError("至少需要 1 只证券")
    if len(symbols) > 10:
        raise ValueError("最多同时对比 10 只证券")

    data_provider = provider or SampleMarketDataProvider()

    # 并行拉取各股票价格，最多5个并发（Stooq 数据源对并发较友好）
    price_dict: dict[str, pd.DataFrame] = {}
    errors: list[str] = []

    def _fetch(sym: str) -> tuple[str, pd.DataFrame | None, str | None]:
        try:
            df = data_provider.get_price_history(sym, start=start, end=end)
            return sym, df.sort_values("date").reset_index(drop=True), None
        except Exception as exc:
            return sym, None, str(exc)

    _FETCH_TIMEOUT = 30  # 单只股票拉取超时 30 秒
    max_workers = min(len(symbols), 5)
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {executor.submit(_fetch, sym): sym for sym in symbols}
        done, not_done = futures_wait(list(futures), timeout=_FETCH_TIMEOUT)
        # 超时未完成的任务：取消并记录错误
        for future in not_done:
            future.cancel()
            sym = futures[future]
            errors.append(f"{sym}: 拉取超时（>{_FETCH_TIMEOUT}s）")
        # 处理已完成的任务
        for future in done:
            try:
                sym, df, err = future.result()
            except Exception as exc:
                sym = futures[future]
                df = None
                err = str(exc)
            if df is not None:
                price_dict[sym] = df
            else:
                errors.append(f"{sym}: {err}")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if not price_dict:
        error_detail = "；".join(errors)
        raise ValueError(f"所有证券数据获取失败：{error_detail}")

    valid_symbols = list(price_dict.keys())

    # 归一化收益率曲线（起点=100）
    all_dates = sorted({d for df in price_dict.values() for d in df["date"].tolist()})
    normalized = pd.DataFrame({"date": all_dates})
    daily_returns_dict: dict[str, pd.Series] = {}

    for sym, df in price_dict.items():
        date_to_close: dict = dict(zip(df["date"], df["close"].astype(float)))
        raw = pd.Series([date_to_close.get(d, float("nan")) for d in all_dates], dtype=float)
        raw = raw.ffill()
        first_valid = raw.dropna().iloc[0] if not raw.dropna().empty else None
        if first_valid and first_valid != 0:
            normalized[sym] = raw / first_valid * 100
        else:
            normalized[sym] = raw
        # 日收益率用于计算相关性
        daily_returns_dict[sym] = raw.pct_change().dropna()

    # 相关性矩阵（仅 2 只及以上时有意义）
    if len(valid_symbols) >= 2:
        rets_df = pd.DataFrame({sym: daily_returns_dict[sym] for sym in valid_symbols})
        corr_matrix = rets_df.corr()
    else:
        corr_matrix = pd.DataFrame([[1.0]], index=valid_symbols, columns=valid_symbols)

    # 逐只计算指标
    metrics = [_compute_metrics(sym, price_dict[sym]) for sym in valid_symbols]

    # 数据来源摘要
    sources = sorted({str(df["source"].iloc[-1]) for df in price_dict.values()})
    source_str = "；".join(sources)

    start_dates = [str(df["date"].iloc[0]) for df in price_dict.values()]
    end_dates   = [str(df["date"].iloc[-1]) for df in price_dict.values()]

    return PortfolioComparisonResult(
        symbols=valid_symbols,
        names=[m.name for m in metrics],
        start_date=min(start_dates),
        end_date=max(end_dates),
        source=source_str,
        normalized_returns=normalized,
        correlation_matrix=corr_matrix,
        metrics=metrics,
        disclaimer=DISCLAIMER,
    )
