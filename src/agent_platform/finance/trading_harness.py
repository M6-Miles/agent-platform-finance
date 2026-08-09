"""
交易 Harness（TradingHarness）
================================
整合 Pre-Flight Checklist：在执行交易信号前进行 6 项关键检查。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PreFlightCheckResult:
    """单项检查结果。"""
    check_name: str
    passed: bool
    message: str


@dataclass
class TradingHarnessResult:
    """交易 Harness 最终输出。"""
    symbol: str
    approved: bool                        # 是否通过所有 Pre-Flight 检查
    checks: list[PreFlightCheckResult]    # 各项检查明细
    final_action: str                     # "execute" / "block" / "manual_review"
    trader_result: dict[str, Any]         # TraderResult.to_dict()
    risk_result: dict[str, Any]           # RiskManagerResult.to_dict()
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "approved": self.approved,
            "checks": [
                {"check_name": c.check_name, "passed": c.passed, "message": c.message}
                for c in self.checks
            ],
            "final_action": self.final_action,
            "trader_result": self.trader_result,
            "risk_result": self.risk_result,
            "timestamp": self.timestamp,
        }

    def to_markdown(self) -> str:
        check_lines = []
        for c in self.checks:
            emoji = "✅" if c.passed else "❌"
            check_lines.append(f"  {emoji} {c.check_name}: {c.message}")
        checks_text = "\n".join(check_lines)
        action_emoji = {"execute": "🟢", "block": "🔴", "manual_review": "🟡"}
        return "\n".join([
            f"### 交易 Harness Pre-Flight Checklist — {self.symbol}",
            f"- 时间：{self.timestamp}",
            f"- 最终决策：{action_emoji.get(self.final_action, '?')} {self.final_action.upper()}",
            "",
            "**检查项明细**",
            checks_text,
            "",
            f"**批准状态**：{'通过' if self.approved else '未通过'}",
        ])


class TradingHarness:
    """
    Pre-Flight Checklist for Trading Signals.
    6 项检查：
      1. 数据溯源（source/updated_at 完整性）
      2. 违禁词拦截（KeywordBlocker 规则）
      3. 仓位合规（≤ RiskManagerResult.approved_position_pct）
      4. Schema 有效性（TRADER_SCHEMA 校验）
      5. 置信度阈值（SynthesisResult.confidence 满足最低要求）
      6. 回撤保护（RiskManagerResult.final_signal 非 "reduce"）
    """

    _BLOCKED_KEYWORDS = [
        "绝对稳赚", "100%收益", "稳赚不赔", "零风险", "必涨", "必赢", "稳定盈利"
    ]

    def __init__(
        self,
        min_confidence: float = 0.5,
        enable_keyword_check: bool = True,
    ):
        self.min_confidence = min_confidence
        self.enable_keyword_check = enable_keyword_check

    def run_preflight(
        self,
        synthesis_result: dict[str, Any],
        trader_result: dict[str, Any],
        risk_result: dict[str, Any],
    ) -> TradingHarnessResult:
        """执行 Pre-Flight Checklist，返回最终批准结果。"""
        symbol = trader_result.get("symbol", "UNKNOWN")
        checks: list[PreFlightCheckResult] = []

        # 1. 数据溯源
        source_ok = self._check_source_attribution(trader_result, risk_result)
        checks.append(source_ok)

        # 2. 违禁词
        keyword_ok = self._check_keywords(trader_result) if self.enable_keyword_check else PreFlightCheckResult("违禁词拦截", True, "已跳过")
        checks.append(keyword_ok)

        # 3. 仓位合规
        position_ok = self._check_position(trader_result, risk_result)
        checks.append(position_ok)

        # 4. Schema 校验
        schema_ok = self._check_schema(trader_result)
        checks.append(schema_ok)

        # 5. 置信度阈值
        confidence_ok = self._check_confidence(synthesis_result)
        checks.append(confidence_ok)

        # 6. 回撤保护
        drawdown_ok = self._check_drawdown_protection(risk_result)
        checks.append(drawdown_ok)

        # 综合判断
        approved = all(c.passed for c in checks)
        if approved:
            final_action = "execute"
        elif not drawdown_ok.passed:
            final_action = "block"
        else:
            final_action = "manual_review"

        return TradingHarnessResult(
            symbol=symbol,
            approved=approved,
            checks=checks,
            final_action=final_action,
            trader_result=trader_result,
            risk_result=risk_result,
            timestamp=datetime.utcnow().isoformat() + "Z",
        )

    def _check_source_attribution(self, trader: dict, risk: dict) -> PreFlightCheckResult:
        trader_src = trader.get("source")
        trader_ts = trader.get("updated_at")
        risk_src = risk.get("source")
        risk_ts = risk.get("updated_at")
        if all([trader_src, trader_ts, risk_src, risk_ts]):
            return PreFlightCheckResult("数据溯源", True, "所有输出包含 source 和 updated_at")
        return PreFlightCheckResult("数据溯源", False, "缺少必需的 source 或 updated_at 字段")

    def _check_keywords(self, trader: dict) -> PreFlightCheckResult:
        text = str(trader.get("rationale", "")) + str(trader.get("disclaimer", ""))
        for kw in self._BLOCKED_KEYWORDS:
            if kw in text:
                return PreFlightCheckResult("违禁词拦截", False, f"触发违禁词: {kw}")
        return PreFlightCheckResult("违禁词拦截", True, "无违禁词")

    def _check_position(self, trader: dict, risk: dict) -> PreFlightCheckResult:
        suggested = trader.get("position_pct_suggestion", 0.0)
        approved = risk.get("approved_position_pct", 0.0)
        if suggested <= approved:
            return PreFlightCheckResult("仓位合规", True, f"建议仓位 {suggested:.1f}% ≤ 风控批准 {approved:.1f}%")
        return PreFlightCheckResult("仓位合规", False, f"建议仓位 {suggested:.1f}% > 风控批准 {approved:.1f}%（超限）")

    def _check_schema(self, trader: dict) -> PreFlightCheckResult:
        from agent_platform.finance.trader_agent import TRADER_SCHEMA
        required = TRADER_SCHEMA.get("required", [])
        missing = [k for k in required if k not in trader]
        if not missing:
            return PreFlightCheckResult("Schema 有效性", True, "所有必填字段完整")
        return PreFlightCheckResult("Schema 有效性", False, f"缺少字段: {', '.join(missing)}")

    def _check_confidence(self, synthesis: dict) -> PreFlightCheckResult:
        conf = synthesis.get("confidence", 0.0)
        if conf >= self.min_confidence:
            return PreFlightCheckResult("置信度阈值", True, f"置信度 {conf:.0%} ≥ 阈值 {self.min_confidence:.0%}")
        return PreFlightCheckResult("置信度阈值", False, f"置信度 {conf:.0%} < 阈值 {self.min_confidence:.0%}")

    def _check_drawdown_protection(self, risk: dict) -> PreFlightCheckResult:
        final_sig = risk.get("final_signal", "hold")
        if final_sig != "reduce":
            return PreFlightCheckResult("回撤保护", True, f"风控信号={final_sig}（未触发减仓）")
        return PreFlightCheckResult("回撤保护", False, "触发回撤保护，信号=reduce，禁止新增仓位")
