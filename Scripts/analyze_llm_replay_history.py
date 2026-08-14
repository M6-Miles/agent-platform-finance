"""Aggregate multi-day real LLM replay reports without inventing elapsed time."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_platform.core.llm_evaluation_suite import aggregate_daily_reports


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总真实 LLM 多日回放与漂移")
    parser.add_argument("--root", default="docs/experiments/daily")
    parser.add_argument("--output", default="docs/experiments/real_llm_7day_summary.json")
    args = parser.parse_args()
    root = (ROOT / args.root).resolve()
    output = (ROOT / args.output).resolve()
    for path in (root, output):
        try:
            path.relative_to(ROOT)
        except ValueError as exc:
            raise ValueError("输入和输出路径必须位于项目目录内") from exc
    reports = list(root.glob("*/*.json")) if root.exists() else []
    summary = aggregate_daily_reports(reports)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"状态: {summary['status']}; 已积累 {summary['distinct_run_days']}/7 天")
    print(f"报告: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
