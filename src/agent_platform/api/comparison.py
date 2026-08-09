"""
多股对比 API
============
只做请求校验与序列化，数学口径全部在 ``finance/comparison_service.py``：
对齐共同交易日、真实日收益、对称相关矩阵、以及与回测页同一口径的 Sharpe。

前端只负责渲染本响应，不得在浏览器里生成任何价格、指标或相关系数。
"""
from __future__ import annotations

from datetime import date
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_platform.finance.comparison_service import ComparisonError, compare_symbols
from agent_platform.finance.data_status import (
    MarketDataAllSourcesFailed,
    normalize_data_mode,
)
from agent_platform.finance.date_window import DateRangeError

router = APIRouter()


class ComparisonRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=2, max_length=10)
    start: date | None = None
    end: date | None = None
    data_mode: str = Field("auto", pattern="^(offline|auto)$")


class SymbolMetricsOut(BaseModel):
    symbol: str
    name: str
    trading_days: int
    latest_close: float
    total_return_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    # 明确命名：上涨交易日占比。买入持有没有"交易"，故不提供 win_rate。
    up_day_ratio_pct: float
    normalized_returns: list[float]
    source: str
    updated_at: str
    data_status: str
    fallback_reason: str | None


class ComparisonResponse(BaseModel):
    symbols: list[str]
    # 共同交易日的实际日期（前端 X 轴只能用这些真实日期）
    dates: list[str]
    trading_days: int
    stocks: list[SymbolMetricsOut]
    correlation_matrix: dict[str, dict[str, float]]
    failed_symbols: dict[str, str]
    source: str
    updated_at: str
    data_status: str
    fallback_reason: str | None
    disclaimer: str


@router.post("/comparison", response_model=ComparisonResponse)
def multi_stock_comparison(req: ComparisonRequest) -> ComparisonResponse:
    try:
        mode = normalize_data_mode(req.data_mode)
        result = compare_symbols(
            req.symbols,
            start=req.start,
            end=req.end,
            data_mode=mode,
        )
    except DateRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ComparisonError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MarketDataAllSourcesFailed as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ComparisonResponse(
        symbols=result.symbols,
        dates=result.dates,
        trading_days=result.trading_days,
        stocks=[
            SymbolMetricsOut(
                symbol=s.symbol,
                name=s.name,
                trading_days=s.trading_days,
                latest_close=s.latest_close,
                total_return_pct=s.total_return_pct,
                annualized_volatility_pct=s.annualized_volatility_pct,
                sharpe_ratio=s.sharpe_ratio,
                max_drawdown_pct=s.max_drawdown_pct,
                up_day_ratio_pct=s.up_day_ratio_pct,
                normalized_returns=s.normalized_returns,
                source=s.source,
                updated_at=s.updated_at,
                data_status=s.data_status,
                fallback_reason=s.fallback_reason,
            )
            for s in result.stocks
        ],
        correlation_matrix=result.correlation_matrix,
        failed_symbols=result.failed_symbols,
        source=result.source,
        updated_at=result.updated_at,
        data_status=result.data_status,
        fallback_reason=result.fallback_reason,
        disclaimer=result.disclaimer,
    )
