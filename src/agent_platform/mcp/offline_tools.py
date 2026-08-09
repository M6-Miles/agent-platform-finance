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
SAMPLE_SOURCE = "offline_sample/offline_sample_data.py"


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
