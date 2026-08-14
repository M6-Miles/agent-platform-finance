"""
MCP 工具注册表
==============
说明书要求把 AkShare/Tushare 工具「统一封装为可被主业务调用的 MCP 工具层」，
且「离线模式必须完全禁止外网」。本注册表是该要求的唯一入口。

核心机制
--------
1. **统一调用**：`registry.call("get_realtime_quote", symbol="600519")`。
   调用方不需要知道底层是 AkShare 还是 Tushare。
2. **离线硬阻断**：`MCPToolRegistry(offline=True)` 时，任何
   `requires_network=True` 的工具在**函数被调用之前**就返回失败信封。
   这是硬阻断而非"尽量不调"——离线模式下网络工具的函数体永不执行，
   因此零网络请求可被测试证明（见 tests/test_mcp_layer.py）。
3. **异常不外泄**：工具内部任何异常都被捕获并转成失败信封，保证主链路
   不会因为数据源抖动而崩。但**不伪造数据** —— 失败信封 data 恒为 None。
4. **调用审计**：每次调用记入 `call_log`，含工具名、耗时、是否成功、
   是否被离线阻断。供可观测面板和测试断言使用。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_platform.mcp.envelope import err_envelope, is_ok

logger = logging.getLogger(__name__)


class MCPToolNotFoundError(KeyError):
    """请求的工具未注册。"""


@dataclass(frozen=True, slots=True)
class MCPToolSpec:
    """一个 MCP 工具的元数据。"""

    name: str
    fn: Callable[..., dict[str, Any]]
    description: str
    requires_network: bool
    provider: str            # akshare / tushare / offline
    category: str            # quote / history / financials / valuation / index / industry

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "requires_network": self.requires_network,
            "provider": self.provider,
            "category": self.category,
        }


@dataclass
class MCPCallRecord:
    """单次工具调用的审计记录。"""

    tool: str
    ok: bool
    duration_s: float
    blocked_offline: bool = False
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "duration_s": round(self.duration_s, 6),
            "blocked_offline": self.blocked_offline,
            "error_type": self.error_type,
        }


class MCPToolRegistry:
    """
    MCP 工具注册表。

    用法::

        reg = build_default_registry(offline=False)
        env = reg.call("get_realtime_quote", symbol="600519")
        if env["ok"]:
            price = env["data"]["price"]
        else:
            # 走降级分支，标记 data_status="fallback"
            reason = env["error"]
    """

    def __init__(self, *, offline: bool = False) -> None:
        self._tools: dict[str, MCPToolSpec] = {}
        self.offline = offline
        self.call_log: list[MCPCallRecord] = []

    # ── 注册 ────────────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        fn: Callable[..., dict[str, Any]],
        *,
        description: str,
        requires_network: bool,
        provider: str,
        category: str,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"MCP 工具重复注册: {name}")
        self._tools[name] = MCPToolSpec(
            name=name,
            fn=fn,
            description=description,
            requires_network=requires_network,
            provider=provider,
            category=category,
        )

    # ── 查询 ────────────────────────────────────────────────────────────────

    def has(self, name: str) -> bool:
        return name in self._tools

    def spec(self, name: str) -> MCPToolSpec:
        if name not in self._tools:
            raise MCPToolNotFoundError(
                f"MCP 工具 {name!r} 未注册。已注册: {sorted(self._tools)}"
            )
        return self._tools[name]

    def list_tools(self) -> list[dict[str, Any]]:
        return [self._tools[n].to_dict() for n in sorted(self._tools)]

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    # ── 调用 ────────────────────────────────────────────────────────────────

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """
        调用工具，恒返回信封（成功或失败），不抛业务异常。

        Raises
        ------
        MCPToolNotFoundError
            工具名未注册。这是调用方的编码错误，必须暴露而非静默降级。
        """
        spec = self.spec(name)   # 未注册 → 抛错（编码错误，不该静默）
        # 用 perf_counter 而非 monotonic 计时：Windows 上 time.monotonic() 底层是
        # GetTickCount64()，分辨率约 15.625ms，而绝大多数 MCP 调用（尤其离线阻断）
        # 都在 1ms 以内，会被记成 duration_s=0.0。审计日志若显示"每一次调用都耗时
        # 0 秒"，就失去了作为耗时证据的意义。perf_counter 分辨率 1e-7s。
        started = time.perf_counter()

        # ── 离线硬阻断：函数体不执行 ──
        if self.offline and spec.requires_network:
            env = err_envelope(
                tool=name,
                source=f"{spec.provider}(blocked)",
                error=(
                    f"离线模式禁止网络调用：工具 {name} 需要访问 {spec.provider}，"
                    f"已在注册表层阻断，未发出任何请求"
                ),
                error_type="OfflineModeBlocked",
                params=kwargs,
            )
            self.call_log.append(MCPCallRecord(
                tool=name,
                ok=False,
                duration_s=time.perf_counter() - started,
                blocked_offline=True,
                error_type="OfflineModeBlocked",
            ))
            logger.info("[MCP] 离线阻断 %s", name)
            return env

        # ── 正常调用，异常一律转失败信封 ──
        try:
            env = spec.fn(**kwargs)
        except Exception as exc:                      # noqa: BLE001 — 边界层需兜住一切
            env = err_envelope(
                tool=name,
                source=spec.provider,
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
                params=kwargs,
            )
            logger.warning("[MCP] 工具 %s 抛出异常: %s", name, exc)

        duration = time.perf_counter() - started
        succeeded = is_ok(env)
        self.call_log.append(MCPCallRecord(
            tool=name,
            ok=succeeded,
            duration_s=duration,
            blocked_offline=False,
            error_type=None if succeeded else str(env.get("error_type")),
        ))
        return env

    # ── 审计 ────────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        total = len(self.call_log)
        if total == 0:
            return {
                "total": 0, "ok": 0, "failed": 0,
                "blocked_offline": 0, "success_rate": 0.0,
                "offline": self.offline,
            }
        ok_n = sum(1 for r in self.call_log if r.ok)
        blocked = sum(1 for r in self.call_log if r.blocked_offline)
        return {
            "total": total,
            "ok": ok_n,
            "failed": total - ok_n,
            "blocked_offline": blocked,
            "success_rate": round(ok_n / total, 3),
            "offline": self.offline,
            "by_tool": {
                name: sum(1 for r in self.call_log if r.tool == name)
                for name in sorted({r.tool for r in self.call_log})
            },
        }

    def reset_log(self) -> None:
        self.call_log.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 默认注册表构造
# ─────────────────────────────────────────────────────────────────────────────

def build_default_registry(*, offline: bool = False) -> MCPToolRegistry:
    """
    构造包含全部内置工具的注册表。

    延迟 import 工具模块，避免 registry 与 tools 之间的循环依赖。
    """
    from agent_platform.mcp import akshare_tools, info_tools, offline_tools, tushare_tools

    reg = MCPToolRegistry(offline=offline)
    akshare_tools.register_all(reg)
    tushare_tools.register_all(reg)
    # 离线样例工具也必须注册：否则 offline 模式会绕开 MCP 直读样例字典，
    # MCP 层就退化成"只在联网时生效"的装饰层（见 offline_tools 模块说明）。
    offline_tools.register_all(reg)
    # 信息工具：财经新闻 / 公司公告 / 研报摘要 / 政策宏观 / 利率
    # 全部 requires_network=True，离线模式由注册表硬阻断，函数体不执行。
    info_tools.register_all(reg)
    return reg


_SHARED: dict[str, MCPToolRegistry] = {}


def get_registry(*, offline: bool = False) -> MCPToolRegistry:
    """
    获取共享注册表（按 offline 分别缓存）。

    共享实例便于可观测面板汇总调用统计；需要独立审计时请直接用
    `build_default_registry()` 自建实例。
    """
    key = "offline" if offline else "online"
    if key not in _SHARED:
        _SHARED[key] = build_default_registry(offline=offline)
    return _SHARED[key]
