"""
测试 P05：天气分析 Demo（通用 Agent 平台非金融领域演示）
=================================================================
验证内容：
1. FastAPI 端点 POST /weather/analyze 正常工作
2. Pydantic 请求验证（city, temps 范围与长度，source）
3. WeatherAnalysisAgent 返回正确的 WeatherReport 结构
4. WeatherHarness Pre-Flight Checklist 4 项检查（完整性/合理性/溯源/违禁词）
5. Guardrail 在超出范围时返回 400 错误
6. 前端 HTML 包含天气分析页面及所有必需字段（city/temps/trend/来源等）
7. XSS 防护：escapeHtml 正确转义用户输入
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_platform.api.main import app
from agent_platform.weather import WeatherAnalysisAgent, WeatherHarness
from agent_platform.weather.open_meteo_provider import (
    DailyForecast,
    OpenMeteoWeatherProvider,
    WeatherForecast,
    _display_location_names,
)


client = TestClient(app)


class TestWeatherEndpoint:
    """测试天气分析 FastAPI 端点。"""

    def test_weather_analyze_success(self):
        """正常请求：返回完整 WeatherAnalysisResponse 及 Harness 结果。"""
        payload = {
            "city": "北京",
            "temps": [5.2, 6.8, 8.1, 9.5, 11.2],
            "source": "样例数据",
        }
        response = client.post("/weather/analyze", json=payload)
        assert response.status_code == 200, f"预期 200，实际 {response.status_code}: {response.text}"

        data = response.json()
        # 基础字段
        assert data["city"] == "北京"
        assert data["period_days"] == 5
        assert isinstance(data["avg_temp_c"], float)
        assert isinstance(data["max_temp_c"], float)
        assert isinstance(data["min_temp_c"], float)
        assert isinstance(data["temp_range_c"], float)
        assert isinstance(data["volatility_c"], float)
        assert data["trend"] in ["warming", "cooling", "stable"]
        assert isinstance(data["summary"], str) and len(data["summary"]) > 10
        assert data["source"] == "内置天气样例数据"
        assert "T" in data["updated_at"]  # ISO 8601
        assert isinstance(data["disclaimer"], str)

        # Harness 检查结果
        assert isinstance(data["harness_approved"], bool)
        assert isinstance(data["harness_checks"], list) and len(data["harness_checks"]) == 4
        assert data["harness_action"] in ["approve", "block", "review"]

        # 验证 4 项检查的名称
        check_names = {c["check_name"] for c in data["harness_checks"]}
        expected = {"数据完整性", "数据合理性", "数据溯源", "违禁词拦截"}
        assert check_names == expected, f"检查项不匹配：{check_names} != {expected}"

    def test_weather_analyze_insufficient_data_points(self):
        """数据点不足 2 个：返回 400。"""
        payload = {"city": "上海", "temps": [20.0], "source": "test"}
        response = client.post("/weather/analyze", json=payload)
        assert response.status_code == 422  # Pydantic 校验失败

    def test_weather_analyze_too_many_data_points(self):
        """数据点超过 366 个：返回 422。"""
        payload = {"city": "广州", "temps": list(range(400)), "source": "test"}
        response = client.post("/weather/analyze", json=payload)
        assert response.status_code == 422

    def test_weather_analyze_temp_out_of_range(self):
        """温度超出 [-100, 100] 范围：返回 400。"""
        payload = {"city": "深圳", "temps": [10, 20, 150], "source": "test"}
        response = client.post("/weather/analyze", json=payload)
        assert response.status_code == 400
        assert "超出合理范围" in response.json()["detail"]

    def test_weather_analyze_missing_city(self):
        """缺少必填字段 city：返回 422。"""
        payload = {"temps": [10, 20, 30], "source": "test"}
        response = client.post("/weather/analyze", json=payload)
        assert response.status_code == 422

    def test_weather_samples(self):
        response = client.get("/weather/samples")
        assert response.status_code == 200
        samples = response.json()
        assert len(samples) >= 5
        assert {item["city"] for item in samples} >= {"北京", "上海", "广州"}
        assert all(len(item["temps"]) >= 2 for item in samples)

    def test_offline_source_cannot_be_forged(self):
        response = client.post("/weather/analyze", json={
            "city": "北京", "temps": [5.0, 6.0],
            "source": "伪造来源", "data_mode": "offline",
        })
        assert response.status_code == 200
        assert response.json()["source"] == "内置天气样例数据"

    def test_weather_online_forecast_enters_same_agent_and_harness(self, monkeypatch):
        forecast = WeatherForecast(
            requested_city="北京", resolved_name="北京市", country="中国",
            latitude=39.9, longitude=116.4, timezone="Asia/Shanghai",
            current_temperature_c=26.4, apparent_temperature_c=27.1,
            relative_humidity_pct=61, precipitation_mm=0.0, wind_speed_kmh=8.2,
            weather_code=1, condition="大部晴朗", observed_at="2026-08-13T15:00",
            fetched_at="2026-08-13T07:00:00Z",
            daily=tuple(
                DailyForecast(
                    date=f"2026-08-{13 + index:02d}", weather_code=1,
                    condition="大部晴朗", temp_max_c=30 + index,
                    temp_min_c=20 + index, precipitation_probability_pct=10,
                )
                for index in range(7)
            ),
        )
        monkeypatch.setattr(OpenMeteoWeatherProvider, "get_forecast", lambda _self, _city: forecast)

        response = client.post("/weather/analyze", json={
            "city": "北京", "temps": [0, 0], "source": "ignored", "data_mode": "online",
        })

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["data_mode"] == "online"
        assert data["city"] == "北京市"
        assert data["period_days"] == 7
        assert data["source"] == "Open-Meteo 公开天气数据"
        assert data["updated_at"] == forecast.fetched_at
        assert data["forecast"]["current_temperature_c"] == 26.4
        assert data["forecast"]["city_name"] == "北京市"
        assert len(data["forecast"]["daily"]) == 7
        assert data["harness_approved"] is True

    def test_online_source_cannot_be_forged_and_district_is_forwarded(self, monkeypatch):
        captured = {}
        forecast = WeatherForecast(
            requested_city="北京市朝阳区", resolved_name="朝阳区", country="中国",
            latitude=39.92, longitude=116.44, timezone="Asia/Shanghai",
            current_temperature_c=26.0, apparent_temperature_c=26.0,
            relative_humidity_pct=60, precipitation_mm=0.0, wind_speed_kmh=5.0,
            weather_code=1, condition="大部晴朗", observed_at="2026-08-13T15:00",
            fetched_at="2026-08-13T07:00:00Z",
            daily=tuple(
                DailyForecast("2026-08-13", 1, "大部晴朗", 30, 20, 10)
                for _ in range(2)
            ),
        )

        def fake_get(_self, city):
            captured["city"] = city
            return forecast

        monkeypatch.setattr(OpenMeteoWeatherProvider, "get_forecast", fake_get)
        response = client.post("/weather/analyze", json={
            "city": "北京市朝阳区", "temps": [0, 0],
            "source": "伪造来源", "data_mode": "online",
        })
        assert response.status_code == 200
        assert captured["city"] == "北京市朝阳区"
        assert response.json()["source"] == "Open-Meteo 公开天气数据"

    def test_weather_online_failure_is_explicit_503(self, monkeypatch):
        def fail(_self, _city):
            raise RuntimeError("upstream unavailable")

        monkeypatch.setattr(OpenMeteoWeatherProvider, "get_forecast", fail)
        response = client.post("/weather/analyze", json={
            "city": "北京", "temps": [0, 0], "data_mode": "online",
        })
        assert response.status_code == 503
        assert "联网天气获取失败" in response.json()["detail"]


class TestWeatherAgent:
    """测试 WeatherAnalysisAgent 核心逻辑。"""

    def test_agent_basic_analysis(self):
        """Agent 返回完整 WeatherReport 结构。"""
        agent = WeatherAnalysisAgent()
        report = agent.analyze(
            city="杭州",
            temps=[15.2, 16.8, 18.1, 19.5, 21.2],
            source="单元测试",
        )
        assert report.city == "杭州"
        assert report.period_days == 5
        assert report.avg_temp_c > 0
        assert report.max_temp_c >= report.min_temp_c
        assert report.temp_range_c == report.max_temp_c - report.min_temp_c
        assert report.trend in ["warming", "cooling", "stable"]
        assert report.volatility_c >= 0
        assert len(report.summary) > 10
        assert report.source == "单元测试"
        assert "T" in report.updated_at
        assert len(report.disclaimer) > 20


class TestOpenMeteoLocationResolution:
    def test_province_only_requires_a_specific_city(self):
        provider = OpenMeteoWeatherProvider()

        try:
            provider._resolve_location("新疆")
        except Exception as exc:
            assert "请选择或输入具体城市" in str(exc)
        else:
            raise AssertionError("省级区域不应被解析为单一天气坐标")

    def test_multimodel_daily_prefers_cma_and_falls_back_to_best_match(self):
        forecast = OpenMeteoWeatherProvider._parse(
            "重庆",
            {"name": "重庆", "country": "中国"},
            {
                "timezone": "Asia/Shanghai",
                "current": {
                    "temperature_2m_best_match": 28.0,
                    "weather_code_best_match": 0,
                },
                "daily": {
                    "time": ["2026-08-16", "2026-08-17"],
                    "weather_code_cma_grapes_global": [3, None],
                    "temperature_2m_max_cma_grapes_global": [36.0, None],
                    "temperature_2m_min_cma_grapes_global": [26.0, None],
                    "weather_code_best_match": [51, 96],
                    "temperature_2m_max_best_match": [36.5, 31.0],
                    "temperature_2m_min_best_match": [26.5, 25.0],
                    "precipitation_probability_max_best_match": [27, 74],
                },
            },
            29.56,
            106.56,
        )

        assert forecast.daily[0].condition == "阴天"
        assert forecast.daily[0].model_source == "中国气象局 CMA-GRAPES"
        assert forecast.daily[1].condition == "局地雷暴（小冰雹风险）"
        assert forecast.daily[1].model_source == "Open-Meteo Best Match"

    def test_district_display_keeps_city_as_municipality(self):
        city, district = _display_location_names(
            "重庆市江北区",
            {"name": "江北", "admin1": "重庆市", "admin2": "重庆市"},
        )
        assert city == "重庆市"
        assert district == "江北区"

    def test_district_candidates_are_filtered_by_parent_region(self, monkeypatch):
        provider = OpenMeteoWeatherProvider()

        def fake_geocode(query, count):
            assert query == "通州区"
            assert count == 10
            return [
                {"name": "通州区", "admin1": "江苏省", "admin2": "南通市"},
                {"name": "通州区", "admin1": "北京市", "admin2": "北京市"},
            ]

        monkeypatch.setattr(provider, "_geocode", fake_geocode)
        location, query, is_fallback = provider._resolve_location("北京市通州区")
        assert location["admin1"] == "北京市"
        assert query == "通州区"
        assert is_fallback is False

    def test_known_district_miss_uses_reference_coordinates(self, monkeypatch):
        provider = OpenMeteoWeatherProvider()
        queries = []

        def fake_json(_url, params):
            if "name" in params:
                queries.append(params["name"])
                if params["name"] != "北京":
                    return {"results": []}
                return {"results": [{
                    "name": "北京市", "country": "中国",
                    "latitude": 39.9, "longitude": 116.4,
                    "timezone": "Asia/Shanghai",
                }]}
            return {
                "timezone": "Asia/Shanghai",
                "current": {"temperature_2m": 26, "weather_code": 1, "time": "2026-08-13T15:00"},
                "daily": {
                    "time": ["2026-08-13", "2026-08-14"],
                    "weather_code": [1, 1],
                    "temperature_2m_max": [30, 31],
                    "temperature_2m_min": [20, 21],
                    "precipitation_probability_max": [10, 10],
                },
            }

        monkeypatch.setattr(provider, "_get_json", fake_json)
        forecast = provider.get_forecast("北京市朝阳区")
        assert queries == []
        assert forecast.resolved_name == "朝阳区"
        assert forecast.city_name == "北京市"
        assert forecast.district_name == "朝阳区"
        assert forecast.latitude == 39.9219
        assert forecast.longitude == 116.4436
        assert "行政区中心参考坐标定位" in forecast.location_note

    def test_unknown_district_still_falls_back_to_city(self, monkeypatch):
        provider = OpenMeteoWeatherProvider()

        def fake_geocode(query, count):
            assert count == 10
            if query == "北京":
                return [{
                    "name": "北京市", "admin1": "北京市", "admin2": "北京市",
                    "country": "中国", "latitude": 39.9, "longitude": 116.4,
                }]
            return []

        monkeypatch.setattr(provider, "_geocode", fake_geocode)
        location, query, is_fallback = provider._resolve_location("北京市不存在区")
        assert location["name"] == "北京市"
        assert query == "北京"
        assert is_fallback is True

    def test_agent_negative_temps(self):
        """Agent 正确处理负温度。"""
        agent = WeatherAnalysisAgent()
        report = agent.analyze(
            city="哈尔滨",
            temps=[-25.0, -20.0, -18.0, -15.0],
            source="冬季数据",
        )
        assert report.city == "哈尔滨"
        assert report.min_temp_c < 0
        assert report.avg_temp_c < 0


class TestWeatherHarness:
    """测试 WeatherHarness Pre-Flight Checklist。"""

    def test_harness_all_checks_pass(self):
        """数据完整且合理：4 项检查全通过，approved=True。"""
        harness = WeatherHarness()
        weather_report = {
            "city": "成都",
            "period_days": 5,
            "avg_temp_c": 18.0,
            "max_temp_c": 25.0,
            "min_temp_c": 12.0,
            "temp_range_c": 13.0,
            "trend": "上升",
            "volatility_c": 4.2,
            "summary": "温度整体呈上升趋势，波动适中。",
            "source": "单元测试",
            "updated_at": "2026-08-12T10:00:00Z",
            "disclaimer": "仅供参考",
        }
        raw_temps = [12.0, 15.0, 18.0, 22.0, 25.0]
        result = harness.run_preflight(weather_report, raw_temps)

        assert result.approved is True
        assert result.final_action == "approve"
        assert len(result.checks) == 4
        assert all(c.passed for c in result.checks)

    def test_harness_insufficient_data_points(self):
        """数据点不足：数据完整性检查失败。"""
        harness = WeatherHarness(min_data_points=5)
        weather_report = {
            "city": "重庆",
            "period_days": 2,
            "avg_temp_c": 20.0,
            "max_temp_c": 22.0,
            "min_temp_c": 18.0,
            "temp_range_c": 4.0,
            "trend": "稳定",
            "volatility_c": 1.5,
            "summary": "温度稳定",
            "source": "测试",
            "updated_at": "2026-08-12T10:00:00Z",
            "disclaimer": "仅供参考",
        }
        raw_temps = [18.0, 22.0]
        result = harness.run_preflight(weather_report, raw_temps)

        assert result.approved is False
        completeness_check = next(c for c in result.checks if c.check_name == "数据完整性")
        assert completeness_check.passed is False

    def test_harness_temp_out_of_range(self):
        """温度超出范围：数据合理性检查失败，final_action=block。"""
        harness = WeatherHarness()
        weather_report = {
            "city": "火星",
            "period_days": 3,
            "avg_temp_c": 0.0,
            "max_temp_c": 150.0,
            "min_temp_c": -200.0,
            "temp_range_c": 350.0,
            "trend": "极端",
            "volatility_c": 100.0,
            "summary": "极端温度",
            "source": "测试",
            "updated_at": "2026-08-12T10:00:00Z",
            "disclaimer": "仅供参考",
        }
        raw_temps = [-200.0, 0.0, 150.0]
        result = harness.run_preflight(weather_report, raw_temps)

        assert result.approved is False
        assert result.final_action == "block"
        validity_check = next(c for c in result.checks if c.check_name == "数据合理性")
        assert validity_check.passed is False
        assert "超出" in validity_check.message

    def test_harness_missing_source(self):
        """缺少 source：数据溯源检查失败。"""
        harness = WeatherHarness()
        weather_report = {
            "city": "南京",
            "period_days": 3,
            "avg_temp_c": 20.0,
            "max_temp_c": 25.0,
            "min_temp_c": 15.0,
            "temp_range_c": 10.0,
            "trend": "上升",
            "volatility_c": 3.0,
            "summary": "温度上升",
            "source": "",  # 缺失
            "updated_at": "",  # 缺失
            "disclaimer": "仅供参考",
        }
        raw_temps = [15.0, 20.0, 25.0]
        result = harness.run_preflight(weather_report, raw_temps)

        assert result.approved is False
        source_check = next(c for c in result.checks if c.check_name == "数据溯源")
        assert source_check.passed is False

    def test_harness_blocked_keyword(self):
        """触发违禁词：违禁词拦截失败。"""
        harness = WeatherHarness()
        weather_report = {
            "city": "武汉",
            "period_days": 3,
            "avg_temp_c": 20.0,
            "max_temp_c": 25.0,
            "min_temp_c": 15.0,
            "temp_range_c": 10.0,
            "trend": "上升",
            "volatility_c": 3.0,
            "summary": "本报告100%准确，绝对不会出错",  # 触发违禁词
            "source": "测试",
            "updated_at": "2026-08-12T10:00:00Z",
            "disclaimer": "仅供参考",
        }
        raw_temps = [15.0, 20.0, 25.0]
        result = harness.run_preflight(weather_report, raw_temps)

        assert result.approved is False
        keyword_check = next(c for c in result.checks if c.check_name == "违禁词拦截")
        assert keyword_check.passed is False
        assert "100%准确" in keyword_check.message


class TestFrontendIntegration:
    """测试前端 HTML 集成。"""

    def test_frontend_contains_weather_page(self):
        """frontend_prototype.html 包含天气分析页面容器。"""
        with open("frontend_prototype.html", encoding="utf-8") as f:
            html = f.read()
        assert 'id="page-weather"' in html, "缺少天气分析页面容器"
        assert 'id="nav-weather"' in html, "缺少导航按钮"

    def test_frontend_weather_input_fields(self):
        """前端包含所有必需输入字段。"""
        with open("frontend_prototype.html", encoding="utf-8") as f:
            html = f.read()
        assert 'id="weather-city"' in html
        assert 'id="weather-temps"' in html
        assert 'id="weather-source"' not in html
        assert 'id="weather-source-display"' in html
        assert 'id="weather-province"' in html
        assert 'id="weather-city-select"' in html
        assert 'id="weather-district"' in html
        assert 'id="weather-custom-location"' in html
        assert 'id="weather-analyze-btn"' in html
        assert 'id="weather-sample"' in html
        assert 'id="weather-data-mode"' in html
        assert "Open-Meteo" in html
        assert "initializeWeatherRegions();" in html

    def test_frontend_weather_result_fields(self):
        """前端包含所有结果显示字段。"""
        with open("frontend_prototype.html", encoding="utf-8") as f:
            html = f.read()
        required_fields = [
            "weather-res-city",
            "weather-res-period",
            "weather-res-trend",
            "weather-res-avg",
            "weather-res-max",
            "weather-res-min",
            "weather-res-range",
            "weather-res-volatility",
            "weather-res-summary",
            "weather-res-source",
            "weather-res-updated",
            "weather-res-disclaimer",
            "weather-harness-badge",
            "weather-harness-checks",
            "weather-live-container",
            "weather-live-current",
            "weather-live-daily",
        ]
        for field in required_fields:
            assert f'id="{field}"' in html, f"缺少字段：{field}"

    def test_frontend_has_escape_html(self):
        """前端包含 escapeHtml 函数。"""
        with open("frontend_prototype.html", encoding="utf-8") as f:
            html = f.read()
        assert "function escapeHtml(" in html or "const escapeHtml" in html

    def test_frontend_weather_uses_escape_html(self):
        """天气分析 JS 代码使用 escapeHtml 防护。"""
        with open("frontend_prototype.html", encoding="utf-8") as f:
            html = f.read()
        assert "escapeHtml(check.check_name)" in html
        assert "escapeHtml(check.message)" in html

    def test_frontend_displays_non_default_location_notes(self):
        with open("frontend_prototype.html", encoding="utf-8") as f:
            html = f.read()
        assert "const hasLocationNote = Boolean(" in html
        assert "forecast.location_note !== '已按请求地点解析'" in html
        assert "classList.toggle('hidden', !hasLocationNote)" in html

    def test_frontend_includes_xinjiang_city_selection(self):
        with open("frontend_prototype.html", encoding="utf-8") as f:
            html = f.read()
        assert "新疆维吾尔自治区" in html
        assert "乌鲁木齐市" in html
