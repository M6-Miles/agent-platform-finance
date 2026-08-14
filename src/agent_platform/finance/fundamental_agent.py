"""
基本面分析 Agent
================
输入：股票代码
工具：AkShare 实时行情（PE/PB/总市值）+ 技术分析结果作为价格基准
输出：结构化基本面报告（符合 JSONSchema，含 source / updated_at）
Harness：JSONSchemaValidator + SourceAttributionFilter + KeywordBlocker
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ─── 输出 Schema（供 JSONSchemaValidator 使用）────────────────────────────────

FUNDAMENTAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["symbol", "source", "updated_at", "valuation_signal"],
    "properties": {
        "symbol": {"type": "string"},
        "name": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "pe_ttm": {},
        "pb": {},
        "total_market_value_cny": {},
        "roe_pct": {},
        "debt_to_asset_pct": {},
        "dcf": {},
        "valuation_signal": {"type": "string", "enum": ["undervalued", "fairly_valued", "overvalued", "unknown"]},
        "valuation_note": {"type": "string"},
        "disclaimer": {"type": "string"},
        "data_status": {"type": "string"},
        "fallback_reason": {},
    },
}


@dataclass(frozen=True, slots=True)
class FundamentalResult:
    symbol: str
    name: str
    source: str
    updated_at: str
    pe_ttm: float | None
    pb: float | None
    total_market_value_cny: float | None
    roe_pct: float | None           # ROE% — Tushare 可提供，AkShare 可能为 None
    valuation_signal: str           # undervalued / fairly_valued / overvalued / unknown
    valuation_note: str
    disclaimer: str
    data_status: str                # live / offline_sample / fallback / unavailable
    fallback_reason: str | None     # 降级或不可用时的原因
    # ↓ 以下两个字段带默认值，保证既有调用方（含既有测试）按原 13 个字段
    #   构造 FundamentalResult 时不会因新增字段而报 TypeError。
    debt_to_asset_pct: float | None = None   # 资产负债率%（说明书要求指标）
    dcf: dict[str, Any] | None = None        # DCF 估值结果（DCFResult.to_dict()）

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "source": self.source,
            "updated_at": self.updated_at,
            "pe_ttm": self.pe_ttm,
            "pb": self.pb,
            "total_market_value_cny": self.total_market_value_cny,
            "roe_pct": self.roe_pct,
            "debt_to_asset_pct": self.debt_to_asset_pct,
            "dcf": self.dcf,
            "valuation_signal": self.valuation_signal,
            "valuation_note": self.valuation_note,
            "disclaimer": self.disclaimer,
            "data_status": self.data_status,
            "fallback_reason": self.fallback_reason,
            # 字段级状态映射：每个关键指标的来源与可用性，前端/下游可直接消费
            "field_status": self.field_status(),
        }

    def field_status(self) -> dict[str, dict[str, str]]:
        """
        返回每个关键财务字段的来源与状态映射。

        结构：{字段名: {"status": "live|offline_sample|fallback|unavailable", "source": "...", "note": "..."}}

        规则：
        - PE_TTM / PB / 总市值：随整体 data_status，但若值为 None 则单独标 unavailable。
        - ROE / 资产负债率：可能单独缺失（AkShare get_financial_indicator EmptyResult），
          此时不能把整体标成 live，而是这两个字段单独标 unavailable。
        - DCF：applicable=True 时 live/offline，否则 not_applicable。
        """
        base_status = self.data_status or "unavailable"

        def _field(value: Any, extra_note: str = "") -> dict[str, str]:
            if value is None:
                return {
                    "status": "unavailable",
                    "source": self.source,
                    "note": extra_note or "上游未返回该字段",
                }
            return {
                "status": base_status,
                "source": self.source,
                "note": extra_note,
            }

        fs: dict[str, dict[str, str]] = {
            "pe_ttm": _field(self.pe_ttm),
            "pb": _field(self.pb),
            "total_market_value_cny": _field(self.total_market_value_cny),
        }

        # ROE 与资产负债率可能单独缺失（财务指标接口独立于估值快照）
        roe_note = ""
        debt_note = ""
        if self.fallback_reason and ("ROE" in self.fallback_reason or "资产负债率" in self.fallback_reason):
            roe_note = self.fallback_reason
            debt_note = self.fallback_reason
        fs["roe_pct"] = _field(self.roe_pct, roe_note)
        fs["debt_to_asset_pct"] = _field(self.debt_to_asset_pct, debt_note)

        if self.dcf is None:
            fs["dcf"] = {"status": "unavailable", "source": self.source, "note": "DCF 计算失败"}
        elif not self.dcf.get("applicable"):
            fs["dcf"] = {
                "status": "not_applicable",
                "source": self.source,
                "note": str(self.dcf.get("reason_not_applicable") or "不满足 DCF 适用条件"),
            }
        else:
            fs["dcf"] = {
                "status": base_status,
                "source": self.source,
                "note": "proxy FCFF 口径，基于 PE/PB/市值反推，非完整企业级报表",
            }
        return fs

    def to_markdown(self) -> str:
        pe_str = f"{self.pe_ttm:.1f}" if self.pe_ttm is not None else "N/A"
        pb_str = f"{self.pb:.2f}" if self.pb is not None else "N/A"
        roe_str = f"{self.roe_pct:.1f}%" if self.roe_pct is not None else "N/A"
        debt_str = (
            f"{self.debt_to_asset_pct:.1f}%"
            if self.debt_to_asset_pct is not None
            else "N/A"
        )
        mv_str = (
            f"{self.total_market_value_cny / 1e8:.0f} 亿"
            if self.total_market_value_cny is not None
            else "N/A"
        )
        signal_map = {
            "undervalued": "🟢 低估",
            "fairly_valued": "🟡 合理",
            "overvalued": "🔴 高估",
            "unknown": "⚪ 无数据",
        }
        lines = [
            f"### {self.name}（{self.symbol}）基本面分析",
            f"- 数据来源：{self.source}，更新时间：{self.updated_at}",
            f"- 数据状态：{self.data_status}"
            + (f"（{self.fallback_reason}）" if self.fallback_reason else ""),
            "",
            "**估值指标**",
            f"- 市盈率（PE TTM）：{pe_str}",
            f"- 市净率（PB）：{pb_str}",
            f"- 总市值：{mv_str}",
            f"- ROE：{roe_str}",
            f"- 资产负债率：{debt_str}",
            "",
            "**综合估值判断**",
            f"- 信号：{signal_map.get(self.valuation_signal, '未知')}",
            f"- 依据：{self.valuation_note}",
        ]

        # DCF 段落：不适用时如实说明原因，不输出编造的估值数字
        if self.dcf is not None:
            lines.append("")
            if self.dcf.get("applicable"):
                mos = self.dcf.get("margin_of_safety_pct")
                lines += [
                    "**DCF 估值（两阶段，可解释）**",
                    f"- 股权价值：{(self.dcf.get('equity_value_cny') or 0) / 1e8:.2f} 亿"
                    f"（当前市值 {(self.dcf.get('market_value_cny') or 0) / 1e8:.2f} 亿）",
                    f"- 安全边际：{mos:+.2f}%" if mos is not None else "- 安全边际：N/A",
                    f"- WACC：{(self.dcf.get('wacc') or 0) * 100:.2f}%"
                    f"，第一阶段增长率：{(self.dcf.get('growth_stage1') or 0) * 100:.2f}%"
                    f"，永续增长率：{(self.dcf.get('terminal_growth') or 0) * 100:.2f}%",
                    f"- 估值信号：{self.dcf.get('valuation_signal')}",
                ]
                for warn in self.dcf.get("warnings") or []:
                    lines.append(f"- ⚠️ 假设风险：{warn}")
            else:
                lines += [
                    "**DCF 估值**",
                    f"- 不适用：{self.dcf.get('reason_not_applicable')}",
                ]

        lines += ["", f"> ⚠️ {self.disclaimer}"]
        return "\n".join(lines)


def _valuation_signal(pe: float | None, pb: float | None) -> tuple[str, str]:
    """
    简化版估值判断（基于 A 股历史均值经验值）。
    PE < 15 且 PB < 2  → 低估
    PE > 40 或 PB > 5  → 高估
    否则               → 合理
    数据缺失           → unknown
    """
    if pe is None and pb is None:
        return "unknown", "PE 和 PB 数据均不可用"

    signals: list[str] = []
    notes: list[str] = []

    if pe is not None:
        if pe < 0:
            signals.append("overvalued")
            notes.append(f"PE={pe:.1f}（亏损）")
        elif pe < 15:
            signals.append("undervalued")
            notes.append(f"PE={pe:.1f}（低于历史均值15x）")
        elif pe > 40:
            signals.append("overvalued")
            notes.append(f"PE={pe:.1f}（高于历史均值40x）")
        else:
            signals.append("fairly_valued")
            notes.append(f"PE={pe:.1f}（处于合理区间15–40x）")

    if pb is not None:
        if pb < 1:
            signals.append("undervalued")
            notes.append(f"PB={pb:.2f}（低于净资产）")
        elif pb > 5:
            signals.append("overvalued")
            notes.append(f"PB={pb:.2f}（高于历史均值5x）")
        else:
            signals.append("fairly_valued")
            notes.append(f"PB={pb:.2f}（处于合理区间1–5x）")

    # 多数表决
    overvalued_count = signals.count("overvalued")
    undervalued_count = signals.count("undervalued")
    if overvalued_count > undervalued_count:
        final = "overvalued"
    elif undervalued_count > overvalued_count:
        final = "undervalued"
    else:
        final = "fairly_valued"

    return final, "；".join(notes)


def analyze_fundamental(symbol: str, name: str = "", force_offline: bool = False) -> FundamentalResult:
    """
    对指定股票进行基本面分析。
    优先使用 AkShare 实时数据；失败时降级为样例占位数据。

    Parameters
    ----------
    force_offline : bool
        True 时完全跳过 AkShare 网络调用，直接使用确定性离线样例数据。
        用于 LangGraph data_mode="offline" 场景，保证零网络请求。
    """
    from agent_platform.finance.constants import DISCLAIMER
    from agent_platform.finance.data_status import (
        STATUS_FALLBACK,
        STATUS_LIVE,
        STATUS_OFFLINE_SAMPLE,
    )
    from agent_platform.finance.dcf_valuation import DCFAssumptions, run_dcf
    from agent_platform.mcp import get_registry

    updated_at = datetime.utcnow().isoformat() + "Z"
    pe: float | None = None
    pb: float | None = None
    total_mv: float | None = None
    roe: float | None = None
    debt_ratio: float | None = None
    source = "内置样例数据（offline_sample_data.py）"
    data_status = STATUS_OFFLINE_SAMPLE
    fallback_reason: str | None = None

    # 统一经 MCP 工具层取数：offline=True 时注册表会在函数体执行前硬阻断所有
    # requires_network 工具，因此离线模式的零网络调用可被测试证明。
    registry = get_registry(offline=force_offline)

    if force_offline:
        env = registry.call("get_offline_fundamental", symbol=symbol)
        sample = env["data"] or {}
        name = sample.get("name") or name or symbol
        pe = sample.get("pe_ttm")
        pb = sample.get("pb")
        total_mv = sample.get("total_market_value_cny")
        roe = sample.get("roe_pct")
        debt_ratio = sample.get("debt_to_asset_pct")
        signal = sample.get("valuation_signal", "unknown")
        note = sample.get("valuation_note", "")
        source = f"内置样例数据（MCP:{env['tool']}）"
    else:
        # 两个公共数据接口互不依赖，并行拉取可将总等待从二者相加降为取较慢者。
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fundamental") as pool:
            val_future = pool.submit(registry.call, "get_valuation_metrics", symbol=symbol)
            ind_future = pool.submit(registry.call, "get_financial_indicator", symbol=symbol)
            val_env = val_future.result()
            ind_env = ind_future.result()
        if val_env["ok"]:
            d = val_env["data"] or {}
            pe = d.get("pe_ttm")
            pb = d.get("pb")
            total_mv = d.get("total_market_value_cny")
            name = name or str(d.get("name") or symbol)
            source = f"MCP:{val_env['tool']} ← {val_env['source']}"
            data_status = STATUS_LIVE

            # ROE 与资产负债率来自财务指标接口，失败时保持 None 并记录原因，
            # 绝不用市场倍数反推的近似值冒充真实财报数字。
            if ind_env["ok"]:
                ind = ind_env["data"] or {}
                roe = ind.get("roe_pct")
                debt_ratio = ind.get("debt_to_asset_pct")
            else:
                fallback_reason = f"ROE/资产负债率不可用: {ind_env['error_type']}"
                logger.warning(
                    "[FundamentalAgent] 财务指标不可用 %s: %s",
                    symbol, ind_env["error"],
                )
        else:
            logger.warning(
                "[FundamentalAgent] MCP 估值取数失败，降级为样例数据: %s",
                val_env["error"],
            )
            env = registry.call("get_offline_fundamental", symbol=symbol)
            sample = env["data"] or {}
            name = sample.get("name") or name or symbol
            pe = sample.get("pe_ttm")
            pb = sample.get("pb")
            total_mv = sample.get("total_market_value_cny")
            roe = sample.get("roe_pct")
            debt_ratio = sample.get("debt_to_asset_pct")
            source = f"降级样例数据（MCP:{val_env['tool']} 失败: {val_env['error_type']}）"
            data_status = STATUS_FALLBACK
            fallback_reason = f"AkShare失败: {val_env['error_type']}"

        signal, note = _valuation_signal(pe, pb)

    # ── DCF 估值 ──────────────────────────────────────────────────────────────
    # 两种模式都计算：DCF 是对 PE/PB/市值 的确定性推导，输入全部可溯源，
    # 不引入任何网络请求。边界不成立时 run_dcf 返回 applicable=False 并给出
    # 原因，而不是返回一个看起来合理的数字。
    dcf_dict: dict[str, Any] | None = None
    try:
        dcf_dict = run_dcf(
            pe_ttm=pe,
            pb=pb,
            total_market_value_cny=total_mv,
            roe_pct=roe,
            assumptions=DCFAssumptions(),
        ).to_dict()
    except Exception as exc:                       # noqa: BLE001 — 估值失败不应阻断主链路
        logger.warning("[FundamentalAgent] DCF 计算异常: %s", exc)
        dcf_dict = None

    return FundamentalResult(
        symbol=symbol,
        name=name or symbol,
        source=source,
        updated_at=updated_at,
        pe_ttm=pe,
        pb=pb,
        total_market_value_cny=total_mv,
        roe_pct=roe,
        valuation_signal=signal,
        valuation_note=note,
        disclaimer=DISCLAIMER,
        data_status=data_status,
        fallback_reason=fallback_reason,
        debt_to_asset_pct=debt_ratio,
        dcf=dcf_dict,
    )
