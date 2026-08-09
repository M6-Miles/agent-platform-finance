"""
MCP: AkShare 工具集（薄委托层 / Shim）
=====================================
本文件保留 Harness 九大组件目录结构中的 `MCP/` 入口，但**不再自带实现**。

为什么改成委托
--------------
改造前本文件是一份独立实现：自己拼 `{"data": ..., "source": ..., "updated_at": ...}`
字典，自己 try/except。问题有三个：

1. **没有任何主链路 import 它**（改造前全仓库检索零引用），属于"摆着好看"的
   死代码，无法作为交付证据。
2. 返回结构与 `src/agent_platform/mcp/envelope.py` 的统一信封不一致，
   缺少 `ok` / `error_type` / `timestamp`，上层无法用同一套逻辑判断成败。
3. **绕过离线阻断**：它直接 `import akshare` 发请求，离线模式管不住它。

现在所有调用统一走 `agent_platform.mcp.registry.get_registry().call(...)`，
因此自动获得：统一信封、离线硬阻断、异常兜底、调用审计。

返回值变化
----------
现在返回统一信封（含 `ok` / `data` / `source` / `updated_at` / `timestamp` /
`error` / `error_type`）。行情载荷在 `env["data"]` 里，失败时 `data` 为 None ——
**不返回空列表冒充"取到了但没数据"**。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _ensure_importable() -> None:
    """
    保证 `agent_platform` 可被导入。

    本文件位于项目根 `MCP/` 下，不属于 `src` 包树；被 importlib 以独立模块
    加载时 sys.path 可能不含 `src`，这里做一次幂等补齐。
    """
    src = Path(__file__).resolve().parents[1] / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _call(tool: str, *, offline: bool = False, **kwargs: Any) -> dict[str, Any]:
    """统一委托入口：转发到 agent_platform.mcp 注册表。"""
    _ensure_importable()
    from agent_platform.mcp.registry import get_registry

    return get_registry(offline=offline).call(tool, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 兼容旧函数名（mcp_get_*）。签名保持向后兼容，内部转发到新工具名。
# ─────────────────────────────────────────────────────────────────────────────

def mcp_get_price_history(
    symbol: str,
    start: str = "",
    end: str = "",
    adjust: str = "qfq",
    *,
    period: str = "daily",
    offline: bool = False,
) -> dict[str, Any]:
    """日/周/月线历史行情。period 取 daily / weekly / monthly。"""
    return _call(
        "get_price_history",
        offline=offline,
        symbol=symbol,
        start=start,
        end=end,
        period=period,
        adjust=adjust,
    )


def mcp_get_realtime_quote(symbol: str, *, offline: bool = False) -> dict[str, Any]:
    """实时行情快照。"""
    return _call("get_realtime_quote", offline=offline, symbol=symbol)


def mcp_get_index_daily(
    index_code: str = "sh000001",
    start: str = "",
    end: str = "",
    *,
    offline: bool = False,
) -> dict[str, Any]:
    """
    指数日线（默认上证指数）。

    注意：改造前本函数的 start 默认值是硬编码的 "2026-01-01"，会随时间失效。
    现在默认空串表示"由下层取默认区间"，不再内置会过期的字面量。
    """
    return _call(
        "get_index_daily", offline=offline, index_code=index_code, start=start, end=end,
    )


def mcp_get_industry_list(*, offline: bool = False) -> dict[str, Any]:
    """行业板块列表。"""
    return _call("get_industry_list", offline=offline)


def mcp_get_minute_bars(
    symbol: str, period: str = "5", *, offline: bool = False,
) -> dict[str, Any]:
    """分钟线（1/5/15/30/60）。仅覆盖近期交易日，不能回溯多年。"""
    return _call("get_minute_bars", offline=offline, symbol=symbol, period=period)


def mcp_get_fund_flow(symbol: str, *, offline: bool = False) -> dict[str, Any]:
    """个股资金流。"""
    return _call("get_fund_flow", offline=offline, symbol=symbol)


def mcp_get_sector_fund_flow(
    indicator: str = "今日", *, offline: bool = False,
) -> dict[str, Any]:
    """行业资金流排名。indicator 取 今日 / 5日 / 10日。"""
    return _call("get_sector_fund_flow", offline=offline, indicator=indicator)


def list_available_tools(*, offline: bool = False) -> list[dict[str, Any]]:
    """列出注册表中全部工具元数据，便于确认本 shim 背后的真实能力集。"""
    _ensure_importable()
    from agent_platform.mcp.registry import get_registry

    return get_registry(offline=offline).list_tools()
