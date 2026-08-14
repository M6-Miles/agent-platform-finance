"""网络弹性层测试：重试、限流、熔断、错误分类。"""
from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from agent_platform.finance.network_resilience import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    ErrorCategory,
    RateLimiter,
    RetryConfig,
    call_with_retry,
    categorize_error,
)


class TestErrorCategorization:
    """错误分类测试。"""

    def test_timeout_is_retryable(self) -> None:
        exc = TimeoutError("Connection timed out")
        assert categorize_error(exc) == ErrorCategory.RETRYABLE

    def test_connection_error_is_retryable(self) -> None:
        exc = ConnectionError("Failed to establish connection")
        assert categorize_error(exc) == ErrorCategory.RETRYABLE

    def test_5xx_error_is_retryable(self) -> None:
        exc = RuntimeError("Server returned 503 Service Unavailable")
        assert categorize_error(exc) == ErrorCategory.RETRYABLE

    def test_invalid_param_is_non_retryable(self) -> None:
        exc = ValueError("invalid symbol parameter")
        assert categorize_error(exc) == ErrorCategory.NON_RETRYABLE

    def test_404_is_non_retryable(self) -> None:
        exc = RuntimeError("HTTP 404 Not Found")
        assert categorize_error(exc) == ErrorCategory.NON_RETRYABLE

    def test_unknown_error_is_unknown(self) -> None:
        exc = RuntimeError("some unknown error")
        assert categorize_error(exc) == ErrorCategory.UNKNOWN


class TestRetry:
    """重试机制测试。"""

    def test_success_on_first_attempt(self) -> None:
        mock_func = Mock(return_value="success")
        result = call_with_retry(mock_func)
        assert result == "success"
        assert mock_func.call_count == 1

    def test_success_after_retry(self) -> None:
        mock_func = Mock(side_effect=[TimeoutError("timeout"), "success"])
        result = call_with_retry(mock_func, config=RetryConfig(max_attempts=3))
        assert result == "success"
        assert mock_func.call_count == 2

    def test_non_retryable_error_fails_immediately(self) -> None:
        mock_func = Mock(side_effect=ValueError("invalid parameter"))
        with pytest.raises(ValueError, match="invalid parameter"):
            call_with_retry(mock_func, config=RetryConfig(max_attempts=3))
        assert mock_func.call_count == 1

    def test_exhausts_retries_on_persistent_failure(self) -> None:
        mock_func = Mock(side_effect=TimeoutError("persistent timeout"))
        with pytest.raises(TimeoutError, match="persistent timeout"):
            call_with_retry(mock_func, config=RetryConfig(max_attempts=3))
        assert mock_func.call_count == 3

    def test_unknown_error_calls_once_no_retry(self) -> None:
        """UNKNOWN 错误只调用一次，不重试。"""
        mock_func = Mock(side_effect=RuntimeError("some unknown error"))
        with pytest.raises(RuntimeError, match="some unknown error"):
            call_with_retry(mock_func, config=RetryConfig(max_attempts=3))
        assert mock_func.call_count == 1

    def test_exponential_backoff(self) -> None:
        call_times: list[float] = []
        def failing_func():
            call_times.append(time.monotonic())
            raise TimeoutError("timeout")

        config = RetryConfig(max_attempts=3, base_delay_s=0.1, exponential_base=2.0)
        with pytest.raises(TimeoutError):
            call_with_retry(failing_func, config=config)

        assert len(call_times) == 3
        # 第1次到第2次：约 0.1s
        # 第2次到第3次：约 0.2s
        assert 0.08 < call_times[1] - call_times[0] < 0.15
        assert 0.18 < call_times[2] - call_times[1] < 0.25


class TestRateLimiter:
    """限流器测试。"""

    def test_allows_calls_within_limit(self) -> None:
        limiter = RateLimiter(max_calls=3, window_s=1.0)
        assert limiter.acquire(timeout=0.1) is True
        assert limiter.acquire(timeout=0.1) is True
        assert limiter.acquire(timeout=0.1) is True

    def test_blocks_calls_exceeding_limit(self) -> None:
        limiter = RateLimiter(max_calls=2, window_s=1.0)
        assert limiter.acquire(timeout=0.01) is True
        assert limiter.acquire(timeout=0.01) is True
        assert limiter.acquire(timeout=0.1) is False  # 超出限制

    def test_sliding_window_releases_old_calls(self) -> None:
        limiter = RateLimiter(max_calls=2, window_s=0.2)
        assert limiter.acquire(timeout=0.01) is True
        assert limiter.acquire(timeout=0.01) is True
        assert limiter.acquire(timeout=0.01) is False  # 超出

        time.sleep(0.25)  # 等待窗口过期
        assert limiter.acquire(timeout=0.01) is True  # 现在可以了

    def test_raises_on_invalid_params(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(max_calls=0, window_s=1.0)
        with pytest.raises(ValueError):
            RateLimiter(max_calls=10, window_s=-1.0)

    def test_concurrent_acquire_respects_limit(self) -> None:
        """并发调用限流器，确保总通过数不超过限制。"""
        import threading
        limiter = RateLimiter(max_calls=5, window_s=0.5)
        success_count = [0]
        lock = threading.Lock()

        def try_acquire():
            if limiter.acquire(timeout=0.05):
                with lock:
                    success_count[0] += 1

        threads = [threading.Thread(target=try_acquire) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 最多允许 5 次成功
        assert success_count[0] <= 5


class TestCircuitBreaker:
    """熔断器测试。"""

    def test_closed_state_allows_calls(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)
        result = breaker.call(lambda: "success")
        assert result == "success"

    def test_raises_on_invalid_params(self) -> None:
        """构造参数必须为正数。"""
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreaker(failure_threshold=0)
        with pytest.raises(ValueError, match="recovery_timeout_s"):
            CircuitBreaker(failure_threshold=3, recovery_timeout_s=-1.0)
        with pytest.raises(ValueError, match="half_open_max_calls"):
            CircuitBreaker(failure_threshold=3, half_open_max_calls=0)

    def test_opens_after_consecutive_failures(self) -> None:
        """连续网络超时失败后熔断器应该开路。"""
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_s=0.5)

        # 连续网络超时 3 次（TimeoutError 是 RETRYABLE，计入熔断统计）
        for _ in range(3):
            with pytest.raises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        # 第 4 次调用应该立即被熔断器拒绝
        with pytest.raises(CircuitBreakerOpenError, match="熔断器开路"):
            breaker.call(lambda: "success")

    def test_transitions_to_half_open_after_timeout(self) -> None:
        """熔断器开路后，等待恢复超时应进入 HALF_OPEN 状态。"""
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.2)

        # 连续网络超时 2 次，熔断器开路
        for _ in range(2):
            with pytest.raises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        # 等待恢复超时
        time.sleep(0.25)

        # 现在应该进入 HALF_OPEN，允许尝试
        result = breaker.call(lambda: "recovered")
        assert result == "recovered"

    def test_half_open_closes_on_success(self) -> None:
        """HALF_OPEN 状态下成功后应回到 CLOSED。"""
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.2)

        # 开路
        for _ in range(2):
            with pytest.raises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        time.sleep(0.25)

        # HALF_OPEN 成功后应该回到 CLOSED
        breaker.call(lambda: "success")
        # 再次调用应该正常工作，而不是抛出熔断器异常
        result = breaker.call(lambda: "still working")
        assert result == "still working"

    def test_half_open_reopens_on_failure(self) -> None:
        """HALF_OPEN 状态下失败后应重新开路。"""
        breaker = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout_s=0.2,
            half_open_max_calls=1,
        )

        # 开路
        for _ in range(2):
            with pytest.raises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        time.sleep(0.25)

        # HALF_OPEN 失败后应该重新开路
        with pytest.raises(TimeoutError):
            breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("still failing")))

        # 立即再次调用应该被熔断器拒绝
        with pytest.raises(CircuitBreakerOpenError):
            breaker.call(lambda: "success")

    def test_success_resets_failure_count(self) -> None:
        """成功调用应重置失败计数。"""
        breaker = CircuitBreaker(failure_threshold=3)

        # 失败 2 次（网络超时）
        for _ in range(2):
            with pytest.raises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        # 成功 1 次应该重置计数
        breaker.call(lambda: "success")

        # 再失败 2 次不应该开路（因为计数已重置）
        for _ in range(2):
            with pytest.raises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        # 还可以再失败 1 次
        with pytest.raises(TimeoutError):
            breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        # 现在应该开路
        with pytest.raises(CircuitBreakerOpenError):
            breaker.call(lambda: "blocked")

    def test_non_retryable_error_does_not_trip_breaker(self) -> None:
        """参数错误、格式错误不应计入熔断统计。"""
        breaker = CircuitBreaker(failure_threshold=2)

        # 连续 3 次参数错误
        for _ in range(3):
            with pytest.raises(ValueError):
                breaker.call(lambda: (_ for _ in ()).throw(ValueError("invalid param")))

        # 熔断器应该仍然是 CLOSED，允许正常调用
        result = breaker.call(lambda: "success")
        assert result == "success"

        # 再来 2 次网络错误才会开路
        for _ in range(2):
            with pytest.raises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        with pytest.raises(CircuitBreakerOpenError):
            breaker.call(lambda: "blocked")

    def test_unknown_error_does_not_trip_breaker(self) -> None:
        """UNKNOWN 类别错误不应计入熔断统计。"""
        breaker = CircuitBreaker(failure_threshold=2)

        # 连续 3 次 UNKNOWN 错误（普通 RuntimeError 无明确 5xx/网络特征）
        for _ in range(3):
            with pytest.raises(RuntimeError):
                breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("some unknown error")))

        # 熔断器应该仍然是 CLOSED，允许正常调用
        result = breaker.call(lambda: "success")
        assert result == "success"

        # 再来 2 次网络错误才会开路
        for _ in range(2):
            with pytest.raises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

        with pytest.raises(CircuitBreakerOpenError):
            breaker.call(lambda: "blocked")
