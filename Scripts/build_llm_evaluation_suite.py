"""Materialize the deterministic 100-task labeled LLM evaluation suite."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_platform.core.llm_evaluation_suite import build_enterprise_100_tasks, save_task_suite


def main() -> None:
    output = ROOT / "data" / "evaluation" / "real_llm_enterprise_100.json"
    tasks = build_enterprise_100_tasks()
    save_task_suite(tasks, output)
    print(f"已生成 {len(tasks)} 条人工标注评测任务: {output}")


if __name__ == "__main__":
    main()
