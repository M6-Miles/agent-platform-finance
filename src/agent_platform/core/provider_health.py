"""Thread-safe health, backoff, and latency metrics for external providers."""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return round(ordered[index], 2)


@dataclass
class ProviderHealthEntry:
    name: str
    configured: bool = True
    status: str = "unknown"
    source: str | None = None
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_data_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    next_retry_monotonic: float = 0.0
    calls: int = 0
    successes: int = 0
    cache_hits: int = 0
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=500))

    def to_dict(self, now_monotonic: float) -> dict[str, Any]:
        retry_after = max(0.0, self.next_retry_monotonic - now_monotonic)
        return {
            "name": self.name,
            "configured": self.configured,
            "status": self.status,
            "source": self.source,
            "last_checked_at": self.last_checked_at,
            "last_success_at": self.last_success_at,
            "last_data_at": self.last_data_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "retry_after_s": round(retry_after, 1),
            "calls": self.calls,
            "success_rate_pct": round(self.successes / self.calls * 100, 1) if self.calls else 0.0,
            "cache_hits": self.cache_hits,
            "cache_hit_rate_pct": round(self.cache_hits / self.calls * 100, 1) if self.calls else 0.0,
            "latency_p95_ms": _nearest_rank(list(self.latencies_ms), 95),
        }


class ProviderHealthRegistry:
    """Records provider truth without persisting credentials or raw responses."""

    def __init__(self) -> None:
        self._entries: dict[str, ProviderHealthEntry] = {}
        self._lock = threading.RLock()

    def configure(self, name: str, configured: bool, *, status: str | None = None) -> None:
        with self._lock:
            entry = self._entries.setdefault(name, ProviderHealthEntry(name=name))
            entry.configured = configured
            if status is not None:
                entry.status = status
            elif not configured:
                entry.status = "not_configured"

    def can_attempt(self, name: str) -> bool:
        with self._lock:
            entry = self._entries.get(name)
            return entry is None or time.monotonic() >= entry.next_retry_monotonic

    def record_success(
        self,
        name: str,
        latency_ms: float,
        *,
        status: str = "real_time",
        source: str | None = None,
        data_at: str | None = None,
        cache_hit: bool = False,
    ) -> None:
        checked = _utc_now()
        with self._lock:
            entry = self._entries.setdefault(name, ProviderHealthEntry(name=name))
            entry.calls += 1
            entry.successes += 1
            entry.cache_hits += int(cache_hit)
            entry.latencies_ms.append(max(0.0, float(latency_ms)))
            entry.status = status
            entry.source = source or entry.source
            entry.last_checked_at = checked
            entry.last_success_at = checked
            entry.last_data_at = data_at or entry.last_data_at
            entry.last_error = None
            entry.consecutive_failures = 0
            entry.next_retry_monotonic = 0.0

    def record_failure(self, name: str, latency_ms: float, error: Exception | str) -> float:
        checked = _utc_now()
        with self._lock:
            entry = self._entries.setdefault(name, ProviderHealthEntry(name=name))
            entry.calls += 1
            entry.latencies_ms.append(max(0.0, float(latency_ms)))
            entry.last_checked_at = checked
            entry.consecutive_failures += 1
            delay = min(900.0, 3.0 * (2 ** (entry.consecutive_failures - 1)))
            entry.next_retry_monotonic = time.monotonic() + delay
            entry.status = "degraded" if entry.last_success_at else "unavailable"
            entry.last_error = f"{type(error).__name__}: provider request failed" if isinstance(error, Exception) else str(error)
            return delay

    def mark_cache_hit(self, name: str, *, source: str | None = None, data_at: str | None = None) -> None:
        checked = _utc_now()
        with self._lock:
            entry = self._entries.setdefault(name, ProviderHealthEntry(name=name))
            entry.calls += 1
            entry.cache_hits += 1
            entry.status = "cached"
            entry.source = source or entry.source
            entry.last_checked_at = checked
            entry.last_data_at = data_at or entry.last_data_at

    def probe(
        self,
        name: str,
        func: Callable[[], dict[str, Any]],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run a check; deliberate user refreshes may bypass backoff once."""
        if not force and not self.can_attempt(name):
            return self.snapshot()["providers"].get(name, {})
        started = time.perf_counter()
        try:
            result = func()
        except Exception as exc:
            self.record_failure(name, (time.perf_counter() - started) * 1000, exc)
        else:
            self.record_success(
                name,
                (time.perf_counter() - started) * 1000,
                status=str(result.get("status") or "real_time"),
                source=result.get("source"),
                data_at=result.get("updated_at"),
            )
        return self.snapshot()["providers"].get(name, {})

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            providers = {
                name: entry.to_dict(now) for name, entry in sorted(self._entries.items())
            }
        alert_states = {"degraded", "unavailable"}
        alerts = [
            {"provider": name, "level": "warning", "message": "外部服务连续失败，已进入退避等待"}
            for name, value in providers.items()
            if value["status"] in alert_states or value["consecutive_failures"] > 0
        ]
        return {"checked_at": _utc_now(), "providers": providers, "alerts": alerts}
