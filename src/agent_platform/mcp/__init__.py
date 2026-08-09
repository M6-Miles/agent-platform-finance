"""
MCP 工具层
==========
把 AkShare / Tushare / 离线样例三类数据源统一封装为「MCP 工具」，供主业务
（Provider、四个专业 Agent、LangGraph 节点）通过同一个注册表调用。

为什么需要这一层
----------------
改造前的问题：根目录 `MCP/akshare_tools.py`、`MCP/tushare_tools.py` 没有被
任何代码 import，各 Agent 内部各自 `import akshare as ak` 直接调用。后果是

1. 工具文件是"摆设"，不构成真实能力；
2. 离线模式靠每个 Agent 自己判断 `force_offline`，漏一处就会外发请求；
3. 返回字段不统一，有的带 source 有的不带，溯源无法保证。

本层解决这三点：唯一信封（envelope）、唯一入口（registry）、注册表级离线硬阻断。

用法
----
    from agent_platform.mcp import get_registry

    reg = get_registry(offline=force_offline)
    env = reg.call("get_realtime_quote", symbol="600519")
    if env["ok"]:
        price = env["data"]["price"]
    else:
        # 必须走降级分支并标记 data_status / fallback_reason
        reason = env["error"]

约束
----
* 失败信封的 data 恒为 None，调用方不得从失败信封取数、不得用 0 或随机值代替。
* offline=True 时所有 requires_network 工具在函数体执行前就被阻断，可测证零请求。
"""
from __future__ import annotations

from agent_platform.mcp.envelope import (
    REQUIRED_ENVELOPE_KEYS,
    err_envelope,
    is_ok,
    ok_envelope,
    utc_now_iso,
    validate_envelope,
)
from agent_platform.mcp.registry import (
    MCPCallRecord,
    MCPToolNotFoundError,
    MCPToolRegistry,
    MCPToolSpec,
    build_default_registry,
    get_registry,
)

__all__ = [
    # envelope
    "REQUIRED_ENVELOPE_KEYS",
    "ok_envelope",
    "err_envelope",
    "is_ok",
    "validate_envelope",
    "utc_now_iso",
    # registry
    "MCPToolRegistry",
    "MCPToolSpec",
    "MCPCallRecord",
    "MCPToolNotFoundError",
    "build_default_registry",
    "get_registry",
]
