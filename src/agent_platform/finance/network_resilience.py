"""网络弹性工具：有界超时、有限重试、指数退避、限流、熔断。

只用于真实行情源网络调用；离线样例数据不经过此层。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ErrorCategory(Enum):
    """错误分类：决定是否重试。"""
    RETRYABLE = "retryable"  # 超时、连接失败、5xx
    NON_RETRYABLE = "non_retryable"  # 参数错误、404、认证失败
    UNKNOWN = "unknown"  # 未分类错误，默认不重试


def categorize_error(exc: Exception) -> ErrorCategory:
    """判断异常是否可重试。

    可重试：网络超时、连接失败、DNS解析失败、5xx服务端错误
    不可重试：参数错误、404、认证失败、4xx客户端错误
    """
    exc_type_name = type(exc).__name__
    exc_message = str(exc).lower()

    # 网络层错误：可重试
    retryable_types = (
        "timeout", "timeouterror", "readtimeout", "connecttimeout",
        "connectionerror", "connectionrefusederror", "connectionreseterror",
        "gaierror",  # DNS 解析失败
    )
    if any(name in exc_type_name.lower() for name in retryable_types):
        return ErrorCategory.RETRYABLE

    # 明确的客户端错误：不可重试
    non_retryable_patterns = (
        "invalid", "参数", "格式", "401", "403", "404", "422",
        "unprocessable", "bad request", "unauthorized",
    )
    if any(pattern in exc_message for pattern in non_retryable_patterns):
        return ErrorCategory.NON_RETRYABLE

    # 5xx 服务端错误：可重试
    if any(code in exc_message for code in ("500", "502", "503", "504")):
        return ErrorCategory.RETRYABLE

    # 默认不重试未知错误，避免无限循环
    return ErrorCategory.UNKNOWN


@dataclass
class RetryConfig:
    """重试配置。"""
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 10.0
    exponential_base: float = 2.0


def call_with_retry(
    func: Callable[[], T],
    *,
    config: RetryConfig | None = None,
    context: str = "",
) -> T:
    """有限重试 + 指数退避，只重试 RETRYABLE 错误。

    UNKNOWN 错误默认不重试（调用一次后立即抛出），避免无限循环。

    Args:
        func: 要调用的函数（无参数）
        config: 重试配置
        context: 上下文描述，用于日志

    Raises:
        最后一次尝试的异常
    """
    cfg = config or RetryConfig()
    last_exc: Exception | None = None

    for attempt in range(1, cfg.max_attempts + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            category = categorize_error(exc)

            # NON_RETRYABLE 和 UNKNOWN 都立即抛出，只重试 RETRYABLE
            if category == ErrorCategory.NON_RETRYABLE:
                logger.info(
                    "%s 不可重试错误 (%s): %s",
                    context, type(exc).__name__, exc,
                )
                raise

            if category == ErrorCategory.UNKNOWN:
                logger.info(
                    "%s 未分类错误不重试 (%s): %s",
                    context, type(exc).__name__, exc,
                )
                raise

            # 只有 RETRYABLE 才继续
            if attempt >= cfg.max_attempts:
                logger.warning(
                    "%s 重试 %d 次后仍失败 (%s): %s",
                    context, cfg.max_attempts, type(exc).__name__, exc,
                )
                raise

            # 指数退避
            delay = min(
                cfg.base_delay_s * (cfg.exponential_base ** (attempt - 1)),
                cfg.max_delay_s,
            )
            logger.debug(
                "%s 第 %d/%d 次尝试失败 (%s)，%0.1f 秒后重试",
                context, attempt, cfg.max_attempts, type(exc).__name__, delay,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


class RateLimiter:
    """滑动窗口限流器。"""

    def __init__(self, max_calls: int, window_s: float) -> None:
        """
        Args:
            max_calls: 窗口内最多调用次数
            window_s: 窗口大小（秒）
        """
        if max_calls <= 0 or window_s <= 0:
            raise ValueError("max_calls 和 window_s 必须为正")
        self.max_calls = max_calls
        self.window_s = window_s
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, *, timeout: float | None = None) -> bool:
        """等待直到可以执行调用，或超时。

        Args:
            timeout: 最长等待秒数，None 表示无限等待

        Returns:
            True 表示获得许可，False 表示超时
        """
        started_at = time.monotonic()

        while True:
            now = time.monotonic()
            sleep_duration: float | None = None

            with self._lock:
                # 清除窗口外的旧时间戳
                while self._timestamps and self._timestamps[0] <= now - self.window_s:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return True

                # 计算下次重试等待时间（在锁内读取但不 sleep）
                if self._timestamps:
                    sleep_until = self._timestamps[0] + self.window_s
                    sleep_duration = max(0.01, sleep_until - now)
                else:
                    sleep_duration = 0.01

            # 未获得许可
            if timeout is not None and (time.monotonic() - started_at) >= timeout:
                return False

            # 在锁外 sleep，避免阻塞其他线程
            if sleep_duration is not None:
                time.sleep(min(sleep_duration, 0.1))


class CircuitBreakerState(Enum):
    """熔断器状态。"""
    CLOSED = "closed"  # 正常工作
    OPEN = "open"  # 熔断开启，拒绝请求
    HALF_OPEN = "half_open"  # 尝试恢复


class CircuitBreaker:
    """熔断器：连续失败达到阈值后开路，避免雪崩。"""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
        half_open_max_calls: int = 1,
    ) -> None:
        """
        Args:
            failure_threshold: 连续失败多少次后开路
            recovery_timeout_s: 开路后多久尝试恢复
            half_open_max_calls: 半开状态下允许多少次尝试
        """
        if failure_threshold <= 0:
            raise ValueError("failure_threshold 必须为正数")
        if recovery_timeout_s <= 0:
            raise ValueError("recovery_timeout_s 必须为正数")
        if half_open_max_calls <= 0:
            raise ValueError("half_open_max_calls 必须为正数")

        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitBreakerState:
        with self._lock:
            return self._state

    def call(self, func: Callable[[], T], *, context: str = "") -> T:
        """通过熔断器调用函数。

        Raises:
            CircuitBreakerOpenError: 熔断器开路时
            原函数的异常
        """
        with self._lock:
            # OPEN 状态：检查是否可以进入 HALF_OPEN
            if self._state == CircuitBreakerState.OPEN:
                if (
                    self._last_failure_time is not None
                    and time.monotonic() - self._last_failure_time >= self.recovery_timeout_s
                ):
                    logger.info("%s 熔断器从 OPEN 进入 HALF_OPEN", context)
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._half_open_calls = 0
                else:
                    raise CircuitBreakerOpenError(
                        f"{context} 熔断器开路，距离恢复还需 "
                        f"{self.recovery_timeout_s - (time.monotonic() - (self._last_failure_time or 0)):.1f} 秒"
                    )

            # HALF_OPEN 状态：限制尝试次数
            if self._state == CircuitBreakerState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitBreakerOpenError(
                        f"{context} 熔断器 HALF_OPEN 状态，已达到最大尝试次数"
                    )
                self._half_open_calls += 1

        # 执行调用
        try:
            result = func()
            self._on_success(context)
            return result
        except Exception as exc:
            self._on_failure(context, exc)
            raise

    def _on_success(self, context: str) -> None:
        with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN:
                logger.info("%s 熔断器从 HALF_OPEN 恢复到 CLOSED", context)
                self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None

    def _on_failure(self, context: str, exc: Exception) -> None:
        with self._lock:
            # 只有可重试的网络/5xx 失败才计入熔断统计
            category = categorize_error(exc)
            if category != ErrorCategory.RETRYABLE:
                logger.debug(
                    "%s 熔断器忽略非网络错误 (%s): %s",
                    context, category.value, type(exc).__name__,
                )
                return

            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitBreakerState.HALF_OPEN:
                logger.warning("%s 熔断器 HALF_OPEN 尝试失败，重新开路", context)
                self._state = CircuitBreakerState.OPEN
                self._failure_count = 0
            elif (
                self._state == CircuitBreakerState.CLOSED
                and self._failure_count >= self.failure_threshold
            ):
                logger.warning(
                    "%s 熔断器连续失败 %d 次，开路",
                    context, self._failure_count,
                )
                self._state = CircuitBreakerState.OPEN
                self._failure_count = 0


class CircuitBreakerOpenError(RuntimeError):
    """熔断器开路异常。"""
