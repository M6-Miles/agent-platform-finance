"""Open-Meteo current weather and seven-day forecast provider."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import re
import threading
import time
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

# Open-Meteo's geocoder does not consistently index Chinese urban districts.
# These centres cover the districts exposed by the frontend and are only used
# after an exact district query has returned no parent-region-consistent match.
_DISTRICT_CENTER_COORDINATES: dict[str, tuple[float, float]] = {
    "北京市|北京市|朝阳区": (39.9219, 116.4436),
    "北京市|北京市|海淀区": (39.9593, 116.2981),
    "北京市|北京市|东城区": (39.9283, 116.4164),
    "北京市|北京市|西城区": (39.9123, 116.3659),
    "北京市|北京市|丰台区": (39.8584, 116.2871),
    "北京市|北京市|昌平区": (40.2208, 116.2312),
    "上海市|上海市|浦东新区": (31.2215, 121.5447),
    "上海市|上海市|黄浦区": (31.2316, 121.4844),
    "上海市|上海市|徐汇区": (31.1883, 121.4368),
    "上海市|上海市|静安区": (31.2290, 121.4482),
    "上海市|上海市|闵行区": (31.1128, 121.3817),
    "广东省|广州市|天河区": (23.1247, 113.3612),
    "广东省|广州市|越秀区": (23.1291, 113.2668),
    "广东省|广州市|海珠区": (23.0833, 113.3172),
    "广东省|广州市|番禺区": (22.9377, 113.3841),
    "广东省|深圳市|福田区": (22.5410, 114.0558),
    "广东省|深圳市|南山区": (22.5333, 113.9304),
    "广东省|深圳市|罗湖区": (22.5484, 114.1317),
    "广东省|深圳市|宝安区": (22.5533, 113.8831),
    "浙江省|杭州市|西湖区": (30.2592, 120.1302),
    "浙江省|杭州市|上城区": (30.2507, 120.1715),
    "浙江省|杭州市|拱墅区": (30.3193, 120.1414),
    "浙江省|杭州市|滨江区": (30.2084, 120.2120),
    "浙江省|宁波市|海曙区": (29.8592, 121.5508),
    "浙江省|宁波市|鄞州区": (29.8172, 121.5470),
    "浙江省|宁波市|江北区": (29.8868, 121.5552),
    "四川省|成都市|锦江区": (30.6561, 104.0833),
    "四川省|成都市|青羊区": (30.6746, 104.0625),
    "四川省|成都市|武侯区": (30.6418, 104.0434),
    "四川省|成都市|高新区": (30.5452, 104.0664),
    "重庆市|重庆市|渝中区": (29.5527, 106.5686),
    "重庆市|重庆市|江北区": (29.6066, 106.5743),
    "重庆市|重庆市|南岸区": (29.5217, 106.5625),
    "重庆市|重庆市|沙坪坝区": (29.5412, 106.4574),
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
    data_status: str = "real_time"
    cache_hit: bool = False
    cache_time: str | None = None
    fallback_reason: str | None = None


class OpenMeteoWeatherProvider:
    """Keyless public-weather client with bounded timeouts and strict validation."""

    FRESH_CACHE_SECONDS = 300.0
    STALE_CACHE_SECONDS = 21_600.0
    _cache: dict[str, tuple[float, WeatherForecast]] = {}
    _failures: dict[str, int] = {}
    _retry_after: dict[str, float] = {}
    _lock = threading.RLock()

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def get_forecast(self, city: str) -> WeatherForecast:
        key = re.sub(r"\s+", "", city.strip())
        if not key:
            raise ValueError("城市名称不能为空")
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            retry_after = self._retry_after.get(key, 0.0)
        if cached is not None and now - cached[0] <= self.FRESH_CACHE_SECONDS:
            return replace(cached[1], data_status="cached", cache_hit=True,
                           cache_time=cached[1].fetched_at)
        if now < retry_after:
            if cached is not None and now - cached[0] <= self.STALE_CACHE_SECONDS:
                return replace(
                    cached[1], data_status="cached", cache_hit=True,
                    cache_time=cached[1].fetched_at,
                    fallback_reason="Open-Meteo 连续失败，指数退避期间使用最近成功缓存",
                )
            raise WeatherDataUnavailableError("公开天气服务处于指数退避期，请稍后重试")
        try:
            result = self._fetch_forecast(city)
        except Exception:
            with self._lock:
                failures = self._failures.get(key, 0) + 1
                self._failures[key] = failures
                self._retry_after[key] = now + min(900.0, 3.0 * (2 ** (failures - 1)))
            if cached is not None and now - cached[0] <= self.STALE_CACHE_SECONDS:
                return replace(
                    cached[1], data_status="cached", cache_hit=True,
                    cache_time=cached[1].fetched_at,
                    fallback_reason="Open-Meteo 获取失败，使用最近一次成功缓存",
                )
            raise
        with self._lock:
            self._cache[key] = (time.monotonic(), result)
            self._failures.pop(key, None)
            self._retry_after.pop(key, None)
        return result

    def _fetch_forecast(self, city: str) -> WeatherForecast:
        requested = city.strip()
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
        if location.get("_coordinate_reference"):
            return replace(
                result,
                location_note=(
                    f"Open-Meteo 地名库未返回“{requested}”的精确候选，"
                    f"已使用{city_name}{district_name or ''}行政区中心参考坐标定位"
                ),
            )
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
            reference = _district_center_location(province, city, district)
            if reference is not None:
                return reference, district, False
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
                response = httpx.get(
                    url,
                    params=params,
                    timeout=httpx.Timeout(10.0, connect=8.0),
                )
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


def _district_center_location(
    province: str, city: str, district: str
) -> dict[str, Any] | None:
    coordinates = _DISTRICT_CENTER_COORDINATES.get(
        f"{province}|{city}|{district}"
    )
    if coordinates is None:
        return None
    return {
        "name": district,
        "admin1": province,
        "admin2": city,
        "country": "中国",
        "latitude": coordinates[0],
        "longitude": coordinates[1],
        "timezone": "Asia/Shanghai",
        "_coordinate_reference": True,
    }
