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
from typing import Literal

import pandas as pd

from agent_platform.finance.provider_factory import create_market_data_provider
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider

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


def provider_for_mode(mode: str):
    """按 data_mode 创建 Provider。offline → 样例数据；auto → 配置的真实数据源。"""
    normalized = normalize_data_mode(mode)
    if normalized == "offline":
        return create_market_data_provider("sample")
    return create_market_data_provider("akshare")


class MarketDataAllSourcesFailed(RuntimeError):
    """真实数据源与样例数据都不可用（端点转换为 HTTP 503）。"""


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
        try:
            sample = SampleMarketDataProvider()
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
