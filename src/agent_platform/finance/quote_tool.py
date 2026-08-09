"""
最新公开报价工具（Quote Tool）
==============================
给 Agent 对话使用的**确定性**行情工具。规则只有两条：

  1. 拿到数据就返回完整字段（代码、名称、价格、昨收、涨跌幅、来源、
     更新时间、数据状态、降级原因）。
  2. 拿不到就显式失败（``QuoteToolError``），由调用方明说"取不到"。
     **绝不返回随机值、占位价格或让模型自行猜测价格。**

这是"模型不得编造价格"这条要求的执行点：模型只能引用本工具的返回值，
工具失败时对话必须承认失败。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agent_platform.finance.data_status import (
    STATUS_FALLBACK,
    STATUS_LIVE,
    STATUS_OFFLINE_SAMPLE,
    normalize_data_mode,
    provider_for_mode,
)

TOOL_NAME = "get_latest_quote"

# 触发行情意图的关键词（中英文），命中即必须调用本工具。
QUOTE_INTENT_KEYWORDS = (
    "当前价",
    "现价",
    "最新价",
    "多少钱",
    "报价",
    "行情",
    "涨跌",
    "股价",
    "价格",
    "quote",
    "price",
)

_SYMBOL_PATTERNS = (
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"\b([A-Z]{2,}\d{3,})\b"),
)


class QuoteToolError(RuntimeError):
    """行情工具失败。必须向用户显式暴露，不得用生成数据掩盖。"""


@dataclass(frozen=True, slots=True)
class QuotePayload:
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

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": self.price,
            "prev_close": self.prev_close,
            "change_pct": self.change_pct,
            "market": self.market,
            "source": self.source,
            "updated_at": self.updated_at,
            "data_status": self.data_status,
            "fallback_reason": self.fallback_reason,
        }

    def to_prompt_text(self) -> str:
        """给模型看的确定性事实块。模型只能引用这里的数字。"""
        sign = "+" if self.change_pct >= 0 else ""
        lines = [
            f"- 证券代码：{self.symbol}",
            f"- 证券名称：{self.name}",
            f"- 最新价：{self.price:.2f}",
            f"- 昨收价：{self.prev_close:.2f}",
            f"- 涨跌幅：{sign}{self.change_pct:.2f}%",
            f"- 数据来源：{self.source}",
            f"- 更新时间：{self.updated_at}",
            f"- 数据状态：{self.data_status}",
        ]
        if self.fallback_reason:
            lines.append(f"- 降级原因：{self.fallback_reason}")
        return "\n".join(lines)

    def to_answer_text(self) -> str:
        """无需 LLM 也能给出的确定性回答文本。"""
        sign = "+" if self.change_pct >= 0 else ""
        status_note = {
            STATUS_LIVE: "实时公开数据",
            STATUS_OFFLINE_SAMPLE: "离线样例数据（非真实行情）",
            STATUS_FALLBACK: "真实数据源降级后的样例数据（非真实行情）",
        }.get(self.data_status, self.data_status)
        text = (
            f"{self.name}（{self.symbol}）最新价 {self.price:.2f} 元，"
            f"昨收 {self.prev_close:.2f} 元，涨跌幅 {sign}{self.change_pct:.2f}%。\n\n"
            f"数据来源：{self.source}（{status_note}），更新时间：{self.updated_at}。"
        )
        if self.fallback_reason:
            text += f"\n\n降级原因：{self.fallback_reason}"
        return text


@dataclass
class ToolInvocation:
    """一次工具调用的真实记录（供前端展示 tool steps）。"""

    tool_name: str
    input: dict[str, object]
    output: dict[str, object] | None = None
    status: str = "success"
    error: str | None = None
    duration_ms: float = 0.0
    extra: dict[str, object] = field(default_factory=dict)


def has_quote_intent(message: str) -> bool:
    """是否为行情类提问。命中则必须调用工具，不允许模型自答价格。"""
    text = (message or "").lower()
    return any(kw.lower() in text for kw in QUOTE_INTENT_KEYWORDS)


def extract_symbol(message: str) -> str | None:
    """从自然语言中提取证券代码（6 位数字，或 DEMO001 这类样例代码）。"""
    upper = (message or "").upper()
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.search(upper)
        if match:
            return match.group(1)
    return None


def get_latest_quote(symbol: str, *, data_mode: str = "auto", provider=None) -> QuotePayload:
    """获取最新公开报价。失败抛 ``QuoteToolError``，绝不返回生成数据。"""
    normalized_symbol = (symbol or "").strip().upper()
    if not normalized_symbol:
        raise QuoteToolError("缺少证券代码，无法查询报价")

    mode = normalize_data_mode(data_mode)
    active = provider if provider is not None else provider_for_mode(mode)

    try:
        raw = active.get_realtime_quote(normalized_symbol)
    except Exception as exc:  # noqa: BLE001 - 统一转换为显式工具失败
        raise QuoteToolError(
            f"行情工具获取 {normalized_symbol} 失败（{type(exc).__name__}）：{exc}"
        ) from exc

    if not isinstance(raw, dict) or raw.get("price") in (None, ""):
        raise QuoteToolError(f"行情工具返回的数据不含有效价格：{normalized_symbol}")

    try:
        price = float(raw["price"])
        prev_close = float(raw.get("prev_close") or price)
    except (TypeError, ValueError) as exc:
        raise QuoteToolError(
            f"行情工具返回的价格无法解析：{raw.get('price')!r}"
        ) from exc

    if price <= 0:
        raise QuoteToolError(f"行情工具返回的价格非正数：{price}")

    change_pct = raw.get("change_pct")
    if change_pct is None:
        change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close else 0.0

    data_status = str(raw.get("data_status") or "")
    if not data_status:
        data_status = STATUS_OFFLINE_SAMPLE if mode == "offline" else STATUS_LIVE

    return QuotePayload(
        symbol=str(raw.get("symbol") or normalized_symbol),
        name=str(raw.get("name") or normalized_symbol),
        price=round(price, 2),
        prev_close=round(prev_close, 2),
        change_pct=round(float(change_pct), 2),
        market=str(raw.get("market") or ""),
        source=str(raw.get("source") or ""),
        updated_at=str(raw.get("updated_at") or datetime.now(UTC).isoformat()),
        data_status=data_status,
        fallback_reason=raw.get("fallback_reason"),
    )
