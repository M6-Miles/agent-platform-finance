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
    request_id: str

    # ── 运行模式 ──────────────────────────────────────────
    # "auto"（默认）= 优先 AkShare 实时，失败降级样例；
    # "offline"     = 完全跳过 AkShare，直接使用样例数据（零网络调用）
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


# ──────────────────────────────────────────────────────────────────────────────
# 2. 内部辅助
# ──────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _te(node: str, t0: float, status: str = "ok", error: str = "") -> list[dict]:
    """构造 trace_entries 追加项。"""
    entry: dict = {
        "node": node,
        "duration_s": round(time.monotonic() - t0, 4),
        "status": status,
    }
    if error:
        entry["error"] = error
    return [entry]


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
    from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
    symbol = state["symbol"]
    offline = state.get("data_mode") == "offline"
    t0 = time.monotonic()
    try:
        if offline:
            provider = SampleMarketDataProvider()
            result = analyze_security(symbol, provider=provider)
        else:
            result = analyze_security(symbol)
        d = result.to_dict()
        d["_markdown"] = result.to_markdown()
        return {
            "technical_analysis": d,
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
    from agent_platform.finance.fundamental_agent import analyze_fundamental
    symbol = state["symbol"]
    offline = state.get("data_mode") == "offline"
    t0 = time.monotonic()
    try:
        result = analyze_fundamental(symbol, force_offline=offline)
        d = result.to_dict()
        d["_markdown"] = result.to_markdown()
        return {
            "fundamental_analysis": d,
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
    from agent_platform.finance.industry_agent import analyze_industry
    symbol = state["symbol"]
    offline = state.get("data_mode") == "offline"
    t0 = time.monotonic()
    try:
        result = analyze_industry(symbol, force_offline=offline)
        d = result.to_dict()
        d["_markdown"] = result.to_markdown()
        return {
            "industry_analysis": d,
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
    from agent_platform.finance.market_regime_agent import analyze_market_regime
    offline = state.get("data_mode") == "offline"
    t0 = time.monotonic()
    try:
        result = analyze_market_regime(force_offline=offline)
        d = result.to_dict()
        d["_markdown"] = result.to_markdown()
        return {
            "market_regime": d,
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
    t0 = time.monotonic()

    tech = state.get("technical_analysis")
    fund = state.get("fundamental_analysis")
    ind = state.get("industry_analysis")
    regime = state.get("market_regime")

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
        d["_markdown"] = result.to_markdown()
        return {
            "synthesis": d,
            "confidence": float(result.confidence),
            "status": "synthesis_done",
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
    t0 = time.monotonic()
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
    t0 = time.monotonic()

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
    harness = TradingHarness()
    t0 = time.monotonic()

    try:
        result = harness.run_preflight(
            synthesis_result=syn,
            trader_result=trader,
            risk_result=risk,
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


def node_human_approval(state: SecuritiesAnalysisState) -> dict:
    """人工审批节点（HumanApprovalRequired 路径）。

    使用 interrupt() 暂停图；恢复时通过 Command(resume=...) 传入
    "approve" 或 "reject"。
    trade_signal 已由 node_trader_agent 存入状态，批准后 risk_manager 直接取用。
    """
    t0 = time.monotonic()
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
    if confidence <= _LOW_CONFIDENCE_THRESHOLD:
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
    builder.add_node("no_trade",            node_no_trade)
    builder.add_node("trader_agent",        node_trader_agent)
    builder.add_node("human_approval",      node_human_approval)
    builder.add_node("risk_manager",        node_risk_manager)
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
    builder.add_edge("risk_manager", "trading_harness")
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
    rid = request_id or uuid.uuid4().hex[:12]
    tid = thread_id or rid
    g = graph or _get_default_graph()

    initial: SecuritiesAnalysisState = {
        "symbol": symbol,
        "request_id": rid,
        "data_mode": data_mode,
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
    }

    config = {"configurable": {"thread_id": tid}}
    return g.invoke(initial, config=config)


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
    return g.invoke(Command(resume=decision), config=config)
