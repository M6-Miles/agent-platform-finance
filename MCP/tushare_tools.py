"""
MCP: Tushare 工具集（薄委托层 / Shim）
=====================================
与 `MCP/akshare_tools.py` 同理：保留目录入口，实现统一收敛到
`src/agent_platform/mcp/tushare_tools.py`，调用统一经过
`agent_platform.mcp.registry`。

改造要点
--------
1. 改造前本文件自带实现且**无人调用**，同时缺少现金流量表、PS、财务指标。
   新包已补齐 `get_cash_flow` / `get_daily_basic`（含 PS/PS_TTM）/
   `get_fina_indicator`（含 ROE、资产负债率）。
2. 改造前 `period` 默认硬编码 `"20251231"`，是会过期的字面量；现在默认空串，
   由下层解析为最近报告期。
3. Token 仍只从环境变量读取，**本文件不打印、不返回、不记录 token**；
   注册表信封会把 token 一类的键脱敏为 `***`。
4. 未配置 TUSHARE_TOKEN 时返回失败信封（`ok=False`，`data=None`），
   **不返回伪造的财务数据**。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_importable() -> None:
    """保证 `agent_platform` 可被导入（本文件不在 src 包树内）。"""
    src = Path(__file__).resolve().parents[1] / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _call(tool: str, *, offline: bool = False, **kwargs: Any) -> dict[str, Any]:
    """统一委托入口：转发到 agent_platform.mcp 注册表。"""
    _ensure_importable()
    from agent_platform.mcp.registry import get_registry

    return get_registry(offline=offline).call(tool, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 兼容旧函数名。ts_code 直接透传：下层 _to_ts_code 同时接受 600519 与 600519.SH。
# ─────────────────────────────────────────────────────────────────────────────

def mcp_get_income_statement(
    ts_code: str, period: str = "", *, limit: int = 4, offline: bool = False,
) -> dict[str, Any]:
    """利润表。ts_code 支持 600519 或 600519.SH。"""
    return _call(
        "get_income_statement", offline=offline, symbol=ts_code, period=period, limit=limit,
    )


def mcp_get_balance_sheet(
    ts_code: str, period: str = "", *, limit: int = 4, offline: bool = False,
) -> dict[str, Any]:
    """资产负债表（附按报表口径直接计算的资产负债率）。"""
    return _call(
        "get_balance_sheet", offline=offline, symbol=ts_code, period=period, limit=limit,
    )


def mcp_get_cash_flow(
    ts_code: str, period: str = "", *, limit: int = 4, offline: bool = False,
) -> dict[str, Any]:
    """现金流量表。改造前缺失，此处补齐（说明书要求三大报表齐全）。"""
    return _call(
        "get_cash_flow", offline=offline, symbol=ts_code, period=period, limit=limit,
    )


def mcp_get_daily_basic(
    ts_code: str, trade_date: str = "", *, offline: bool = False,
) -> dict[str, Any]:
    """每日指标：PE / PE_TTM / PB / PS / PS_TTM / 总市值 / 换手率 / 股息率。"""
    return _call(
        "get_daily_basic", offline=offline, symbol=ts_code, trade_date=trade_date,
    )


def mcp_get_fina_indicator(
    ts_code: str, period: str = "", *, limit: int = 4, offline: bool = False,
) -> dict[str, Any]:
    """财务指标：ROE、扣非 ROE、资产负债率、毛利率、净利率。"""
    return _call(
        "get_fina_indicator", offline=offline, symbol=ts_code, period=period, limit=limit,
    )


def mcp_get_index_daily_ts(
    index_code: str = "000001.SH",
    start_date: str = "",
    end_date: str = "",
    *,
    offline: bool = False,
) -> dict[str, Any]:
    """指数日线（Tushare 口径）。"""
    return _call(
        "get_index_daily_ts",
        offline=offline,
        index_code=index_code,
        start_date=start_date,
        end_date=end_date,
    )
