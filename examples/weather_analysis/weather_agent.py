"""
天气分析 Agent — 演示通用 Agent 平台在非金融领域的接入
==========================================================
本模块仅依赖标准库 + 平台 core 层，与金融模块完全解耦。
展示：同一套 AgentHarness / Guardrail 机制可零改动用于任意领域。

用法（独立运行）：
    python examples/weather_analysis/run_demo.py
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any

# ── 使用平台 core 层 Guardrail（与金融 Agent 完全相同的机制）────────────────
from agent_platform.core.harness import (
    JSONSchemaValidator,
    KeywordBlocker,
    SourceAttributionFilter,
)

# ─────────────────────────────────────────────────────────────────────────────
# 输出 Schema（结构校验，与金融 Agent 保持同等严格程度）
# ─────────────────────────────────────────────────────────────────────────────

WEATHER_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "city", "period_days", "avg_temp_c", "max_temp_c", "min_temp_c",
        "temp_range_c", "trend", "volatility_c", "summary",
        "source", "updated_at", "disclaimer",
    ],
    "properties": {
        "city":          {"type": "string"},
        "period_days":   {"type": "integer", "minimum": 1},
        "avg_temp_c":    {"type": "number"},
        "max_temp_c":    {"type": "number"},
        "min_temp_c":    {"type": "number"},
        "temp_range_c":  {"type": "number", "minimum": 0},
        "trend":         {"type": "string", "enum": ["warming", "cooling", "stable"]},
        "volatility_c":  {"type": "number", "minimum": 0},
        "summary":       {"type": "string"},
        "source":        {"type": "string"},
        "updated_at":    {"type": "string"},
        "disclaimer":    {"type": "string"},
    },
    "additionalProperties": False,
}

# ─────────────────────────────────────────────────────────────────────────────
# 输出数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WeatherReport:
    city: str
    period_days: int
    avg_temp_c: float
    max_temp_c: float
    min_temp_c: float
    temp_range_c: float
    trend: str              # "warming" | "cooling" | "stable"
    volatility_c: float     # 温度标准差
    summary: str
    source: str
    updated_at: str
    disclaimer: str


# ─────────────────────────────────────────────────────────────────────────────
# 天气分析 Agent
# ─────────────────────────────────────────────────────────────────────────────

class WeatherAnalysisAgent:
    """
    天气趋势分析 Agent。

    使用与金融 Agent 完全相同的 Guardrail 机制，演示平台可移植性：
    - JSONSchemaValidator  → 结构化输出校验
    - SourceAttributionFilter → 数据来源完整性
    - KeywordBlocker       → 过滤不负责任的表述
    """

    # 过滤"绝对预测"类表述，避免不负责任的承诺
    _BLOCKED = ["100%准确", "绝对不会", "保证温度", "精准预测未来"]

    def __init__(self) -> None:
        self._guardrails = [
            JSONSchemaValidator(schema=WEATHER_REPORT_SCHEMA),
            SourceAttributionFilter(required=["source", "updated_at"]),
            KeywordBlocker(keywords=self._BLOCKED),
        ]

    # ------------------------------------------------------------------
    def analyze(
        self,
        city: str,
        temps: list[float],
        source: str = "内置样例数据",
    ) -> WeatherReport:
        """
        分析日气温序列，返回结构化天气报告。

        Args:
            city:   城市名称
            temps:  日均温列表（摄氏度，由近到远或由远到近均可）
            source: 数据来源标注
        """
        if len(temps) < 2:
            raise ValueError(f"气温序列至少需要 2 个数据点，当前 {len(temps)} 个")

        avg = round(statistics.mean(temps), 1)
        mx  = max(temps)
        mn  = min(temps)
        rng = round(mx - mn, 1)
        vol = round(statistics.stdev(temps), 2)

        # 线性趋势：后半段均值 − 前半段均值，阈值 1°C
        mid   = len(temps) // 2
        first = statistics.mean(temps[:mid])
        last  = statistics.mean(temps[mid:])
        diff  = last - first
        trend = "warming" if diff > 1.0 else "cooling" if diff < -1.0 else "stable"

        trend_zh = {"warming": "升温", "cooling": "降温", "stable": "平稳"}[trend]

        output: dict[str, Any] = {
            "city":         city,
            "period_days":  len(temps),
            "avg_temp_c":   avg,
            "max_temp_c":   mx,
            "min_temp_c":   mn,
            "temp_range_c": rng,
            "trend":        trend,
            "volatility_c": vol,
            "summary": (
                f"{city} 近 {len(temps)} 天气温分析：均温 {avg}°C，"
                f"最高 {mx}°C，最低 {mn}°C，温差 {rng}°C，"
                f"波动率(σ) {vol}°C，趋势：{trend_zh}。"
            ),
            "source":    source,
            "updated_at": date.today().isoformat(),
            "disclaimer": "仅供参考，气象预测存在不确定性，不构成任何承诺。",
        }

        # 依次通过所有 Guardrail（与金融 Agent 逻辑完全相同）
        validated = output
        for g in self._guardrails:
            validated = g.validate_output(validated)

        return WeatherReport(**{k: validated[k] for k in WeatherReport.__dataclass_fields__})



