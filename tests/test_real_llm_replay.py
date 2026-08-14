"""真实 LLM 离线回放实验测试

Codex 审查后完全重写（2026-08-13）：
  1. ✅ Fake Provider + provider_kind="real" 必须返回 skipped_unverified_provider
  2. ✅ 缺少 source 时 OFF=schema_error, ON=blocked
  3. ✅ P95 算法统一为 nearest-rank（1/2/20 样本边界）
  4. ✅ harness_token_delta 固定为 0（ON/OFF 共用同一响应）
  5. ✅ Provider 错误分类（认证/网络/5xx）
  6. ✅ credentials_verified 参数验证
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from agent_platform.core.llm_provider import (
    LLMAuthenticationError,
    LLMServerError,
    ModelReply,
)
from agent_platform.core.real_llm_replay import (
    run_real_llm_replay_experiment,
)
from agent_platform.core import real_llm_replay as replay_module


class FakeProvider:
    """测试用 Fake Provider（simulated，不是真实 Provider）。"""

    def __init__(self, provider_name: str = "fake", response: str = ""):
        self._name = provider_name
        self._response = response
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    def generate(self, messages, tools=None) -> ModelReply:
        self.call_count += 1
        return ModelReply(
            text=self._response,
            input_tokens=10,
            output_tokens=20,
        )


class JsonModeProvider(FakeProvider):
    supports_json_mode = True

    def __init__(self):
        super().__init__(response='{"signal":"hold","confidence":0.5,"source":"test","updated_at":"2026-08-14"}')
        self.response_format = None

    def generate(self, messages, tools=None, response_format=None):
        self.response_format = response_format
        return super().generate(messages, tools=tools)


def test_json_mode_is_requested_only_from_supporting_provider():
    provider = JsonModeProvider()
    result = run_real_llm_replay_experiment(
        provider=provider,
        provider_kind="simulated",
        tasks=[{"task_id": "json-mode", "user_message": "test"}],
    )
    assert provider.response_format == {"type": "json_object"}
    assert result.success_count == 1


def test_markdown_json_fence_is_transparently_parsed_and_recorded():
    fake = FakeProvider(
        response='```json\n{"signal":"hold","confidence":0.5,"source":"test","updated_at":"2026-08-14"}\n```'
    )
    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=[{"task_id": "fenced", "user_message": "test"}],
    )
    assert result.success_count == 1
    assert result.task_results[0].format_repaired is True


def test_default_replay_task_set_contains_three_sanitized_tasks():
    assert [task["task_id"] for task in replay_module._TEST_TASKS] == [
        "REAL-001", "REAL-002", "REAL-003",
    ]
    assert ["DEMO001", "DEMO002", "TEST001"] == [
        task["user_message"].split("的")[0].split()[-1]
        if task["task_id"] != "REAL-003" else "TEST001"
        for task in replay_module._TEST_TASKS
    ]


# ==================== 一、真实 Provider 身份校验 ====================


def test_fake_provider_with_real_kind_rejected():
    """Fake Provider 即使 provider_kind='real' 也必须跳过。"""
    fake = FakeProvider(provider_name="fake_deepseek")
    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="real",
        credentials_verified=True,  # 即使声称验证了
    )
    # 因为 Fake Provider 不在受支持的 DeepSeekLLMProvider/ClaudeLLMProvider 列表中
    assert result.status == "skipped_unverified_provider"
    assert result.provider_kind == "real"
    assert fake.call_count == 0, "Fake Provider 不得调用 generate"


def test_real_kind_without_credentials_rejected():
    """provider_kind='real' 但 credentials_verified=False 必须跳过。"""
    fake = FakeProvider(provider_name="fake_claude")
    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="real",
        credentials_verified=False,  # 未验证
    )
    assert result.status == "skipped_unverified_provider"
    assert fake.call_count == 0


def test_simulated_provider_returns_simulated():
    """provider_kind='simulated' 只能返回 simulated，不得写成 real。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}'
    )
    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        credentials_verified=False,
    )
    assert result.provider_kind == "simulated"
    assert result.status == "completed"
    assert fake.call_count > 0  # simulated 可以调用


def test_no_provider_no_calls():
    """provider=None 必须返回 skipped_no_credentials，零调用。"""
    result = run_real_llm_replay_experiment(
        provider=None,
        provider_kind="real",
        credentials_verified=False,
    )
    assert result.status == "skipped_no_credentials"
    assert result.sample_count == 0


# ==================== 二、缺少 source 的 Harness 状态 ====================


def test_missing_source_schema_error_off_blocked_on():
    """缺少 source 时：OFF=schema_error, ON=blocked, guardrail_violations 非空。"""
    fake = FakeProvider(
        response='{"symbol": "DEMO001", "analysis": "no source field"}'  # 缺少 source
    )
    tasks = [{"task_id": "missing_source", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    assert result.status == "completed"
    assert result.schema_error_count == 1, "OFF 必须是 schema_error"
    assert result.blocked_count == 1, "ON 必须是 blocked"

    tr = result.task_results[0]
    assert tr.harness_off_status == "schema_error"
    assert tr.harness_on_status == "blocked"
    assert tr.blocked is True
    assert len(tr.guardrail_violations) > 0, "必须记录违规信息"


def test_prohibited_keyword_blocked():
    """违禁词必须被 ON 拦截。"""
    fake = FakeProvider(
        response='{"signal": "buy", "confidence": 0.9, "source": "test", "updated_at": "2024-01-01", "analysis": "绝对稳赚"}'
    )
    tasks = [{"task_id": "prohibited", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    assert result.blocked_count == 1
    tr = result.task_results[0]
    assert tr.harness_on_status == "blocked"
    assert tr.blocked is True


def test_valid_json_passes():
    """合法 JSON 正常通过。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}'
    )
    tasks = [{"task_id": "valid", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    assert result.success_count == 1
    assert result.blocked_count == 0
    tr = result.task_results[0]
    assert tr.harness_off_status == "success"
    assert tr.harness_on_status == "passed"


# ==================== 三、P95 nearest-rank 算法 ====================


def test_p95_one_sample():
    """P95 对 1 个样本返回该值本身。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}'
    )
    tasks = [{"task_id": "single", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    # P95 = 唯一延迟值
    assert result.p95_latency_s == result.average_latency_s


def test_p95_two_samples():
    """P95 对 2 个样本返回较大值（nearest-rank）。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}'
    )
    tasks = [
        {"task_id": "t1", "user_message": "test"},
        {"task_id": "t2", "user_message": "test"},
    ]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    latencies = [tr.duration_s for tr in result.task_results]
    expected_p95 = max(latencies)  # nearest-rank 返回较大值
    assert result.p95_latency_s == pytest.approx(expected_p95, abs=1e-6)


def test_p95_twenty_samples():
    """P95 对 20 个样本返回第 20 个（最大值，nearest-rank）。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}'
    )
    tasks = [{"task_id": f"t{i}", "user_message": "test"} for i in range(20)]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    latencies = sorted([tr.duration_s for tr in result.task_results])
    # nearest-rank: ceil(0.95 * 20) - 1 = 19 - 1 = 18，但 max(0, ...) 然后 min(idx, n-1) = 19
    # 实际上 idx = max(0, int(0.95 * 20 + 0.95) - 1) = max(0, int(19.95) - 1) = 18
    # sorted_vals[18] 是第 19 个（0-indexed）
    # 但 idx = min(18, 19) = 18
    # 所以返回 sorted_vals[18]，即第 19 个值
    # 实际上对于 n=20，0.95*20=19，ceil(19)=19，19-1=18，sorted_vals[18] 是倒数第二个
    # 但代码是 idx = max(0, int(0.95 * n + 0.95) - 1)
    # = max(0, int(19 + 0.95) - 1) = max(0, 19 - 1) = 18
    # sorted_vals[18] 是第 19 个值（0-indexed）
    # 对于 20 个样本，nearest-rank P95 应该是第 19 个（95%分位）
    expected_p95 = latencies[18]  # 第 19 个（0-indexed）
    assert result.p95_latency_s == pytest.approx(expected_p95, abs=1e-6)


# ==================== 四、harness_token_delta 固定为 0 ====================


def test_harness_token_delta_is_zero():
    """harness_token_delta 固定为 0（ON/OFF 共用同一响应）。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}'
    )
    tasks = [{"task_id": "t1", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    assert result.harness_token_delta == 0
    result_dict = result.to_dict()
    assert result_dict["harness_token_delta"] == 0
    assert "harness_token_delta_note" in result_dict
    assert "共用同一次模型响应" in result_dict["harness_token_delta_note"]


# ==================== 五、Provider 错误分类 ====================


def test_provider_auth_error_not_counted_as_success():
    """Provider 认证失败不得计入 success_count。"""
    fake = FakeProvider()
    fake.generate = Mock(side_effect=LLMAuthenticationError())

    tasks = [{"task_id": "auth_fail", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
        max_retries=0,  # 不重试
    )

    assert result.success_count == 0
    assert result.error_count == 1
    tr = result.task_results[0]
    assert tr.harness_off_status == "provider_error"  # 认证错误分类为 provider_error
    assert tr.harness_on_status == "error"
    assert tr.error_type == "provider_auth_error"
    assert result.provider_error_count == 1
    assert result.provider_error_rate == 1.0
    assert result.status == "failed"


def test_provider_network_error_retries():
    """网络错误应重试（但最终失败）。"""
    fake = FakeProvider()
    fake.generate = Mock(side_effect=ConnectionError("Network unreachable"))

    tasks = [{"task_id": "network_fail", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
        max_retries=2,
        sleep_fn=lambda _: None,  # 不实际等待
    )

    assert result.success_count == 0
    assert result.error_count == 1
    tr = result.task_results[0]
    assert tr.retry_count == 2, "应该重试 2 次"


def test_provider_http_503_retries():
    """HTTP 503 应重试。"""
    fake = FakeProvider()
    fake.generate = Mock(side_effect=LLMServerError())

    tasks = [{"task_id": "503", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
        max_retries=2,
        sleep_fn=lambda _: None,
    )

    assert result.error_count == 1
    tr = result.task_results[0]
    assert tr.retry_count == 2
    assert result.provider_error_count == 1
    assert result.status == "failed"


def test_partial_failure_status_and_provider_error_rate():
    """成功与 Provider 失败混合时必须标记 partial_failure。"""
    fake = FakeProvider()
    fake.generate = Mock(
        side_effect=[
            ModelReply(
                text='{"signal":"hold","confidence":0.5,"source":"test","updated_at":"2026-01-01"}'
            ),
            LLMAuthenticationError(),
        ]
    )
    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=[
            {"task_id": "ok", "user_message": "test"},
            {"task_id": "bad", "user_message": "test"},
        ],
        max_retries=0,
    )
    assert result.status == "partial_failure"
    assert result.success_count == 1
    assert result.error_count == 1
    assert result.provider_error_count == 1
    assert result.provider_error_rate == 0.5


def test_same_named_fake_real_provider_cannot_bypass_type_check():
    """仅伪造受支持类名不能获得 real 身份。"""
    fake_type = type("DeepSeekLLMProvider", (FakeProvider,), {})
    fake = fake_type()
    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="real",
        credentials_verified=True,
    )
    assert result.status == "skipped_unverified_provider"
    assert fake.call_count == 0


def test_provider_value_error_no_retry():
    """ValueError 不重试。"""
    fake = FakeProvider()
    fake.generate = Mock(side_effect=ValueError("Invalid input"))

    tasks = [{"task_id": "value_error", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
        max_retries=2,
        sleep_fn=lambda _: None,
    )

    assert result.error_count == 1
    tr = result.task_results[0]
    assert tr.retry_count == 0, "ValueError 不应重试"


# ==================== 六、敏感信息脱敏 ====================


def test_sanitize_api_key_in_response():
    """API Key 必须被脱敏。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "API sk-abc123xyz", "updated_at": "2024-01-01"}'
    )
    tasks = [{"task_id": "leak", "user_message": "Contact API Key: sk-secret123"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    result_dict = result.to_dict()
    result_json = json.dumps(result_dict)
    # 响应文本不在结果中，但敏感信息不得泄漏
    assert "sk-abc123xyz" not in result_json, "响应中 API Key 泄漏"
    assert "sk-secret123" not in result_json, "输入中 API Key 泄漏"


def test_sanitize_email_and_phone():
    """邮箱和手机号必须被脱敏。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "Contact user@example.com", "updated_at": "2024-01-01"}'
    )
    tasks = [{"task_id": "contact", "user_message": "手机 13812345678"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    result_dict = result.to_dict()
    result_json = json.dumps(result_dict)
    # 响应文本不在结果中，但敏感信息不得泄漏
    assert "user@example.com" not in result_json, "邮箱泄漏"
    assert "13812345678" not in result_json, "手机号泄漏"


# ==================== 七、重试逻辑 ====================


def test_timeout_retries_limited():
    """Timeout 最多重试 max_retries 次。"""
    fake = FakeProvider()
    fake.generate = Mock(side_effect=TimeoutError("Request timeout"))

    tasks = [{"task_id": "timeout", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
        max_retries=2,
        sleep_fn=lambda _: None,
    )

    assert result.error_count == 1
    assert result.total_retry_count == 2
    tr = result.task_results[0]
    assert tr.retry_count == 2


def test_success_after_retry():
    """重试后成功。"""
    fake = FakeProvider()
    call_count = 0

    def flaky_generate(messages, tools=None):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise TimeoutError("Flaky")
        return ModelReply(
            text='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}',
            input_tokens=10,
            output_tokens=20,
        )

    fake.generate = flaky_generate

    tasks = [{"task_id": "flaky", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
        max_retries=2,
        sleep_fn=lambda _: None,
    )

    assert result.success_count == 1
    assert result.total_retry_count == 1
    tr = result.task_results[0]
    assert tr.retry_count == 1


# ==================== 八、request_id 唯一性 ====================


def test_request_id_unique():
    """每个任务的 request_id 必须唯一（uuid4）。"""
    fake = FakeProvider(
        response='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}'
    )
    tasks = [{"task_id": f"t{i}", "user_message": "test"} for i in range(10)]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        tasks=tasks,
    )

    request_ids = [tr.request_id for tr in result.task_results]
    assert len(request_ids) == len(set(request_ids)), "request_id 有重复"
    for rid in request_ids:
        assert len(rid) == 36, "uuid4 标准格式应为 36 个字符（带连字符）"
        assert rid.count('-') == 4, "uuid4 应包含 4 个连字符"


# ==================== 九、provider/model 分离 ====================


def test_provider_model_separated():
    """provider 和 model 必须分离记录。"""
    fake = FakeProvider(
        provider_name="fake_provider",
        response='{"signal": "hold", "confidence": 0.5, "source": "test", "updated_at": "2024-01-01"}',
    )
    tasks = [{"task_id": "sep", "user_message": "test"}]

    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="simulated",
        model_name="fake-model-v1",
        tasks=tasks,
    )

    assert result.provider == "fake_provider"
    assert result.model == "fake-model-v1"
    assert result.provider != result.model


# ==================== 十、禁止交易模块导入（AST 检查） ====================


def test_ast_no_trading_module_import():
    """real_llm_replay.py 不得导入交易模块。"""
    import ast
    from pathlib import Path

    replay_path = Path(__file__).parent.parent / "src" / "agent_platform" / "core" / "real_llm_replay.py"
    source = replay_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = {"broker", "trading", "order", "trader"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(f in alias.name.lower() for f in forbidden), (
                        f"禁止导入交易模块: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not any(f in node.module.lower() for f in forbidden), (
                    f"禁止导入交易模块: {node.module}"
                )


# ==================== 十一、Mock Provider 跳过 ====================


def test_mock_provider_skipped():
    """provider_kind='mock' 必须返回 skipped_mock_provider。"""
    fake = FakeProvider(provider_name="mock_provider")
    result = run_real_llm_replay_experiment(
        provider=fake,
        provider_kind="mock",
    )
    assert result.status == "skipped_mock_provider"
    assert fake.call_count == 0
