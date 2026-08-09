"""
策略回测 API
============
只做请求校验与序列化。取数、信号、指标全部在
``finance/backtest_service.py`` + ``finance/backtesting.py``：

* Sharpe / 波动率 / 回撤由 ``run_backtest`` 计算，本层不重算、不"美化"。
* 预热行只用于让 MA20 在请求区间首日已成熟；交易、净值、交易日数都只覆盖请求区间。
* MA20 历史不足时返回 400 并说明缺多少，而不是返回一条空结果。

异常映射（不再把所有错误笼统压成 503）：
  400 日期区间非法 / 策略不支持 / 区间无数据 / MA20 历史不足
  503 真实数据源与样例数据同时不可用
"""
from __future__ import annotations

from datetime import date
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_platform.finance.backtest_service import (
    SUPPORTED_STRATEGIES,
    BacktestError,
    run_strategy_backtest,
)
from agent_platform.finance.data_status import (
    MarketDataAllSourcesFailed,
    normalize_data_mode,
)
from agent_platform.finance.date_window import (
    DateRangeError,
    InsufficientHistoryError,
)
from agent_platform.finance.errors import InvalidSecuritySymbolError

router = APIRouter()


class BacktestRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    start: date | None = None
    end: date | None = None
    initial_capital: float = Field(1_000_000.0, gt=0)
    data_mode: str = Field("auto", pattern="^(offline|auto)$")
    strategy: str = Field("ma_crossover")


class TradeOut(BaseModel):
    entry_signal_date: str
    exit_signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float
    profit_loss: float
    signal: str
    direction: str


class EquityPoint(BaseModel):
    date: str
    equity: float
    nav: float


class BacktestResponse(BaseModel):
    symbol: str
    strategy: str
    # 实际回测区间（真实交易日），与 requested_* 区分开
    start_date: str
    end_date: str
    requested_start: str | None
    requested_end: str | None
    trading_days: int
    warmup_rows_used: int
    initial_capital: float
    final_equity: float
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    # 日历口径 Sharpe（空仓日记 0），比持仓口径更接近可实现收益
    sharpe_calendar: float
    annualized_volatility_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    time_in_market_pct: float
    equity_curve: list[EquityPoint]
    trades: list[TradeOut]
    source: str
    updated_at: str
    data_status: str
    fallback_reason: str | None
    disclaimer: str


@router.post("/backtest", response_model=BacktestResponse)
def backtest_strategy(req: BacktestRequest) -> BacktestResponse:
    if req.strategy not in SUPPORTED_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的策略：{req.strategy!r}。可选值：{', '.join(SUPPORTED_STRATEGIES)}",
        )
    try:
        mode = normalize_data_mode(req.data_mode)
        service = run_strategy_backtest(
            req.symbol,
            start=req.start,
            end=req.end,
            initial_capital=req.initial_capital,
            data_mode=mode,
            strategy=req.strategy,
        )
    except InsufficientHistoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DateRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BacktestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InvalidSecuritySymbolError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketDataAllSourcesFailed as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = service.result
    final_nav = service.equity_curve[-1]["nav"] if service.equity_curve else 1.0

    return BacktestResponse(
        symbol=service.symbol,
        strategy=service.strategy,
        start_date=service.start_date,
        end_date=service.end_date,
        requested_start=service.requested_start,
        requested_end=service.requested_end,
        trading_days=service.trading_days,
        warmup_rows_used=service.warmup_rows_used,
        initial_capital=service.initial_capital,
        final_equity=round(float(final_nav) * service.initial_capital, 2),
        total_return_pct=round(result.total_return_pct, 4),
        annualized_return_pct=round(result.annualized_return_pct, 4),
        sharpe_ratio=round(result.sharpe_ratio, 4),
        sharpe_calendar=round(result.sharpe_calendar, 4),
        annualized_volatility_pct=round(result.annualized_volatility_pct, 4),
        max_drawdown_pct=round(result.max_drawdown_pct, 4),
        win_rate_pct=round(result.win_rate_pct, 2),
        total_trades=result.total_trades,
        winning_trades=result.winning_trades,
        losing_trades=result.losing_trades,
        time_in_market_pct=round(result.time_in_market_pct, 2),
        equity_curve=[EquityPoint(**point) for point in service.equity_curve],
        trades=[TradeOut(**trade) for trade in service.trades],
        source=service.source,
        updated_at=service.updated_at,
        data_status=service.data_status,
        fallback_reason=service.fallback_reason,
        disclaimer=service.disclaimer,
    )
