"""Reusable weather trend Agent demonstrating domain-neutral Guardrails."""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from agent_platform.core.harness import (
    JSONSchemaValidator,
    KeywordBlocker,
    SourceAttributionFilter,
)

MIN_DATA_POINTS = 2
TREND_THRESHOLD_C = 1.0
MIN_TEMPERATURE_C = -100.0
MAX_TEMPERATURE_C = 100.0
DEFAULT_SOURCE = "内置天气样例数据"
DISCLAIMER = "仅供通用 Agent 能力演示；气象分析存在不确定性，不构成任何承诺。"

WEATHER_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "city", "period_days", "avg_temp_c", "max_temp_c", "min_temp_c",
        "temp_range_c", "trend", "volatility_c", "summary", "source",
        "updated_at", "disclaimer",
    ],
    "properties": {
        "city": {"type": "string"},
        "period_days": {"type": "integer", "minimum": 1},
        "avg_temp_c": {"type": "number"},
        "max_temp_c": {"type": "number"},
        "min_temp_c": {"type": "number"},
        "temp_range_c": {"type": "number", "minimum": 0},
        "trend": {"type": "string", "enum": ["warming", "cooling", "stable"]},
        "volatility_c": {"type": "number", "minimum": 0},
        "summary": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class WeatherReport:
    city: str
    period_days: int
    avg_temp_c: float
    max_temp_c: float
    min_temp_c: float
    temp_range_c: float
    trend: str
    volatility_c: float
    summary: str
    source: str
    updated_at: str
    disclaimer: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SAMPLE_CITIES: dict[str, list[float]] = {
    "北京": [2, 3, 5, 8, 12, 18, 22, 25, 28, 30, 29, 27, 22, 18],
    "上海": [8, 9, 10, 12, 15, 18, 22, 26, 28, 30, 30, 28, 25, 22],
    "广州": [18, 19, 20, 22, 24, 26, 28, 30, 31, 32, 32, 30, 28, 26],
    "哈尔滨": [-18, -16, -12, -8, -3, 2, 8, 14, 18, 20, 17, 12, 5, 0],
    "昆明": [10, 11, 13, 15, 17, 19, 20, 20, 19, 18, 17, 16, 15, 14],
}


class WeatherAnalysisAgent:
    """Analyze a temperature series and validate the structured report."""

    _BLOCKED = ["100%准确", "绝对不会", "保证温度", "精准预测未来"]

    def __init__(self) -> None:
        self._guardrails = [
            JSONSchemaValidator(schema=WEATHER_REPORT_SCHEMA),
            SourceAttributionFilter(required=["source", "updated_at"]),
            KeywordBlocker(keywords=self._BLOCKED),
        ]

    def analyze(
        self,
        city: str,
        temps: list[float],
        source: str = DEFAULT_SOURCE,
        updated_at: str | None = None,
    ) -> WeatherReport:
        city = city.strip()
        source = source.strip()
        if not city:
            raise ValueError("城市名称不能为空")
        if len(temps) < MIN_DATA_POINTS:
            raise ValueError(f"气温序列至少需要 {MIN_DATA_POINTS} 个数据点")
        invalid = [value for value in temps if not MIN_TEMPERATURE_C <= value <= MAX_TEMPERATURE_C]
        if invalid:
            raise ValueError(f"温度值超出合理范围 [{MIN_TEMPERATURE_C:g}, {MAX_TEMPERATURE_C:g}]°C")

        avg = round(statistics.mean(temps), 1)
        maximum = max(temps)
        minimum = min(temps)
        temp_range = round(maximum - minimum, 1)
        volatility = round(statistics.stdev(temps), 2)
        midpoint = len(temps) // 2
        delta = statistics.mean(temps[midpoint:]) - statistics.mean(temps[:midpoint])
        trend = (
            "warming" if delta > TREND_THRESHOLD_C
            else "cooling" if delta < -TREND_THRESHOLD_C
            else "stable"
        )
        trend_zh = {"warming": "升温", "cooling": "降温", "stable": "平稳"}[trend]
        output: dict[str, Any] = {
            "city": city,
            "period_days": len(temps),
            "avg_temp_c": avg,
            "max_temp_c": maximum,
            "min_temp_c": minimum,
            "temp_range_c": temp_range,
            "trend": trend,
            "volatility_c": volatility,
            "summary": (
                f"{city} 共 {len(temps)} 个温度点：均温 {avg}°C，最高 {maximum}°C，"
                f"最低 {minimum}°C，温差 {temp_range}°C，波动 {volatility}°C，趋势为{trend_zh}。"
            ),
            "source": source,
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "disclaimer": DISCLAIMER,
        }
        validated = output
        for guardrail in self._guardrails:
            validated = guardrail.validate_output(validated)
        return WeatherReport(**validated)
