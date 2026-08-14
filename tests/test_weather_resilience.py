from __future__ import annotations

from dataclasses import replace
from time import monotonic

from agent_platform.weather.open_meteo_provider import (
    DailyForecast,
    OpenMeteoWeatherProvider,
    WeatherForecast,
)


def _forecast(city: str = "测试市") -> WeatherForecast:
    return WeatherForecast(
        requested_city=city, resolved_name=city, country="中国",
        latitude=1.0, longitude=2.0, timezone="Asia/Shanghai",
        current_temperature_c=20.0, apparent_temperature_c=20.0,
        relative_humidity_pct=50, precipitation_mm=0.0, wind_speed_kmh=2.0,
        weather_code=0, condition="晴朗", observed_at="2026-08-14T10:00:00",
        fetched_at="2026-08-14T10:00:01Z",
        daily=(DailyForecast("2026-08-14", 0, "晴朗", 25, 15, 0),),
    )


def test_weather_uses_recent_success_cache(monkeypatch) -> None:
    provider = OpenMeteoWeatherProvider()
    key = "缓存测试市"
    provider._cache.pop(key, None)
    monkeypatch.setattr(provider, "_fetch_forecast", lambda city: replace(_forecast(), requested_city=city))

    first = provider.get_forecast(key)
    second = provider.get_forecast(key)

    assert first.data_status == "real_time"
    assert second.data_status == "cached"
    assert second.cache_hit is True
    assert second.cache_time == first.fetched_at


def test_weather_backoff_returns_stale_cache(monkeypatch) -> None:
    provider = OpenMeteoWeatherProvider()
    key = "退避测试市"
    provider._cache[key] = (monotonic() - 600, _forecast(key))
    provider._retry_after.pop(key, None)
    monkeypatch.setattr(provider, "_fetch_forecast", lambda city: (_ for _ in ()).throw(ConnectionError("down")))

    result = provider.get_forecast(key)

    assert result.data_status == "cached"
    assert result.fallback_reason
    assert provider._retry_after[key] > monotonic()
