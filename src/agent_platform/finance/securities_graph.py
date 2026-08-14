"""
证券分析主工作流 — LangGraph 实现
=====================================
证券分析主流程使用 LangGraph StateGraph 编排。

工作流结构：
  START
    ├─ technical_agent     ┐
    ├─ fundamental_agent   │ 并行
    ├─ industry_agent      │
    └─ market_regime_agent ┘
          ↓ 四路汇合
    synthesis_agent
          ↓ 条件路由
    低置信度(≤0.3) → END (no_trade)
    可交易       → trader_agent
                      ↓
              [HumanApprovalRequired] → human_approval (interrupt)
              [OK]                    → risk_manager_agent
                                            ↓
                                    trading_harness
                                            ↓ 条件路由
                            execute       → END
                            manual_review → human_approval (interrupt)
                            block         → END

Checkpoint：默认 MemorySaver；生产可注入 SqliteSaver。
人工审批：interrupt() 暂停图执行，通过 Command(resume=...) 恢复。
异常策略：
  - 数据源/网络错误（Exception 通用） → 写入 errors 字段，继续图执行
  - 编程错误（TypeError/AttributeError/NameError） → 直接 reraise，暴露 bug
  - interrupt()（GraphInterrupt）→ 必须在 try/except 之外调用，否则被捕获导致中断失效
"""
from __future__ import annotations

import logging
import operator
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 1. 状态模型
# ──────────────────────────────────────────────────────────────────────────────

from typing import TypedDict


class SecuritiesAnalysisState(TypedDict, total=False):
    """证券分析工作流完整状态。

    全部字段均为可选（total=False），节点只需返回局部更新。
    errors / trace_entries 使用 Annotated + operator.add，允许多节点追加。
    """
    # ── 输入 ──────────────────────────────────────────────
    symbol: str
    request_id: str     # 内部字段，LangGraph state 保留
    run_id: str         # 对外暴露字段（与 request_id 相同值）

    # ── 运行模式 ──────────────────────────────────────────
    # requested_data_mode: 用户请求的原始模式（"auto" / "offline"）
    # data_mode: 有效模式（effective data mode）
    #   - requested=offline → effective=offline（显式离线）
    #   - requested=auto + 样例代码 → effective=offline（自动路由）
    #   - requested=auto + 真实代码 → effective=auto（保持联网）
    requested_data_mode: str
    data_mode: str

    # ── 各 Agent 输出（并行四路）──────────────────────────
    technical_analysis: Optional[dict]
    fundamental_analysis: Optional[dict]
    industry_analysis: Optional[dict]
    market_regime: Optional[dict]

    # ── 综合研判 ──────────────────────────────────────────
    synthesis: Optional[dict]
    confidence: float          # 0.0–1.0，来自 synthesis

    # ── 交易链 ───────────────────────────────────────────
    trade_signal: Optional[dict]       # TraderResult.to_dict()
    har_required: bool                 # True = HumanApprovalRequired 触发
    har_detail: Optional[str]          # 触发时的异常消息

    # ── 风控 & Harness ────────────────────────────────────
    risk_result: Optional[dict]        # RiskManagerResult.to_dict()
    evaluator_summary: Optional[dict]  # Synthesis/Trader/Risk 独立质量评分
    preflight_result: Optional[dict]   # TradingHarnessResult.to_dict()

    # ── 最终状态 ──────────────────────────────────────────
    status: str
    final_action: Optional[str]        # execute / manual_review / block

    # ── 累积字段（多节点可安全追加）──────────────────────
    errors: Annotated[list[str], operator.add]

    # ── 上下文（legacy，供 application_service 向后兼容）──
    trace: dict

    # ── 逐节点计时（每个节点 append 自己的耗时记录）────────
    trace_entries: Annotated[list[dict], operator.add]
    specialist_audit: Annotated[list[dict], operator.add]

    # ── 信息 MCP 证据（五类工具摘要，synthesis 节点写入）──
    information_evidence: Optional[list[dict]]   # 每类工具的摘要记录
    information_trace: Optional[dict]            # 汇总统计
    information_limitations: Optional[list[str]] # 不可用工具的限制说明


# ──────────────────────────────────────────────────────────────────────────────
# 2. 内部辅助
# ──────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ── 信息 MCP 五类工具调用配置 ──────────────────────────────────────────────────
_INFO_TOOL_CALLS: list[tuple[str, dict]] = [
    ("get_financial_news",          {"limit": 5}),
    ("get_stock_announcements",     {"limit": 3}),
    ("get_research_report_summary", {"limit": 3}),
    ("get_macro_policy",            {"indicator": "money_supply", "limit": 3}),
    ("get_interest_rates",          {"rate_type": "lpr", "limit": 3}),
]


def _summarize_info_env(tool: str, env: dict) -> dict:
    """将工具信封裁剪为只含摘要字段的轻量记录，不把全文塞进 state。"""
    is_blocked = env.get("error_type") == "OfflineModeBlocked"
    ok = bool(env.get("ok"))
    rec: dict = {
        "tool": tool,
        "ok": ok,
        "source": env.get("source", ""),
        "updated_at": env.get("updated_at", ""),
        "data_status": (
            "ok" if ok else
            ("offline_blocked" if is_blocked else env.get("data_status", "unavailable"))
        ),
        "record_count": (env.get("data") or {}).get("rows", 0) if ok else 0,
    }
    if not ok:
        if is_blocked:
            rec["error_type"] = "OfflineModeBlocked"
        else:
            rec["fallback_reason"] = env.get("fallback_reason") or env.get("error", "")
    return rec


def _collect_information_evidence(
    state: "SecuritiesAnalysisState",
) -> tuple[list[dict], dict, list[str]]:
    """通过 Registry 调用五类信息工具，返回 (evidence, trace, limitations)。

    离线模式：Registry 硬阻断 → data_status=offline_blocked，核心链路不中断。
    在线失败：返回 unavailable 记录，不阻断核心链路，不生成任何事实性内容。
    """
    from agent_platform.mcp.registry import build_default_registry

    symbol = state.get("symbol", "")
    offline = state.get("data_mode") == "offline"
    reg = build_default_registry(offline=offline)

    def call_one(item: tuple[str, dict]) -> dict:
        tool_name, base_kwargs = item
        kwargs = dict(base_kwargs)
        # 个股工具需要 symbol；宏观工具（macro_policy/interest_rates）不需要
        if tool_name in ("get_financial_news", "get_stock_announcements", "get_research_report_summary"):
            kwargs["symbol"] = symbol
        try:
            env = reg.call(tool_name, **kwargs)
            return _summarize_info_env(tool_name, env)
        except Exception as exc:
            return {
                "tool": tool_name, "ok": False, "source": "",
                "updated_at": "", "data_status": "unavailable",
                "record_count": 0, "fallback_reason": str(exc),
            }

    # 五类信息工具彼此独立。保留配置顺序以保证输出稳定，但并发发起调用，
    # 避免新闻接口的慢响应阻塞公告、研报、宏观和利率数据。
    with ThreadPoolExecutor(max_workers=len(_INFO_TOOL_CALLS), thread_name_prefix="info-mcp") as executor:
        evidence = list(executor.map(call_one, _INFO_TOOL_CALLS))

    limitations: list[str] = []
    for rec in evidence:
        if not rec["ok"]:
            limitations.append(
                f"{rec['tool']}: {rec.get('fallback_reason') or rec.get('error_type', 'unavailable')}"
            )

    ok_count = sum(1 for r in evidence if r["ok"])
    trace = {
        "total": len(evidence),
        "ok": ok_count,
        "unavailable": len(evidence) - ok_count,
        "offline": offline,
    }
    return evidence, trace, limitations


def _te(node: str, t0: float, status: str = "ok", error: str = "") -> list[dict]:
    """构造 trace_entries 追加项。使用 perf_counter 确保精度。"""
    entry: dict = {
        "node": node,
        "duration_s": time.perf_counter() - t0,
        "status": status,
    }
    if error:
        entry["error"] = error
    return [entry]


def _run_specialist(
    *, name: str, schema: dict, analyzer, state: SecuritiesAnalysisState,
    technical_cross_validation: bool = False,
) -> tuple[dict, dict]:
    """通过统一 AgentLoop + AgentHarness 运行一个 Specialist。"""
    from agent_platform.finance.specialist_runtime import SpecialistRuntime

    runtime = SpecialistRuntime(
        name=name,
        schema=schema,
        analyzer=analyzer,
        technical_cross_validation=technical_cross_validation,
    )
    output = runtime.run({
        "symbol": state.get("symbol"),
        "data_mode": state.get("data_mode"),
        "session_id": f"{state.get('request_id', 'request')}:{name}",
    })
    return output, runtime.last_audit


# ──────────────────────────────────────────────────────────────────────────────
# 3. 节点函数
# ──────────────────────────────────────────────────────────────────────────────

def node_technical_agent(state: SecuritiesAnalysisState) -> dict:
    """技术分析节点（并行层）。

    异常策略
    --------
    - TypeError / AttributeError / NameError → reraise（编程错误，不应吞掉）
    - 其余 Exception → 写入 errors，返回 None，下游可感知
    """
    from agent_platform.finance.analysis import analyze_security
    from agent_platform.finance.specialist_runtime import TECHNICAL_SCHEMA
    symbol = state["symbol"]
    offline = state.get("data_mode") == "offline"
    t0 = time.perf_counter()
    try:
        def analyze() -> dict:
            from agent_platform.finance.mcp_market_data_provider import MCPMarketDataProvider

            result = analyze_security(
                symbol, provider=MCPMarketDataProvider(offline=offline),
            )
            d = result.to_dict()
            d["_markdown"] = result.to_markdown()
            return d

        d, audit = _run_specialist(
            name="technical_agent", schema=TECHNICAL_SCHEMA, analyzer=analyze,
            state=state, technical_cross_validation=True,
        )
        return {
            "technical_analysis": d,
            "specialist_audit": [audit],
            "trace_entries": _te("technical_agent", t0),
        }
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:
        logger.error("[technical_agent] %s: %s", symbol, exc)
        return {
            "technical_analysis": None,
            "errors": [f"technical_agent: {exc}"],
            "trace_entries": _te("technical_agent", t0, "error", str(exc)),
        }


def node_fundamental_agent(state: SecuritiesAnalysisState) -> dict:
    """基本面分析节点（并行层）。"""
    from agent_platform.finance.fundamental_agent import FUNDAMENTAL_SCHEMA, analyze_fundamental
    symbol = state["symbol"]
    offline = state.get("data_mode") == "offline"
    t0 = time.perf_counter()
    try:
        def analyze() -> dict:
            result = analyze_fundamental(symbol, force_offline=offline)
            d = result.to_dict()
            d["_markdown"] = result.to_markdown()
            return d

        d, audit = _run_specialist(
            name="fundamental_agent", schema=FUNDAMENTAL_SCHEMA,
            analyzer=analyze, state=state,
        )
        return {
            "fundamental_analysis": d,
            "specialist_audit": [audit],
            "trace_entries": _te("fundamental_agent", t0),
        }
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:
        logger.error("[fundamental_agent] %s: %s", symbol, exc)
        return {
            "fundamental_analysis": None,
            "errors": [f"fundamental_agent: {exc}"],
            "trace_entries": _te("fundamental_agent", t0, "error", str(exc)),
        }


def node_industry_agent(state: SecuritiesAnalysisState) -> dict:
    """行业分析节点（并行层）。"""
    from agent_platform.finance.industry_agent import INDUSTRY_SCHEMA, analyze_industry
    symbol = state["symbol"]
    offline = state.get("data_mode") == "offline"
    t0 = time.perf_counter()
    try:
        def analyze() -> dict:
            result = analyze_industry(symbol, force_offline=offline)
            d = result.to_dict()
            d["_markdown"] = result.to_markdown()
            return d

        d, audit = _run_specialist(
            name="industry_agent", schema=INDUSTRY_SCHEMA,
            analyzer=analyze, state=state,
        )
        return {
            "industry_analysis": d,
            "specialist_audit": [audit],
            "trace_entries": _te("industry_agent", t0),
        }
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:
        logger.error("[industry_agent] %s: %s", symbol, exc)
        return {
            "industry_analysis": None,
            "errors": [f"industry_agent: {exc}"],
            "trace_entries": _te("industry_agent", t0, "error", str(exc)),
        }


def node_market_regime_agent(state: SecuritiesAnalysisState) -> dict:
    """大盘状态节点（并行层）。"""
    from agent_platform.finance.market_regime_agent import MARKET_REGIME_SCHEMA, analyze_market_regime
    offline = state.get("data_mode") == "offline"
    t0 = time.perf_counter()
    try:
        def analyze() -> dict:
            result = analyze_market_regime(force_offline=offline)
            d = result.to_dict()
            d["_markdown"] = result.to_markdown()
            return d

        d, audit = _run_specialist(
            name="market_regime_agent", schema=MARKET_REGIME_SCHEMA,
            analyzer=analyze, state=state,
        )
        return {
            "market_regime": d,
            "specialist_audit": [audit],
            "trace_entries": _te("market_regime_agent", t0),
        }
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:
        logger.error("[market_regime_agent]: %s", exc)
        return {
            "market_regime": None,
            "errors": [f"market_regime_agent: {exc}"],
            "trace_entries": _te("market_regime_agent", t0, "error", str(exc)),
        }


def node_synthesis_agent(state: SecuritiesAnalysisState) -> dict:
    """综合研判节点 — 必须等四路全部完成后才运行。"""
    from agent_platform.finance.synthesis_agent import synthesize
    symbol = state["symbol"]
    t0 = time.perf_counter()

    tech = state.get("technical_analysis")
    fund = state.get("fundamental_analysis")
    ind = state.get("industry_analysis")
    regime = state.get("market_regime")

    evidence, info_trace, limitations = _collect_information_evidence(state)

    # 必需字段校验：任一缺失则阻断
    missing = [
        name for name, val in [
            ("technical_analysis", tech),
            ("fundamental_analysis", fund),
            ("industry_analysis", ind),
            ("market_regime", regime),
        ] if val is None
    ]
    if missing:
        return {
            "synthesis": None,
            "confidence": 0.0,
            "status": "error",
            "information_evidence": evidence,
            "information_trace": info_trace,
            "information_limitations": limitations,
            "errors": [f"synthesis_agent: 缺少必要输入 {missing}，拒绝合成"],
            "trace_entries": _te("synthesis_agent", t0, "error", f"missing={missing}"),
        }

    try:
        # with_debate=True：主链启用两轮结构化多空辩论。
        # 辩论只读四路输入、不回写 signal/confidence，故本节点的
        # confidence 与状态流转与开启前完全一致；一致性检查与偏见检测的
        # 结论随 synthesis 字典透出（debate / debate_blocked / debate_warnings），
        # 不写入 errors —— errors 非空会被 resolve_research_status() 判为 failed。
        result = synthesize(
            symbol=symbol,
            technical=tech,
            fundamental=fund,
            industry=ind,
            regime=regime,
            with_debate=True,
        )
        d = result.to_dict()
        # 证据覆盖说明：不生成任何事实性内容，仅记录可用性与置信度影响
        info_ok_count = sum(1 for r in evidence if r.get("ok"))
        if info_ok_count == 0:
            d["information_coverage"] = "none"
            d["information_note"] = (
                "五类信息工具（新闻/公告/研报/宏观/利率）均不可用；"
                "本次研判仅基于量化/基本面/行业/大盘数据，不含定性信息来源，"
                "该限制未自动改变模型置信度，请谨慎参考。"
            )
        else:
            available = [r["tool"] for r in evidence if r.get("ok")]
            d["information_coverage"] = "partial"
            d["information_note"] = f"可用信息工具：{available}；其余工具不可用，置信度评估受限。"
        d["information_evidence"] = evidence
        d["information_limitations"] = limitations
        d["_markdown"] = result.to_markdown()
        return {
            "synthesis": d,
            "confidence": float(result.confidence),
            "status": "synthesis_done",
            "information_evidence": evidence,
            "information_trace": info_trace,
            "information_limitations": limitations,
            "trace_entries": _te("synthesis_agent", t0),
        }
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:
        logger.error("[synthesis_agent] %s: %s", symbol, exc)
        return {
            "synthesis": None,
            "confidence": 0.0,
            "status": "error",
            "errors": [f"synthesis_agent: {exc}"],
            "trace_entries": _te("synthesis_agent", t0, "error", str(exc)),
        }


def node_no_trade(state: SecuritiesAnalysisState) -> dict:
    """低置信度退出节点：置信度 ≤ _LOW_CONFIDENCE_THRESHOLD 时写入 status="no_trade"。

    不设置 final_action，resolve_research_status() 依赖 raw_status="no_trade" 判断。
    """
    return {"status": "no_trade"}


def node_debate_approval(state: SecuritiesAnalysisState) -> dict:
    """辩论一致性或偏见检查阻断后的人工澄清节点。"""
    synthesis = state.get("synthesis") or {}
    debate = synthesis.get("debate") or {}
    reasons = debate.get("blocking_reasons") or []
    t0 = time.perf_counter()
    decision = interrupt({
        "type": "debate_review",
        "symbol": state.get("symbol"),
        "reason": "辩论一致性/偏见检查要求人工澄清",
        "blocking_reasons": reasons,
        "synthesis": synthesis,
    })
    if decision == "approve":
        return {
            "status": "debate_approved_by_human",
            "trace_entries": _te("debate_approval", t0, "approved"),
        }
    return {
        "status": "debate_rejected_by_human",
        "final_action": "block",
        "trace_entries": _te("debate_approval", t0, "rejected"),
    }


def node_trader_agent(state: SecuritiesAnalysisState) -> dict:
    """交易信号节点。

    HumanApprovalRequired 时将已计算好的 TraderResult 存入 trade_signal，
    确保 risk_manager 在批准后能获得完整的交易建议。
    """
    from agent_platform.finance.trader_agent import (
        HumanApprovalRequired,
        generate_trade_signal,
    )
    syn = state.get("synthesis") or {}
    regime = state.get("market_regime") or {}
    tech = state.get("technical_analysis")
    t0 = time.perf_counter()
    try:
        result = generate_trade_signal(synthesis=syn, regime=regime, technical=tech)
        d = result.to_dict()
        d["_markdown"] = result.to_markdown()
        return {
            "trade_signal": d,
            "har_required": False,
            "status": "trading",
            "trace_entries": _te("trader_agent", t0),
        }
    except HumanApprovalRequired as exc:
        # 携带完整 TraderResult —— 批准后 risk_manager 直接取用，无需重算
        pending_signal: dict | None = None
        if exc.trader_result is not None:
            pending_signal = exc.trader_result.to_dict()
            pending_signal["_markdown"] = exc.trader_result.to_markdown()
        return {
            "trade_signal": pending_signal,   # 保存待批准的信号，不置 None
            "har_required": True,
            "har_detail": str(exc),
            "status": "pending_human_approval",
            "trace_entries": _te("trader_agent", t0, "har", str(exc)),
        }
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:
        logger.error("[trader_agent] %s: %s", state.get("symbol"), exc)
        return {
            "trade_signal": None,
            "har_required": False,
            "status": "error",
            "errors": [f"trader_agent: {exc}"],
            "trace_entries": _te("trader_agent", t0, "error", str(exc)),
        }


def node_risk_manager(state: SecuritiesAnalysisState) -> dict:
    """风控节点。

    trade_signal 为空时直接返回结构化错误，不静默忽略。
    """
    from agent_platform.finance.risk_manager_agent import assess_risk
    trader = state.get("trade_signal")
    t0 = time.perf_counter()

    # 防御：批准后 trade_signal 必须有值
    if not trader:
        return {
            "risk_result": None,
            "errors": ["risk_manager: trade_signal 为空，无法执行风控审核"],
            "status": "error",
            "trace_entries": _te("risk_manager", t0, "error", "trade_signal empty"),
        }

    try:
        result = assess_risk(trader_result=trader)
        d = result.to_dict()
        d["_markdown"] = result.to_markdown()
        return {
            "risk_result": d,
            "trace_entries": _te("risk_manager", t0),
        }
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:
        logger.error("[risk_manager] %s: %s", state.get("symbol"), exc)
        return {
            "risk_result": None,
            "errors": [f"risk_manager: {exc}"],
            "trace_entries": _te("risk_manager", t0, "error", str(exc)),
        }


def node_trading_harness(state: SecuritiesAnalysisState) -> dict:
    """Pre-Flight Checklist 节点。manual_review → interrupt() 暂停工作流。

    注意：interrupt() 必须在 try/except 之外调用。
    LangGraph 通过 GraphInterrupt（Exception 子类）实现中断信号，
    若包在 except Exception 内会被捕获，导致中断静默失效。
    """
    from agent_platform.finance.trading_harness import TradingHarness
    syn = state.get("synthesis") or {}
    trader = state.get("trade_signal") or {}
    risk = state.get("risk_result") or {}
    technical = state.get("technical_analysis") or {}
    harness = TradingHarness()
    t0 = time.perf_counter()

    try:
        result = harness.run_preflight(
            synthesis_result=syn,
            trader_result=trader,
            risk_result=risk,
            technical_analysis=technical,
            fundamental_analysis=state.get("fundamental_analysis"),
            industry_analysis=state.get("industry_analysis"),
            market_regime=state.get("market_regime"),
            evaluator_summary=state.get("evaluator_summary"),
            execution_context={
                "as_of": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "latest_volume": technical.get("latest_volume"),
                "latest_close": technical.get("latest_close"),
            },
        )
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:
        logger.error("[trading_harness] %s: %s", state.get("symbol"), exc)
        return {
            "preflight_result": None,
            "final_action": "block",
            "status": "error",
            "errors": [f"trading_harness: {exc}"],
            "trace_entries": _te("trading_harness", t0, "error", str(exc)),
        }

    pf = result.to_dict()
    action = result.final_action

    # interrupt() 在 try/except 块之外 —— GraphInterrupt 可以正常传播
    if action == "manual_review":
        decision = interrupt({
            "type": "manual_review",
            "symbol": state.get("symbol"),
            "reason": "preflight_manual_review",
            "preflight": pf,
        })
        if decision == "approve":
            return {
                "preflight_result": pf,
                "final_action": "execute",
                "status": "approved_by_human",
                "trace_entries": _te("trading_harness", t0, "approved"),
            }
        else:
            return {
                "preflight_result": pf,
                "final_action": "block",
                "status": "rejected_by_human",
                "trace_entries": _te("trading_harness", t0, "rejected"),
            }

    return {
        "preflight_result": pf,
        "final_action": action,
        "status": "preflight_done",
        "trace_entries": _te("trading_harness", t0),
    }


def node_evaluator_agent(state: SecuritiesAnalysisState) -> dict:
    """独立评估综合、交易和风控输出，并把低分交给 Pre-Flight 复核。"""
    from agent_platform.core.evaluator_agent import evaluate

    t0 = time.perf_counter()
    subjects = {
        "synthesis": state.get("synthesis"),
        "trader": state.get("trade_signal"),
        "risk_manager": state.get("risk_result"),
    }
    results: dict[str, dict] = {}
    issues: list[str] = []
    scores: list[float] = []

    for subject, output in subjects.items():
        if not output:
            issues.append(f"{subject}: 缺少待评估输出")
            scores.append(0.0)
            continue
        result = evaluate(subject, output)
        results[subject] = result.to_dict()
        scores.append(result.overall_score)
        issues.extend(f"{subject}: {issue}" for issue in result.issues)

    minimum = min(scores) if scores else 0.0
    average = sum(scores) / len(scores) if scores else 0.0
    summary = {
        "evaluations": results,
        "minimum_score": round(minimum, 1),
        "average_score": round(average, 1),
        "requires_manual_review": minimum < 80.0,
        "issues": issues,
        "source": "evaluator_agent",
        "updated_at": _now_iso(),
    }
    return {
        "evaluator_summary": summary,
        "trace_entries": _te("evaluator_agent", t0),
    }


def node_human_approval(state: SecuritiesAnalysisState) -> dict:
    """人工审批节点（HumanApprovalRequired 路径）。

    使用 interrupt() 暂停图；恢复时通过 Command(resume=...) 传入
    "approve" 或 "reject"。
    trade_signal 已由 node_trader_agent 存入状态，批准后 risk_manager 直接取用。
    """
    t0 = time.perf_counter()
    decision = interrupt({
        "type": "har_approval",
        "symbol": state.get("symbol"),
        "reason": state.get("har_detail", "仓位超过自动阈值"),
        "trade_signal": state.get("trade_signal"),
    })
    if decision == "approve":
        return {
            "status": "approved_by_human",
            "har_required": False,
            "trace_entries": _te("human_approval", t0, "approved"),
        }
    else:
        return {
            "status": "rejected_by_human",
            "final_action": "block",
            "har_required": False,
            "trade_signal": None,   # 拒绝后清除信号，risk_manager 不会运行
            "trace_entries": _te("human_approval", t0, "rejected"),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 4. 条件路由
# ──────────────────────────────────────────────────────────────────────────────

_LOW_CONFIDENCE_THRESHOLD = 0.3


def route_after_synthesis(state: SecuritiesAnalysisState) -> str:
    if state.get("status") == "error":
        return END
    confidence = state.get("confidence", 0.0)
    # 原始业务约束：低置信度永远 no_trade，不因辩论元数据再次打断用户。
    if confidence <= _LOW_CONFIDENCE_THRESHOLD:
        return "no_trade"
    synthesis = state.get("synthesis") or {}
    if synthesis.get("debate_blocked") is True:
        return "debate_approval"
    return "trader_agent"


def route_after_debate_approval(state: SecuritiesAnalysisState) -> str:
    if state.get("final_action") == "block":
        return END
    if state.get("confidence", 0.0) <= _LOW_CONFIDENCE_THRESHOLD:
        return "no_trade"
    return "trader_agent"


def route_after_trader(state: SecuritiesAnalysisState) -> str:
    if state.get("har_required", False):
        return "human_approval"
    if state.get("status") == "error":
        return END
    return "risk_manager"


def route_after_human_approval(state: SecuritiesAnalysisState) -> str:
    if state.get("final_action") == "block":
        return END
    return "risk_manager"


def route_after_preflight(state: SecuritiesAnalysisState) -> str:
    action = state.get("final_action", "block")
    if action == "execute":
        return "execute"
    return "block"


# ──────────────────────────────────────────────────────────────────────────────
# 5. 图构建
# ──────────────────────────────────────────────────────────────────────────────

def build_securities_graph(checkpointer=None):
    """编译证券分析 LangGraph 工作流。

    Parameters
    ----------
    checkpointer : langgraph checkpointer, optional
        默认 MemorySaver（内存）；生产环境可传入 SqliteSaver。
    """
    builder = StateGraph(SecuritiesAnalysisState)

    builder.add_node("technical_agent",     node_technical_agent)
    builder.add_node("fundamental_agent",   node_fundamental_agent)
    builder.add_node("industry_agent",      node_industry_agent)
    builder.add_node("market_regime_agent", node_market_regime_agent)
    builder.add_node("synthesis_agent",     node_synthesis_agent)
    builder.add_node("debate_approval",     node_debate_approval)
    builder.add_node("no_trade",            node_no_trade)
    builder.add_node("trader_agent",        node_trader_agent)
    builder.add_node("human_approval",      node_human_approval)
    builder.add_node("risk_manager",        node_risk_manager)
    builder.add_node("evaluator_agent",     node_evaluator_agent)
    builder.add_node("trading_harness",     node_trading_harness)

    # 并行分析层
    builder.add_edge(START, "technical_agent")
    builder.add_edge(START, "fundamental_agent")
    builder.add_edge(START, "industry_agent")
    builder.add_edge(START, "market_regime_agent")

    # 汇合到 synthesis
    builder.add_edge("technical_agent",     "synthesis_agent")
    builder.add_edge("fundamental_agent",   "synthesis_agent")
    builder.add_edge("industry_agent",      "synthesis_agent")
    builder.add_edge("market_regime_agent", "synthesis_agent")

    builder.add_conditional_edges(
        "synthesis_agent",
        route_after_synthesis,
        {
            "debate_approval": "debate_approval",
            "no_trade": "no_trade",
            "trader_agent": "trader_agent",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "debate_approval",
        route_after_debate_approval,
        {"no_trade": "no_trade", "trader_agent": "trader_agent", END: END},
    )
    builder.add_edge("no_trade", END)
    builder.add_conditional_edges(
        "trader_agent",
        route_after_trader,
        {"human_approval": "human_approval", "risk_manager": "risk_manager", END: END},
    )
    builder.add_conditional_edges(
        "human_approval",
        route_after_human_approval,
        {"risk_manager": "risk_manager", END: END},
    )
    builder.add_edge("risk_manager", "evaluator_agent")
    builder.add_edge("evaluator_agent", "trading_harness")
    builder.add_conditional_edges(
        "trading_harness",
        route_after_preflight,
        {"execute": END, "block": END, END: END},
    )

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(checkpointer=cp)


# ──────────────────────────────────────────────────────────────────────────────
# 6. 便捷调用接口
# ──────────────────────────────────────────────────────────────────────────────

_default_graph = None


def _get_default_graph():
    global _default_graph
    if _default_graph is None:
        _default_graph = build_securities_graph()
    return _default_graph


def run_securities_analysis(
    symbol: str,
    request_id: str | None = None,
    graph=None,
    thread_id: str | None = None,
    data_mode: str = "auto",
) -> SecuritiesAnalysisState:
    """同步执行完整证券分析流程，返回最终状态。

    Parameters
    ----------
    symbol     : 股票代码
    request_id : 本次分析 ID；缺省自动生成
    graph      : 已编译的 StateGraph；缺省使用模块级单例
    thread_id  : Checkpoint thread_id；缺省与 request_id 相同
    data_mode  : "auto"（默认）或 "offline"（跳过 AkShare，零网络）
    """
    from agent_platform.finance.data_status import resolve_effective_data_mode

    rid = request_id or uuid.uuid4().hex[:12]
    tid = thread_id or rid
    g = graph or _get_default_graph()

    # 解析有效数据模式：DEMO/TEST + auto → offline
    requested_mode = data_mode
    effective_mode = resolve_effective_data_mode(symbol, requested_mode)

    initial: SecuritiesAnalysisState = {
        "symbol": symbol,
        "request_id": rid,
        "run_id": rid,
        "requested_data_mode": requested_mode,
        "data_mode": effective_mode,
        "har_required": False,
        "har_detail": None,
        "confidence": 0.0,
        "status": "pending",
        "errors": [],
        "trace": {
            "start_time": _now_iso(),
            "request_id": rid,
        },
        "trace_entries": [],
        "specialist_audit": [],
        "information_trace": {},
    }

    config = {"configurable": {"thread_id": tid}}
    started = time.perf_counter()
    result = g.invoke(initial, config=config)
    duration = time.perf_counter() - started
    return {**result, "duration_s": duration}


def resume_securities_analysis(
    decision: str,
    thread_id: str,
    graph=None,
) -> SecuritiesAnalysisState:
    """恢复被 interrupt 暂停的工作流。

    Parameters
    ----------
    decision  : "approve" 或 "reject"
    thread_id : 对应暂停时的 thread_id
    graph     : 已编译图；缺省使用模块级单例
    """
    g = graph or _get_default_graph()
    config = {"configurable": {"thread_id": thread_id}}
    started = time.perf_counter()
    result = g.invoke(Command(resume=decision), config=config)
    duration = _trace_duration(result.get("trace_entries") or [])
    duration = max(duration, time.perf_counter() - started)
    return {**result, "duration_s": duration}


def _trace_duration(entries: list[dict]) -> float:
    """从 checkpoint 中持久化的节点轨迹估算工作流耗时。

    四路专业 Agent 并行执行，因此取其最大值；后续节点串行，逐项累加。
    """
    parallel_nodes = {
        "technical_agent", "fundamental_agent", "industry_agent", "market_regime_agent"
    }
    parallel = [
        float(entry.get("duration_s") or 0.0)
        for entry in entries
        if entry.get("node") in parallel_nodes
    ]
    serial = sum(
        float(entry.get("duration_s") or 0.0)
        for entry in entries
        if entry.get("node") not in parallel_nodes
    )
    return max(parallel, default=0.0) + serial
