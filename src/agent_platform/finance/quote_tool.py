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
import math
from dataclasses import dataclass, field
from datetime import datetime

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
    # 中文字符在 Python 正则中属于 word character，不能用 \b 判断
    # “000001多少钱”这类常见输入。这里只排除相邻数字，避免截取 7 位以上号码。
    re.compile(r"(?<!\d)(\d{6})(?!\d)"),
    re.compile(r"(?<![A-Z0-9])([A-Z]{2,}\d{3,})(?![A-Z0-9])"),
)

_SECURITY_NAME_ALIASES = {
    "贵州茅台": "600519",
    "平安银行": "000001",
    "万科A": "000002",
    "招商银行": "600036",
    "中国平安": "601318",
}


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
    for name, symbol in _SECURITY_NAME_ALIASES.items():
        if name.upper() in upper:
            return symbol
    return None


def is_sample_symbol(symbol: str) -> bool:
    value = (symbol or "").strip().upper()
    return value.startswith("DEMO") or value.startswith("TEST")


def get_latest_quote(symbol: str, *, data_mode: str = "auto", provider=None) -> QuotePayload:
    """获取最新公开报价。失败抛 ``QuoteToolError``，绝不返回生成数据。"""
    normalized_symbol = (symbol or "").strip().upper()
    if not normalized_symbol:
        raise QuoteToolError("缺少证券代码，无法查询报价")

    # 校验证券代码格式
    if not _is_valid_symbol_format(normalized_symbol):
        raise QuoteToolError(f"证券代码格式无效：{normalized_symbol}")

    mode = normalize_data_mode(data_mode)
    effective_mode = "offline" if mode == "auto" and is_sample_symbol(normalized_symbol) else mode
    active = (
        provider
        if provider is not None and effective_mode == mode
        else provider_for_mode(effective_mode)
    )

    try:
        raw = active.get_realtime_quote(normalized_symbol)
    except Exception as exc:  # noqa: BLE001 - 统一转换为显式工具失败
        raise QuoteToolError(
            f"行情工具获取 {normalized_symbol} 失败（{type(exc).__name__}）：{exc}"
        ) from exc

    if not isinstance(raw, dict) or raw.get("price") in (None, ""):
        raise QuoteToolError(f"行情工具返回的数据不含有效价格：{normalized_symbol}")

    # 校验必需字段
    if not raw.get("symbol"):
        raise QuoteToolError("行情工具返回的数据缺少 symbol 字段")
    if not raw.get("source"):
        raise QuoteToolError("行情工具返回的数据缺少 source 字段")
    if not raw.get("updated_at"):
        raise QuoteToolError("行情工具返回的数据缺少 updated_at 字段")

    returned_symbol = str(raw["symbol"]).strip().upper()
    if returned_symbol != normalized_symbol:
        raise QuoteToolError(
            f"行情工具返回证券代码不一致：请求 {normalized_symbol}，返回 {returned_symbol}"
        )

    updated_at = str(raw["updated_at"]).strip()
    try:
        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuoteToolError(f"行情工具返回的 updated_at 不是 ISO 8601：{updated_at}") from exc

    try:
        price = float(raw["price"])
        prev_close = float(raw.get("prev_close") or price)
    except (TypeError, ValueError) as exc:
        raise QuoteToolError(
            f"行情工具返回的价格无法解析：{raw.get('price')!r}"
        ) from exc

    if not math.isfinite(price) or price <= 0:
        raise QuoteToolError(f"行情工具返回的价格非正数：{price}")
    if not math.isfinite(prev_close) or prev_close <= 0:
        raise QuoteToolError(f"行情工具返回的昨收价非正数：{prev_close}")

    change_pct = raw.get("change_pct")
    computed_change_pct = (price - prev_close) / prev_close * 100.0
    if change_pct is None:
        change_pct = computed_change_pct
    try:
        change_pct = float(change_pct)
    except (TypeError, ValueError) as exc:
        raise QuoteToolError(f"行情工具返回的涨跌幅无法解析：{change_pct!r}") from exc
    if not math.isfinite(change_pct):
        raise QuoteToolError(f"行情工具返回的涨跌幅不是有限值：{change_pct}")
    # 上游通常保留两位小数，允许 0.05 个百分点的舍入误差。
    if abs(change_pct - computed_change_pct) > 0.05:
        raise QuoteToolError(
            "行情价格与涨跌幅不一致："
            f"现价={price}，昨收={prev_close}，返回涨跌幅={change_pct:.4f}%，"
            f"重算={computed_change_pct:.4f}%"
        )

    data_status = str(raw.get("data_status") or "")
    if not data_status:
        data_status = STATUS_OFFLINE_SAMPLE if effective_mode == "offline" else STATUS_LIVE

    # 校验 data_status 有效性
    valid_statuses = {STATUS_LIVE, STATUS_OFFLINE_SAMPLE, STATUS_FALLBACK, "delayed", "historical", "unavailable"}
    if data_status not in valid_statuses:
        raise QuoteToolError(f"行情工具返回的 data_status 无效：{data_status}")

    return QuotePayload(
        symbol=returned_symbol,
        name=str(raw.get("name") or normalized_symbol),
        price=round(price, 2),
        prev_close=round(prev_close, 2),
        change_pct=round(change_pct, 2),
        market=str(raw.get("market") or ""),
        source=str(raw.get("source") or ""),
        updated_at=updated_at,
        data_status=data_status,
        fallback_reason=raw.get("fallback_reason"),
    )


def _is_valid_symbol_format(symbol: str) -> bool:
    """校验证券代码格式（6位数字或样例代码）。"""
    if not symbol:
        return False
    # A股代码：6位数字
    if len(symbol) == 6 and symbol.isdigit():
        return True
    # 样例代码：DEMO001-999 或 TEST001-999
    if symbol.startswith(("DEMO", "TEST")):
        suffix = symbol[4:]
        if len(suffix) == 3 and suffix.isdigit():
            return True
    return False
