"""
大盘/宏观 Agent（Market Regime）
==================================
输入：指数代码（默认 sh000001）
工具：MCP 工具层 —— 在线 get_index_daily + get_northbound_flow；
      离线 get_offline_market_regime（零网络）
输出：市场状态判断（bull / bear / consolidation / unknown）+ 风险偏好
      并附 source / updated_at / data_status / fallback_reason
Harness：JSONSchemaValidator + SourceAttributionFilter + KeywordBlocker

已知边界
--------
* **不使用融资余额**：早期文档曾声称接入融资余额，实际从未实现。此处如实声明
  当前只用指数日线与北向资金两路输入，避免文档虚报数据源。
* **北向资金逐日净买额已于 2024-08 停止披露**：仅在 MCP 工具报告新鲜时采用，
  否则该字段为 None 并在 regime_note 写明原因，绝不用存量数据冒充当日资金面。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

MARKET_REGIME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["regime", "risk_appetite", "source", "updated_at"],
    "properties": {
        "regime": {
            "type": "string",
            "enum": ["bull", "bear", "consolidation", "unknown"],
        },
        "risk_appetite": {
            "type": "string",
            "enum": ["high", "medium", "low", "unknown"],
        },
        "index_code": {"type": "string"},
        "index_close": {},
        "index_change_pct_5d": {},
        "northbound_flow_cny": {},
        "regime_note": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}


@dataclass(frozen=True, slots=True)
class MarketRegimeResult:
    regime: str              # bull / bear / consolidation / unknown
    risk_appetite: str       # high / medium / low / unknown
    index_code: str
    index_close: float | None
    index_change_pct_5d: float | None    # 5日涨跌幅
    northbound_flow_cny: float | None    # 近期北向资金净流入（元）
    regime_note: str
    source: str
    updated_at: str
    disclaimer: str
    data_status: str                     # live / offline_sample / fallback / unavailable
    fallback_reason: str | None          # 降级或不可用时的原因

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "risk_appetite": self.risk_appetite,
            "index_code": self.index_code,
            "index_close": self.index_close,
            "index_change_pct_5d": self.index_change_pct_5d,
            "northbound_flow_cny": self.northbound_flow_cny,
            "regime_note": self.regime_note,
            "source": self.source,
            "updated_at": self.updated_at,
            "disclaimer": self.disclaimer,
            "data_status": self.data_status,
            "fallback_reason": self.fallback_reason,
        }

    def to_markdown(self) -> str:
        regime_map = {
            "bull": "🟢 牛市 / 上升趋势",
            "bear": "🔴 熊市 / 下降趋势",
            "consolidation": "🟡 震荡整理",
            "unknown": "⚪ 无法判断",
        }
        appetite_map = {"high": "🟢 偏高", "medium": "🟡 中性", "low": "🔴 偏低", "unknown": "⚪ 未知"}
        close_str = f"{self.index_close:.2f}" if self.index_close is not None else "N/A"
        chg_str = f"{self.index_change_pct_5d:+.2f}%" if self.index_change_pct_5d is not None else "N/A"
        nb_str = (
            f"{self.northbound_flow_cny / 1e8:.2f} 亿"
            if self.northbound_flow_cny is not None
            else "N/A"
        )
        return "\n".join([
            f"### 大盘/宏观分析（{self.index_code}）",
            f"- 数据来源：{self.source}，更新时间：{self.updated_at}",
            "",
            "**市场状态（Market Regime）**",
            f"- 当前 Regime：{regime_map.get(self.regime, '未知')}",
            f"- 风险偏好：{appetite_map.get(self.risk_appetite, '未知')}",
            f"- 指数最新收盘：{close_str}",
            f"- 5日涨跌幅：{chg_str}",
            f"- 北向资金净流入：{nb_str}",
            f"- 判断依据：{self.regime_note}",
            "",
            f"> ⚠️ {self.disclaimer}",
        ])


def _determine_regime(
    change_5d: float | None,
    northbound: float | None,
) -> tuple[str, str, str]:
    """返回 (regime, risk_appetite, note)。"""
    if change_5d is None:
        return "unknown", "unknown", "指数数据不可用"

    notes: list[str] = [f"5日涨跌幅 {change_5d:+.2f}%"]
    if northbound is not None:
        notes.append(f"北向净流入 {northbound/1e8:.1f} 亿")

    # Regime 判断
    if change_5d > 3.0:
        regime = "bull"
    elif change_5d < -3.0:
        regime = "bear"
    else:
        regime = "consolidation"

    # 风险偏好（结合北向资金）
    if northbound is not None:
        if northbound > 5e8:
            risk = "high"
        elif northbound < -5e8:
            risk = "low"
        else:
            risk = "medium"
    else:
        risk = "medium" if regime != "bear" else "low"

    return regime, risk, "；".join(notes)


def analyze_market_regime(index_code: str = "sh000001", force_offline: bool = False) -> MarketRegimeResult:
    """分析当前大盘状态（Market Regime）。

    取数统一经 MCP 工具层
    --------------------
    离线：``get_offline_market_regime``（requires_network=False）。
    在线：``get_index_daily`` + ``get_northbound_flow``。

    ``force_offline=True`` 时使用 offline 注册表，任何 requires_network 工具都会
    在函数体执行前被硬阻断，因此「离线零网络请求」可被测试证明，而不是靠
    本函数自觉不调网络。

    北向资金的真实边界（不假装有实时数据）
    ------------------------------------
    交易所自 2024-08 起取消沪深港通逐日净买额披露，上游最新数百行的
    「当日成交净买额」恒为 NaN。本函数**只在** MCP 工具报告 ``is_fresh=True``
    时采用该数值，否则保持 ``northbound_flow_cny=None`` 并在 ``regime_note``
    写明原因。既不把 NaN 当 0，也不把停止披露前的存量数据当当日资金面 ——
    两者都属于伪造行情。

    历史缺陷修复记录
    ----------------
    原实现硬编码 ``ak.stock_em_hsgt_north_acc_flow_in_one``，该接口在
    akshare 1.18.x 已被移除，AttributeError 被 ``logger.debug("非关键")``
    吞掉，导致在线模式下 ``northbound_flow_cny`` 恒为 None 且无人可见。
    现由 MCP 工具做多候选名解析，接口消失会成为可见失败并写入 regime_note。

    Parameters
    ----------
    force_offline : bool
        True 时跳过全部网络调用，返回确定性离线样例数据（零网络请求）。
    """
    from agent_platform.finance.constants import DISCLAIMER
    from agent_platform.finance.data_status import (
        STATUS_FALLBACK,
        STATUS_LIVE,
        STATUS_OFFLINE_SAMPLE,
    )
    from agent_platform.mcp import get_registry

    updated_at = datetime.utcnow().isoformat() + "Z"
    index_close: float | None = None
    change_5d: float | None = None
    northbound: float | None = None
    source = "内置样例数据（offline_sample_data.py）"
    data_status = STATUS_OFFLINE_SAMPLE
    fallback_reason: str | None = None

    registry = get_registry(offline=force_offline)

    if force_offline:
        # 离线模式：确定性样例数据，经 MCP 离线工具取，与在线路径同一入口
        env = registry.call("get_offline_market_regime", index_code=index_code)
        sample = env["data"] or {}
        regime = sample.get("regime", "unknown")
        risk_appetite = sample.get("risk_appetite", "unknown")
        index_close = sample.get("index_close")
        change_5d = sample.get("index_change_pct_5d")
        northbound = sample.get("northbound_flow_cny")
        note = sample.get("regime_note", "")
        source = f"内置样例数据（MCP:{env['tool']}）"
    else:
        notes: list[str] = []

        # ── 1. 大盘指数日线 ──
        idx_env = registry.call("get_index_daily", index_code=index_code, limit=10)
        idx_failure: str | None = None

        if idx_env["ok"]:
            records = (idx_env["data"] or {}).get("records") or []
            closes = [
                float(r["close"])
                for r in records
                if isinstance(r.get("close"), (int, float))
            ]
            if len(closes) < 2:
                idx_failure = f"指数日线有效收盘价不足（{len(closes)} 条 < 2 条）"
            else:
                base = closes[max(-6, -len(closes))]
                if not base:
                    idx_failure = "基期收盘价为 0，无法计算 5 日涨跌幅"
                else:
                    index_close = closes[-1]
                    change_5d = (index_close - base) / base * 100
                    source = f"MCP:{idx_env['tool']} ← {idx_env['source']}"
                    data_status = STATUS_LIVE
        else:
            idx_failure = f"{idx_env['error_type']}: {idx_env['error']}"

        if idx_failure is not None:
            # 指数取数失败 → 降级为样例数据，并显式标记 fallback。
            # 原实现在这里仍标 offline_sample，等于把「联网失败」说成「主动离线」，
            # 现改为 fallback + fallback_reason，降级永不静默。
            logger.warning(
                "[MarketRegimeAgent] 指数数据不可用，降级为样例数据: %s", idx_failure,
            )
            fb_env = registry.call("get_offline_market_regime", index_code=index_code)
            sample = fb_env["data"] or {}
            index_close = sample.get("index_close")
            change_5d = sample.get("index_change_pct_5d")
            northbound = sample.get("northbound_flow_cny")
            source = f"降级样例数据（MCP:{fb_env['tool']}，指数取数失败）"
            data_status = STATUS_FALLBACK
            fallback_reason = f"指数取数失败: {idx_failure}"
        else:
            # ── 2. 北向资金：仅在上游确认新鲜时才采用 ──
            nb_env = registry.call("get_northbound_flow")
            if nb_env["ok"]:
                nb = nb_env["data"] or {}
                fresh = bool(nb.get("is_fresh"))
                latest = nb.get("latest_net_inflow_cny")
                if fresh and latest is not None:
                    northbound = float(latest)
                else:
                    notes.append(
                        "北向资金当日净买额不可用（最后披露 "
                        f"{nb.get('last_available_date') or '未知'}，距今 "
                        f"{nb.get('staleness_days')} 个自然日，交易所已停止逐日披露）"
                    )
            else:
                notes.append(f"北向资金取数失败（{nb_env['error_type']}）")

        # 在线模式使用动态判断
        regime, risk_appetite, note = _determine_regime(change_5d, northbound)
        if notes:
            note = "；".join([note, *notes])

    return MarketRegimeResult(
        regime=regime,
        risk_appetite=risk_appetite,
        index_code=index_code,
        index_close=index_close,
        index_change_pct_5d=change_5d,
        northbound_flow_cny=northbound,
        regime_note=note,
        source=source,
        updated_at=updated_at,
        disclaimer=DISCLAIMER,
        data_status=data_status,
        fallback_reason=fallback_reason,
    )
