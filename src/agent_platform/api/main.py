from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager
from datetime import date
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent_platform import __version__
from agent_platform.config import get_settings
from agent_platform.finance.errors import (
    InvalidSecuritySymbolError,
    MarketDataDependencyError,
    MarketDataUnavailableError,
)
from agent_platform.logging_config import configure_logging
from agent_platform.services.application_service import (
    ApplicationService,
    resolve_research_status,
)

# 导入子路由
from agent_platform.api.comparison import router as comparison_router
from agent_platform.api.backtest import router as backtest_router
from agent_platform.api.chat import router as chat_router

configure_logging()


class SessionCreateRequest(BaseModel):
    title: str | None = None


class SecurityAnalysisResponse(BaseModel):
    market: str
    symbol: str
    name: str
    start_date: str
    end_date: str
    source: str
    updated_at: str
    # ── 请求区间语义（实际交易日数，不是日历天数估算）────────────────────
    trading_days: int = 0
    warmup_rows_used: int = 0
    requested_start: str | None = None
    requested_end: str | None = None
    # ── 真实指标序列；未成熟点为 null，前端必须画成缺口而非 0 ─────────────
    series: list[dict[str, Any]] = []
    total_return_pct: float
    annualized_volatility_pct: float
    max_drawdown_pct: float
    latest_close: float
    latest_ma5: float
    latest_ma20: float
    latest_rsi: float
    latest_macd: float
    latest_macd_signal: float
    latest_bb_upper: float
    latest_bb_lower: float
    latest_bb_position_pct: float
    latest_kdj_k: float
    latest_kdj_d: float
    latest_kdj_j: float
    latest_atr: float
    latest_cci: float
    latest_ema12: float
    latest_ema26: float
    disclaimer: str
    data_status: str
    fallback_reason: str | None


# ── ApplicationService 模块级单例 ─────────────────────────────────────────────
# 必须是单例：ApplicationService.__init__ 中初始化 LangGraph 图（含 checkpointer），
# 每次请求新建会丢失 checkpoint，interrupt 后无法 resume。
_app_service: ApplicationService | None = None


def get_application_service() -> ApplicationService:
    global _app_service
    if _app_service is None:
        _app_service = ApplicationService()
    return _app_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理：启动时初始化单例，关闭时显式释放 SQLite 连接。"""
    # 启动：确保单例已创建（预热，可选）
    get_application_service()
    yield
    # 关闭：显式关闭 SQLite checkpoint 连接，避免 Windows 下文件占用
    global _app_service
    if _app_service is not None:
        _app_service.close()
        _app_service = None


app = FastAPI(
    title=get_settings().app_name,
    version=__version__,
    description="本地演示版：离线 Mock Agent + 证券行情数据 + SQLite 历史。",
    lifespan=lifespan,
)

# 允许前端 file:// 及本地开发服务器跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

# 注册子路由
app.include_router(comparison_router, tags=["Comparison"])
app.include_router(backtest_router, tags=["Backtest"])
app.include_router(chat_router, tags=["Chat"])


# ── 深度投研 Pydantic 模型 ────────────────────────────────────────────────────

class ResearchDecision(str, Enum):
    approve = "approve"
    reject  = "reject"


class ResearchStartResponse(BaseModel):
    symbol: str
    run_id: str
    thread_id: str
    status: str
    duration_s: float
    final_action: str | None = None
    errors: list[str] = []


class ResearchStateResponse(BaseModel):
    thread_id: str
    status: str                      # completed | interrupted | blocked | failed | not_found | no_trade
    interrupt_payload: dict | None = None
    final_action: str | None = None
    errors: list[str] = []
    # 新增字段：供前端展示完整 Agent 结果
    symbol: str | None = None
    run_id: str | None = None
    data_mode: str | None = None
    technical_analysis: dict | None = None
    fundamental_analysis: dict | None = None
    industry_analysis: dict | None = None
    market_regime: dict | None = None
    synthesis: dict | None = None
    trade_signal: dict | None = None
    risk_result: dict | None = None
    confidence: float | None = None
    # 真实执行轨迹：trace_entries 仅包含实际运行过的节点（含耗时与状态）
    trace_entries: list[dict] = []
    # 由 trace_entries 派生的已执行节点名集合，供前端区分"已执行/跳过/等待"
    executed_nodes: list[str] = []


class ResearchResumeResponse(BaseModel):
    thread_id: str
    decision: str
    status: str
    final_action: str | None = None
    errors: list[str] = []


_HTML_FILE = pathlib.Path(__file__).parents[3] / "frontend_prototype.html"


@app.get("/", include_in_schema=False)
def root():
    from fastapi.responses import FileResponse
    return FileResponse(
        str(_HTML_FILE),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "llm_provider": settings.llm_provider,
        "market_data_provider": settings.market_data_provider,
        "storage": "sqlite",
        "version": __version__,
    }


@app.get("/securities")
def list_securities() -> list[dict[str, str]]:
    try:
        return [
            {
                "market": info.market,
                "symbol": info.symbol,
                "name": info.name,
                "source": info.source,
                "updated_at": info.updated_at,
            }
            for info in get_application_service().list_securities()
        ]
    except MarketDataDependencyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except MarketDataUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/analysis/{symbol}", response_model=SecurityAnalysisResponse)
def security_analysis(
    symbol: str,
    start: date | None = None,
    end: date | None = None,
    data_mode: str = Query("auto", pattern="^(offline|auto)$"),
) -> SecurityAnalysisResponse:
    """证券分析端点（offline / auto）。

    统一走 ``analysis_service.analyze_window``：
    - 后端校验 ``start < end <= today``；
    - 指标可用 start 之前的预热数据计算，但返回行与交易日计数只含请求区间；
    - 指标序列在回看窗口未填满处返回 null（图表画缺口，绝不补 0）。
    """
    from agent_platform.finance.analysis_service import (
        AnalysisError,
        analyze_window,
    )
    from agent_platform.finance.data_status import MarketDataAllSourcesFailed
    from agent_platform.finance.date_window import (
        DateRangeError,
        InsufficientHistoryError,
    )

    try:
        window_result = analyze_window(
            symbol, start=start, end=end, data_mode=data_mode
        )
        result = window_result.result

        # 记录到存储（可选）。trigger 沿用既有契约 "direct"：
        # 用户直接请求分析端点 → direct；Agent 工具调用 → agent_tool。
        try:
            get_application_service().store.add_analysis(
                result, trigger="direct", session_id=None
            )
        except Exception:
            pass  # 存储失败不影响响应

    except InvalidSecuritySymbolError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketDataDependencyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except (MarketDataUnavailableError, MarketDataAllSourcesFailed) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (DateRangeError, InsufficientHistoryError, AnalysisError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SecurityAnalysisResponse(
        trading_days=window_result.trading_days,
        warmup_rows_used=window_result.warmup_rows_used,
        requested_start=window_result.requested_start,
        requested_end=window_result.requested_end,
        series=window_result.series,
        market=result.market,
        symbol=result.symbol,
        name=result.name,
        start_date=result.start_date,
        end_date=result.end_date,
        source=result.source,
        updated_at=result.updated_at,
        total_return_pct=round(result.total_return_pct, 4),
        annualized_volatility_pct=round(result.annualized_volatility_pct, 4),
        max_drawdown_pct=round(result.max_drawdown_pct, 4),
        latest_close=round(result.latest_close, 4),
        latest_ma5=round(result.latest_ma5, 4),
        latest_ma20=round(result.latest_ma20, 4),
        latest_rsi=round(result.latest_rsi, 4),
        latest_macd=round(result.latest_macd, 6),
        latest_macd_signal=round(result.latest_macd_signal, 6),
        latest_bb_upper=round(result.latest_bb_upper, 4),
        latest_bb_lower=round(result.latest_bb_lower, 4),
        latest_bb_position_pct=round(result.latest_bb_position_pct, 2),
        latest_kdj_k=round(result.latest_kdj_k, 4),
        latest_kdj_d=round(result.latest_kdj_d, 4),
        latest_kdj_j=round(result.latest_kdj_j, 4),
        latest_atr=round(result.latest_atr, 4),
        latest_cci=round(result.latest_cci, 4),
        latest_ema12=round(result.latest_ema12, 4),
        latest_ema26=round(result.latest_ema26, 4),
        disclaimer=result.disclaimer,
        data_status=result.data_status,
        fallback_reason=result.fallback_reason,
    )


@app.post("/sessions")
def create_session(request: SessionCreateRequest) -> dict[str, str]:
    record = get_application_service().create_session(request.title)
    return record_to_dict(record)


@app.get("/sessions")
def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    return [
        record_to_dict(record)
        for record in get_application_service().list_sessions(limit)
    ]


@app.get("/sessions/{session_id}/messages")
def list_messages(session_id: str) -> list[dict[str, Any]]:
    try:
        records = get_application_service().list_messages(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [record_to_dict(record) for record in records]


@app.get("/analysis-history")
def list_analysis_history(
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    return [
        record_to_dict(record)
        for record in get_application_service().list_analysis_history(limit)
    ]


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, str]:
    try:
        get_application_service().delete_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "session_id": session_id}


@app.patch("/sessions/{session_id}")
def rename_session(session_id: str, title: str = Query(..., min_length=1, max_length=200)) -> dict[str, str]:
    try:
        get_application_service().rename_session(session_id, title)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "renamed", "session_id": session_id}


class PriceBar(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class QuoteResponse(BaseModel):
    symbol: str
    name: str
    price: float
    prev_close: float
    change_pct: float
    market: str
    source: str
    data_status: str
    fallback_reason: str | None


@app.get("/price-history/{symbol}", response_model=list[PriceBar])
def price_history(
    symbol: str,
    start: date | None = None,
    end: date | None = None,
    data_mode: str = Query("auto", pattern="^(offline|auto)$"),
) -> list[PriceBar]:
    """返回指定证券在**请求区间内**的日线 OHLCV，供前端图表与原始数据表使用。

    - 后端校验 ``start < end <= today``（非法区间返回 400）。
    - 不取预热数据：本端点是"原始行情"，返回行必须严格落在 [start, end]。
    - offline 模式零网络调用；auto 模式失败时降级到样例数据（状态在
      ``/analysis`` 中暴露；本端点只保证日期不越界）。
    """
    from agent_platform.finance.data_status import (
        MarketDataAllSourcesFailed,
        fetch_price_history,
    )
    from agent_platform.finance.date_window import (
        DateRangeError,
        assert_dates_in_window,
        build_window,
        split_warmup,
    )

    try:
        window = build_window(start, end, warmup_trading_days=0)
        outcome = fetch_price_history(
            symbol, data_mode=data_mode, start=window.start, end=window.end
        )
        in_window, _ = split_warmup(outcome.frame, window)
        assert_dates_in_window(in_window["date"], window, label="行情")
    except DateRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InvalidSecuritySymbolError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketDataDependencyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except (MarketDataUnavailableError, MarketDataAllSourcesFailed) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return [
        PriceBar(
            date=row["date"].isoformat(),
            open=round(float(row["open"]), 4),
            high=round(float(row["high"]), 4),
            low=round(float(row["low"]), 4),
            close=round(float(row["close"]), 4),
            volume=round(float(row["volume"]), 0),
        )
        for _, row in in_window.iterrows()
    ]


@app.get("/quote/{symbol}", response_model=QuoteResponse)
def quote(symbol: str) -> QuoteResponse:
    """返回实时行情报价及涨跌幅，供模拟盘初始化使用。"""
    try:
        data = get_application_service().get_realtime_quote(symbol)
    except Exception as exc:
        if isinstance(exc, InvalidSecuritySymbolError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, (MarketDataDependencyError, MarketDataUnavailableError)):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return QuoteResponse(
        symbol=data["symbol"],
        name=data["name"],
        price=round(data["price"], 2),
        prev_close=round(data["prev_close"], 2),
        change_pct=round(data["change_pct"], 2),
        market=data["market"],
        source=data["source"],
        data_status=data["data_status"],
        fallback_reason=data["fallback_reason"],
    )


def record_to_dict(record: Any) -> dict[str, Any]:
    import dataclasses
    return {field.name: getattr(record, field.name) for field in dataclasses.fields(record)}


# ── 深度投研 LangGraph 接口 ───────────────────────────────────────────────────

@app.post("/research/{symbol}", response_model=ResearchStartResponse)
def start_research(
    symbol: str,
    data_mode: str = Query(default="auto", pattern="^(auto|offline)$"),
) -> ResearchStartResponse:
    """启动深度投研工作流（LangGraph）。

    工作流若中途触发 interrupt（HAR / manual_review），则在 checkpoint 暂停，
    返回 status="interrupted"；客户端应通过 GET /research/{thread_id}/state
    确认 interrupt_payload，然后 POST /research/{thread_id}/resume 恢复。

    Parameters
    ----------
    symbol : str
        证券代码（如 DEMO001、600519）
    data_mode : str
        数据模式，"auto"（默认，自动选择）或 "offline"（离线样本数据）
    """
    svc = get_application_service()
    try:
        result = svc.deep_research(symbol, data_mode=data_mode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ── Issue 2+3 修复：使用 DeepResearchResult 中的真实 status 和 final_action ──
    # result.status 已由 deep_research() 通过 get_state() 查询快照确定，不再硬编码。
    # result.final_action 直接来自 state["final_action"]，取值 execute/block/None，
    # 不会出现 buy/sell/hold。
    return ResearchStartResponse(
        symbol=result.symbol,
        run_id=result.run_id,
        thread_id=result.thread_id,
        status=result.status,
        duration_s=result.duration_s,
        final_action=result.final_action,
        errors=result.errors,
    )


@app.get("/research/{thread_id}/state", response_model=ResearchStateResponse)
def get_research_state(thread_id: str) -> ResearchStateResponse:
    """查询 LangGraph checkpoint 当前状态。

    返回值 status 枚举（与 POST /research 及 POST resume 保持一致）：
    - completed   : 图已运行到 END，final_action in ("execute", "manual_review")
    - interrupted : 图在 interrupt() 处暂停，interrupt_payload 含暂停原因
    - blocked     : final_action == "block"（HAR/preflight 拒绝）
    - no_trade    : 置信度不足，跳过交易建议
    - failed      : 状态中含 errors 且 final_action 未设置
    - not_found   : checkpoint 不存在（thread_id 无效或已过期）
    """
    svc = get_application_service()
    g = svc._securities_graph
    config = {"configurable": {"thread_id": thread_id}}

    try:
        snapshot = g.get_state(config)
    except Exception:
        snapshot = None

    if snapshot is None or not snapshot.values:
        return ResearchStateResponse(
            thread_id=thread_id,
            status="not_found",
        )

    state_vals = snapshot.values

    # 判断是否处于 interrupt 暂停状态
    interrupt_payload: dict | None = None
    for task in (snapshot.tasks or []):
        if hasattr(task, "interrupts") and task.interrupts:
            interrupt_payload = task.interrupts[0].value if task.interrupts else None
            break

    final_action = state_vals.get("final_action")
    errors = state_vals.get("errors") or []
    raw_status = state_vals.get("status", "unknown")

    status = resolve_research_status(
        interrupt_payload=interrupt_payload,
        final_action=final_action,
        errors=errors,
        raw_status=raw_status,
    )

    # 真实执行轨迹：trace_entries 由各节点在实际运行时追加，未运行的节点不会出现。
    # executed_nodes 用于前端区分"已执行 / 已跳过 / 等待中"，不再依赖任何推测。
    raw_trace = state_vals.get("trace_entries") or []
    trace_entries: list[dict] = [dict(entry) for entry in raw_trace if isinstance(entry, dict)]
    executed_nodes: list[str] = []
    for entry in trace_entries:
        node_name = entry.get("node") or entry.get("name")
        if node_name and node_name not in executed_nodes:
            executed_nodes.append(str(node_name))

    return ResearchStateResponse(
        thread_id=thread_id,
        status=status,
        interrupt_payload=interrupt_payload,
        final_action=final_action,
        errors=list(errors),
        # 新增：从 state_vals 提取完整字段
        symbol=state_vals.get("symbol"),
        run_id=state_vals.get("run_id"),
        data_mode=state_vals.get("data_mode"),
        technical_analysis=state_vals.get("technical_analysis"),
        fundamental_analysis=state_vals.get("fundamental_analysis"),
        industry_analysis=state_vals.get("industry_analysis"),
        market_regime=state_vals.get("market_regime"),
        synthesis=state_vals.get("synthesis"),
        trade_signal=state_vals.get("trade_signal"),
        risk_result=state_vals.get("risk_result"),
        confidence=state_vals.get("confidence"),
        trace_entries=trace_entries,
        executed_nodes=executed_nodes,
    )


@app.post("/research/{thread_id}/resume", response_model=ResearchResumeResponse)
def resume_research(
    thread_id: str,
    decision: ResearchDecision,
) -> ResearchResumeResponse:
    """恢复被 interrupt 暂停的深度投研工作流。

    decision 枚举：
    - approve : 批准（HAR 批准后继续执行 risk_manager；manual_review 后执行交易）
    - reject  : 拒绝（写入 block，结束图执行）

    HTTP 状态码：
    - 200 : 成功恢复并完成
    - 404 : thread_id 不存在或已过期
    - 409 : checkpoint 存在但当前不处于 interrupt 状态（已完成/失败/未暂停）
    - 422 : decision 枚举非法（FastAPI 自动处理）
    - 500 : 内部执行错误
    """
    from agent_platform.finance.securities_graph import resume_securities_analysis

    svc = get_application_service()
    g = svc._securities_graph
    config = {"configurable": {"thread_id": thread_id}}

    # ── 1. 确认 checkpoint 存在 ──────────────────────────────────────────────
    try:
        snapshot = g.get_state(config)
    except Exception:
        snapshot = None

    if snapshot is None or not snapshot.values:
        raise HTTPException(
            status_code=404,
            detail=f"thread_id={thread_id!r} 不存在或已过期",
        )

    # ── 2. 确认当前处于 interrupt 状态（Issue 5）─────────────────────────────
    # 只有 snapshot.tasks 中存在 interrupts 时才能 resume；
    # 已完成、已失败、未暂停的 thread 不得继续调用 resume。
    interrupt_found = False
    for task in (snapshot.tasks or []):
        if hasattr(task, "interrupts") and task.interrupts:
            interrupt_found = True
            break

    if not interrupt_found:
        final_action = snapshot.values.get("final_action")
        status_val = snapshot.values.get("status", "unknown")
        raise HTTPException(
            status_code=409,
            detail=(
                f"thread_id={thread_id!r} 当前不处于 interrupt 状态，无法 resume。"
                f"（status={status_val!r}, final_action={final_action!r}）"
                " 已完成或已 block 的工作流不能重复 resume。"
            ),
        )

    # ── 3. 恢复工作流执行 ────────────────────────────────────────────────────
    try:
        resume_securities_analysis(
            decision=decision.value,
            thread_id=thread_id,
            graph=g,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ── 4. 重新查询 snapshot，与 GET state 使用同一状态转换逻辑 ─────────────
    # 不直接用 resume 返回的 state，而是重新 get_state，保证 POST resume 与
    # GET state 在相同 thread_id 下返回完全一致的 status。
    try:
        post_snap = g.get_state(config)
    except Exception:
        post_snap = None

    if post_snap and post_snap.values:
        final_action = post_snap.values.get("final_action")
        errors = list(post_snap.values.get("errors") or [])
        raw_status = post_snap.values.get("status", "unknown")
        # 检查 resume 后是否有新的 interrupt（理论上不应有，防御性处理）
        resume_interrupt: dict | None = None
        for task in (post_snap.tasks or []):
            if hasattr(task, "interrupts") and task.interrupts:
                resume_interrupt = task.interrupts[0].value
                break
    else:
        final_action = None
        errors = []
        raw_status = "unknown"
        resume_interrupt = None

    status_val = resolve_research_status(
        interrupt_payload=resume_interrupt,
        final_action=final_action,
        errors=errors,
        raw_status=raw_status,
    )

    return ResearchResumeResponse(
        thread_id=thread_id,
        decision=decision.value,
        status=status_val,
        final_action=final_action,
        errors=errors,
    )
