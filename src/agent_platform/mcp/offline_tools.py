"""
MCP 离线工具
============
把 offline_sample_data.py 的确定性样例封装成 MCP 工具。

为什么离线也要走 MCP
--------------------
如果只把 AkShare/Tushare 注册成 MCP 工具，那么 data_mode="offline" 时主链路
会绕开注册表直接读样例字典 —— MCP 层就退化成「只在联网时才生效」的装饰层，
说明书要求的「统一封装为可被主业务调用的 MCP 工具层」并未真正达成。

把样例也注册成工具后，离线与在线的取数路径统一为 `registry.call(...)`，
MCP 层才真正是主业务的取数入口，并且这一点可以用 `registry.stats()` 的
调用计数在测试里证明（见 tests/test_mcp_layer.py）。

溯源纪律
--------
1. 这些工具的 source 一律显式写明 offline_sample，**不得**伪装成实时行情。
   调用方必须把 data_status 标为 offline_sample。这是说明书红线。
2. 工具本身 requires_network=False，因此在离线注册表里不会被阻断；
   它们也不 import 任何网络库，离线零网络由「函数体内无网络调用」保证。
"""
from __future__ import annotations

from typing import Any

from agent_platform.mcp.envelope import err_envelope, ok_envelope

# 样例数据来源标识 —— 前缀 offline_sample 让溯源检查一眼看出非实时
SAMPLE_SOURCE = "offline_sample/内置样例数据(offline_sample_data.py)"


def get_offline_price_history(
    *, symbol: str, start: str = "", end: str = ""
) -> dict[str, Any]:
    """离线日线行情，字段与 MarketDataProvider 的标准 OHLCV 契约一致。"""
    tool = "get_offline_price_history"
    params = {"symbol": symbol, "start": start, "end": end}
    try:
        from datetime import date

        from agent_platform.finance.sample_data_provider import SampleMarketDataProvider

        start_date = date.fromisoformat(start) if start else None
        end_date = date.fromisoformat(end) if end else None
        frame = SampleMarketDataProvider().get_price_history(symbol, start_date, end_date)
        records = frame.to_dict(orient="records")
        for record in records:
            if hasattr(record.get("date"), "isoformat"):
                record["date"] = record["date"].isoformat()
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, exc, params)
    return ok_envelope(
        tool=tool,
        source=SAMPLE_SOURCE,
        data={"symbol": symbol, "rows": len(records), "records": records, "is_sample": True},
        params=params,
    )


def get_offline_realtime_quote(*, symbol: str) -> dict[str, Any]:
    """离线报价样例；明确标记非实时，不会伪装成真实行情。"""
    tool = "get_offline_realtime_quote"
    params = {"symbol": symbol}
    try:
        from agent_platform.finance.sample_data_provider import SampleMarketDataProvider

        data = SampleMarketDataProvider().get_realtime_quote(symbol)
        data["is_sample"] = True
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, exc, params)
    return ok_envelope(tool=tool, source=SAMPLE_SOURCE, data=data, params=params)


def _fail(tool: str, exc: Exception, params: dict[str, Any]) -> dict[str, Any]:
    """样例查询失败（通常是代码错误而非数据源问题）转失败信封。"""
    return err_envelope(
        tool=tool,
        source=SAMPLE_SOURCE,
        error=f"离线样例读取失败: {type(exc).__name__}: {exc}",
        error_type=type(exc).__name__,
        params=params,
    )


# ─── 基本面样例 ──────────────────────────────────────────────────────────────

def get_offline_fundamental(*, symbol: str) -> dict[str, Any]:
    """
    离线基本面样例：PE_TTM / PB / 总市值 / ROE / 资产负债率。

    返回 data 中额外带 is_sample=True，供上层断言「这不是实时数据」。
    """
    tool = "get_offline_fundamental"
    params = {"symbol": symbol}
    try:
        from agent_platform.finance.offline_sample_data import get_sample_fundamental

        sample = get_sample_fundamental(symbol)
    except Exception as exc:  # noqa: BLE001 — 边界层兜住一切并转信封
        return _fail(tool, exc, params)

    data = {"symbol": symbol, **sample, "is_sample": True}
    return ok_envelope(tool=tool, source=SAMPLE_SOURCE, data=data, params=params)


# ─── 行业样例 ────────────────────────────────────────────────────────────────

def get_offline_industry(*, symbol: str) -> dict[str, Any]:
    """离线行业样例：行业名 / 景气信号 / 3日资金流 / 龙头排序。"""
    tool = "get_offline_industry"
    params = {"symbol": symbol}
    try:
        from agent_platform.finance.offline_sample_data import get_sample_industry

        sample = get_sample_industry(symbol)
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, exc, params)

    data = {"symbol": symbol, **sample, "is_sample": True}
    return ok_envelope(tool=tool, source=SAMPLE_SOURCE, data=data, params=params)


# ─── 市场状态样例 ────────────────────────────────────────────────────────────

def get_offline_market_regime(
    *,
    index_code: str = "sh000001",
    scenario: str = "default",
) -> dict[str, Any]:
    """离线市场状态样例：Market Regime / 指数点位 / 风险偏好。"""
    tool = "get_offline_market_regime"
    params = {"index_code": index_code, "scenario": scenario}
    try:
        from agent_platform.finance.offline_sample_data import get_sample_market_regime

        sample = get_sample_market_regime(index_code, scenario)
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, exc, params)

    data = {"index_code": index_code, "scenario": scenario, **sample, "is_sample": True}
    return ok_envelope(tool=tool, source=SAMPLE_SOURCE, data=data, params=params)


# ─── 注册 ────────────────────────────────────────────────────────────────────

def register_all(reg: Any) -> None:
    """把离线样例工具注册进注册表。requires_network 全为 False。"""
    reg.register(
        "get_offline_price_history",
        get_offline_price_history,
        description="离线日线 OHLCV 样例，确定性、零网络",
        requires_network=False,
        provider="offline",
        category="history",
    )
    reg.register(
        "get_offline_realtime_quote",
        get_offline_realtime_quote,
        description="离线报价样例，明确非实时、零网络",
        requires_network=False,
        provider="offline",
        category="quote",
    )
    reg.register(
        "get_offline_fundamental",
        get_offline_fundamental,
        description="离线基本面样例（PE/PB/总市值/ROE/资产负债率），确定性、零网络",
        requires_network=False,
        provider="offline",
        category="valuation",
    )
    reg.register(
        "get_offline_industry",
        get_offline_industry,
        description="离线行业样例（行业名/景气/资金流/龙头），确定性、零网络",
        requires_network=False,
        provider="offline",
        category="industry",
    )
    reg.register(
        "get_offline_market_regime",
        get_offline_market_regime,
        description="离线市场状态样例（Regime/指数/风险偏好），确定性、零网络",
        requires_network=False,
        provider="offline",
        category="index",
    )
