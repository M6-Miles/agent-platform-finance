"""网络弹性层集成测试：限流、熔断、超时在真实调用路径中的表现。"""
from __future__ import annotations

import pandas as pd
import pytest

from agent_platform.finance.akshare_data_provider import AkShareMarketDataProvider
from agent_platform.finance.errors import MarketDataUnavailableError
from agent_platform.finance.network_resilience import CircuitBreaker, CircuitBreakerState


class TestRateLimiterIntegration:
    """限流器在真实调用路径中的表现。"""

    def test_rate_limit_enforced_on_production_path(self) -> None:
        """超过限流上限时明确失败。"""
        provider = AkShareMarketDataProvider(
            stock_list_loader=lambda: pd.DataFrame({"code": ["600519"], "name": ["茅台"]}),
            history_loader=lambda **_kw: pd.DataFrame(),
        )
        class DenyingLimiter:
            def acquire(self, *, timeout=None):
                assert timeout == 30.0
                return False

        provider._rate_limiter = DenyingLimiter()
        with pytest.raises(MarketDataUnavailableError, match="限流等待超时"):
            provider._network_call(lambda: pytest.fail("限流后不应调用数据源"), context="测试限流")


class TestCircuitBreakerIntegration:
    """熔断器在真实调用路径中的表现。"""

    def test_circuit_opens_after_retryable_failures(self, monkeypatch) -> None:
        """连续可重试网络失败达到阈值后开路。"""
        call_count = [0]

        def failing_loader():
            call_count[0] += 1
            raise TimeoutError("network timeout")

        provider = AkShareMarketDataProvider(
            stock_list_loader=lambda: pd.DataFrame({"code": ["600519"], "name": ["茅台"]}),
        )
        provider._circuit_breaker = CircuitBreaker(
            failure_threshold=2, recovery_timeout_s=30.0
        )
        monkeypatch.setattr("agent_platform.finance.network_resilience.time.sleep", lambda _s: None)

        # 前2次会重试并失败
        for _ in range(2):
            with pytest.raises((TimeoutError, MarketDataUnavailableError)):
                provider._network_call(failing_loader, context="测试熔断")

        calls_before_open_check = call_count[0]
        with pytest.raises(MarketDataUnavailableError, match="熔断器开路"):
            provider._network_call(failing_loader, context="测试熔断")
        assert call_count[0] == calls_before_open_check

    def test_circuit_recovers_after_timeout(self, monkeypatch) -> None:
        """成功后恢复/计数重置。"""
        call_count = [0]

        def conditional_loader():
            call_count[0] += 1
            if call_count[0] <= 6:
                raise TimeoutError("fail")
            return pd.DataFrame({"日期": [], "开盘": []})

        provider = AkShareMarketDataProvider(
            stock_list_loader=lambda: pd.DataFrame({"code": ["600519"], "name": ["茅台"]}),
        )
        provider._circuit_breaker = CircuitBreaker(
            failure_threshold=2, recovery_timeout_s=0.01
        )
        monkeypatch.setattr("agent_platform.finance.network_resilience.time.sleep", lambda _s: None)

        # 2次失败后开路
        for _ in range(2):
            with pytest.raises((TimeoutError, MarketDataUnavailableError)):
                provider._network_call(conditional_loader, context="测试恢复")

        provider._circuit_breaker._last_failure_time -= 1.0

        # 现在应该进入 HALF_OPEN，允许尝试并成功
        result = provider._network_call(conditional_loader, context="测试恢复")
        assert isinstance(result, pd.DataFrame)

        # 再次调用应正常工作（计数已重置）
        result = provider._network_call(conditional_loader, context="测试恢复")
        assert isinstance(result, pd.DataFrame)

    def test_non_retryable_error_does_not_trip_circuit(self) -> None:
        """参数/格式错误不应触发重试，也不应被当作服务端暂时故障反复调用。"""
        call_count = [0]

        def param_error_loader():
            call_count[0] += 1
            raise ValueError("invalid parameter")

        provider = AkShareMarketDataProvider(
            stock_list_loader=lambda: pd.DataFrame({"code": ["600519"], "name": ["茅台"]}),
        )
        provider._circuit_breaker = CircuitBreaker(
            failure_threshold=2, recovery_timeout_s=30.0
        )

        # 参数错误应立即抛出，只调用一次
        with pytest.raises(ValueError, match="invalid parameter"):
            provider._network_call(param_error_loader, context="测试非可重试")

        assert call_count[0] == 1

        # 熔断器应该仍然是 CLOSED
        assert provider._circuit_breaker.state == CircuitBreakerState.CLOSED
