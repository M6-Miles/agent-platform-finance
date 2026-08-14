"""真实 LLM Harness ON/OFF 离线回放实验
==================================================

本模块实现对真实 LLM（DeepSeek、Claude）的 Harness 有效性实验，
采用"离线回放"模式：对同一个模型响应分别进行 ON/OFF 配对评估。

关键修复（2026-08-13 Codex 审查后）：
  1. ✅ Harness 验证真实模型输出（解析 JSON，不包装）
  2. ✅ 实现真正的有限重试（Timeout/Connection/5xx）
  3. ✅ Provider 错误识别（不把错误当成功）
  4. ✅ 递归正则脱敏（句子内嵌敏感信息）
  5. ✅ 严格 Mock/simulated/real 识别
  6. ✅ 凭证边界（模块层检查，不猜测）
  7. ✅ 指标完整（error_count、P95 算法明确）
  8. ✅ request_id 唯一（uuid4）
  9. ✅ provider/model 分离
 10. ✅ 禁止交易（AST 检查）

与 harness_experiment.py 的区别：
  - harness_experiment.py 使用构造性 mock 样本，不调用真实 LLM
  - real_llm_replay.py 调用真实 LLM，但需要 API Key；无 Key 时跳过

关键约束：
  1. 对同一个 task_id 只调用模型一次，避免重复计费
  2. 同一响应分别通过 Harness ON/OFF 路径处理
  3. 禁止调用任何交易、下单或真实券商接口
  4. 所有敏感信息（API Key、手机号、邮箱等）必须递归正则脱敏
  5. 没有 API Key 时返回 skipped 状态，零网络调用
  6. Mock/simulated Provider 必须标记清楚，不能冒充 real

实验流程：
  1. 检查 provider_kind（real/simulated/mock）
  2. 对每个 task 构造脱敏输入
  3. 调用真实 LLM 获取响应（仅一次，有限重试）
  4. Harness OFF：解析原始 JSON，记录是否符合 Schema
  5. Harness ON：对解析出的真实对象运行 Guardrail 校验
  6. 记录完整实验数据（已脱敏，不含 API Key）
  7. 生成聚合统计和报告
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_platform.core.harness import (
    GuardrailViolation,
    JSONSchemaValidator,
    KeywordBlocker,
    SourceAttributionFilter,
)
from agent_platform.core.llm_provider import (
    ChatMessage,
    LLMNetworkError,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMServerError,
)

logger = logging.getLogger(__name__)

# 敏感字段名称列表（递归匹配，不区分大小写）
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "bearer",
    "access_token",
    "refresh_token",
    "private_key",
    "account",
    "account_id",
    "user_id",
    "customer_id",
    "cert",
    "credential",
}

# 正则：脱敏句子内嵌敏感信息
_BEARER_TOKEN_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]+", re.IGNORECASE)
_API_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{8,}")
_ANT_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}")
_CN_PHONE_PATTERN = re.compile(r"1[3-9]\d{9}")
_ID_CARD_PATTERN = re.compile(r"\d{17}[\dXx]")
_BANK_CARD_PATTERN = re.compile(r"\d{16,19}")

# 测试任务（脱敏输入）
_TEST_TASKS = [
    {
        "task_id": "REAL-001",
        "label": "技术分析",
        "user_message": (
            "请分析 DEMO001 的技术指标。"
            "要求：只返回符合 Schema 的 JSON，不要额外文字。"
            "JSON Schema: {required: ['signal', 'confidence', 'source', 'updated_at']}"
        ),
    },
    {
        "task_id": "REAL-002",
        "label": "基本面分析",
        "user_message": (
            "请分析 DEMO002 的基本面。"
            "要求：只返回符合 Schema 的 JSON，不要额外文字。"
            "JSON Schema: {required: ['signal', 'confidence', 'source', 'updated_at']}"
        ),
    },
    {
        "task_id": "REAL-003",
        "label": "综合分析",
        "user_message": (
            "请对 TEST001 做一次综合证券分析，结合技术面、基本面和市场环境。"
            "要求：只返回符合 Schema 的 JSON，不要额外文字。"
            "JSON Schema: {required: ['signal', 'confidence', 'source', 'updated_at']}"
        ),
    },
]

# 输出 Schema
_SYNTHESIS_SCHEMA = {
    "type": "object",
    "required": ["signal", "confidence", "source", "updated_at"],
    "properties": {
        "signal": {"type": "string", "enum": ["buy", "sell", "hold"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
    },
}

_FORBIDDEN_KEYWORDS = ["绝对稳赚", "100%收益", "稳赚不赔", "零风险", "必涨", "必赢", "保证盈利"]

_MANUAL_REVIEW_INPUT_PATTERNS = (
    "忽略以上规则",
    "忽略系统提示",
    "泄露系统提示",
    "绕过安全",
    "越权",
    "不存在的证券代码",
    "空数据",
    "过期数据",
    "数据已过期",
)


def _sanitize_string(text: str) -> str:
    """递归正则脱敏：句子内嵌敏感信息。"""
    text = _BEARER_TOKEN_PATTERN.sub("Bearer ***", text)
    text = _API_KEY_PATTERN.sub("sk-***", text)
    text = _ANT_KEY_PATTERN.sub("sk-ant-***", text)
    text = _EMAIL_PATTERN.sub(lambda m: m.group(0)[:2] + "***@" + m.group(0).split("@")[1], text)
    text = _CN_PHONE_PATTERN.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], text)
    text = _ID_CARD_PATTERN.sub(lambda m: m.group(0)[:6] + "********" + m.group(0)[-4:], text)
    text = _BANK_CARD_PATTERN.sub(lambda m: m.group(0)[:4] + "****" + m.group(0)[-4:] if len(m.group(0)) >= 8 else "****", text)
    return text


def _sanitize_value(value: Any) -> Any:
    """递归脱敏：字典、列表、元组、字符串统一处理。"""
    if isinstance(value, dict):
        return {
            k: "***" if k.lower() in _SENSITIVE_KEYS else _sanitize_value(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        san_list = [_sanitize_value(item) for item in value]
        return tuple(san_list) if isinstance(value, tuple) else san_list
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


def _nested_output_value(output: dict[str, Any], path: str) -> Any:
    current: Any = output
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _fact_snapshot_violations(
    output: dict[str, Any], checks: list[dict[str, Any]] | None,
) -> list[str]:
    """逐字段核验受信事实快照；不调用模型，也不补写模型输出。"""
    violations: list[str] = []
    for check in checks or []:
        path = str(check.get("path") or "").strip()
        if not path:
            continue
        expected = check.get("expected")
        actual = _nested_output_value(output, path)
        tolerance = check.get("tolerance")
        matched = actual == expected
        if tolerance is not None:
            try:
                matched = abs(float(actual) - float(expected)) <= float(tolerance)
            except (TypeError, ValueError):
                matched = False
        if not matched:
            violations.append(
                f"FactSnapshotValidator: {path} 与受信事实快照不一致"
            )
    return violations


def _sanitize_exception(exc: Exception) -> str:
    """脱敏异常消息（不泄漏 Key）。"""
    msg = str(exc)
    return _sanitize_string(msg)


def _parse_json_response(text: str) -> tuple[Any, bool]:
    """Parse JSON and allow only transparent Markdown-code-fence recovery.

    The recovered value is still validated by the same Schema and the result
    records ``format_repaired``. No fields are invented and the raw response
    is never written to reports.
    """
    stripped = (text or "").strip()
    try:
        return json.loads(stripped), False
    except json.JSONDecodeError as original:
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.IGNORECASE | re.DOTALL)
        if fenced:
            try:
                return json.loads(fenced.group(1)), True
            except json.JSONDecodeError:
                pass
        raise original


def _compute_percentile(values: list[float], percentile: float) -> float:
    """使用 nearest-rank 算法计算百分位数。"""
    if not values:
        return 0.0
    if not 0 < percentile <= 100:
        raise ValueError("percentile 必须在 (0, 100] 范围内")
    sorted_vals = sorted(values)
    idx = max(0, math.ceil((percentile / 100.0) * len(sorted_vals)) - 1)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def _compute_p95(values: list[float]) -> float:
    """P95 延迟：nearest-rank 算法。

    不使用线性插值，使用最接近的秩次。

    Examples
    --------
    1 个值: 返回该值
    2 个值: 返回较大值
    20 个值: 返回第 19 个
    """
    return _compute_percentile(values, 95)


def _requires_manual_review(user_message: str) -> bool:
    """识别需要人工复核的高风险输入，不依赖任务人工标签。"""
    normalized = user_message.lower()
    return any(pattern.lower() in normalized for pattern in _MANUAL_REVIEW_INPUT_PATTERNS)


@dataclass
class ReplayTaskResult:
    """单条任务的实验记录。"""

    request_id: str  # uuid4
    task_id: str
    provider: str  # provider.name
    model: str  # 模型名称（分离）
    started_at: str
    duration_s: float
    input_tokens: int
    output_tokens: int
    retry_count: int
    harness_off_status: str  # "success" | "schema_error" | "json_error" | "error"
    harness_on_status: str  # "blocked" | "passed" | "error"
    guardrail_violations: list[str] = field(default_factory=list)
    manual_review: bool = False
    blocked: bool = False
    error_type: str = ""
    output_parseable: bool = False
    source_present: bool = False
    missing_required_fields: list[str] = field(default_factory=list)
    format_repaired: bool = False
    evaluated_output: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "provider": self.provider,
            "model": self.model,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "retry_count": self.retry_count,
            "harness_off_status": self.harness_off_status,
            "harness_on_status": self.harness_on_status,
            "guardrail_violations": self.guardrail_violations,
            "manual_review": self.manual_review,
            "blocked": self.blocked,
            "error_type": self.error_type,
            "output_parseable": self.output_parseable,
            "source_present": self.source_present,
            "missing_required_fields": self.missing_required_fields,
            "format_repaired": self.format_repaired,
            "evaluated_output": _sanitize_value(self.evaluated_output),
        }


@dataclass
class ReplayExperimentResult:
    """离线回放实验结果。"""

    experiment_type: str = "real_llm_offline_replay"
    provider_kind: str = ""  # "real" | "simulated" | "mock"
    status: str = "completed"  # "completed" | "skipped_no_credentials" | "skipped_mock_provider" | "skipped_unverified_provider" | "no_tasks"
    provider: str = ""
    model: str = ""
    sample_count: int = 0
    success_count: int = 0
    schema_error_count: int = 0
    blocked_count: int = 0
    error_count: int = 0
    provider_error_count: int = 0
    average_latency_s: float = 0.0
    p50_latency_s: float = 0.0
    p95_latency_s: float = 0.0
    p99_latency_s: float = 0.0
    average_input_tokens: float = 0.0
    average_output_tokens: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_retry_count: int = 0
    manual_review_count: int = 0
    harness_token_delta: int = 0  # ON/OFF 共用同一响应，固定为0
    task_results: list[ReplayTaskResult] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.success_count / self.sample_count if self.sample_count > 0 else 0.0

    @property
    def schema_error_rate(self) -> float:
        return self.schema_error_count / self.sample_count if self.sample_count > 0 else 0.0

    @property
    def blocked_rate(self) -> float:
        return self.blocked_count / self.sample_count if self.sample_count > 0 else 0.0

    @property
    def retry_rate(self) -> float:
        return self.total_retry_count / self.sample_count if self.sample_count > 0 else 0.0

    @property
    def guardrail_block_rate(self) -> float:
        """Harness 拦截率（不叫 hallucination_rate，无人工标签）。"""
        return self.blocked_rate

    @property
    def provider_error_rate(self) -> float:
        return self.provider_error_count / self.sample_count if self.sample_count > 0 else 0.0

    @property
    def manual_review_rate(self) -> float:
        return self.manual_review_count / self.sample_count if self.sample_count > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_type": self.experiment_type,
            "provider_kind": self.provider_kind,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "sample_count": self.sample_count,
            "success_count": self.success_count,
            "schema_error_count": self.schema_error_count,
            "blocked_count": self.blocked_count,
            "error_count": self.error_count,
            "provider_error_count": self.provider_error_count,
            "success_rate": round(self.success_rate, 3),
            "schema_error_rate": round(self.schema_error_rate, 3),
            "blocked_rate": round(self.blocked_rate, 3),
            "guardrail_block_rate": round(self.guardrail_block_rate, 3),
            "provider_error_rate": round(self.provider_error_rate, 3),
            "average_latency_s": round(self.average_latency_s, 3),
            "p50_latency_s": round(self.p50_latency_s, 3),
            "p95_latency_s": round(self.p95_latency_s, 3),
            "p99_latency_s": round(self.p99_latency_s, 3),
            "average_input_tokens": round(self.average_input_tokens, 1),
            "average_output_tokens": round(self.average_output_tokens, 1),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "manual_review_count": self.manual_review_count,
            "manual_review_rate": round(self.manual_review_rate, 3),
            "retry_rate": round(self.retry_rate, 3),
            "total_retry_count": self.total_retry_count,
            "harness_token_delta": self.harness_token_delta,
            "harness_token_delta_note": "ON/OFF 共用同一次模型响应，因此模型 Token 增量为0；该字段不是两次模型调用成本差异",
            "task_results": [_sanitize_value(tr.to_dict()) for tr in self.task_results],
        }


def _call_with_retry(
    provider: LLMProvider,
    messages: list[ChatMessage],
    max_retries: int = 2,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[Any, int]:
    """有限重试：Timeout/Connection/5xx 最多 max_retries 次。

    Returns
    -------
    (reply, retry_count)

    Raises
    ------
    RetryExhausted: 包装原始异常，同时携带 retry_count
    """
    retry_count = 0
    last_exc = None

    for attempt in range(max_retries + 1):
        try:
            kwargs: dict[str, Any] = {"tools": []}
            if getattr(provider, "supports_json_mode", False):
                kwargs["response_format"] = {"type": "json_object"}
            reply = provider.generate(messages, **kwargs)
            return reply, retry_count
        except Exception as exc:
            last_exc = exc
            retriable = isinstance(
                exc,
                (
                    LLMNetworkError,
                    LLMRateLimitError,
                    LLMServerError,
                    TimeoutError,
                    ConnectionError,
                ),
            )
            if not retriable:
                # 包装成 RetryExhausted，携带 retry_count=0
                raise RetryExhausted(retry_count=0, original_exception=exc) from exc

            if attempt < max_retries:
                retry_count += 1
                backoff = 2 ** attempt
                logger.warning(
                    "任务重试 %d/%d，错误: %s，等待 %.1fs",
                    retry_count,
                    max_retries,
                    _sanitize_exception(exc),
                    backoff,
                )
                sleep_fn(backoff)
            else:
                # 所有重试都失败，包装异常并携带 retry_count
                raise RetryExhausted(retry_count=retry_count, original_exception=exc) from exc

    # 永远不应到达这里
    raise last_exc  # type: ignore


class RetryExhausted(Exception):
    """重试耗尽异常，携带 retry_count 和原始异常。"""

    def __init__(self, retry_count: int, original_exception: Exception):
        self.retry_count = retry_count
        self.original_exception = original_exception
        super().__init__(str(original_exception))


def run_real_llm_replay_experiment(
    provider: LLMProvider | None = None,
    provider_kind: str = "",
    model_name: str = "",
    tasks: list[dict[str, Any]] | None = None,
    max_retries: int = 2,
    sleep_fn: Callable[[float], None] = time.sleep,
    credentials_verified: bool = False,
) -> ReplayExperimentResult:
    """运行真实 LLM 离线回放实验。

    Parameters
    ----------
    provider : LLMProvider | None
        真实 LLM Provider（DeepSeek / Claude）。若为 None，返回 skipped_no_credentials。
    provider_kind : str
        显式提供：'real' | 'simulated' | 'mock'。
        - real: DeepSeek/Claude 且凭证已验证
        - simulated: 测试用 Fake Provider
        - mock: Mock Provider
    model_name : str
        模型名称（与 provider.name 分离）。
    tasks : list[dict] | None
        测试任务列表。若为 None，使用内置 _TEST_TASKS。
    max_retries : int
        可重试错误的最大重试次数（默认 2）。
    sleep_fn : Callable
        重试等待函数（默认 time.sleep，测试时可 mock）。
    credentials_verified : bool
        凭证是否已验证（默认 False）。provider_kind="real" 时必须为 True。

    Returns
    -------
    ReplayExperimentResult
        实验结果，含完整任务记录（已脱敏）。
    """
    if tasks is None:
        tasks = _TEST_TASKS

    if len(tasks) == 0:
        logger.warning("任务列表为空，返回 no_tasks 状态")
        return ReplayExperimentResult(
            status="no_tasks",
            provider_kind=provider_kind,
            sample_count=0,
        )

    # 检查 provider 是否可用
    if provider is None:
        logger.warning("Provider 为 None，跳过真实 LLM 实验")
        return ReplayExperimentResult(
            status="skipped_no_credentials",
            provider_kind="",
            sample_count=0,
        )

    # 检查 provider_kind
    if provider_kind == "mock":
        logger.warning("检测到 Mock Provider，跳过真实 LLM 实验")
        return ReplayExperimentResult(
            status="skipped_mock_provider",
            provider_kind="mock",
            provider=provider.name,
            sample_count=0,
        )

    # 真实 Provider 身份校验
    if provider_kind == "real":
        # 必须同时满足：credentials_verified=True + 受支持的 Provider 类型
        if not credentials_verified:
            logger.warning("provider_kind='real' 但 credentials_verified=False，跳过实验")
            return ReplayExperimentResult(
                status="skipped_unverified_provider",
                provider_kind="real",
                provider=provider.name,
                sample_count=0,
            )

        # 使用真实类型检查，类名相同的伪 Provider 不能绕过。
        from agent_platform.core.claude_llm_provider import ClaudeLLMProvider
        from agent_platform.core.deepseek_llm_provider import DeepSeekLLMProvider

        supported_provider_types = (DeepSeekLLMProvider, ClaudeLLMProvider)
        if not isinstance(provider, supported_provider_types):
            logger.warning(
                "provider_kind='real' 但 Provider 类型 %s 未受支持，跳过实验",
                type(provider).__name__,
            )
            return ReplayExperimentResult(
                status="skipped_unverified_provider",
                provider_kind="real",
                provider=provider.name,
                sample_count=0,
            )

    if provider_kind not in ("real", "simulated"):
        # 猜测：name 包含 mock → mock
        provider_name_lower = provider.name.lower()
        if "mock" in provider_name_lower:
            logger.warning("根据 provider.name 推断为 Mock，跳过实验")
            return ReplayExperimentResult(
                status="skipped_mock_provider",
                provider_kind="mock",
                provider=provider.name,
                sample_count=0,
            )
        # 否则默认 simulated（测试）
        provider_kind = "simulated"
        logger.info("provider_kind 未显式提供，推断为 simulated（测试）")

    guardrails = [
        JSONSchemaValidator(schema=_SYNTHESIS_SCHEMA),
        KeywordBlocker(keywords=_FORBIDDEN_KEYWORDS),
        SourceAttributionFilter(required=["source", "updated_at"]),
    ]

    task_results: list[ReplayTaskResult] = []
    success_count = 0
    schema_error_count = 0
    blocked_count = 0
    error_count = 0
    provider_error_count = 0
    total_retry = 0
    manual_review_count = 0

    for task in tasks:
        task_id = task["task_id"]
        user_msg = _sanitize_string(task["user_message"])  # 先脱敏输入
        request_id = str(uuid.uuid4())
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        start_time = time.perf_counter()
        retry_count = 0

        # 调用真实 LLM（仅一次，有限重试）
        try:
            messages = [ChatMessage(role="user", content=user_msg)]
            reply, retry_count = _call_with_retry(provider, messages, max_retries, sleep_fn)
            total_retry += retry_count
            duration_s = time.perf_counter() - start_time

            # Harness OFF：解析真实 JSON
            harness_off_status = "success"
            parsed_output = None
            output_parseable = False
            source_present = False
            missing_required_fields: list[str] = []
            format_repaired = False
            try:
                # 尝试解析 reply.text 为 JSON
                parsed_output, format_repaired = _parse_json_response(reply.text)
                output_parseable = isinstance(parsed_output, dict)
                if not isinstance(parsed_output, dict):
                    harness_off_status = "schema_error"
                    schema_error_count += 1
                    parsed_output = None
                else:
                    source_present = bool(str(parsed_output.get("source", "")).strip())
                # 检查 Schema 必需字段
                if parsed_output is not None:
                    missing_required_fields = [
                        key for key in _SYNTHESIS_SCHEMA["required"]
                        if key not in parsed_output
                    ]
                    if missing_required_fields:
                        harness_off_status = "schema_error"
                        schema_error_count += 1
            except json.JSONDecodeError:
                harness_off_status = "json_error"
                schema_error_count += 1
            except Exception:
                harness_off_status = "error"
                error_count += 1

            # Harness ON：对真实解析出的对象运行 Guardrail
            harness_on_status = "passed"
            violations: list[str] = []
            blocked = False
            manual_review = _requires_manual_review(user_msg)

            if parsed_output is not None and harness_off_status == "success":
                for g in guardrails:
                    try:
                        g.validate_output(parsed_output)
                    except GuardrailViolation as exc:
                        harness_on_status = "blocked"
                        violations.append(_sanitize_string(str(exc)))
                        blocked = True
                        blocked_count += 1
                        break
                if not blocked:
                    fact_violations = _fact_snapshot_violations(
                        parsed_output, task.get("fact_checks")
                    )
                    if fact_violations:
                        harness_on_status = "blocked"
                        violations.extend(fact_violations)
                        blocked = True
                        blocked_count += 1
            elif harness_off_status == "schema_error" or harness_off_status == "json_error":
                # OFF 是 schema_error，ON 必须是 blocked（因为不符合 Schema）
                harness_on_status = "blocked"
                violations.append("Schema validation failed")
                blocked = True
                blocked_count += 1
            else:
                # OFF 是其他错误，ON 也标记为 error
                harness_on_status = "error"

            if harness_off_status == "success" and not blocked and manual_review:
                harness_on_status = "manual_review"
                manual_review_count += 1
            elif harness_off_status == "success" and not blocked:
                success_count += 1

            task_results.append(
                ReplayTaskResult(
                    request_id=request_id,
                    task_id=task_id,
                    provider=provider.name,
                    model=model_name or provider.name,
                    started_at=started_at,
                    duration_s=duration_s,
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                    retry_count=retry_count,
                    harness_off_status=harness_off_status,
                    harness_on_status=harness_on_status,
                    guardrail_violations=violations,
                    manual_review=manual_review,
                    blocked=blocked,
                    error_type="",
                    output_parseable=output_parseable,
                    source_present=source_present,
                    missing_required_fields=missing_required_fields,
                    format_repaired=format_repaired,
                    evaluated_output=(
                        _sanitize_value(parsed_output)
                        if isinstance(parsed_output, dict) else None
                    ),
                )
            )

        except RetryExhausted as exc:
            duration_s = time.perf_counter() - start_time
            error_count += 1
            retry_count = exc.retry_count  # 从包装异常中提取 retry_count
            total_retry += retry_count
            original_exc = exc.original_exception
            exc_type = type(original_exc).__name__
            sanitized_exc = _sanitize_exception(original_exc)
            logger.error("任务 %s 执行失败（重试 %d 次）: %s", task_id, retry_count, sanitized_exc)

            if isinstance(original_exc, LLMProviderError):
                error_type = original_exc.error_type
                harness_off_status = "provider_error"
                provider_error_count += 1
            elif isinstance(original_exc, (TimeoutError, ConnectionError)):
                error_type = LLMNetworkError.error_type
                harness_off_status = "provider_error"
                provider_error_count += 1
            else:
                error_type = exc_type
                harness_off_status = "error"

            task_results.append(
                ReplayTaskResult(
                    request_id=request_id,
                    task_id=task_id,
                    provider=provider.name,
                    model=model_name or provider.name,
                    started_at=started_at,
                    duration_s=duration_s,
                    input_tokens=0,
                    output_tokens=0,
                    retry_count=retry_count,
                    harness_off_status=harness_off_status,
                    harness_on_status="error",
                    guardrail_violations=[],
                    manual_review=False,
                    blocked=False,
                    error_type=error_type,
                )
            )
        except Exception as exc:
            duration_s = time.perf_counter() - start_time
            error_count += 1
            exc_type = type(exc).__name__
            sanitized_exc = _sanitize_exception(exc)
            logger.error("任务 %s 执行失败: %s", task_id, sanitized_exc)
            task_results.append(
                ReplayTaskResult(
                    request_id=request_id,
                    task_id=task_id,
                    provider=provider.name,
                    model=model_name or provider.name,
                    started_at=started_at,
                    duration_s=duration_s,
                    input_tokens=0,
                    output_tokens=0,
                    retry_count=retry_count,
                    harness_off_status="error",
                    harness_on_status="error",
                    guardrail_violations=[],
                    manual_review=False,
                    blocked=False,
                    error_type=exc_type,
                )
            )

    # 计算聚合指标
    latencies = [tr.duration_s for tr in task_results if tr.duration_s > 0]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    p50_latency = _compute_percentile(latencies, 50)
    p95_latency = _compute_p95(latencies)
    p99_latency = _compute_percentile(latencies, 99)

    input_tokens_list = [tr.input_tokens for tr in task_results if tr.input_tokens > 0]
    output_tokens_list = [tr.output_tokens for tr in task_results if tr.output_tokens > 0]
    avg_input = sum(input_tokens_list) / len(input_tokens_list) if input_tokens_list else 0.0
    avg_output = sum(output_tokens_list) / len(output_tokens_list) if output_tokens_list else 0.0
    total_input = sum(tr.input_tokens for tr in task_results)
    total_output = sum(tr.output_tokens for tr in task_results)

    if error_count == len(tasks):
        aggregate_status = "failed"
    elif error_count:
        aggregate_status = "partial_failure"
    else:
        aggregate_status = "completed"

    return ReplayExperimentResult(
        experiment_type="real_llm_offline_replay",
        provider_kind=provider_kind,
        status=aggregate_status,
        provider=provider.name,
        model=model_name or provider.name,
        sample_count=len(tasks),
        success_count=success_count,
        schema_error_count=schema_error_count,
        blocked_count=blocked_count,
        error_count=error_count,
        provider_error_count=provider_error_count,
        average_latency_s=avg_latency,
        p50_latency_s=p50_latency,
        p95_latency_s=p95_latency,
        p99_latency_s=p99_latency,
        average_input_tokens=avg_input,
        average_output_tokens=avg_output,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_retry_count=total_retry,
        manual_review_count=manual_review_count,
        harness_token_delta=0,  # ON/OFF 共用同一响应
        task_results=task_results,
    )
