"""One-shot daily runner intended for Windows Task Scheduler or cron.

It intentionally exits after one run. The operating system scheduler owns the
calendar, while the report directory and history aggregator own evidence.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "Scripts"))

from run_real_llm_replay import main as replay_main


def main() -> int:
    # Delegate to the canonical CLI so scheduled runs write the same complete
    # JSON/Markdown reports as manual runs and never execute twice.
    forwarded = list(sys.argv[1:])
    if "--help" in forwarded:
        sys.argv = [sys.argv[0], *forwarded]
        return replay_main()
    if "--provider" not in forwarded:
        forwarded += ["--provider", os.environ.get("LLM_REPLAY_PROVIDER", "deepseek")]
    if "--suite" not in forwarded:
        forwarded += ["--suite", "enterprise100"]
    if "--daily" not in forwarded:
        forwarded += ["--daily"]
    model = os.environ.get("LLM_REPLAY_MODEL", "")
    if model and "--model" not in forwarded:
        forwarded += ["--model", model]
    input_price = os.environ.get("LLM_REPLAY_INPUT_PRICE_PER_MILLION")
    output_price = os.environ.get("LLM_REPLAY_OUTPUT_PRICE_PER_MILLION")
    if input_price and output_price:
        if "--input-price-per-million" not in forwarded:
            forwarded += ["--input-price-per-million", input_price]
        if "--output-price-per-million" not in forwarded:
            forwarded += ["--output-price-per-million", output_price]
    sys.argv = [sys.argv[0], *forwarded]
    return replay_main()


if __name__ == "__main__":
    raise SystemExit(main())
