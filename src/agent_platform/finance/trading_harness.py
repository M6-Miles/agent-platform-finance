"""
交易 Harness（TradingHarness）
================================
整合 Pre-Flight Checklist：在执行交易信号前进行 6 项关键检查。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

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
    data_quality_summary: dict[str, Any]

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
            "data_quality_summary": self.data_quality_summary,
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
    11 项检查：
      1. 数据质量决策（data_status 离线/fallback 约束）
      2. 数据溯源（source/updated_at 完整性）
      3. 违禁词拦截（KeywordBlocker 规则）
      4. 仓位合规（≤ RiskManagerResult.approved_position_pct）
      5. Schema 有效性（TRADER_SCHEMA 校验）
      6. 置信度阈值（SynthesisResult.confidence 满足最低要求）
      7. 回撤保护（RiskManagerResult.final_signal 非 "reduce"）
      8. 交易时段（正式执行路径必须处于 A 股连续竞价时段）
      9. 流动性（最新日成交额代理值满足最低阈值）
      10. 独立 Evaluator（低于 80 分进入人工复核）
      11. 止盈止损关系与风险收益比
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
        technical_analysis: dict[str, Any] | None = None,
        fundamental_analysis: dict[str, Any] | None = None,
        industry_analysis: dict[str, Any] | None = None,
        market_regime: dict[str, Any] | None = None,
        evaluator_summary: dict[str, Any] | None = None,
        execution_context: dict[str, Any] | None = None,
    ) -> TradingHarnessResult:
        """执行 Pre-Flight Checklist，返回最终批准结果。"""
        symbol = trader_result.get("symbol", "UNKNOWN")
        checks: list[PreFlightCheckResult] = []

        # 1. 数据质量决策
        data_quality_summary = self._summarize_data_quality(
            technical_analysis, fundamental_analysis, industry_analysis, market_regime
        )
        data_quality_ok = self._check_data_quality(data_quality_summary)
        checks.append(data_quality_ok)

        # 2. 数据溯源
        source_ok = self._check_source_attribution(trader_result, risk_result)
        checks.append(source_ok)

        # 3. 违禁词
        keyword_ok = self._check_keywords(trader_result) if self.enable_keyword_check else PreFlightCheckResult("违禁词拦截", True, "已跳过")
        checks.append(keyword_ok)

        # 4. 仓位合规
        position_ok = self._check_position(trader_result, risk_result)
        checks.append(position_ok)
        protection_ok = self._check_protective_prices(trader_result, risk_result)
        checks.append(protection_ok)

        # 5. Schema 校验
        schema_ok = self._check_schema(trader_result)
        checks.append(schema_ok)

        # 6. 置信度阈值
        confidence_ok = self._check_confidence(synthesis_result)
        checks.append(confidence_ok)

        # 7. 回撤保护
        drawdown_ok = self._check_drawdown_protection(risk_result)
        checks.append(drawdown_ok)

        # 8-9. 只有正式编排路径传入执行上下文。旧 SDK 三参数调用保持兼容，
        # 但检查明细会明确标记为未评估，避免伪装成已完成校验。
        trading_hours_ok = self._check_trading_hours(execution_context)
        checks.append(trading_hours_ok)
        liquidity_ok = self._check_liquidity(execution_context)
        checks.append(liquidity_ok)
        evaluator_ok = self._check_evaluator(evaluator_summary)
        checks.append(evaluator_ok)

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
            data_quality_summary=data_quality_summary,
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

    @staticmethod
    def _check_protective_prices(trader: dict, risk: dict) -> PreFlightCheckResult:
        if trader.get("signal") != "buy":
            return PreFlightCheckResult("止盈止损", True, "非买入信号，无需新增保护价")
        try:
            entry = float(trader.get("entry_price"))
            stop = float(risk.get("stop_loss_price"))
            take = float(risk.get("take_profit_price"))
            ratio = float(risk.get("risk_reward_ratio"))
        except (TypeError, ValueError):
            return PreFlightCheckResult(
                "止盈止损", False, "入场价、止损价、止盈价或风险收益比缺失"
            )
        if not (0 < stop < entry < take):
            return PreFlightCheckResult(
                "止盈止损", False,
                f"保护价关系无效：要求 止损({stop:.2f}) < 入场({entry:.2f}) < 止盈({take:.2f})",
            )
        if ratio < 1.5:
            return PreFlightCheckResult(
                "止盈止损", False, f"风险收益比 {ratio:.2f}:1 低于 1.50:1"
            )
        return PreFlightCheckResult(
            "止盈止损", True,
            f"止损 {stop:.2f} / 止盈 {take:.2f} / 风险收益比 {ratio:.2f}:1",
        )

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

    @staticmethod
    def _check_evaluator(summary: dict[str, Any] | None) -> PreFlightCheckResult:
        if summary is None:
            return PreFlightCheckResult(
                "独立质量评估", True, "兼容调用未提供 Evaluator 结果，本项未评估"
            )
        score = summary.get("minimum_score")
        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            return PreFlightCheckResult(
                "独立质量评估", False, "Evaluator 最低分缺失或无效，需人工复核"
            )
        if not summary.get("requires_manual_review") and numeric_score >= 80.0:
            return PreFlightCheckResult(
                "独立质量评估", True, f"三类输出最低评分 {numeric_score:.1f}/100"
            )
        return PreFlightCheckResult(
            "独立质量评估", False,
            f"三类输出最低评分 {numeric_score:.1f}/100，低于 80 分，需人工复核",
        )

    @staticmethod
    def _check_trading_hours(context: dict[str, Any] | None) -> PreFlightCheckResult:
        if context is None:
            return PreFlightCheckResult("交易时段", True, "兼容调用未提供执行上下文，本项未评估")
        raw = context.get("as_of")
        try:
            current = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if current.tzinfo is None:
                current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            current = current.astimezone(ZoneInfo("Asia/Shanghai"))
        except (TypeError, ValueError):
            return PreFlightCheckResult("交易时段", False, "执行时间缺失或格式无效，需人工复核")
        local_time = current.time().replace(tzinfo=None)
        in_session = current.weekday() < 5 and (
            time(9, 30) <= local_time <= time(11, 30)
            or time(13, 0) <= local_time <= time(15, 0)
        )
        if in_session:
            return PreFlightCheckResult("交易时段", True, f"上海时间 {current:%Y-%m-%d %H:%M:%S} 位于交易时段")
        return PreFlightCheckResult("交易时段", False, f"上海时间 {current:%Y-%m-%d %H:%M:%S} 不在交易时段，需人工复核")

    @staticmethod
    def _check_liquidity(context: dict[str, Any] | None) -> PreFlightCheckResult:
        if context is None:
            return PreFlightCheckResult("流动性", True, "兼容调用未提供执行上下文，本项未评估")
        try:
            volume = float(context.get("latest_volume"))
            close = float(context.get("latest_close"))
        except (TypeError, ValueError):
            return PreFlightCheckResult("流动性", False, "缺少最新成交量或收盘价，需人工复核")
        minimum = float(context.get("min_daily_turnover", 5_000_000.0))
        turnover = volume * close
        if volume <= 0 or close <= 0:
            return PreFlightCheckResult("流动性", False, "成交量或价格非正数，需人工复核")
        if turnover >= minimum:
            return PreFlightCheckResult("流动性", True, f"估算日成交额 {turnover:,.0f} 元 ≥ {minimum:,.0f} 元")
        return PreFlightCheckResult("流动性", False, f"估算日成交额 {turnover:,.0f} 元 < {minimum:,.0f} 元，需人工复核")

    def _summarize_data_quality(
        self,
        technical: dict[str, Any] | None,
        fundamental: dict[str, Any] | None,
        industry: dict[str, Any] | None,
        regime: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """汇总四路 Agent 数据质量，供决策与 API 共同使用。"""
        agents = {
            "technical": technical,
            "fundamental": fundamental,
            "industry": industry,
            "market_regime": regime,
        }
        statuses = {
            name: str(result.get("data_status", "unavailable"))
            for name, result in agents.items()
            if result is not None
        }
        counts = {
            status: sum(1 for value in statuses.values() if value == status)
            for status in ("live", "offline_sample", "fallback", "stale", "unavailable")
        }
        total = len(statuses)
        reasons: list[str] = []
        if total == 0:
            return {
                "total": 0,
                "counts": counts,
                "agent_statuses": {},
                "passed": True,
                "reasons": ["调用方未提供专业 Agent 数据，跳过数据质量检查"],
                "evaluated": False,
            }
        if counts["offline_sample"] == total:
            reasons.append("全部专业 Agent 使用离线样例数据，仅供研究演示")
        if counts["offline_sample"] and counts["live"]:
            reasons.append("专业 Agent 数据存在在线与离线混用")
        if counts["fallback"] >= total * 0.5:
            reasons.append("至少一半专业 Agent 使用降级数据")
        if counts["stale"] or counts["unavailable"]:
            reasons.append("存在过期或不可用的专业 Agent 数据")
        passed = counts["live"] >= total * 0.75 and not reasons
        if not passed and not reasons:
            reasons.append("真实数据覆盖率不足 75%")
        return {
            "total": total,
            "counts": counts,
            "agent_statuses": statuses,
            "passed": passed,
            "reasons": reasons,
            "evaluated": True,
        }

    def _check_data_quality(
        self,
        summary: dict[str, Any],
    ) -> PreFlightCheckResult:
        """数据质量决策约束。

        规则：
        - 全部 offline_sample：允许完成研究，但 final_action 必须 manual_review
        - 混合模式（offline + live）或高比例 fallback：manual_review
        - 全部 live 且无 fallback：通过，可 execute
        """
        counts = summary["counts"]
        offline_count = counts["offline_sample"]
        fallback_count = counts["fallback"]
        live_count = counts["live"]
        total = summary["total"]
        if not summary.get("evaluated", True):
            return PreFlightCheckResult("数据质量决策", True, summary["reasons"][0])

        # 全离线样例：允许研究演示，但不可实盘执行
        if offline_count == total:
            return PreFlightCheckResult(
                "数据质量决策",
                False,
                f"全部使用离线样例数据（{total}/{total}），仅供演示，不可实盘执行"
            )

        # 混合模式或高比例降级
        if offline_count > 0 or fallback_count >= total * 0.5:
            details = f"offline={offline_count}, fallback={fallback_count}, live={live_count}"
            return PreFlightCheckResult(
                "数据质量决策",
                False,
                f"数据质量不足以自动执行（{details}），需人工复核"
            )

        # 真实数据且降级比例可接受
        if summary["passed"]:
            return PreFlightCheckResult(
                "数据质量决策",
                True,
                f"数据质量良好（live={live_count}/{total}），可自动执行"
            )

        # 其他情况保守处理
        return PreFlightCheckResult(
            "数据质量决策",
            False,
            f"数据质量不确定（live={live_count}, fallback={fallback_count}），建议人工复核"
        )
