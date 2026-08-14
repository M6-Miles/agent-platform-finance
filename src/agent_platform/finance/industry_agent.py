"""
行业分析 Agent
==============
输入：股票代码（或行业名称）
工具：AkShare 申万行业资金流向 + 行业指数
输出：景气度判断 + 龙头排序（符合 JSONSchema，含 source / updated_at）
Harness：JSONSchemaValidator + SourceAttributionFilter + KeywordBlocker
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

INDUSTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["symbol", "industry_name", "prosperity_signal", "source", "updated_at"],
    "properties": {
        "symbol": {"type": "string"},
        "industry_name": {"type": "string"},
        "prosperity_signal": {
            "type": "string",
            "enum": ["booming", "normal", "sluggish", "unknown"],
        },
        "prosperity_note": {"type": "string"},
        "top_stocks": {"type": "array"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}

# A 股行业映射（代码前缀 → 大致行业，仅用于 Sample 模式降级）
_CODE_INDUSTRY_MAP = {
    "60": "金融/工业",
    "00": "消费/制造",
    "30": "科技/创业板",
    "68": "科技/科创板",
    "DEMO": "样例行业",
}


@dataclass(frozen=True, slots=True)
class IndustryResult:
    symbol: str
    industry_name: str
    source: str
    updated_at: str
    prosperity_signal: str        # booming / normal / sluggish / unknown
    prosperity_note: str
    top_stocks: list[dict[str, Any]]   # [{"rank":1,"code":"...","name":"...","change_pct":...}]
    fund_flow_3d_cny: float | None     # 3日资金净流入（元）
    disclaimer: str
    data_status: str                   # live / offline_sample / fallback / unavailable
    fallback_reason: str | None        # 降级或不可用时的原因

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "industry_name": self.industry_name,
            "source": self.source,
            "updated_at": self.updated_at,
            "prosperity_signal": self.prosperity_signal,
            "prosperity_note": self.prosperity_note,
            "top_stocks": self.top_stocks,
            "fund_flow_3d_cny": self.fund_flow_3d_cny,
            "disclaimer": self.disclaimer,
            "data_status": self.data_status,
            "fallback_reason": self.fallback_reason,
        }

    def to_markdown(self) -> str:
        signal_map = {
            "booming": "🟢 景气（资金净流入）",
            "normal": "🟡 中性",
            "sluggish": "🔴 低迷（资金净流出）",
            "unknown": "⚪ 无数据",
        }
        ff_str = (
            f"{self.fund_flow_3d_cny / 1e8:.2f} 亿"
            if self.fund_flow_3d_cny is not None
            else "N/A"
        )
        tops = "\n".join(
            f"  {r['rank']}. {r.get('name', r.get('code', ''))} "
            f"({r.get('change_pct', 'N/A')}%)"
            for r in self.top_stocks[:5]
        )
        return "\n".join([
            f"### {self.industry_name} 行业分析（{self.symbol}）",
            f"- 数据来源：{self.source}，更新时间：{self.updated_at}",
            "",
            "**行业景气度**",
            f"- 信号：{signal_map.get(self.prosperity_signal, '未知')}",
            f"- 3日资金净流入：{ff_str}",
            f"- 判断依据：{self.prosperity_note}",
            "",
            "**行业龙头（涨幅排序）**",
            tops or "  数据不可用",
            "",
            f"> ⚠️ {self.disclaimer}",
        ])


def _prosperity_from_fund_flow(fund_flow_cny: float | None) -> tuple[str, str]:
    if fund_flow_cny is None:
        return "unknown", "资金流向数据不可用"
    if fund_flow_cny > 5e8:      # 净流入 > 5亿
        return "booming", f"3日资金净流入 {fund_flow_cny/1e8:.1f} 亿，行业景气"
    if fund_flow_cny < -5e8:     # 净流出 > 5亿
        return "sluggish", f"3日资金净流出 {abs(fund_flow_cny)/1e8:.1f} 亿，行业低迷"
    return "normal", f"3日资金净流入 {fund_flow_cny/1e8:.1f} 亿，行业中性"


def _guess_industry(symbol: str) -> str:
    prefix = symbol[:2] if len(symbol) >= 2 else symbol
    return _CODE_INDUSTRY_MAP.get(prefix, "未知行业")


def analyze_industry(symbol: str, force_offline: bool = False) -> IndustryResult:
    """对指定股票所在行业进行景气度分析。

    Parameters
    ----------
    force_offline : bool
        True 时跳过 AkShare 调用，直接返回确定性离线样例数据（零网络请求）。
    """
    from agent_platform.finance.constants import DISCLAIMER
    from agent_platform.finance.offline_sample_data import get_sample_industry

    updated_at = datetime.utcnow().isoformat() + "Z"
    industry_name = _guess_industry(symbol)
    fund_flow: float | None = None
    top_stocks: list[dict[str, Any]] = []
    source = "内置样例数据（offline_sample_data.py）"
    data_status = "offline_sample"
    fallback_reason: str | None = None

    # 取数统一走 MCP 工具层（registry），不再在本模块内联 import akshare。
    # 这样离线模式由注册表硬阻断网络工具，且每次调用都进审计日志。
    from agent_platform.mcp.registry import get_registry

    reg = get_registry(offline=force_offline)

    if force_offline:
        # 离线模式：经 MCP 离线工具取确定性样例（requires_network=False，不会被阻断）
        env = reg.call("get_offline_industry", symbol=symbol)
        if env["ok"]:
            sample = env["data"]
            source = "内置样例数据（MCP:get_offline_industry）"
        else:
            # 离线工具都失败属异常情况，退回直读样例并说明原因
            sample = get_sample_industry(symbol)
            source = "内置样例数据（offline_sample_data.py 直读）"
            fallback_reason = f"MCP 离线工具失败: {env['error_type']}"
        industry_name = sample["industry_name"]
        fund_flow = sample["fund_flow_3d_cny"]
        top_stocks = sample["top_stocks"]
        signal = sample["prosperity_signal"]
        note = sample["prosperity_note"]
    else:
        # auto 模式：经 MCP 调真实数据源；任一环节失败都不编造，降级并标注原因
        akshare_success = False
        industry_identity_success = False
        industry_source = ""
        failures: list[str] = []

        # 1. 个股所属行业
        ind_env = reg.call("get_stock_industry", symbol=symbol)
        if ind_env["ok"]:
            industry_name = str(ind_env["data"]["industry"] or industry_name)
            industry_identity_success = True
            industry_source = str(ind_env.get("source") or "MCP:get_stock_industry")
        else:
            failures.append(f"行业识别({ind_env['error_type']})")

        # 2. 行业资金流排名 → 匹配本股所属行业
        flow_env = reg.call("get_sector_fund_flow", indicator="今日")
        if flow_env["ok"]:
            keywords = [k for k in industry_name.split("/") if k]
            for row in flow_env["data"]["records"]:
                sector = str(row.get("名称", ""))
                if not any(kw in sector for kw in keywords):
                    continue
                for key in ("今日主力净流入-净额", "今日净流入", "主力净流入-净额"):
                    raw = row.get(key)
                    if raw is None:
                        continue
                    try:
                        # 上游为万元口径，换算为元
                        fund_flow = float(str(raw).replace(",", "")) * 1e4
                        break
                    except (TypeError, ValueError):
                        continue

                # 3. 该行业板块龙头排序
                spot_env = reg.call("get_industry_spot", sector=sector)
                if spot_env["ok"]:
                    for i, r in enumerate(spot_env["data"]["records"][:5], start=1):
                        top_stocks.append({
                            "rank": i,
                            "code": r.get("代码", ""),
                            "name": r.get("名称", ""),
                            "change_pct": r.get("涨跌幅"),
                        })
                else:
                    failures.append(f"龙头排序({spot_env['error_type']})")

                source = "AkShare - 行业资金流向（MCP:get_sector_fund_flow）"
                data_status = "live"
                akshare_success = True
                break
            if not akshare_success:
                failures.append(f"资金流未匹配到行业 {industry_name}")
        else:
            failures.append(f"行业资金流({flow_env['error_type']})")

        # 已取得真实行业身份时，即使资金流不可用也保留真实结果；缺失字段明确留空。
        # 只有行业身份本身也失败时才降级样例，避免一项上游故障覆盖全部真实证据。
        if not akshare_success:
            fallback_reason = "；".join(failures) or "行业资金流不可用"
            if industry_identity_success:
                logger.warning("[IndustryAgent] 行业身份可用，附加数据缺失: %s", failures)
                source = f"MCP:get_stock_industry ← {industry_source}"
                data_status = "live"
                fund_flow = None
                top_stocks = []
            else:
                logger.warning("[IndustryAgent] MCP 行业数据不可用，降级样例: %s", failures)
                sample = get_sample_industry(symbol)
                industry_name = sample["industry_name"]
                fund_flow = sample["fund_flow_3d_cny"]
                top_stocks = sample["top_stocks"]
                source = "降级样例数据（MCP 行业数据不可用）"
                data_status = "fallback"

        # 在线模式使用动态景气度判断
        signal, note = _prosperity_from_fund_flow(fund_flow)

    return IndustryResult(
        symbol=symbol,
        industry_name=industry_name,
        source=source,
        updated_at=updated_at,
        prosperity_signal=signal,
        prosperity_note=note,
        top_stocks=top_stocks,
        fund_flow_3d_cny=fund_flow,
        disclaimer=DISCLAIMER,
        data_status=data_status,
        fallback_reason=fallback_reason,
    )
