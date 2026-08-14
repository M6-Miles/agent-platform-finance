from __future__ import annotations

import pathlib
import logging
import time
import threading
from uuid import uuid4
from contextlib import asynccontextmanager
from datetime import date
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_platform import __version__
from agent_platform.config import get_settings
from agent_platform.finance.errors import (
    InvalidSecuritySymbolError,
    MarketDataDependencyError,
    MarketDataUnavailableError,
)
from agent_platform.finance.quote_tool import QuoteToolError
from agent_platform.logging_config import configure_logging
from agent_platform.security import AuthenticationError, Principal, issue_token, verify_token
from agent_platform.services.application_service import (
    ApplicationService,
    resolve_research_status,
)

# 导入子路由
from agent_platform.api.comparison import router as comparison_router
from agent_platform.api.backtest import router as backtest_router
from agent_platform.api.chat import router as chat_router

configure_logging()
logger = logging.getLogger(__name__)


class SessionCreateRequest(BaseModel):
    title: str | None = None


class AuthCredentials(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=8, max_length=200)
    email: str | None = Field(default=None, max_length=200)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: int
    user: dict[str, str | None]


class UserRoleUpdateRequest(BaseModel):
    role: str = Field(..., pattern="^(admin|user)$")


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=8, max_length=200)
    new_password: str = Field(..., min_length=8, max_length=200)


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


_PUBLIC_PATHS = frozenset({
    "/", "/health", "/ready", "/auth/status", "/auth/register", "/auth/login",
    "/docs", "/openapi.json", "/redoc",
})


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_application_service().settings
        if not settings.auth_enabled or request.url.path in _PUBLIC_PATHS:
            request.state.principal = None
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return JSONResponse({"detail": "需要 Bearer 访问令牌"}, status_code=401)
        try:
            request.state.principal = verify_token(
                authorization.split(None, 1)[1], secret=settings.auth_secret
            )
        except AuthenticationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=401)
        return await call_next(request)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id", "").strip()[:128] or uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled request failure", extra={
                "request_id": request_id, "method": request.method, "path": request.url.path,
            })
            return JSONResponse(
                {"detail": "服务器内部错误", "request_id": request_id}, status_code=500,
                headers={"X-Request-ID": request_id},
            )
        principal = getattr(request.state, "principal", None)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; connect-src 'self' http://127.0.0.1:* http://localhost:*"
        )
        logger.info("request completed", extra={
            "request_id": request_id,
            "user_id": getattr(principal, "user_id", None),
            "method": request.method, "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        })
        return response


class SecurityRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-process safety limit; production gateways should enforce a shared limit."""
    _lock = threading.Lock()
    _windows: dict[tuple[str, str], tuple[float, int]] = {}

    async def dispatch(self, request: Request, call_next):
        path_group = "auth" if request.url.path in {"/auth/login", "/auth/register"} else "api"
        limit = 10 if path_group == "auth" else 300
        identity = request.client.host if request.client else "unknown"
        key = (identity, path_group)
        now = time.monotonic()
        with self._lock:
            started, count = self._windows.get(key, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            count += 1
            self._windows[key] = (started, count)
        if count > limit:
            retry_after = max(1, int(60 - (now - started)))
            return JSONResponse(
                {"detail": "请求过于频繁，请稍后重试"}, status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


app.add_middleware(AuthenticationMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(SecurityRateLimitMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(get_settings().trusted_hosts))

# 允许前端 file:// 及本地开发服务器跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().allowed_origins),
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


def _principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


def _claim(request: Request, resource_type: str, resource_id: str) -> None:
    principal = _principal(request)
    if principal is not None:
        get_application_service().store.claim_resource(resource_type, resource_id, principal.user_id)


def _authorize(request: Request, resource_type: str, resource_id: str) -> None:
    principal = _principal(request)
    if principal is None:
        return
    owner = get_application_service().store.resource_owner(resource_type, resource_id)
    if owner is None and principal.is_admin:
        get_application_service().store.claim_resource(resource_type, resource_id, principal.user_id)
        return
    if owner != principal.user_id and not principal.is_admin:
        raise HTTPException(status_code=404, detail="资源不存在")


def _require_admin(request: Request) -> None:
    service = get_application_service()
    if not service.settings.auth_enabled:
        return
    principal = _principal(request)
    user = service.store.get_user_by_username(principal.username) if principal else None
    if user is None or user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

# 注册子路由
app.include_router(comparison_router, tags=["Comparison"])
app.include_router(backtest_router, tags=["Backtest"])
app.include_router(chat_router, tags=["Chat"])

# ── 天气分析端点（演示通用 Agent 平台在非金融领域的应用）─────────────────────

class WeatherAnalysisRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "city": "北京",
                "temps": [5.2, 6.8, 8.1, 9.5, 11.2],
                "source": "内置天气样例数据",
            }
        }
    )

    city: str = Field(..., min_length=1, max_length=100)
    temps: list[float] = Field(..., min_length=2, max_length=366)
    # Kept for backward compatibility only. The server always assigns the
    # authoritative source from data_mode and never trusts this value.
    source: str | None = Field(default=None, max_length=200)
    data_mode: str = Field(default="offline", pattern="^(offline|online)$")


class WeatherAnalysisResponse(BaseModel):
    city: str
    period_days: int
    avg_temp_c: float
    max_temp_c: float
    min_temp_c: float
    temp_range_c: float
    trend: str
    volatility_c: float
    summary: str
    source: str
    updated_at: str
    disclaimer: str
    # Harness 检查结果
    harness_approved: bool
    harness_checks: list[dict[str, Any]]
    harness_action: str
    data_mode: str = "offline"
    forecast: dict[str, Any] | None = None


class WeatherSampleResponse(BaseModel):
    city: str
    temps: list[float]
    source: str


@app.get("/weather/samples", response_model=list[WeatherSampleResponse], tags=["Weather"])
def list_weather_samples() -> list[WeatherSampleResponse]:
    """Return deterministic sample inputs for the non-financial demo."""
    from agent_platform.weather import SAMPLE_CITIES

    return [
        WeatherSampleResponse(city=city, temps=temps, source="内置天气样例数据")
        for city, temps in SAMPLE_CITIES.items()
    ]


@app.post("/weather/analyze", response_model=WeatherAnalysisResponse, tags=["Weather"])
def analyze_weather(request: WeatherAnalysisRequest) -> WeatherAnalysisResponse:
    """
    天气趋势分析端点（演示通用 Agent 平台）。

    使用与金融 Agent 相同的 Guardrail 机制：
    - JSONSchemaValidator（结构校验）
    - SourceAttributionFilter（数据溯源）
    - KeywordBlocker（违禁词拦截）
    - WeatherHarness（Pre-Flight Checklist）
    """
    from agent_platform.weather import WeatherAnalysisAgent, WeatherHarness

    # 1. 联网模式先取得公开预报，再把每日高低温均值交给同一 Agent 分析。
    forecast_payload = None
    authoritative_source = "内置天气样例数据"
    if request.data_mode == "online":
        from agent_platform.weather import OpenMeteoWeatherProvider
        try:
            forecast = OpenMeteoWeatherProvider().get_forecast(request.city)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"联网天气获取失败：{exc}") from exc
        display_city = forecast.city_name or forecast.resolved_name
        request = request.model_copy(update={
            "city": display_city,
            "temps": [round((day.temp_max_c + day.temp_min_c) / 2, 1) for day in forecast.daily],
            "source": "Open-Meteo 公开天气数据",
        })
        authoritative_source = "Open-Meteo 公开天气数据"
        forecast_payload = {
            "requested_city": forecast.requested_city,
            "resolved_name": forecast.resolved_name,
            "city_name": display_city,
            "district_name": forecast.district_name,
            "country": forecast.country,
            "latitude": forecast.latitude,
            "longitude": forecast.longitude,
            "timezone": forecast.timezone,
            "current_temperature_c": forecast.current_temperature_c,
            "apparent_temperature_c": forecast.apparent_temperature_c,
            "relative_humidity_pct": forecast.relative_humidity_pct,
            "precipitation_mm": forecast.precipitation_mm,
            "wind_speed_kmh": forecast.wind_speed_kmh,
            "weather_code": forecast.weather_code,
            "condition": forecast.condition,
            "observed_at": forecast.observed_at,
            "fetched_at": forecast.fetched_at,
            "location_note": forecast.location_note,
            "daily": [day.to_dict() for day in forecast.daily],
            "source": authoritative_source,
        }
    else:
        request = request.model_copy(update={"source": authoritative_source})

    # 2. 数据校验
    for temp in request.temps:
        if not (-100 <= temp <= 100):
            raise HTTPException(
                status_code=400,
                detail=f"温度值 {temp}°C 超出合理范围 [-100, 100]"
            )

    try:
        # 2. Agent 分析（带 Guardrail）
        agent = WeatherAnalysisAgent()
        report = agent.analyze(
            city=request.city,
            temps=request.temps,
            source=authoritative_source,
            updated_at=(forecast_payload or {}).get("fetched_at"),
        )

        # 3. Harness Pre-Flight 检查
        harness = WeatherHarness()
        harness_result = harness.run_preflight(
            weather_report={
                "city": report.city,
                "period_days": report.period_days,
                "avg_temp_c": report.avg_temp_c,
                "max_temp_c": report.max_temp_c,
                "min_temp_c": report.min_temp_c,
                "temp_range_c": report.temp_range_c,
                "trend": report.trend,
                "volatility_c": report.volatility_c,
                "summary": report.summary,
                "source": report.source,
                "updated_at": report.updated_at,
                "disclaimer": report.disclaimer,
            },
            raw_temps=request.temps,
        )

        return WeatherAnalysisResponse(
            city=report.city,
            period_days=report.period_days,
            avg_temp_c=report.avg_temp_c,
            max_temp_c=report.max_temp_c,
            min_temp_c=report.min_temp_c,
            temp_range_c=report.temp_range_c,
            trend=report.trend,
            volatility_c=report.volatility_c,
            summary=report.summary,
            source=report.source,
            updated_at=report.updated_at,
            disclaimer=report.disclaimer,
            harness_approved=harness_result.approved,
            harness_checks=[
                {
                    "check_name": c.check_name,
                    "passed": c.passed,
                    "message": c.message,
                }
                for c in harness_result.checks
            ],
            harness_action=harness_result.final_action,
            data_mode=request.data_mode,
            forecast=forecast_payload,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"分析失败: {str(exc)}") from exc


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
    requested_data_mode: str | None = None
    effective_data_mode: str | None = None
    data_mode: str | None = None


class ResearchStateResponse(BaseModel):
    thread_id: str
    status: str                      # completed | interrupted | blocked | failed | not_found | no_trade
    interrupt_payload: dict | None = None
    final_action: str | None = None
    errors: list[str] = []
    # 新增字段：供前端展示完整 Agent 结果
    symbol: str | None = None
    run_id: str | None = None
    duration_s: float = 0.0
    data_mode: str | None = None
    requested_data_mode: str | None = None
    effective_data_mode: str | None = None
    technical_analysis: dict | None = None
    fundamental_analysis: dict | None = None
    industry_analysis: dict | None = None
    market_regime: dict | None = None
    synthesis: dict | None = None
    trade_signal: dict | None = None
    risk_result: dict | None = None
    preflight_result: dict | None = None
    data_quality_summary: dict | None = None
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
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
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


@app.get("/ready")
def readiness() -> dict[str, Any]:
    service = get_application_service()
    checks: dict[str, Any] = {}
    try:
        from agent_platform.storage.database_admin import database_health
        checks["database"] = database_health(service.settings.sqlite_path)
    except Exception as exc:
        checks["database"] = f"failed:{type(exc).__name__}"
    checks["checkpoint"] = {
        "type": service._checkpointer_type,
        "available": service._securities_graph is not None,
    }
    checks["paper_monitor"] = service.paper_monitor.status()
    ready = checks.get("database", {}).get("integrity") == "ok" and checks["checkpoint"]["available"]
    if not ready:
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ready", "checks": checks}


@app.post("/admin/database/backup")
def backup_application_database(request: BackupRequest, http_request: Request) -> dict[str, Any]:
    _require_admin(http_request)
    service = get_application_service()
    from datetime import datetime
    from agent_platform.storage.database_admin import backup_database
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = service.settings.sqlite_path.parent / "backups" / f"{request.name}_{timestamp}.sqlite3"
    return backup_database(service.settings.sqlite_path, destination)


@app.post("/admin/database/retention")
def prune_application_database(request: RetentionRequest, http_request: Request) -> dict[str, Any]:
    _require_admin(http_request)
    from agent_platform.storage.database_admin import prune_operational_data
    return {"deleted": prune_operational_data(
        get_application_service().settings.sqlite_path,
        retention_days=request.retention_days,
    )}


@app.get("/auth/status")
def auth_status() -> dict[str, Any]:
    service = get_application_service()
    settings = service.settings
    return {
        "enabled": settings.auth_enabled,
        "has_users": service.store.has_any_user(),
        "registration_open": settings.auth_enabled and (
            settings.auth_registration_enabled or not service.store.has_any_user()
        ),
    }


@app.post("/auth/register", response_model=AuthResponse)
def register(request: AuthCredentials) -> AuthResponse:
    service = get_application_service()
    settings = service.settings
    if not settings.auth_enabled:
        raise HTTPException(status_code=409, detail="当前环境未启用认证")
    has_users = service.store.has_any_user()
    if has_users and not settings.auth_registration_enabled:
        raise HTTPException(status_code=403, detail="初始化注册已关闭，请联系管理员")
    try:
        user = service.store.create_user(
            request.username, request.password, request.email,
            role="user" if has_users else "admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token, expires_at = issue_token(
        user_id=user.id, username=user.username, role=user.role,
        secret=settings.auth_secret, ttl_s=settings.auth_token_ttl_s,
    )
    return AuthResponse(access_token=token, expires_at=expires_at, user={
        "id": user.id, "username": user.username, "email": user.email, "role": user.role,
    })


@app.get("/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    principal = _principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="需要 Bearer 访问令牌")
    user = get_application_service().store.get_user_by_username(principal.username)
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return {
        "id": user.id, "username": user.username, "email": user.email,
        "role": user.role, "created_at": user.created_at,
    }


def _public_user(user: Any) -> dict[str, Any]:
    return {
        "id": user.id, "username": user.username, "email": user.email,
        "role": user.role, "created_at": user.created_at,
    }


@app.get("/admin/users")
def list_users(request: Request) -> dict[str, Any]:
    _require_admin(request)
    users = get_application_service().store.list_users()
    return {"users": [_public_user(user) for user in users], "total": len(users)}


@app.patch("/admin/users/{user_id}/role")
def update_user_role(
    user_id: str, payload: UserRoleUpdateRequest, request: Request,
) -> dict[str, Any]:
    _require_admin(request)
    try:
        user = get_application_service().store.update_user_role(user_id, payload.role)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public_user(user)


@app.post("/auth/login", response_model=AuthResponse)
def login(request: AuthCredentials) -> AuthResponse:
    service = get_application_service()
    settings = service.settings
    if not settings.auth_enabled:
        raise HTTPException(status_code=409, detail="当前环境未启用认证")
    user = service.store.verify_user(request.username, request.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token, expires_at = issue_token(
        user_id=user.id, username=user.username, role=user.role,
        secret=settings.auth_secret, ttl_s=settings.auth_token_ttl_s,
    )
    return AuthResponse(access_token=token, expires_at=expires_at, user={
        "id": user.id, "username": user.username, "email": user.email, "role": user.role,
    })


@app.patch("/auth/password")
def change_password(payload: PasswordChangeRequest, request: Request) -> dict[str, str]:
    principal = _principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="需要 Bearer 访问令牌")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    try:
        get_application_service().store.change_password(
            principal.user_id, payload.current_password, payload.new_password,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "password_changed"}


@app.get("/observability")
def get_observability(request: Request) -> dict[str, Any]:
    """所有已登录用户可读取服务端调用指标；重置仍仅限管理员。"""
    return get_application_service().observability.get_summary()


@app.delete("/observability")
def reset_observability(request: Request) -> dict[str, str]:
    _require_admin(request)
    get_application_service().observability.reset()
    return {"status": "reset"}


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
    request: Request,
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
            record = get_application_service().store.add_analysis(
                result, trigger="direct", session_id=None
            )
            _claim(request, "analysis", record.id)
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
def create_session(request: SessionCreateRequest, http_request: Request) -> dict[str, str]:
    record = get_application_service().create_session(request.title)
    _claim(http_request, "session", record.id)
    return record_to_dict(record)


@app.get("/sessions")
def list_sessions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    records = [
        record_to_dict(record)
        for record in get_application_service().list_sessions(limit)
    ]
    principal = _principal(request)
    if principal is None or principal.is_admin:
        return records
    owned = get_application_service().store.owned_resource_ids("session", principal.user_id)
    return [record for record in records if record["id"] in owned]


@app.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, request: Request) -> list[dict[str, Any]]:
    _authorize(request, "session", session_id)
    try:
        records = get_application_service().list_messages(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [record_to_dict(record) for record in records]


@app.get("/analysis-history")
def list_analysis_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    records = [
        record_to_dict(record)
        for record in get_application_service().list_analysis_history(limit)
    ]
    principal = _principal(request)
    if principal is None or principal.is_admin:
        return records
    owned = get_application_service().store.owned_resource_ids(
        "analysis", principal.user_id
    )
    return [record for record in records if record["id"] in owned]


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request) -> dict[str, str]:
    _authorize(request, "session", session_id)
    try:
        get_application_service().delete_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "session_id": session_id}


@app.patch("/sessions/{session_id}")
def rename_session(request: Request, session_id: str, title: str = Query(..., min_length=1, max_length=200)) -> dict[str, str]:
    _authorize(request, "session", session_id)
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
    updated_at: str
    data_status: str
    fallback_reason: str | None
    quote_cache_hit: bool = False
    quote_age_s: float = 0.0


class PaperAccountCreateRequest(BaseModel):
    initial_cash: float = 1_000_000.0


class PaperOrderRequest(BaseModel):
    symbol: str
    side: str
    quantity: int
    order_type: str = "market"
    limit_price: float | None = None
    data_mode: str = "auto"
    request_id: str | None = None


class PaperRefreshRequest(BaseModel):
    symbols: list[str] = []
    data_mode: str = "auto"
    force_refresh: bool = False


class PaperRunRequest(BaseModel):
    symbols: list[str]
    data_mode: str = "offline"
    days: int = Field(20, ge=1, le=250, description="请求模拟的交易日数量")
    initial_cash: float = 1_000_000.0


class PaperMonitorCreateRequest(BaseModel):
    symbols: list[str]
    data_mode: str = Field("auto", pattern="^(offline|auto)$")
    run_time: str = Field("15:10", pattern="^(?:[01]\\d|2[0-3]):[0-5]\\d$")
    initial_cash: float = Field(1_000_000.0, gt=0)
    account_id: str | None = None


class PaperMonitorToggleRequest(BaseModel):
    enabled: bool


class BackupRequest(BaseModel):
    name: str = Field(default="manual", pattern="^[A-Za-z0-9_-]{1,50}$")


class RetentionRequest(BaseModel):
    retention_days: int = Field(default=90, ge=7, le=3650)


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
def quote(
    symbol: str,
    data_mode: str = Query("auto", pattern="^(offline|auto)$"),
    force_refresh: bool = Query(False),
) -> QuoteResponse:
    """返回实时行情报价及涨跌幅，供模拟盘初始化使用。"""
    try:
        data = get_application_service().paper_broker.get_quote(
            symbol, data_mode=data_mode, force_refresh=force_refresh,
        )
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
        updated_at=data["updated_at"],
        data_status=data["data_status"],
        fallback_reason=data["fallback_reason"],
        quote_cache_hit=bool(data.get("quote_cache_hit", False)),
        quote_age_s=float(data.get("quote_age_s", 0.0)),
    )


@app.post("/paper-trading/accounts")
def create_paper_account(request: PaperAccountCreateRequest, http_request: Request) -> dict[str, Any]:
    """创建仅连接本地 MockBroker 的持久化模拟账户。"""
    try:
        account = get_application_service().paper_broker.create_account(request.initial_cash)
        _claim(http_request, "paper_account", account["account_id"])
        return account
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/paper-trading/accounts/{account_id}")
def get_paper_account(account_id: str, request: Request) -> dict[str, Any]:
    _authorize(request, "paper_account", account_id)
    try:
        return get_application_service().paper_broker.get_account(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/paper-trading/accounts/{account_id}/orders")
def place_paper_order(account_id: str, request: PaperOrderRequest, http_request: Request) -> dict[str, Any]:
    _authorize(http_request, "paper_account", account_id)
    try:
        return get_application_service().paper_broker.place_order(
            account_id,
            symbol=request.symbol.strip().upper(),
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            limit_price=request.limit_price,
            data_mode=request.data_mode,
            request_id=request.request_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QuoteToolError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ValueError, MarketDataUnavailableError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/paper-trading/accounts/{account_id}/refresh")
def refresh_paper_account(account_id: str, request: PaperRefreshRequest, http_request: Request) -> dict[str, Any]:
    _authorize(http_request, "paper_account", account_id)
    try:
        return get_application_service().paper_broker.refresh(
            account_id,
            symbols=request.symbols,
            data_mode=request.data_mode,
            force_refresh=request.force_refresh,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/paper-trading/runs")
def run_continuous_paper_trading(request: PaperRunRequest, http_request: Request) -> dict[str, Any]:
    """运行并持久化连续多交易日策略模拟，不连接真实券商。"""
    try:
        result = get_application_service().paper_broker.run_continuous(
            symbols=request.symbols,
            data_mode=request.data_mode,
            days=request.days,
            initial_cash=request.initial_cash,
        )
        _claim(http_request, "paper_run", result["run_id"])
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/paper-trading/runs/{run_id}")
def get_continuous_paper_trading(run_id: str, request: Request) -> dict[str, Any]:
    _authorize(request, "paper_run", run_id)
    try:
        return get_application_service().paper_broker.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/paper-trading/monitor/jobs")
def create_paper_monitor_job(request: PaperMonitorCreateRequest, http_request: Request) -> dict[str, Any]:
    """创建每日行情与模拟账户快照任务，不连接真实券商。"""
    try:
        result = get_application_service().paper_monitor.create_job(
            request.symbols,
            data_mode=request.data_mode,
            run_time=request.run_time,
            initial_cash=request.initial_cash,
            account_id=request.account_id,
        )
        _claim(http_request, "paper_monitor_job", result["id"])
        _claim(http_request, "paper_account", result["account_id"])
        return result
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/paper-trading/monitor/jobs")
def list_paper_monitor_jobs(request: Request) -> list[dict[str, Any]]:
    jobs = get_application_service().paper_monitor.list_jobs()
    principal = _principal(request)
    if principal is None or principal.is_admin:
        return jobs
    owned = get_application_service().store.owned_resource_ids(
        "paper_monitor_job", principal.user_id
    )
    return [job for job in jobs if job["id"] in owned]


@app.get("/paper-trading/monitor/status")
def get_paper_monitor_status(request: Request) -> dict[str, Any]:
    """Return scheduler truth separately from each task's enabled flag."""
    status = get_application_service().paper_monitor.status()
    principal = _principal(request)
    if principal is not None and not principal.is_admin:
        status.pop("job_count", None)
    return status


@app.patch("/paper-trading/monitor/jobs/{job_id}")
def toggle_paper_monitor_job(
    job_id: str, request: PaperMonitorToggleRequest, http_request: Request,
) -> dict[str, Any]:
    _authorize(http_request, "paper_monitor_job", job_id)
    try:
        return get_application_service().paper_monitor.set_enabled(job_id, request.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/paper-trading/monitor/jobs/{job_id}/run")
def run_paper_monitor_job(job_id: str, request: Request) -> dict[str, Any]:
    _authorize(request, "paper_monitor_job", job_id)
    """立即执行一次；同一任务同一自然日重复调用返回同一记录。"""
    try:
        return get_application_service().paper_monitor.run_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/paper-trading/monitor/jobs/{job_id}")
def delete_paper_monitor_job(job_id: str, request: Request) -> dict[str, bool]:
    _authorize(request, "paper_monitor_job", job_id)
    try:
        get_application_service().paper_monitor.delete_job(job_id)
        return {"deleted": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/paper-trading/monitor/jobs/{job_id}/runs")
def list_paper_monitor_runs(job_id: str, request: Request) -> list[dict[str, Any]]:
    _authorize(request, "paper_monitor_job", job_id)
    try:
        return get_application_service().paper_monitor.list_runs(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def record_to_dict(record: Any) -> dict[str, Any]:
    import dataclasses
    return {field.name: getattr(record, field.name) for field in dataclasses.fields(record)}


# ── 深度投研 LangGraph 接口 ───────────────────────────────────────────────────

@app.post("/research/{symbol}", response_model=ResearchStartResponse)
def start_research(
    request: Request,
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
    _claim(request, "research_thread", result.thread_id)
    return ResearchStartResponse(
        symbol=result.symbol,
        run_id=result.run_id,
        thread_id=result.thread_id,
        status=result.status,
        duration_s=result.duration_s,
        final_action=result.final_action,
        errors=result.errors,
        requested_data_mode=result.requested_data_mode,
        effective_data_mode=result.effective_data_mode,
        data_mode=result.effective_data_mode,
    )


@app.get("/research/{thread_id}/state", response_model=ResearchStateResponse)
def get_research_state(thread_id: str, request: Request) -> ResearchStateResponse:
    _authorize(request, "research_thread", thread_id)
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

    from agent_platform.finance.securities_graph import _trace_duration

    preflight_result = state_vals.get("preflight_result")
    if preflight_result is None and interrupt_payload:
        preflight_result = interrupt_payload.get("preflight")

    return ResearchStateResponse(
        thread_id=thread_id,
        status=status,
        interrupt_payload=interrupt_payload,
        final_action=final_action,
        errors=list(errors),
        # 新增：从 state_vals 提取完整字段
        symbol=state_vals.get("symbol"),
        run_id=state_vals.get("run_id"),
        duration_s=_trace_duration(trace_entries),
        data_mode=state_vals.get("data_mode"),
        requested_data_mode=state_vals.get("requested_data_mode"),
        effective_data_mode=state_vals.get("data_mode"),
        technical_analysis=state_vals.get("technical_analysis"),
        fundamental_analysis=state_vals.get("fundamental_analysis"),
        industry_analysis=state_vals.get("industry_analysis"),
        market_regime=state_vals.get("market_regime"),
        synthesis=state_vals.get("synthesis"),
        trade_signal=state_vals.get("trade_signal"),
        risk_result=state_vals.get("risk_result"),
        preflight_result=preflight_result,
        data_quality_summary=(preflight_result or {}).get("data_quality_summary"),
        confidence=state_vals.get("confidence"),
        trace_entries=trace_entries,
        executed_nodes=executed_nodes,
    )


@app.post("/research/{thread_id}/resume", response_model=ResearchResumeResponse)
def resume_research(
    request: Request,
    thread_id: str,
    decision: ResearchDecision,
) -> ResearchResumeResponse:
    _authorize(request, "research_thread", thread_id)
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
