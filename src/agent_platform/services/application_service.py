from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from agent_platform.config import Settings, get_settings
from agent_platform.core.agent_runtime import AgentRunResult
from agent_platform.core.harness import (
    AgentHarness,
    CircuitBreaker,
    CircuitBreakerOpen,
    GuardrailViolation,
    JSONSchemaValidator,
    KeywordBlocker,
    RateLimiter,
    SourceAttributionFilter,
)
from agent_platform.core.llm_provider import ChatMessage
from agent_platform.core.observability import ObservabilityPanel
from agent_platform.finance.analysis import SecurityAnalysisResult, analyze_security
from agent_platform.finance.constants import DISCLAIMER
from agent_platform.finance.data_status import normalize_data_mode
from agent_platform.finance.market_data_provider import MarketDataProvider, SecurityInfo
from agent_platform.finance.provider_factory import create_market_data_provider
from agent_platform.finance.quote_tool import (
    TOOL_NAME as QUOTE_TOOL_NAME,
    QuotePayload,
    QuoteToolError,
    ToolInvocation,
    extract_symbol,
    get_latest_quote,
    has_quote_intent,
)
from agent_platform.services.runtime_factory import build_runtime
from agent_platform.storage.sqlite_store import (
    AnalysisRecord,
    MessageRecord,
    SQLiteStore,
    SessionRecord,
)

# Harness 对聊天输出的结构约束：答案文本 + 数据来源 + 更新时间
CHAT_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["answer", "source", "updated_at"],
    "properties": {
        "answer": {"type": "string", "minLength": 1},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "provider": {"type": "string"},
    },
}


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _timed_agent(panel: ObservabilityPanel, agent_name: str, fn):
    """将图节点可调用对象包装为带 ObservabilityPanel 计时记录的版本。"""
    def wrapped(state: dict) -> dict:
        t0 = time.monotonic()
        try:
            result = fn(state)
            panel.record_call(
                agent_name=agent_name,
                task=str(state.get("symbol", "")),
                duration_s=time.monotonic() - t0,
                success=True,
            )
            return result
        except Exception:
            panel.record_call(
                agent_name=agent_name,
                task=str(state.get("symbol", "")),
                duration_s=time.monotonic() - t0,
                success=False,
            )
            raise
    return wrapped


def resolve_research_status(
    *,
    interrupt_payload: "dict | None",
    final_action: "str | None",
    errors: list,
    raw_status: str,
) -> str:
    """统一 API 工作流状态转换函数。

    POST /research/{symbol}、GET /research/{thread_id}/state、
    POST /research/{thread_id}/resume 三个接口共享此函数，保证同一 thread_id
    在任何接口中返回一致的 status 值。

    优先级（高 → 低）：
      interrupted → blocked → completed → no_trade → failed → completed（默认）

    返回值枚举（仅这5种合法值）：
      "interrupted" — 图在 interrupt() 处暂停，等待人工审批
      "blocked"     — final_action == "block"（HAR/preflight 拒绝）
      "completed"   — final_action in ("execute", "manual_review")，工作流正常结束
      "no_trade"    — 置信度不足，跳过交易建议（final_action=None, errors=[]）
      "failed"      — 运行时错误（errors 非空，final_action=None）
    """
    if interrupt_payload is not None:
        return "interrupted"
    if final_action == "block":
        return "blocked"
    if final_action in ("execute", "manual_review"):
        return "completed"
    if raw_status == "no_trade" and not errors:
        return "no_trade"
    if errors:
        return "failed"
    # 图运行到 END，无 final_action、无错误、非 no_trade → 视为 completed
    return "completed"


@dataclass(frozen=True, slots=True)
class DeepResearchResult:
    """深度投研完整结果，包含7路Agent输出及汇总报告。"""
    symbol: str
    run_id: str
    thread_id: str        # LangGraph checkpoint thread_id，可用于 resume
    duration_s: float
    technical: dict
    fundamental: dict
    industry: dict
    regime: dict
    synthesis: dict
    trader: dict | None   # 置信度 ≤0.3 时为 None
    risk: dict | None     # trader 为 None 时同为 None
    full_markdown: str
    # ── Issue 2+3 修复：新增字段用于 API 状态判断 ──
    status: str                        # pending / completed / interrupted / error
    final_action: str | None           # execute / manual_review / block / None
    errors: list[str]                  # 错误列表
    interrupt_payload: dict | None     # interrupt 时的 payload


@dataclass(frozen=True, slots=True)
class ChatServiceResult:
    session_id: str
    answer: str
    provider: str
    run: AgentRunResult
    guardrail_violations: tuple[str, ...] = ()
    harness_retries: int = 0
    # 真实工具调用记录（含确定性行情工具），供 /chat 返回给前端渲染
    tool_invocations: tuple[ToolInvocation, ...] = ()
    data_mode: str = "auto"


class ApplicationService:
    def __init__(
        self,
        settings: Settings | None = None,
        store: SQLiteStore | None = None,
        market_data: MarketDataProvider | None = None,
        market_data_provider: str | None = None,
        chat_rate_limit_per_minute: int = 20,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or SQLiteStore(self.settings.sqlite_path)
        self.market_data_provider = (
            market_data_provider or self.settings.market_data_provider
        ).strip().lower()
        self.market_data = market_data or create_market_data_provider(
            self.market_data_provider,
            self.settings,
        )
        # ── LangGraph 证券分析图（长生命周期，SQLite checkpoint 跨调用保存）──
        # 必须持有单例图：MemorySaver 不跨调用，每次 build_securities_graph()
        # 会创建新 MemorySaver，丢失 checkpoint，无法 resume interrupt。
        from agent_platform.finance.securities_graph import build_securities_graph

        # 生产模式必须使用 SQLite checkpoint；只有明确配置时才允许降级到 MemorySaver
        # （通过 Settings.langgraph_use_memory_saver / 环境变量 LANGGRAPH_USE_MEMORY_SAVER）
        use_memory_saver = self.settings.langgraph_use_memory_saver
        self._langgraph_checkpoint_conn: "sqlite3.Connection | None" = None  # 生命周期管理

        if use_memory_saver:
            import logging
            logging.getLogger(__name__).warning(
                "LangGraph checkpoint 使用 MemorySaver（内存模式）："
                "interrupt 不能跨进程恢复，仅限开发/测试环境。"
            )
            from langgraph.checkpoint.memory import MemorySaver as _MemorySaver
            _checkpointer = _MemorySaver()
        else:
            # 生产模式：必须使用 SqliteSaver
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver
            except ImportError as exc:
                raise RuntimeError(
                    "生产模式需要 langgraph-checkpoint-sqlite，请安装："
                    "pip install 'langgraph-checkpoint-sqlite>=2.0.6'"
                ) from exc

            import sqlite3
            from pathlib import Path as _Path
            _sp = _Path(self.settings.sqlite_path)
            _cp_path = str(_sp.with_name(_sp.stem + "_lg_checkpoints.db"))
            _Path(_cp_path).parent.mkdir(parents=True, exist_ok=True)

            # SqliteSaver 接受已连接的 sqlite3.Connection（check_same_thread=False
            # 允许多线程共享；SqliteSaver 内部持有 threading.Lock 保证序列化）。
            # 不能使用 from_conn_string 上下文管理器（退出时会关闭连接）。
            try:
                _conn = sqlite3.connect(_cp_path, check_same_thread=False)
                _checkpointer = _SqliteSaver(_conn)
                _checkpointer.setup()   # 建表（幂等，已存在时 no-op）
            except Exception as exc:
                raise RuntimeError(
                    f"初始化 SQLite checkpoint 失败（路径={_cp_path}）：{exc}"
                ) from exc

            # 保存连接引用，供 close() 显式关闭（避免依赖 GC）
            self._langgraph_checkpoint_conn = _conn

        self._securities_graph = build_securities_graph(checkpointer=_checkpointer)
        self._checkpointer_type = "memory" if use_memory_saver else "sqlite"
        self._checkpointer_path = None if use_memory_saver else _cp_path

        # Harness 常驻单例：RateLimiter 的滑动窗口与 CircuitBreaker 的失败计数
        # 都必须跨多次 chat() 调用保持状态，所以挂在 service 实例上而非每次新建。
        self.chat_harness = AgentHarness(
            agent=None,          # 每次 chat() 前替换为当次的 runtime 闭包
            guardrails=[
                RateLimiter(max_calls_per_minute=chat_rate_limit_per_minute),
                JSONSchemaValidator(CHAT_OUTPUT_SCHEMA),
                SourceAttributionFilter(),
                KeywordBlocker(),
            ],
            max_retries=1,       # LLM 调用昂贵，最多重试 1 次
            circuit_breaker=CircuitBreaker(max_failures=3, cooldown_s=300.0),
        )

    def create_session(self, title: str | None = None) -> SessionRecord:
        return self.store.create_session(title)

    def close(self) -> None:
        """幂等关闭：释放 SQLite checkpoint 连接及其他需要清理的资源。

        应在 FastAPI lifespan shutdown 或测试 teardown 时显式调用，
        而不是依赖垃圾回收。MemorySaver 模式下此方法为空操作。
        Windows 下不显式关闭 SQLite 连接会导致数据库文件被占用，
        阻止测试 tmp_path 的清理。
        """
        conn = self._langgraph_checkpoint_conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._langgraph_checkpoint_conn = None

    def rename_session(self, session_id: str, new_title: str) -> None:
        self.store.rename_session(session_id, new_title)

    def delete_session(self, session_id: str) -> None:
        self.store.delete_session(session_id)

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        return self.store.list_sessions(limit)

    def list_messages(self, session_id: str) -> list[MessageRecord]:
        return self.store.list_messages(session_id)

    def list_analysis_history(self, limit: int = 20) -> list[AnalysisRecord]:
        return self.store.list_analyses(limit)

    def list_securities(self) -> list[SecurityInfo]:
        return self.market_data.list_securities()

    def get_price_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ):
        """返回原始日线 DataFrame，供 /price-history 端点及图表使用。"""
        return self.market_data.get_price_history(symbol, start=start, end=end)

    def get_realtime_quote(self, symbol: str) -> dict:
        """获取实时报价（用于模拟盘联网行情按钮）。"""
        return self.market_data.get_realtime_quote(symbol)

    def analyze_security(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        session_id: str | None = None,
        trigger: str = "direct",
    ) -> SecurityAnalysisResult:
        if start is not None and end is not None and start > end:
            raise ValueError("开始日期不能晚于结束日期")
        result = analyze_security(
            symbol=symbol,
            start=start,
            end=end,
            provider=self.market_data,
        )
        self.store.add_analysis(result, trigger=trigger, session_id=session_id)
        return result

    def analyze_security_as_markdown(
        self,
        symbol: str,
        session_id: str | None = None,
    ) -> str:
        return self.analyze_security(
            symbol=symbol,
            session_id=session_id,
            trigger="agent_tool",
        ).to_markdown()

    def chat(
        self,
        message: str,
        session_id: str | None = None,
        data_mode: str = "auto",
    ) -> ChatServiceResult:
        normalized_message = message.strip()
        if not normalized_message:
            raise ValueError("问题不能为空")

        mode = normalize_data_mode(data_mode)

        if session_id is None:
            session = self.create_session(self._make_session_title(normalized_message))
            session_id = session.id
        elif self.store.get_session(session_id) is None:
            raise ValueError(f"会话不存在：{session_id}")

        # ── 确定性行情工具：命中行情意图必须调用工具，模型不得自行猜价 ─────────
        # 工具成功 → 把完整报价事实块注入 prompt，并作为真实 tool step 返回。
        # 工具失败 → 显式返回失败（不降级为随机值、不让模型编造价格）。
        tool_invocations: list[ToolInvocation] = []
        quote_payload: QuotePayload | None = None
        quote_error: str | None = None
        quote_symbol: str | None = None

        if has_quote_intent(normalized_message):
            symbol = quote_symbol = extract_symbol(normalized_message)
            if symbol is None:
                quote_error = "未能从提问中识别证券代码（需 6 位 A 股代码或样例代码如 DEMO001）"
                tool_invocations.append(
                    ToolInvocation(
                        tool_name=QUOTE_TOOL_NAME,
                        input={"symbol": None, "data_mode": mode},
                        output=None,
                        status="error",
                        error=quote_error,
                    )
                )
            else:
                t0 = time.monotonic()
                provider = self.market_data if mode == "auto" else None
                try:
                    quote_payload = get_latest_quote(
                        symbol, data_mode=mode, provider=provider
                    )
                    tool_invocations.append(
                        ToolInvocation(
                            tool_name=QUOTE_TOOL_NAME,
                            input={"symbol": symbol, "data_mode": mode},
                            output=quote_payload.to_dict(),
                            status="success",
                            duration_ms=round((time.monotonic() - t0) * 1000, 2),
                        )
                    )
                except QuoteToolError as exc:
                    quote_error = str(exc)
                    tool_invocations.append(
                        ToolInvocation(
                            tool_name=QUOTE_TOOL_NAME,
                            input={"symbol": symbol, "data_mode": mode},
                            output=None,
                            status="error",
                            error=quote_error,
                            duration_ms=round((time.monotonic() - t0) * 1000, 2),
                        )
                    )

        # 行情意图的确定性工具失败时必须短路返回，绝不把提问交给通用 Agent：
        # 失败原因文本里含有"可选样例：DEMO001, ..."之类的代码，通用 Agent 会把
        # 它当成待分析标的去调用分析工具，最终用**另一只证券**的真实样例数字
        # 回答，等于用无关数据掩盖本次失败。这是"工具失败必须显式暴露、
        # 模型不得给出任何推测价格"的执行点。
        if quote_error is not None:
            self.store.add_message(session_id, "user", normalized_message)
            error_answer = (
                f"无法获取 {quote_symbol or '该证券'} 的最新行情。\n\n"
                f"行情工具失败原因：{quote_error}\n"
                "未返回任何推测价格。\n\n"
                f"> ⚠️ {DISCLAIMER}"
            )
            self.store.add_message(session_id, "assistant", error_answer, provider="quote_tool")
            return ChatServiceResult(
                session_id=session_id,
                answer=error_answer,
                provider="quote_tool",
                run=AgentRunResult(
                    answer=error_answer,
                    steps=(),
                    provider="quote_tool",
                ),
                tool_invocations=tuple(tool_invocations),
                data_mode=mode,
            )

        # 工具成功：把确定性事实块注入 prompt，模型只能引用这些数字。
        if quote_payload is not None:
            runtime_message = (
                "【行情工具 get_latest_quote 返回的确定性数据 — 只能引用以下数字，"
                "不得改写、不得根据训练数据推测价格】\n"
                + quote_payload.to_prompt_text()
                + "\n\n用户提问：\n"
                + normalized_message
            )
        else:
            runtime_message = normalized_message

        previous_messages = self.store.list_messages(session_id)
        history = [
            ChatMessage(role=item.role, content=item.content)
            for item in previous_messages
            if item.role in {"user", "assistant"}
        ]
        try:
            runtime = build_runtime(
                lambda symbol: self.analyze_security_as_markdown(symbol, session_id),
                settings=self.settings,
            )
        except (RuntimeError, ImportError) as exc:
            self.store.add_message(session_id, "user", normalized_message)
            error_answer = (
                f"❌ 大模型初始化失败：{exc}\n\n"
                "请在 .env 中检查 LLM_PROVIDER 和相关 API Key 配置，"
                "或将 LLM_PROVIDER 改回 'mock' 使用离线演示。"
            )
            # LLM 不可用时，行情工具的确定性结果仍然必须交付给用户：
            # 价格来自工具，不来自模型，所以不需要 LLM 也能如实回答。
            if quote_payload is not None:
                error_answer = (
                    quote_payload.to_answer_text()
                    + f"\n\n（大模型不可用：{exc}；以上行情由后端 "
                    f"{QUOTE_TOOL_NAME} 工具直接返回。）\n\n> ⚠️ {DISCLAIMER}"
                )
            self.store.add_message(session_id, "assistant", error_answer)
            return ChatServiceResult(
                session_id=session_id,
                answer=error_answer,
                provider="error",
                run=AgentRunResult(
                    answer=error_answer,
                    steps=(),
                    provider="error",
                ),
                tool_invocations=tuple(tool_invocations),
                data_mode=mode,
            )
        self.store.add_message(session_id, "user", normalized_message)

        # ── 经由 AgentHarness 执行：Pre-flight → Run → Post-flight ──────────
        captured: dict[str, AgentRunResult] = {}

        def _run_agent(task: str) -> dict:
            run = runtime.run(task, history=history)
            captured["run"] = run
            return {
                "answer": run.answer,
                "provider": run.provider,
                "source": f"agent_runtime/{run.provider}",
                "updated_at": _utc_now_iso(),
            }

        self.chat_harness.agent = _run_agent
        trace_before = len(self.chat_harness.traces)

        try:
            payload = self.chat_harness.run(runtime_message)
        except CircuitBreakerOpen as exc:
            answer = (
                f"🔌 服务已熔断：{exc}\n\n"
                "连续失败次数达到阈值，请稍后重试（冷却 5 分钟后自动恢复）。\n\n"
                f"> ⚠️ {DISCLAIMER}"
            )
            self.store.add_message(session_id, "assistant", answer, provider="circuit_breaker")
            return ChatServiceResult(
                session_id=session_id,
                answer=answer,
                provider="circuit_breaker",
                run=AgentRunResult(answer=answer, steps=(), provider="circuit_breaker"),
                guardrail_violations=("CircuitBreakerOpen",),
                tool_invocations=tuple(tool_invocations),
                data_mode=mode,
            )
        except GuardrailViolation as exc:
            violations = self._latest_violations(trace_before)
            answer = (
                f"🛡️ 输出被 Guardrail 拦截：{exc}\n\n"
                "该回答未通过合规校验，已阻止返回。请调整问题后重试。\n\n"
                f"> ⚠️ {DISCLAIMER}"
            )
            self.store.add_message(session_id, "assistant", answer, provider="guardrail")
            return ChatServiceResult(
                session_id=session_id,
                answer=answer,
                provider="guardrail",
                run=AgentRunResult(answer=answer, steps=(), provider="guardrail"),
                guardrail_violations=violations,
                tool_invocations=tuple(tool_invocations),
                data_mode=mode,
            )

        run = captured.get("run") or AgentRunResult(
            answer=str(payload.get("answer", "")),
            steps=(),
            provider=str(payload.get("provider", "unknown")),
        )
        answer = str(payload.get("answer", run.answer))
        # SourceAttributionFilter 可能注入 _source_warning，附到答案尾部而非静默丢弃
        warning = payload.get("_source_warning")
        if warning:
            answer = f"{answer}\n\n> ℹ️ 数据来源提示：{warning}"

        self.store.add_message(
            session_id,
            "assistant",
            answer,
            provider=run.provider,
        )
        # Agent 在 runtime 内部真实执行的工具（如 analyze_security）也必须
        # 作为 tool step 暴露给前端：tool_steps 是真实调用记录，不是展示用假步骤。
        for step in run.steps:
            tool_invocations.append(
                ToolInvocation(
                    tool_name=step.name,
                    input={"source": "agent_runtime"},
                    output=None if step.is_error else {"result": step.output},
                    status="error" if step.is_error else "success",
                    error=step.output if step.is_error else None,
                )
            )

        trace = self.chat_harness.traces[-1] if self.chat_harness.traces else None
        return ChatServiceResult(
            session_id=session_id,
            answer=answer,
            provider=run.provider,
            run=run,
            guardrail_violations=self._latest_violations(trace_before),
            harness_retries=trace.retries if trace else 0,
            tool_invocations=tuple(tool_invocations),
            data_mode=mode,
        )

    def _latest_violations(self, trace_before: int) -> tuple[str, ...]:
        """收集本次 chat 调用期间新增的 Guardrail 违规记录。"""
        violations: list[str] = []
        for trace in self.chat_harness.traces[trace_before:]:
            violations.extend(trace.guardrail_violations)
        return tuple(violations)

    def deep_research(
        self,
        symbol: str,
        obs_panel: ObservabilityPanel | None = None,
        graph=None,
        data_mode: str = "auto",
    ) -> DeepResearchResult:
        """
        深度投研：LangGraph 并行编排 4 路专业 Agent
        （技术 / 基本面 / 行业 / 大盘）→ 综合研判 → 交易建议 → 风控审核 → Pre-Flight。

        LangGraph 是主编排引擎（securities_graph.build_securities_graph）。
        执行指标写入 obs_panel（若提供，否则新建临时面板）。
        所有输出均附带免责声明，不构成投资建议。

        Parameters
        ----------
        symbol    : 股票代码
        obs_panel : 可观测面板，缺省新建临时面板
        graph     : 已编译图（测试注入用）；缺省使用 self._securities_graph
        data_mode : "auto"（默认）| "offline"（跳过 AkShare，零网络调用）
        """
        from agent_platform.finance.securities_graph import run_securities_analysis

        sym = symbol.strip()
        panel = obs_panel or ObservabilityPanel()
        run_id = uuid.uuid4().hex[:12]
        thread_id = uuid.uuid4().hex[:16]
        t_start = time.monotonic()

        # ── 执行 LangGraph 主流程 ───────────────────────────────────────────
        g = graph if graph is not None else self._securities_graph
        state = run_securities_analysis(
            symbol=sym,
            request_id=run_id,
            graph=g,
            thread_id=thread_id,
            data_mode=data_mode,
        )
        duration = time.monotonic() - t_start

        # ── 查询真实 interrupt 状态（不依赖 state 字段推测）────────────────
        # LangGraph invoke 遇到 interrupt() 后返回当前状态，不抛异常。
        # 需要通过 get_state() 检查 snapshot.tasks 中的 interrupts 来判断暂停状态。
        interrupt_payload: dict | None = None
        result_status: str
        try:
            _config = {"configurable": {"thread_id": thread_id}}
            _snap = g.get_state(_config)
            for _task in (_snap.tasks if _snap else []):
                if hasattr(_task, "interrupts") and _task.interrupts:
                    interrupt_payload = _task.interrupts[0].value
                    break
        except Exception:
            pass  # checkpointer 不支持查询时忽略（MemorySaver 应支持）

        # 使用统一状态转换函数（与 GET state、POST resume 端点共享同一逻辑）
        result_status = resolve_research_status(
            interrupt_payload=interrupt_payload,
            final_action=state.get("final_action"),
            errors=list(state.get("errors") or []),
            raw_status=state.get("status", "unknown"),
        )

        # ── ObservabilityPanel 计时记录（真实节点耗时，来自 trace_entries）──
        # 只记录实际执行过的节点（trace_entries 仅含运行过的项）。
        for entry in (state.get("trace_entries") or []):
            node = entry.get("node", "unknown")
            dur  = entry.get("duration_s", 0.0)
            ok   = entry.get("status", "ok") not in ("error",)
            panel.record_call(
                agent_name=node,
                task=sym,
                duration_s=dur,
                success=ok,
            )

        # ── 辅助：从状态取 _markdown 或构建占位文字 ────────────────────────
        def _md(key: str) -> str:
            return str((state.get(key) or {}).get("_markdown", ""))

        def _clean(d: dict | None) -> dict:
            out = dict(d or {})
            out.pop("_markdown", None)
            return out

        tech_md  = _md("technical_analysis")
        fund_md  = _md("fundamental_analysis")
        ind_md   = _md("industry_analysis")
        reg_md   = _md("market_regime")
        syn_md   = _md("synthesis")
        trd_md   = _md("trade_signal")
        rsk_md   = _md("risk_result")

        final_action = state.get("final_action")
        status       = state.get("status", "unknown")

        # 根据 final_action 构建交易/风控摘要段落
        if final_action == "execute":
            action_note = "🟢 **execute** — Pre-Flight 通过，可执行交易"
        elif final_action == "manual_review":
            action_note = "🟡 **manual_review** — 需人工审批"
        elif final_action == "block":
            action_note = "🔴 **block** — Pre-Flight 阻断"
        elif status == "no_trade" or (state.get("confidence", 0.0) <= 0.3
                                       and not state.get("trade_signal")):
            action_note = "⚪ **no_trade** — 置信度不足（≤30%），跳过交易建议"
        else:
            action_note = f"⚪ status={status}"

        trader_section = trd_md or "_置信度不足（≤30%），已跳过交易建议。_"
        risk_section   = rsk_md or "_无交易建议，风控审核已跳过。_"
        pf_md = _md("preflight_result")
        preflight_section = pf_md or f"_{action_note}_"

        errors = state.get("errors") or []
        error_section = (
            "\n".join(f"- {e}" for e in errors) if errors
            else "_无错误_"
        )

        full_md = "\n\n---\n\n".join([
            f"# 深度投研报告 — {sym}",
            f"_运行 ID：{run_id}　Thread：{thread_id}　耗时：{duration:.1f}s　引擎：LangGraph 1.x_",
            "## 📊 技术面分析", tech_md or "_数据不可用_",
            "## 📋 基本面分析", fund_md or "_数据不可用_",
            "## 🏭 行业分析",   ind_md or "_数据不可用_",
            "## 🌐 大盘/宏观",  reg_md or "_数据不可用_",
            "## 🧠 综合研判",   syn_md or "_未执行_",
            "## 💼 交易建议",   trader_section,
            "## 🛡️ 风控审核",  risk_section,
            "## ✅ Pre-Flight 决策", preflight_section,
            "## ⚠️ 错误日志", error_section,
            f"> ⚠️ {DISCLAIMER}",
        ])

        return DeepResearchResult(
            symbol=sym,
            run_id=run_id,
            thread_id=thread_id,
            duration_s=round(duration, 2),
            technical=_clean(state.get("technical_analysis")),
            fundamental=_clean(state.get("fundamental_analysis")),
            industry=_clean(state.get("industry_analysis")),
            regime=_clean(state.get("market_regime")),
            synthesis=_clean(state.get("synthesis")),
            trader=_clean(state.get("trade_signal")) if state.get("trade_signal") else None,
            risk=_clean(state.get("risk_result")) if state.get("risk_result") else None,
            full_markdown=full_md,
            # ── Issue 2+3 修复：从 state 直接取真实状态 ──
            status=result_status,
            final_action=state.get("final_action"),
            errors=list(state.get("errors") or []),
            interrupt_payload=interrupt_payload,
        )

    @staticmethod
    def _make_session_title(message: str) -> str:
        return message if len(message) <= 30 else f"{message[:30]}…"
