"""
统一的数据状态（Data Status）解析
==================================
所有财务响应都必须暴露 ``source`` / ``updated_at`` / ``data_status`` /
``fallback_reason``。本模块把"用哪个 Provider 取到了什么"这件事收敛成一个
函数，避免每个端点各写一份 if/else 并出现不一致的标签。

四级状态（与前端徽标一一对应）：

  live           真实数据源成功返回
  offline_sample 明确请求离线模式，使用内置样例数据
  fallback       请求了真实数据源但失败，已降级到样例数据
  unavailable    既拿不到真实数据，也拿不到样例数据
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Literal

import pandas as pd

from agent_platform.finance.sample_data_provider import SampleMarketDataProvider

logger = logging.getLogger(__name__)

DataMode = Literal["offline", "auto"]

STATUS_LIVE = "live"
STATUS_OFFLINE_SAMPLE = "offline_sample"
STATUS_FALLBACK = "fallback"
STATUS_UNAVAILABLE = "unavailable"

VALID_DATA_MODES = ("offline", "auto")

SOURCE_LABELS = {
    STATUS_LIVE: "AkShare 公开数据",
    STATUS_OFFLINE_SAMPLE: "内置样例数据（离线）",
    STATUS_FALLBACK: "内置样例数据（真实数据源降级）",
    STATUS_UNAVAILABLE: "数据不可用",
}


def normalize_data_mode(mode: str | None) -> str:
    """把任意输入规整为 ``offline`` / ``auto``。

    历史上前端曾传 ``sample`` / ``akshare``，后端语义已统一为 offline/auto，
    这里只接受这两个值，其余一律报错而不是静默当作 auto。
    """
    value = (mode or "auto").strip().lower()
    if value not in VALID_DATA_MODES:
        raise ValueError(
            f"不支持的 data_mode：{mode!r}。可选值：{', '.join(VALID_DATA_MODES)}"
        )
    return value


def resolve_effective_data_mode(symbol: str, requested_mode: str) -> str:
    """解析有效数据模式（effective data mode）。

    规则：
    - requested=offline → effective=offline（显式离线）
    - requested=auto + 样例代码(DEMO*/TEST*) → effective=offline（自动路由到离线）
    - requested=auto + 真实代码 → effective=auto（保持联网）

    返回值：'offline' 或 'auto'
    """
    normalized = normalize_data_mode(requested_mode)
    if normalized == "offline":
        return "offline"
    # auto 模式下，样例代码自动路由到 offline
    if is_sample_symbol(symbol):
        return "offline"
    return "auto"


def provider_for_mode(mode: str):
    """按 data_mode 创建 MCP Provider；默认业务取数不再绕过 MCP 注册表。"""
    normalized = normalize_data_mode(mode)
    from agent_platform.finance.mcp_market_data_provider import MCPMarketDataProvider

    return MCPMarketDataProvider(offline=normalized == "offline")


class MarketDataAllSourcesFailed(RuntimeError):
    """真实数据源与样例数据都不可用（端点转换为 HTTP 503）。"""


def is_sample_symbol(symbol: str) -> bool:
    """判断是否为样例代码（DEMO*/TEST*），供外部公开调用。"""
    value = symbol.strip().upper()
    return value.startswith("DEMO") or value.startswith("TEST")


def _is_offline_sample_symbol(symbol: str) -> bool:
    """内部别名，保持向后兼容。"""
    return is_sample_symbol(symbol)


def _public_unavailable_reason(symbol: str, exc: Exception) -> str:
    """生成适合 API 用户阅读的错误，不暴露上游 URL 或底层连接细节。"""
    error_name = type(exc).__name__
    return (
        f"真实行情源暂时无法访问（{error_name}），未返回 {symbol} 的行情。"
        "请稍后重试，并检查本机防火墙、代理或网络访问策略。"
        "系统未生成模拟价格，也未将其他证券的样例数据替代该结果。"
    )


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """一次取数的结果及其数据状态元信息。"""

    frame: pd.DataFrame
    data_status: str
    source: str
    updated_at: str
    fallback_reason: str | None

    def metadata(self) -> dict[str, str | None]:
        return {
            "source": self.source,
            "updated_at": self.updated_at,
            "data_status": self.data_status,
            "fallback_reason": self.fallback_reason,
        }


def _updated_at_from(frame: pd.DataFrame) -> str:
    if "updated_at" in frame.columns and not frame.empty:
        return str(frame.iloc[-1]["updated_at"])
    return datetime.now(UTC).date().isoformat()


def _source_from(frame: pd.DataFrame, default: str) -> str:
    if "source" in frame.columns and not frame.empty:
        return str(frame.iloc[-1]["source"])
    return default


def fetch_price_history(
    symbol: str,
    *,
    data_mode: str,
    start=None,
    end=None,
    provider=None,
) -> FetchOutcome:
    """按 data_mode 取日线数据，并给出四级数据状态。

    offline 模式不做任何网络调用；auto 模式失败时降级到样例数据并把
    ``data_status`` 标为 ``fallback``、同时给出 ``fallback_reason``。
    降级绝不静默：调用方总能从返回值分辨数据的真实来源。
    """
    normalized = normalize_data_mode(data_mode)
    active = provider if provider is not None else provider_for_mode(normalized)

    if normalized == "offline" or isinstance(active, SampleMarketDataProvider):
        frame = active.get_price_history(symbol, start=start, end=end)
        return FetchOutcome(
            frame=frame,
            data_status=STATUS_OFFLINE_SAMPLE,
            source=_source_from(frame, SOURCE_LABELS[STATUS_OFFLINE_SAMPLE]),
            updated_at=_updated_at_from(frame),
            fallback_reason=None,
        )

    try:
        frame = active.get_price_history(symbol, start=start, end=end)
        return FetchOutcome(
            frame=frame,
            data_status=STATUS_LIVE,
            source=_source_from(frame, SOURCE_LABELS[STATUS_LIVE]),
            updated_at=_updated_at_from(frame),
            fallback_reason=None,
        )
    except Exception as exc:  # noqa: BLE001 - 任何真实数据源故障都应可降级
        reason = f"真实数据源失败（{type(exc).__name__}）：{exc}"
        if not _is_offline_sample_symbol(symbol):
            logger.warning(
                "real market data unavailable: symbol=%s error_type=%s error=%s",
                symbol,
                type(exc).__name__,
                exc,
            )
            raise MarketDataAllSourcesFailed(
                _public_unavailable_reason(symbol, exc)
            ) from exc
        try:
            from agent_platform.finance.mcp_market_data_provider import MCPMarketDataProvider

            sample = MCPMarketDataProvider(offline=True)
            frame = sample.get_price_history(symbol, start=start, end=end)
        except Exception as sample_exc:  # noqa: BLE001
            raise MarketDataAllSourcesFailed(
                f"{reason}；样例数据同样不可用：{sample_exc}"
            ) from exc
        return FetchOutcome(
            frame=frame,
            data_status=STATUS_FALLBACK,
            source=SOURCE_LABELS[STATUS_FALLBACK],
            updated_at=_updated_at_from(frame),
            fallback_reason=reason,
        )


def combine_statuses(statuses: list[str]) -> str:
    """多标的聚合状态：任一降级即整体降级，避免用"最好"的标签掩盖问题。"""
    if not statuses:
        return STATUS_UNAVAILABLE
    if any(s == STATUS_FALLBACK for s in statuses):
        return STATUS_FALLBACK
    if all(s == STATUS_OFFLINE_SAMPLE for s in statuses):
        return STATUS_OFFLINE_SAMPLE
    if any(s == STATUS_OFFLINE_SAMPLE for s in statuses) and any(
        s == STATUS_LIVE for s in statuses
    ):
        return STATUS_FALLBACK
    if all(s == STATUS_LIVE for s in statuses):
        return STATUS_LIVE
    return STATUS_UNAVAILABLE
