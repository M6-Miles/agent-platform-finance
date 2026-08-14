from __future__ import annotations

from agent_platform.core.provider_health import ProviderHealthRegistry


def test_provider_health_records_latency_cache_and_backoff() -> None:
    registry = ProviderHealthRegistry()
    registry.configure("quote", True)
    registry.record_success("quote", 12.5, source="公开源", data_at="2026-08-14T10:00:00Z")
    registry.mark_cache_hit("quote", source="公开源", data_at="2026-08-14T10:00:00Z")
    delay = registry.record_failure("quote", 30.0, TimeoutError("secret detail"))

    result = registry.snapshot()["providers"]["quote"]
    assert delay == 3.0
    assert result["status"] == "degraded"
    assert result["cache_hits"] == 1
    assert result["latency_p95_ms"] == 30.0
    assert "secret detail" not in result["last_error"]
    assert registry.can_attempt("quote") is False
    assert registry.snapshot()["alerts"]


def test_provider_health_success_resets_backoff() -> None:
    registry = ProviderHealthRegistry()
    registry.record_failure("weather", 10, ConnectionError("offline"))
    registry.record_success("weather", 5, status="real_time")

    result = registry.snapshot()["providers"]["weather"]
    assert result["consecutive_failures"] == 0
    assert result["retry_after_s"] == 0.0
    assert result["status"] == "real_time"


def test_manual_force_probe_bypasses_backoff_once() -> None:
    registry = ProviderHealthRegistry()
    registry.record_failure("market", 1.0, RuntimeError("offline"))
    calls = 0

    def probe() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "real_time", "source": "manual", "updated_at": "now"}

    registry.probe("market", probe)
    assert calls == 0
    registry.probe("market", probe, force=True)
    assert calls == 1
    assert registry.snapshot()["providers"]["market"]["status"] == "real_time"
