"""
MCP 工具统一返回信封
====================
说明书要求「所有工具结果必须带 source、updated_at/timestamp 和明确错误字段」。
本模块定义唯一的返回结构，所有 MCP 工具必须通过 `ok_envelope` / `err_envelope`
构造返回值，不允许各工具自己拼字典 —— 那是原 MCP/*.py 的做法，导致字段不齐。

信封字段
--------
tool        : 工具名（注册名），便于调用方在日志里定位
params      : 本次调用的入参（脱敏后），便于复现
ok          : 布尔。True 表示取数成功且 data 可用
data        : 成功时的载荷；失败时为 None（**不返回猜测值**）
source      : 数据来源标识，形如 "akshare/stock_zh_a_daily"
updated_at  : 本次取数的 UTC 时间戳（ISO 8601，带 Z）
timestamp   : 与 updated_at 同值，兼容按 timestamp 取字段的调用方
error       : 失败时的人类可读原因；成功时为 None
error_type  : 失败时的异常类名或错误码；成功时为 None

设计约束
--------
1. **失败不得伪造数据**：err_envelope 的 data 恒为 None。上层看到 ok=False
   必须走降级分支并标记 data_status，不得把 None 当 0 或随机值使用。
2. **成功也必须有 source**：ok_envelope 强制要求 source 非空，否则抛
   ValueError —— 让缺失溯源在开发期就暴露，而不是流到输出里。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# 入参中不得回显的键（避免把 token 写进日志或返回体）
_REDACT_KEYS = frozenset({"token", "api_key", "apikey", "secret", "password"})


def utc_now_iso() -> str:
    """UTC 时间戳，ISO 8601，带 Z 后缀。"""
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


def _redact(params: dict[str, Any] | None) -> dict[str, Any]:
    """脱敏入参：命中 _REDACT_KEYS 的值替换为 ***。"""
    if not params:
        return {}
    return {
        k: ("***" if k.lower() in _REDACT_KEYS else v)
        for k, v in params.items()
    }


def ok_envelope(
    *,
    tool: str,
    source: str,
    data: Any,
    params: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    构造成功信封。

    Raises
    ------
    ValueError
        source 为空时。溯源字段缺失属于开发期错误，必须立即暴露。
    """
    if not source:
        raise ValueError(f"MCP 工具 {tool} 返回成功但未提供 source，违反溯源规则")
    now = utc_now_iso()
    env: dict[str, Any] = {
        "tool": tool,
        "params": _redact(params),
        "ok": True,
        "data": data,
        "source": source,
        "updated_at": now,
        "timestamp": now,
        "error": None,
        "error_type": None,
    }
    if extra:
        env.update(extra)
    return env


def err_envelope(
    *,
    tool: str,
    source: str,
    error: str,
    error_type: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    构造失败信封。data 恒为 None —— 调用方不得从失败信封里取数。
    """
    now = utc_now_iso()
    return {
        "tool": tool,
        "params": _redact(params),
        "ok": False,
        "data": None,
        "source": source or "unknown",
        "updated_at": now,
        "timestamp": now,
        "error": error,
        "error_type": error_type,
    }


def is_ok(envelope: Any) -> bool:
    """判断是否为成功信封。非 dict 或缺字段一律视为失败。"""
    return isinstance(envelope, dict) and envelope.get("ok") is True


REQUIRED_ENVELOPE_KEYS = frozenset({
    "tool", "params", "ok", "data",
    "source", "updated_at", "timestamp",
    "error", "error_type",
})


def validate_envelope(envelope: Any) -> list[str]:
    """
    校验信封结构，返回问题列表（空列表表示合规）。
    供测试和 Guardrail 使用。
    """
    problems: list[str] = []
    if not isinstance(envelope, dict):
        return [f"信封不是 dict，实际类型 {type(envelope).__name__}"]

    missing = REQUIRED_ENVELOPE_KEYS - set(envelope)
    if missing:
        problems.append(f"缺少字段: {sorted(missing)}")

    if not envelope.get("source"):
        problems.append("source 为空")
    if not envelope.get("updated_at"):
        problems.append("updated_at 为空")

    ok = envelope.get("ok")
    if ok is True:
        if envelope.get("error") is not None:
            problems.append("ok=True 但 error 非空")
    elif ok is False:
        if envelope.get("data") is not None:
            problems.append("ok=False 但 data 非空（失败不得返回数据）")
        if not envelope.get("error"):
            problems.append("ok=False 但 error 为空")
        if not envelope.get("error_type"):
            problems.append("ok=False 但 error_type 为空")
    else:
        problems.append(f"ok 必须是布尔，实际 {ok!r}")

    return problems
