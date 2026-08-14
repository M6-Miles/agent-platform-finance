"""Run one idempotent real-market paper-monitor observation for today."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_platform.config import get_settings  # noqa: E402
from agent_platform.finance.paper_broker_service import PaperBrokerService  # noqa: E402
from agent_platform.finance.paper_trading_monitor import PaperTradingMonitor  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="记录一次真实行情模拟盘日度证据")
    parser.add_argument("--symbols", nargs="+", default=["000001", "600519"])
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "experiments" / "paper_trading_evidence.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    try:
        output.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError("输出路径必须位于项目目录内") from exc

    settings = get_settings()
    broker = PaperBrokerService(settings.sqlite_path)
    monitor = PaperTradingMonitor(settings.sqlite_path, broker)
    symbols = sorted({str(code).strip().upper() for code in args.symbols if str(code).strip()})

    matching = [
        job for job in monitor.list_jobs()
        if job["symbols"] == symbols and job["data_mode"] == "auto"
    ]
    if matching:
        job = matching[0]
        if not job["enabled"]:
            job = monitor.set_enabled(job["id"], True)
    else:
        job = monitor.create_job(symbols, data_mode="auto", run_time="15:10")

    run = monitor.run_job(job["id"])
    summary = next(item for item in monitor.list_jobs() if item["id"] == job["id"])
    report = {
        "evidence_type": "natural_daily_real_market_paper_trading",
        "acceptance_rule": "7-14个不同真实交易日，且每日报价均为auto/live并带source",
        "job": summary,
        "latest_run": run,
        "honesty_note": "离线、降级、失败、非交易日和同日重复运行均不增加有效证据天数",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if run["status"] in {"completed", "skipped_non_trading_day"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
