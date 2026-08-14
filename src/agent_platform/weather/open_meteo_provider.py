"""Open-Meteo current weather and seven-day forecast provider."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import re
from typing import Any

import httpx


class WeatherDataUnavailableError(RuntimeError):
    """Raised when public weather data cannot be obtained or validated."""


WEATHER_CODE_LABELS = {
    0: "晴朗", 1: "大部晴朗", 2: "局部多云", 3: "阴天",
    45: "雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨", 55: "强毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
    77: "米雪", 80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪",
    86: "强阵雪", 95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴强冰雹",
}


@dataclass(frozen=True, slots=True)
class DailyForecast:
    date: str
    weather_code: int
    condition: str
    temp_max_c: float
    temp_min_c: float
    precipitation_probability_pct: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "weather_code": self.weather_code,
            "condition": self.condition,
            "temp_max_c": self.temp_max_c,
            "temp_min_c": self.temp_min_c,
            "precipitation_probability_pct": self.precipitation_probability_pct,
        }


@dataclass(frozen=True, slots=True)
class WeatherForecast:
    requested_city: str
    resolved_name: str
    country: str
    latitude: float
    longitude: float
    timezone: str
    current_temperature_c: float
    apparent_temperature_c: float | None
    relative_humidity_pct: int | None
    precipitation_mm: float | None
    wind_speed_kmh: float | None
    weather_code: int
    condition: str
    observed_at: str
    fetched_at: str
    daily: tuple[DailyForecast, ...]
    source: str = "Open-Meteo 公开天气数据"
    location_note: str = "已按请求地点解析"
    city_name: str | None = None
    district_name: str | None = None


class OpenMeteoWeatherProvider:
    """Keyless public-weather client with bounded timeouts and strict validation."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def get_forecast(self, city: str) -> WeatherForecast:
        requested = city.strip()
        if not requested:
            raise ValueError("城市名称不能为空")
        location, matched_query, is_fallback = self._resolve_location(requested)
        try:
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherDataUnavailableError("城市坐标数据无效") from exc

        forecast = self._get_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": (
                    "temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "timezone": "auto",
                "forecast_days": 7,
            },
        )
        result = self._parse(requested, location, forecast, latitude, longitude)
        city_name, district_name = _display_location_names(requested, location)
        result = replace(result, city_name=city_name, district_name=district_name)
        if not is_fallback:
            return result
        return replace(
            result,
            location_note=(
                f"未能精确解析“{requested}”，已回退到“{matched_query}”的城市级天气"
            ),
        )

    def _resolve_location(self, requested: str) -> tuple[dict[str, Any], str, bool]:
        province, city, district = _parse_location_parts(requested)

        # District names are queried independently because Open-Meteo rarely
        # recognizes concatenated Chinese administrative addresses. Multiple
        # candidates are filtered by their province/city metadata.
        if district:
            for query in (district, district.removesuffix("区").removesuffix("县")):
                if not query:
                    continue
                results = self._geocode(query, count=10)
                match = next(
                    (
                        item for item in results
                        if _location_matches(item, province, city, district)
                    ),
                    None,
                )
                if match is not None:
                    return match, district, False

        # Fall back to city only after district candidates have been exhausted.
        city_query = (city or province or requested).removesuffix("市")
        results = self._geocode(city_query, count=10)
        city_match = next(
            (item for item in results if _location_matches(item, province, city, None)),
            results[0] if results else None,
        )
        if city_match is not None:
            return city_match, city_query, bool(district)
        raise WeatherDataUnavailableError(f"未找到地区：{requested}")

    def _geocode(self, query: str, count: int) -> list[dict[str, Any]]:
        geocoding = self._get_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": query, "count": count, "language": "zh", "format": "json"},
        )
        return [item for item in (geocoding.get("results") or []) if isinstance(item, dict)]

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = self._client.get(url, params=params)
            else:
                response = httpx.get(url, params=params, timeout=httpx.Timeout(5.0))
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherDataUnavailableError(
                f"公开天气服务暂不可用（{type(exc).__name__}）"
            ) from exc
        if not isinstance(payload, dict):
            raise WeatherDataUnavailableError("公开天气服务返回格式无效")
        return payload

    @staticmethod
    def _parse(
        requested: str,
        location: dict[str, Any],
        forecast: dict[str, Any],
        latitude: float,
        longitude: float,
    ) -> WeatherForecast:
        current = forecast.get("current") or {}
        daily = forecast.get("daily") or {}
        dates = daily.get("time") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        codes = daily.get("weather_code") or []
        rain = daily.get("precipitation_probability_max") or [None] * len(dates)
        if not dates or not (len(dates) == len(highs) == len(lows) == len(codes)):
            raise WeatherDataUnavailableError("未来 7 天天气预报字段不完整")
        try:
            current_temp = float(current["temperature_2m"])
            current_code = int(current["weather_code"])
            days = tuple(
                DailyForecast(
                    date=str(dates[index]),
                    weather_code=int(codes[index]),
                    condition=WEATHER_CODE_LABELS.get(int(codes[index]), "未知天气"),
                    temp_max_c=round(float(highs[index]), 1),
                    temp_min_c=round(float(lows[index]), 1),
                    precipitation_probability_pct=(
                        None if index >= len(rain) or rain[index] is None else int(rain[index])
                    ),
                )
                for index in range(len(dates))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherDataUnavailableError("天气数值字段无法解析") from exc
        if not days:
            raise WeatherDataUnavailableError("天气预报没有有效日期")
        fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return WeatherForecast(
            requested_city=requested,
            resolved_name=str(location.get("name") or requested),
            country=str(location.get("country") or ""),
            latitude=latitude,
            longitude=longitude,
            timezone=str(forecast.get("timezone") or location.get("timezone") or "auto"),
            current_temperature_c=round(current_temp, 1),
            apparent_temperature_c=_optional_float(current.get("apparent_temperature")),
            relative_humidity_pct=_optional_int(current.get("relative_humidity_2m")),
            precipitation_mm=_optional_float(current.get("precipitation")),
            wind_speed_kmh=_optional_float(current.get("wind_speed_10m")),
            weather_code=current_code,
            condition=WEATHER_CODE_LABELS.get(current_code, "未知天气"),
            observed_at=str(current.get("time") or fetched_at),
            fetched_at=fetched_at,
            daily=days,
        )


def _optional_float(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _parse_location_parts(requested: str) -> tuple[str, str, str]:
    compact = re.sub(r"\s+", "", requested)
    municipality = next(
        (name for name in ("北京", "上海", "天津", "重庆") if compact.startswith(name)),
        "",
    )
    district_match = re.search(r"([^省市]+?(?:新区|区|县|旗))$", compact)
    district = district_match.group(1) if district_match else ""
    if municipality:
        return f"{municipality}市", f"{municipality}市", district
    province_match = re.match(r"(.+?(?:省|自治区|特别行政区))", compact)
    province = province_match.group(1) if province_match else ""
    remainder = compact[len(province):]
    city_match = re.match(r"(.+?市)", remainder)
    city = city_match.group(1) if city_match else ""
    return province, city, district


def _normalized_location_text(value: Any) -> str:
    text = str(value or "").replace(" ", "")
    for suffix in ("特别行政区", "自治区", "省", "市", "新区", "区", "县", "旗"):
        text = text.replace(suffix, "")
    return text.lower()


def _location_matches(
    item: dict[str, Any],
    province: str,
    city: str,
    district: str | None,
) -> bool:
    name = _normalized_location_text(item.get("name"))
    admin_text = "".join(
        _normalized_location_text(item.get(field))
        for field in ("admin1", "admin2", "admin3", "admin4")
    )
    if district and _normalized_location_text(district) not in name:
        return False
    province_key = _normalized_location_text(province)
    city_key = _normalized_location_text(city)
    if province_key and province_key not in admin_text and province_key not in name:
        return False
    if city_key and city_key not in admin_text and city_key not in name:
        return False
    return True


def _display_location_names(
    requested: str, location: dict[str, Any]
) -> tuple[str, str | None]:
    """Keep the administrative city separate from a district result name."""
    _, city, district = _parse_location_parts(requested)
    if city:
        return city, district or str(location.get("name") or "") or None
    return str(location.get("name") or requested), None
