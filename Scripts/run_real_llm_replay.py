"""真实 LLM Harness ON/OFF 离线回放实验 - 命令行入口

用法：
    python Scripts/run_real_llm_replay.py --provider deepseek --model deepseek-chat
    python Scripts/run_real_llm_replay.py --provider claude --model claude-sonnet-4-20250514
    python Scripts/run_real_llm_replay.py --provider mock  # 会跳过

环境变量：
    DEEPSEEK_API_KEY  - DeepSeek API Key
    ANTHROPIC_API_KEY - Anthropic API Key

输出：
    - 控制台显示实验结果摘要
    - 生成 docs/experiments/real_llm_replay_<timestamp>.json
    - 生成 docs/experiments/real_llm_replay_<timestamp>.md
    - 所有敏感信息已脱敏
    - 输出路径限制在项目目录内

修复（2026-08-13 Codex 审查后）：
    - 支持 --model 参数
    - 支持 --output-json 和 --output-md
    - 默认同时生成 JSON 和 Markdown
    - 路径解析并限制在项目目录内
    - 即使 skipped，也写诚实状态报告
    - Markdown 明确区分 Mock/simulated/real/production
    - provider_kind 显式传递（real/simulated/mock）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

# 确保能导入项目模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 仅在真实 LLM 可选依赖中提供
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)

from agent_platform.core.llm_provider import LLMProvider
from agent_platform.core.llm_evaluation_suite import (
    build_enterprise_100_tasks,
    evaluate_labeled_replay,
    load_task_suite,
)
from agent_platform.core.real_llm_replay import run_real_llm_replay_experiment


def _resolve_output_path(path_str: str, project_root: Path) -> Path:
    """解析输出路径，限制在项目目录内。"""
    path = Path(path_str).resolve()
    try:
        path.relative_to(project_root)
    except ValueError:
        raise ValueError(f"输出路径必须在项目目录内: {path_str}")
    return path


def _atomic_copy(source: Path, destination: Path) -> None:
    """同目录临时文件 + replace，避免读取方看到半写入报告。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def _atomic_write_text(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, destination)


def publish_canonical_reports(
    *, result: dict, provider: str, suite: str, json_path: Path, md_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> list[Path]:
    """发布语义明确的权威报告，且失败/冒烟不会覆盖企业评测。

    ``latest_attempt`` 是小型索引，记录最近一次尝试（包括无凭证跳过）；
    ``latest`` 只保存完整 100 条真实企业评测。3 条冒烟单独写 ``smoke_latest``。
    """
    output_dir = project_root / "docs" / "experiments"
    published: list[Path] = []

    def publish_copy(stem: str) -> None:
        for source, suffix in ((json_path, ".json"), (md_path, ".md")):
            destination = output_dir / f"real_llm_replay_{provider}_{stem}{suffix}"
            _atomic_copy(source, destination)
            published.append(destination)

    relative_json = json_path.resolve().relative_to(project_root.resolve()).as_posix()
    relative_md = md_path.resolve().relative_to(project_root.resolve()).as_posix()
    attempt = {
        "status": result.get("status"),
        "provider_kind": result.get("provider_kind"),
        "sample_count": int(result.get("sample_count") or 0),
        "suite": suite,
        "run_date": result.get("run_date"),
        "json_report": relative_json,
        "markdown_report": relative_md,
    }
    attempt_json = output_dir / f"real_llm_replay_{provider}_latest_attempt.json"
    attempt_md = output_dir / f"real_llm_replay_{provider}_latest_attempt.md"
    _atomic_write_text(attempt_json, json.dumps(attempt, ensure_ascii=False, indent=2) + "\n")
    _atomic_write_text(
        attempt_md,
        "# 最近一次真实 LLM 回放尝试\n\n"
        f"- 状态：{attempt['status']}\n"
        f"- 评测集：{suite}\n"
        f"- 样本数：{attempt['sample_count']}\n"
        f"- 详细报告：`{relative_md}`\n",
    )
    published.extend((attempt_json, attempt_md))
    completed_real = (
        result.get("status") == "completed"
        and result.get("provider_kind") == "real"
        and int(result.get("sample_count") or 0) > 0
    )
    if not completed_real:
        return published

    if suite == "enterprise100" and int(result.get("sample_count") or 0) == 100:
        latest_json = output_dir / f"real_llm_replay_{provider}_latest.json"
        latest_md = output_dir / f"real_llm_replay_{provider}_latest.md"
        evaluation = result.get("evaluation") or {}
        latest_summary = {
            "status": result.get("status"),
            "provider_kind": result.get("provider_kind"),
            "provider": result.get("provider"),
            "model": result.get("model"),
            "sample_count": result.get("sample_count"),
            "run_date": result.get("run_date"),
            "archive_json": relative_json,
            "archive_markdown": relative_md,
            "metrics": {
                key: evaluation.get(key)
                for key in (
                    "label_match_rate", "schema_compliance_rate",
                    "guardrail_precision", "guardrail_recall",
                    "normal_false_positive_rate", "fact_checked_count",
                    "hallucination_count", "hallucination_rate",
                    "hallucination_block_rate",
                    "invalid_downstream_calls_harness_off",
                    "invalid_downstream_calls_harness_on",
                    "invalid_downstream_call_reduction_rate",
                    "manual_review_rate", "latency_p50_s", "latency_p95_s",
                    "latency_p99_s",
                )
            },
        }
        _atomic_write_text(
            latest_json, json.dumps(latest_summary, ensure_ascii=False, indent=2) + "\n"
        )
        _atomic_copy(md_path, latest_md)
        published.extend((latest_json, latest_md))
    else:
        publish_copy("smoke_latest")
    return published


def create_provider(
    provider_type: str, model_name: str
) -> tuple[LLMProvider | None, str, str, bool]:
    """根据类型创建 Provider。

    Returns
    -------
    (provider, provider_kind, actual_model_name, credentials_verified)
        provider_kind: 'real' | 'simulated' | 'mock'
        credentials_verified: True 只当真实 API Key 存在且 Provider 创建成功
    """
    if provider_type == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key or api_key == "你的key":
            print("[WARN] 未配置 DEEPSEEK_API_KEY，跳过真实 LLM 实验")
            return None, "", "", False
        from agent_platform.core.deepseek_llm_provider import DeepSeekLLMProvider

        actual_model = model_name or "deepseek-chat"
        provider = DeepSeekLLMProvider(api_key=api_key, model=actual_model)
        return provider, "real", actual_model, True

    elif provider_type == "claude":
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            print("[WARN] 未配置 ANTHROPIC_API_KEY，跳过真实 LLM 实验")
            return None, "", "", False
        from agent_platform.core.claude_llm_provider import ClaudeLLMProvider

        actual_model = model_name or "claude-sonnet-4-20250514"
        provider = ClaudeLLMProvider(api_key=api_key, model=actual_model)
        return provider, "real", actual_model, True

    elif provider_type == "mock":
        print("[WARN] Mock Provider 不执行真实 LLM 实验")
        return None, "mock", "", False

    else:
        print(f"[ERROR] 未知 provider 类型: {provider_type}")
        return None, "", "", False


def generate_markdown_report(result: dict, output_path: Path) -> None:
    """生成 Markdown 报告。"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# 真实 LLM Harness ON/OFF 离线回放实验报告\n\n")
        f.write(f"**生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("---\n\n")

        f.write("## 实验概览\n\n")
        f.write(f"- **实验类型**: {result['experiment_type']}\n")
        f.write(f"- **Provider 类型**: {result['provider_kind']}\n")
        f.write(f"- **状态**: {result['status']}\n")
        f.write(f"- **Provider**: {result['provider']}\n")
        f.write(f"- **Model**: {result['model']}\n")
        f.write(f"- **样本数**: {result['sample_count']}\n\n")

        if result["status"] == "skipped_no_credentials":
            f.write("## ⚠️ 实验未执行\n\n")
            f.write("**原因**: 未配置 API Key\n\n")
            f.write("本次结果不代表真实模型效果，也不代表生产流量表现。\n\n")
            f.write("如需执行真实实验，请设置环境变量：\n")
            f.write("```bash\n")
            f.write("export DEEPSEEK_API_KEY=your_key  # DeepSeek\n")
            f.write("export ANTHROPIC_API_KEY=your_key # Claude\n")
            f.write("```\n\n")
            return

        if result["status"] == "skipped_mock_provider":
            f.write("## ⚠️ 实验未执行\n\n")
            f.write("**原因**: Mock Provider 不执行真实 LLM 实验\n\n")
            f.write("Mock Provider 用于固定评测集，不代表真实模型行为。\n\n")
            return

        if result["status"] == "no_tasks":
            f.write("## ⚠️ 实验未执行\n\n")
            f.write("**原因**: 任务列表为空\n\n")
            return

        f.write("## 聚合指标\n\n")
        f.write("| 指标 | 值 |\n")
        f.write("|------|----|\n")
        f.write(f"| 成功数 | {result['success_count']} |\n")
        f.write(f"| Schema 错误数 | {result['schema_error_count']} |\n")
        f.write(f"| 拦截数 | {result['blocked_count']} |\n")
        f.write(f"| 错误数 | {result['error_count']} |\n")
        f.write(f"| Provider 错误数 | {result['provider_error_count']} |\n")
        f.write(f"| 成功率 | {result['success_rate']:.1%} |\n")
        f.write(f"| Schema 错误率 | {result['schema_error_rate']:.1%} |\n")
        f.write(f"| Guardrail 拦截率 | {result['guardrail_block_rate']:.1%} |\n")
        f.write(f"| Provider 错误率 | {result['provider_error_rate']:.1%} |\n")
        f.write(f"| 平均延迟 | {result['average_latency_s']:.2f}s |\n")
        f.write(f"| P50 延迟 | {result['p50_latency_s']:.2f}s |\n")
        f.write(f"| P95 延迟 | {result['p95_latency_s']:.2f}s |\n")
        f.write(f"| P99 延迟 | {result['p99_latency_s']:.2f}s |\n")
        f.write(f"| 平均输入 Token | {result['average_input_tokens']:.0f} |\n")
        f.write(f"| 平均输出 Token | {result['average_output_tokens']:.0f} |\n")
        f.write(f"| 总输入 Token | {result['total_input_tokens']} |\n")
        f.write(f"| 总输出 Token | {result['total_output_tokens']} |\n")
        f.write(f"| 人工审核率 | {result['manual_review_rate']:.1%} |\n")
        f.write(f"| 总重试次数 | {result['total_retry_count']} |\n")
        f.write(f"| 重试率 | {result['retry_rate']:.1%} |\n")
        f.write(f"| Harness Token 增量 | {result['harness_token_delta']} |\n\n")

        f.write("## 任务详情\n\n")
        f.write("| Task ID | OFF 状态 | ON 状态 | 延迟(s) | Token | 重试 | 错误类型 |\n")
        f.write("|---------|---------|---------|--------|-------|------|----------|\n")
        for tr in result["task_results"]:
            f.write(
                f"| {tr['task_id']} "
                f"| {tr['harness_off_status']} "
                f"| {tr['harness_on_status']} "
                f"| {tr['duration_s']:.2f} "
                f"| {tr['input_tokens']}+{tr['output_tokens']} "
                f"| {tr['retry_count']} "
                f"| {tr['error_type']} |\n"
            )

        evaluation = result.get("evaluation")
        if evaluation:
            f.write("\n## 人工标签评估\n\n")
            f.write(f"- 评测集: `{evaluation['suite_id']}`\n")
            f.write(f"- 标签匹配率: {evaluation['label_match_rate']:.1%}\n")
            f.write(f"- Schema 合格率: {evaluation['schema_compliance_rate']:.1%}\n")
            recall = evaluation.get("guardrail_recall")
            precision = evaluation.get("guardrail_precision")
            f.write(f"- 违规拦截召回率: {'N/A' if recall is None else f'{recall:.1%}'}\n")
            f.write(f"- 违规拦截精确率: {'N/A' if precision is None else f'{precision:.1%}'}\n")
            f.write(f"- 正常请求误报率: {evaluation.get('normal_false_positive_rate', 0) or 0:.1%}\n")
            f.write(f"- 无来源率（可解析响应）: {evaluation.get('no_source_rate_among_parseable')}\n")
            hallucination_rate = evaluation.get("hallucination_rate")
            f.write(
                "- 固定事实幻觉率: "
                f"{'N/A' if hallucination_rate is None else f'{hallucination_rate:.1%}'} "
                f"({evaluation.get('hallucination_count', 0)}/"
                f"{evaluation.get('fact_checked_count', 0)})\n"
            )
            hallucination_block_rate = evaluation.get("hallucination_block_rate")
            f.write(
                "- 事实错误阻断率: "
                f"{'N/A（本轮未观测到事实错误）' if hallucination_block_rate is None else f'{hallucination_block_rate:.1%}'}\n"
            )
            f.write(f"- 口径说明: {evaluation['hallucination_rate_note']}\n")
            f.write(
                "- 无效下游动作资格（Harness OFF → ON）: "
                f"{evaluation.get('invalid_downstream_calls_harness_off', 0)} → "
                f"{evaluation.get('invalid_downstream_calls_harness_on', 0)}\n"
            )
            reduction = evaluation.get("invalid_downstream_call_reduction_rate")
            f.write(
                "- 无效下游动作资格降幅: "
                f"{'N/A' if reduction is None else f'{reduction:.1%}'}\n"
            )
            f.write(f"- 下游动作口径: {evaluation['invalid_downstream_call_note']}\n")
            cost = evaluation["cost_estimate"]
            if cost["configured"]:
                f.write(f"- Token 估算费用: {cost['estimated_total']:.6f}（币种由输入价格决定）\n")
            else:
                f.write("- Token 估算费用: 未配置当日官方单价，未计算\n")

        f.write("\n---\n\n")
        f.write("## 重要说明\n\n")
        f.write("### 实验类型区分\n\n")
        f.write("- **Mock 固定评测**: 不调用真实 LLM，使用构造性样本\n")
        f.write("- **simulated 测试**: 测试用 Fake Provider，不代表真实模型\n")
        f.write("- **real_llm_offline_replay**: 调用真实 LLM（本报告）\n")
        f.write("- **production_traffic**: 真实生产流量（尚未开展）\n\n")

        if result["provider_kind"] == "simulated":
            f.write("⚠️ **当前实验使用 simulated Provider，不代表真实 LLM 效果**\n\n")

        f.write("### Guardrail 拦截率说明\n\n")
        f.write("- 无人工标签时，不能称为 'hallucination_rate' 或 'false_positive_rate'\n")
        f.write("- 当前指标名称: guardrail_block_rate（Harness 拦截率）\n")
        f.write("- 代表 Harness 判定为违规的比例，不等同于模型幻觉率\n\n")

        f.write("### 样本量限制\n\n")
        f.write("- 本实验样本量有限，不代表大规模生产流量表现\n")
        f.write("- 真实生产环境的拦截率、误报率需要长期观测\n\n")

        f.write("### Sharpe 指标\n\n")
        f.write("- Sharpe 指标仍按原公式和 0.5 阈值评估\n")
        f.write("- 本实验不影响 Sharpe 计算\n\n")

        f.write("### 敏感信息\n\n")
        f.write("- 所有 API Key、邮箱、手机号等敏感信息已脱敏\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 LLM Harness ON/OFF 离线回放实验")
    parser.add_argument(
        "--provider",
        type=str,
        choices=["deepseek", "claude", "mock"],
        default="deepseek",
        help="LLM Provider 类型",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="",
        help="模型名称（可选，默认使用 Provider 默认模型）",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default="",
        help="JSON 报告输出路径（可选，默认 docs/experiments/）",
    )
    parser.add_argument(
        "--output-md",
        type=str,
        default="",
        help="Markdown 报告输出路径（可选，默认 docs/experiments/）",
    )
    parser.add_argument(
        "--suite",
        choices=["default3", "enterprise100"],
        default="default3",
        help="评测集：默认 3 条冒烟任务或 100 条企业人工标注任务",
    )
    parser.add_argument("--tasks-file", default="", help="自定义人工标注评测集 JSON")
    parser.add_argument("--daily", action="store_true", help="按自然日期保存到 docs/experiments/daily/YYYY-MM-DD")
    parser.add_argument("--input-price-per-million", type=float, default=None, help="当日输入 Token 每百万单价")
    parser.add_argument("--output-price-per-million", type=float, default=None, help="当日输出 Token 每百万单价")
    args = parser.parse_args()

    print("=" * 70)
    print("真实 LLM Harness ON/OFF 离线回放实验")
    print("=" * 70)
    print(f"Provider: {args.provider}")
    if args.model:
        print(f"Model: {args.model}")
    print()

    # 创建 Provider
    provider, provider_kind, actual_model, credentials_verified = create_provider(args.provider, args.model)

    # 检查 API Key 状态
    print("API Key 配置状态:")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    print(f"  DEEPSEEK_API_KEY: {'已设置' if deepseek_key and deepseek_key != '你的key' else '未设置'}")
    print(f"  ANTHROPIC_API_KEY: {'已设置' if anthropic_key else '未设置'}")
    print()

    # 运行实验
    print("开始实验...")
    if args.tasks_file:
        tasks_path = _resolve_output_path(args.tasks_file, PROJECT_ROOT)
        tasks = load_task_suite(tasks_path)
    elif args.suite == "enterprise100":
        tasks = build_enterprise_100_tasks()
    else:
        tasks = None

    result = run_real_llm_replay_experiment(
        provider=provider,
        provider_kind=provider_kind,
        model_name=actual_model,
        credentials_verified=credentials_verified,
        tasks=tasks,
    )
    result_dict = result.to_dict()
    if tasks and all("expected_outcome" in task for task in tasks) and result.task_results:
        result_dict["evaluation"] = evaluate_labeled_replay(
            result,
            tasks,
            input_price_per_million=args.input_price_per_million,
            output_price_per_million=args.output_price_per_million,
        )
    result_dict["run_date"] = time.strftime("%Y-%m-%d")
    result_dict["suite"] = args.suite if not args.tasks_file else "custom"

    # 显示摘要
    print()
    print("=" * 70)
    print("实验结果摘要")
    print("=" * 70)
    print(f"实验类型: {result.experiment_type}")
    print(f"Provider 类型: {result.provider_kind}")
    print(f"状态: {result.status}")

    if result.status in ("skipped_no_credentials", "skipped_mock_provider", "no_tasks"):
        print()
        if result.status == "skipped_no_credentials":
            print("[WARN] 真实 LLM 实验未执行，原因是未配置 API Key。")
            print("   本次结果不代表真实模型效果，也不代表生产流量表现。")
            print()
            print("如需执行真实实验，请设置环境变量：")
            if args.provider == "deepseek":
                print("   export DEEPSEEK_API_KEY=your_key")
            elif args.provider == "claude":
                print("   export ANTHROPIC_API_KEY=your_key")
        elif result.status == "skipped_mock_provider":
            print("[WARN] Mock Provider 不执行真实 LLM 实验。")
        elif result.status == "no_tasks":
            print("[WARN] 任务列表为空，无实验数据。")
        print()
    else:
        print(f"Provider: {result.provider}")
        print(f"Model: {result.model}")
        print(f"样本数: {result.sample_count}")
        print(f"成功率: {result.success_rate:.1%}")
        print(f"Schema 错误率: {result.schema_error_rate:.1%}")
        print(f"拦截率: {result.blocked_rate:.1%}")
        print(f"Provider 错误率: {result.provider_error_rate:.1%}")
        print(f"平均延迟: {result.average_latency_s:.2f}s")
        print(f"P50 延迟: {result.p50_latency_s:.2f}s")
        print(f"P95 延迟: {result.p95_latency_s:.2f}s")
        print(f"P99 延迟: {result.p99_latency_s:.2f}s")
        print(f"平均输入 Token: {result.average_input_tokens:.0f}")
        print(f"平均输出 Token: {result.average_output_tokens:.0f}")
        print(f"重试率: {result.retry_rate:.1%}")
        print()

    # 解析输出路径
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    default_dir = PROJECT_ROOT / "docs" / "experiments"
    if args.daily:
        default_dir = default_dir / "daily" / time.strftime("%Y-%m-%d")
    default_dir.mkdir(parents=True, exist_ok=True)

    if args.output_json:
        json_path = _resolve_output_path(args.output_json, PROJECT_ROOT)
    else:
        json_path = default_dir / f"real_llm_replay_{args.provider}_{timestamp}.json"

    if args.output_md:
        md_path = _resolve_output_path(args.output_md, PROJECT_ROOT)
    else:
        md_path = default_dir / f"real_llm_replay_{args.provider}_{timestamp}.md"

    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    # 保存 JSON 报告
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result_dict, f, ensure_ascii=False, indent=2)
    print(f"[OK] JSON 报告已保存: {json_path}")

    # 保存 Markdown 报告
    generate_markdown_report(result_dict, md_path)
    print(f"[OK] Markdown 报告已保存: {md_path}")

    published = publish_canonical_reports(
        result=result_dict,
        provider=args.provider,
        suite=result_dict["suite"],
        json_path=json_path,
        md_path=md_path,
    )
    print(f"[OK] 已更新 {len(published)} 个语义化报告指针")

    print()
    print("=" * 70)
    print("重要说明")
    print("=" * 70)
    if result.status == "skipped_no_credentials":
        print("1. 本次真实 LLM 实验未执行；报告仅记录无凭证跳过状态。")
    elif result.provider_kind == "simulated":
        print("1. 本次使用 simulated Provider，不代表真实 LLM 或生产流量表现。")
    else:
        print("1. 本实验使用真实 LLM 输出，但样本量有限，不代表大规模生产流量表现。")
    print("2. 所有敏感信息（API Key、邮箱、手机号等）已脱敏。")
    print("3. 本实验禁止调用任何交易、下单或真实券商接口。")
    print("4. Sharpe 指标仍按原公式和 0.5 阈值评估，不受本实验影响。")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
