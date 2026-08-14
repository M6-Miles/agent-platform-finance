"""Compatibility exports for the reusable weather Agent package."""

from agent_platform.weather.agent import (  # noqa: F401
    DEFAULT_SOURCE,
    DISCLAIMER,
    MAX_TEMPERATURE_C,
    MIN_DATA_POINTS,
    MIN_TEMPERATURE_C,
    SAMPLE_CITIES,
    TREND_THRESHOLD_C,
    WEATHER_REPORT_SCHEMA,
    WeatherAnalysisAgent,
    WeatherReport,
)

__all__ = ["WEATHER_REPORT_SCHEMA", "WeatherAnalysisAgent", "WeatherReport"]
