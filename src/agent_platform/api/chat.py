"""
Agent 对话 API
==============
唯一的 ``/chat`` 实现。前端不得直连任何 LLM 厂商，也不得在浏览器保存 API Key：
密钥只存在于后端 ``.env``。

本端点把请求交给 ``ApplicationService.chat()``，因此完整复用：
  * AgentHarness（Pre-flight / Post-flight）
  * 5 项 Guardrail + CircuitBreaker
  * SQLite 会话与消息持久化
  * 确定性行情工具 ``get_latest_quote``（行情意图必调用，失败显式暴露）
  * Harness trace（重试次数、Guardrail 违规）

响应中的 ``tool_steps`` 是真实调用记录，不是展示用的假步骤。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agent_platform.finance.data_status import normalize_data_mode
from agent_platform.finance.quote_tool import TOOL_NAME as QUOTE_TOOL_NAME

router = APIRouter()


class ChatMessageIn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: str | None = None
    # history 由后端 SQLite 会话历史决定；此字段仅为兼容旧前端，不参与推理。
    history: list[ChatMessageIn] = Field(default_factory=list)
    data_mode: str = Field("auto", pattern="^(offline|auto)$")


class ToolStep(BaseModel):
    tool_name: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    status: str
    error: str | None = None
    duration_ms: float = 0.0


class GuardrailResult(BaseModel):
    name: str
    passed: bool
    reason: str | None = None


class TracingInfo(BaseModel):
    provider: str
    harness_retries: int
    guardrail_violations: list[str]
    tool_calls: int


class ChatResponse(BaseModel):
    session_id: str
    # answer 是既有契约字段，reply 为新前端字段，两者恒为同一文本。
    answer: str
    reply: str
    provider: str
    data_mode: str
    tool_steps: list[ToolStep]
    guardrail_results: list[GuardrailResult]
    tracing: TracingInfo
    # 命中行情意图时，工具返回的确定性报价（失败为 None，并在 tool_steps 中给出原因）
    quote: dict[str, Any] | None = None


# Harness 中实际注册的 Guardrail 名称（与 ApplicationService.chat_harness 一致）
_HARNESS_GUARDRAILS = (
    "RateLimiter",
    "JSONSchemaValidator",
    "SourceAttributionFilter",
    "KeywordBlocker",
)


@router.post("/chat", response_model=ChatResponse)
def chat_with_agent(req: ChatRequest, request: Request) -> ChatResponse:
    """后端 Agent 对话。密钥仅在后端，浏览器不持有任何凭据。"""
    from agent_platform.api.main import get_application_service

    try:
        mode = normalize_data_mode(req.data_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    service = get_application_service()
    from agent_platform.api.main import _authorize, _claim
    if req.session_id:
        _authorize(request, "session", req.session_id)
    started_at = time.perf_counter()
    try:
        result = service.chat(
            req.message, req.session_id, data_mode=mode,
            user_id=getattr(request.state, "principal", None).user_id
            if getattr(request.state, "principal", None) else "anonymous",
        )
    except ValueError as exc:
        service.observability.record_call(
            agent_name="chat_agent", task=req.message[:200],
            duration_s=time.perf_counter() - started_at, success=False,
            guardrail_violations=[str(exc)],
        )
        status = 404 if "会话不存在" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    service.observability.record_call(
        agent_name="chat_agent", task=req.message[:200],
        duration_s=time.perf_counter() - started_at,
        success=not bool(result.guardrail_violations),
        input_tokens=result.run.input_tokens,
        output_tokens=result.run.output_tokens,
        guardrail_violations=list(result.guardrail_violations),
        retries=result.harness_retries,
    )
    _claim(request, "session", result.session_id)

    tool_steps = [
        ToolStep(
            tool_name=call.tool_name,
            input=call.input,
            output=call.output,
            status=call.status,
            error=call.error,
            duration_ms=call.duration_ms,
        )
        for call in result.tool_invocations
    ]

    # Guardrail 结果来自真实 trace：违规列表非空即为未通过。
    violated = set(result.guardrail_violations)
    guardrail_results = [
        GuardrailResult(
            name=name,
            passed=not any(name in v for v in violated),
            reason=next((v for v in violated if name in v), None),
        )
        for name in _HARNESS_GUARDRAILS
    ]

    # quote 只能来自确定性行情工具，绝不把其它工具（如 analyze_security）的
    # 输出当成报价字段返回。
    quote: dict[str, Any] | None = None
    for call in result.tool_invocations:
        if call.tool_name == QUOTE_TOOL_NAME and call.status == "success" and call.output:
            quote = dict(call.output)
            break

    return ChatResponse(
        session_id=result.session_id,
        answer=result.answer,
        reply=result.answer,
        provider=result.provider,
        data_mode=result.data_mode,
        tool_steps=tool_steps,
        guardrail_results=guardrail_results,
        tracing=TracingInfo(
            provider=result.provider,
            harness_retries=result.harness_retries,
            guardrail_violations=list(result.guardrail_violations),
            tool_calls=len(tool_steps),
        ),
        quote=quote,
    )
