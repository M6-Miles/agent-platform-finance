"""
Harness 有效性实验测试
"""
from __future__ import annotations
from agent_platform.core.harness_experiment import (
    run_harness_effectiveness_experiment,
    EXPERIMENT_SAMPLES,
)


def test_experiment_runs_with_default_samples():
    """使用内置样本运行实验。"""
    result = run_harness_effectiveness_experiment()
    assert result.total_samples == len(EXPERIMENT_SAMPLES)
    assert result.hallucination_count > 0
    assert result.normal_count > 0


def test_harness_blocks_all_hallucinations():
    """Harness 应拦截所有已知幻觉。"""
    result = run_harness_effectiveness_experiment()
    assert result.hallucination_blocked_rate == 1.0, (
        f"幻觉拦截率仅 {result.hallucination_blocked_rate:.0%}，"
        f"漏过了 {result.hallucination_count - result.harness_blocked_hallucinations} 条幻觉"
    )


def test_harness_zero_false_positives():
    """Harness 不误拦截正常输出。"""
    result = run_harness_effectiveness_experiment()
    assert result.false_positive_rate == 0.0, (
        f"误拦截率 {result.false_positive_rate:.0%}，误拦了 {result.harness_blocked_normals} 条正常输出"
    )


def test_no_harness_passes_all_hallucinations():
    """无 Harness 时幻觉全部通过（通过率 = 100%）。"""
    result = run_harness_effectiveness_experiment()
    assert result.pass_rate_no_harness == 1.0


def test_experiment_result_markdown():
    """Markdown 报告包含关键字段。"""
    result = run_harness_effectiveness_experiment()
    md = result.to_markdown()
    assert "Harness 有效性实验报告" in md
    assert "幻觉拦截率" in md or "幻觉通过率" in md
    assert "100%" in md  # 无 Harness 时幻觉通过率
    assert "模拟无效下游调用数" in md
    assert "端到端正确处理率" in md


def test_experiment_quantifies_invalid_calls_and_e2e_rate():
    result = run_harness_effectiveness_experiment()

    assert result.invalid_calls_no_harness == result.hallucination_count
    assert result.invalid_calls_with_harness == 0
    assert result.invalid_call_reduction_rate == 1.0
    assert result.e2e_correct_rate_no_harness == result.normal_count / result.total_samples
    assert result.e2e_correct_rate_with_harness == 1.0


def test_experiment_custom_samples_all_normal():
    """全部正常样本 → 幻觉拦截率 = 0%（无幻觉可拦），误拦截率 = 0%。"""
    normal_samples = [
        {
            "label": "正常样本1",
            "is_hallucination": False,
            "output": {
                "signal": "buy",
                "confidence": 0.8,
                "source": "synthesis_agent",
                "updated_at": "2024-01-01T00:00:00Z",
                "disclaimer": "仅供研究参考，不构成投资建议",
            },
        }
    ]
    result = run_harness_effectiveness_experiment(normal_samples)
    assert result.hallucination_count == 0
    assert result.harness_blocked_normals == 0
    assert result.false_positive_rate == 0.0


def test_experiment_custom_samples_all_hallucinations():
    """全部幻觉样本 → 幻觉拦截率 应 = 100%。"""
    hallucination_samples = [
        {
            "label": "缺 source",
            "is_hallucination": True,
            "output": {
                "signal": "buy",
                "confidence": 0.8,
                "updated_at": "2024-01-01T00:00:00Z",
                "disclaimer": "仅供研究参考，不构成投资建议",
            },
        },
        {
            "label": "含违禁词",
            "is_hallucination": True,
            "output": {
                "signal": "buy",
                "confidence": 0.9,
                "source": "agent",
                "updated_at": "2024-01-01T00:00:00Z",
                "disclaimer": "绝对稳赚不赔",
            },
        },
    ]
    result = run_harness_effectiveness_experiment(hallucination_samples)
    assert result.hallucination_count == 2
    assert result.hallucination_blocked_rate == 1.0
