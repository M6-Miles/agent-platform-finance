"""
统一的日期区间校验与指标预热窗口（Date Window）
================================================
所有对外的行情/分析/对比/回测端点共享同一套日期语义，避免各页面各自解释：

  1. 请求区间必须满足 ``start < end <= today``（后端校验，不依赖前端）。
  2. 指标预热（warm-up）允许向 ``start`` 之前多取数据，但预热行
     **不得**作为请求区间的返回行，也**不得**计入请求区间交易日数。
  3. 交易日数一律用实际返回行数统计，不用日历天数估算。

术语
----
requested window : 用户请求的 [start, end]，是唯一对外可见的区间。
fetch window     : 实际向 Provider 请求的 [fetch_start, end]，可能早于 start。
warmup rows      : fetch window 中早于 start 的行，仅用于指标预热。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

# MA20 / 布林带 / CCI(20) 需要 20 个交易日；KDJ(9)、RSI(14)、MACD(26) 更长。
# 取 26 作为“完整指标”所需的最小交易日数（MACD 慢线周期）。
MIN_TRADING_DAYS_FOR_MA20 = 20
MIN_TRADING_DAYS_FOR_FULL_INDICATORS = 26

# 预热窗口上界：按 A 股约 0.69 个交易日/日历日折算，60 个交易日 ≈ 90 个日历日。
DEFAULT_WARMUP_TRADING_DAYS = 60
MAX_WARMUP_CALENDAR_DAYS = 200


class DateRangeError(ValueError):
    """请求的日期区间不合法（由端点转换为 HTTP 400）。"""


class InsufficientHistoryError(ValueError):
    """请求区间内的交易日不足以计算所需指标（由端点转换为 HTTP 400）。"""

    def __init__(self, message: str, *, available: int, required: int) -> None:
        super().__init__(message)
        self.available = available
        self.required = required


@dataclass(frozen=True, slots=True)
class DateWindow:
    """已校验的请求区间及其预热区间。"""

    start: date | None
    end: date | None
    fetch_start: date | None
    warmup_trading_days: int

    @property
    def has_warmup(self) -> bool:
        return (
            self.start is not None
            and self.fetch_start is not None
            and self.fetch_start < self.start
        )


def _coerce_date(value: date | datetime | str | None, field: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()).date()
        except ValueError as exc:
            raise DateRangeError(f"{field} 不是合法的 ISO 日期（YYYY-MM-DD）：{value!r}") from exc
    raise DateRangeError(f"{field} 类型不支持：{type(value).__name__}")


def validate_date_range(
    start: date | datetime | str | None,
    end: date | datetime | str | None,
    *,
    today: date | None = None,
) -> tuple[date | None, date | None]:
    """校验 ``start < end <= today``；两端均可为 None（表示由 Provider 取默认区间）。

    Raises
    ------
    DateRangeError
        start >= end，或 end 晚于今天，或 start 晚于今天。
    """
    start_d = _coerce_date(start, "start")
    end_d = _coerce_date(end, "end")
    today_d = today or date.today()

    if end_d is not None and end_d > today_d:
        raise DateRangeError(
            f"结束日期不能晚于今天：end={end_d.isoformat()}，today={today_d.isoformat()}"
        )
    if start_d is not None and start_d > today_d:
        raise DateRangeError(
            f"开始日期不能晚于今天：start={start_d.isoformat()}，today={today_d.isoformat()}"
        )
    if start_d is not None and end_d is not None and start_d >= end_d:
        raise DateRangeError(
            f"开始日期必须早于结束日期：start={start_d.isoformat()}，end={end_d.isoformat()}"
        )
    return start_d, end_d


def build_window(
    start: date | datetime | str | None,
    end: date | datetime | str | None,
    *,
    warmup_trading_days: int = 0,
    today: date | None = None,
) -> DateWindow:
    """校验区间并计算预热起点。

    预热按 0.69 交易日/日历日折算，并被 ``MAX_WARMUP_CALENDAR_DAYS`` 截断，
    保证任何情况下都不会向 Provider 请求无界的历史数据。
    """
    start_d, end_d = validate_date_range(start, end, today=today)
    if start_d is None or warmup_trading_days <= 0:
        return DateWindow(
            start=start_d, end=end_d, fetch_start=start_d, warmup_trading_days=0
        )

    calendar_days = min(
        int(round(warmup_trading_days / 0.69)) + 5, MAX_WARMUP_CALENDAR_DAYS
    )
    return DateWindow(
        start=start_d,
        end=end_d,
        fetch_start=start_d - timedelta(days=calendar_days),
        warmup_trading_days=warmup_trading_days,
    )


def normalize_date_column(frame: pd.DataFrame, column: str = "date") -> pd.DataFrame:
    """把 date 列统一为 ``datetime.date``，便于与请求区间做比较。"""
    result = frame.copy()
    result[column] = pd.to_datetime(result[column], errors="coerce").dt.date
    return result.dropna(subset=[column])


def split_warmup(
    frame: pd.DataFrame,
    window: DateWindow,
    *,
    column: str = "date",
) -> tuple[pd.DataFrame, int]:
    """把 fetch window 的数据切成 (请求区间行, 预热行数)。

    返回的第一个元素**只**包含 ``window.start <= date <= window.end`` 的行，
    因此调用方无法误把预热行当作请求区间数据返回或计数。
    """
    data = normalize_date_column(frame, column).sort_values(column).reset_index(drop=True)
    warmup_count = 0
    if window.start is not None:
        warmup_count = int((data[column] < window.start).sum())
        data = data[data[column] >= window.start]
    if window.end is not None:
        data = data[data[column] <= window.end]
    return data.reset_index(drop=True), warmup_count


def assert_dates_in_window(
    dates: list[date] | pd.Series,
    window: DateWindow,
    *,
    label: str = "返回数据",
) -> None:
    """防御性断言：任何越界日期都是缺陷，直接抛错而不是静默返回。"""
    for value in list(dates):
        if window.start is not None and value < window.start:
            raise DateRangeError(f"{label}包含早于 start 的日期：{value}")
        if window.end is not None and value > window.end:
            raise DateRangeError(f"{label}包含晚于 end 的日期：{value}")
