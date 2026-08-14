"""Pre-flight checks for weather reports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class WeatherCheckResult:
    check_name: str
    passed: bool
    message: str


@dataclass(frozen=True)
class WeatherHarnessResult:
    city: str
    approved: bool
    checks: list[WeatherCheckResult]
    final_action: str
    weather_report: dict[str, Any]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WeatherHarness:
    """Apply completeness, range, attribution, and wording checks."""

    _BLOCKED_KEYWORDS = ["100%准确", "绝对不会", "保证温度", "精准预测未来"]

    def __init__(
        self,
        min_data_points: int = 2,
        temp_range: tuple[float, float] = (-100.0, 100.0),
        enable_keyword_check: bool = True,
    ) -> None:
        self.min_data_points = min_data_points
        self.temp_range = temp_range
        self.enable_keyword_check = enable_keyword_check

    def run_preflight(
        self,
        weather_report: dict[str, Any],
        raw_temps: list[float],
    ) -> WeatherHarnessResult:
        checks = [
            self._check_data_completeness(raw_temps),
            self._check_data_validity(raw_temps),
            self._check_source_attribution(weather_report),
            self._check_keywords(weather_report),
        ]
        approved = all(check.passed for check in checks)
        validity = checks[1]
        action = "approve" if approved else "block" if not validity.passed else "review"
        return WeatherHarnessResult(
            city=str(weather_report.get("city") or "UNKNOWN"),
            approved=approved,
            checks=checks,
            final_action=action,
            weather_report=weather_report,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    def _check_data_completeness(self, temps: list[float]) -> WeatherCheckResult:
        passed = len(temps) >= self.min_data_points
        message = (
            f"温度序列包含 {len(temps)} 个数据点（至少 {self.min_data_points} 个）"
            if passed else f"仅有 {len(temps)} 个数据点，不足 {self.min_data_points} 个"
        )
        return WeatherCheckResult("数据完整性", passed, message)

    def _check_data_validity(self, temps: list[float]) -> WeatherCheckResult:
        low, high = self.temp_range
        invalid = [value for value in temps if not low <= value <= high]
        message = (
            f"所有温度值均在 [{low:g}, {high:g}]°C 范围内"
            if not invalid else f"发现 {len(invalid)} 个值超出 [{low:g}, {high:g}]°C"
        )
        return WeatherCheckResult("数据合理性", not invalid, message)

    @staticmethod
    def _check_source_attribution(report: dict[str, Any]) -> WeatherCheckResult:
        passed = bool(report.get("source") and report.get("updated_at"))
        message = "包含 source 与 updated_at" if passed else "缺少 source 或 updated_at"
        return WeatherCheckResult("数据溯源", passed, message)

    def _check_keywords(self, report: dict[str, Any]) -> WeatherCheckResult:
        if not self.enable_keyword_check:
            return WeatherCheckResult("违禁词拦截", True, "检查已禁用")
        text = f"{report.get('summary', '')}{report.get('disclaimer', '')}"
        matched = next((word for word in self._BLOCKED_KEYWORDS if word in text), None)
        return WeatherCheckResult(
            "违禁词拦截",
            matched is None,
            "未发现违禁词" if matched is None else f"触发违禁词：{matched}",
        )
