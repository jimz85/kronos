#!/usr/bin/env python3
"""
order_executor.py - Unified order execution with retry and rate limiting
==========================================================================

Provides a unified OrderExecutor class that handles order execution with:
- Automatic retry with exponential backoff
- Rate limiting to prevent API throttling
- Execution result tracking and status reporting

Version: 5.0.0
"""

import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Any
from datetime import datetime, timedelta
import threading

logger = logging.getLogger('kronos.execution')


class ExecutionStatus(Enum):
    """Execution result status."""
    SUCCESS = "success"
    FAILED = "failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class ExecutionResult:
    """Result of an order execution attempt."""
    status: ExecutionStatus
    order_id: Optional[str] = None
    message: str = ""
    attempts: int = 0
    latency_ms: float = 0.0
    data: dict = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS


class RateLimiter:
    """Token bucket rate limiter for API calls."""

    def __init__(self, calls_per_second: float = 10.0, burst: int = 20):
        self.rate = calls_per_second
        self.burst = burst
        self.tokens = float(burst)
        self.last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        """Acquire a token, waiting up to timeout seconds."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_update
                self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
                self.last_update = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True

            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)


class OrderExecutor:
    """
    Unified order executor with retry and rate limiting.

    Features:
        - Configurable retry with exponential backoff
        - Token bucket rate limiting
        - Timeout handling
        - Execution result tracking

    Usage:
        executor = OrderExecutor(
            max_retries=3,
            base_delay=1.0,
            rate_limit=10.0  # 10 calls per second
        )

        result = await executor.execute(
            order_fn=lambda: exchange.place_order(...),
            order_id="order_123"
        )
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        rate_limit: float = 10.0,
        burst_limit: int = 20,
        timeout: float = 30.0,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.rate_limiter = RateLimiter(rate_limit, burst_limit)

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt using exponential backoff with jitter."""
        import random
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        jitter = delay * 0.1 * random.random()
        return delay + jitter

    def _execute_with_retry(
        self,
        order_fn: Callable[[], Any],
        order_id: Optional[str] = None,
    ) -> ExecutionResult:
        """
        Execute order function with retry logic.

        Args:
            order_fn: Callable that performs the actual order execution
            order_id: Optional order identifier for logging

        Returns:
            ExecutionResult with status and details
        """
        last_error = None
        start_time = time.monotonic()

        for attempt in range(self.max_retries + 1):
            try:
                if not self.rate_limiter.acquire(timeout=self.timeout):
                    return ExecutionResult(
                        status=ExecutionStatus.RATE_LIMITED,
                        message="Rate limiter timeout",
                        attempts=attempt + 1,
                        latency_ms=(time.monotonic() - start_time) * 1000,
                    )

                result = order_fn()
                latency_ms = (time.monotonic() - start_time) * 1000

                if result is None:
                    return ExecutionResult(
                        status=ExecutionStatus.SUCCESS,
                        order_id=order_id,
                        message="Order executed successfully",
                        attempts=attempt + 1,
                        latency_ms=latency_ms,
                    )

                if isinstance(result, dict):
                    if result.get('code') == '0' or result.get('data', [{}])[0].get('sCode') == '0':
                        return ExecutionResult(
                            status=ExecutionStatus.SUCCESS,
                            order_id=order_id or result.get('orderId'),
                            message="Order executed successfully",
                            attempts=attempt + 1,
                            latency_ms=latency_ms,
                            data=result,
                        )
                    else:
                        error_msg = result.get('msg', result.get('sMsg', 'Unknown error'))
                        last_error = Exception(error_msg)
                        if attempt < self.max_retries:
                            logger.warning(f"Order {order_id} attempt {attempt + 1} failed: {error_msg}")
                        continue
                elif isinstance(result, str):
                    if 'error' in result.lower() or 'fail' in result.lower():
                        last_error = Exception(result)
                        if attempt < self.max_retries:
                            logger.warning(f"Order {order_id} attempt {attempt + 1} failed: {result}")
                        continue

                return ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    order_id=order_id,
                    message="Order executed",
                    attempts=attempt + 1,
                    latency_ms=latency_ms,
                )

            except Exception as e:
                last_error = e
                latency_ms = (time.monotonic() - start_time) * 1000

                if "rate" in str(e).lower() or "429" in str(e):
                    if attempt < self.max_retries:
                        delay = self._calculate_delay(attempt)
                        logger.warning(f"Rate limited, retrying in {delay:.1f}s")
                        time.sleep(delay)
                        continue
                    return ExecutionResult(
                        status=ExecutionStatus.RATE_LIMITED,
                        message=str(e),
                        attempts=attempt + 1,
                        latency_ms=latency_ms,
                    )

                if attempt < self.max_retries:
                    delay = self._calculate_delay(attempt)
                    logger.warning(f"Order {order_id} attempt {attempt + 1} exception: {e}, retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue

                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    message=str(e),
                    attempts=attempt + 1,
                    latency_ms=latency_ms,
                )

        latency_ms = (time.monotonic() - start_time) * 1000
        return ExecutionResult(
            status=ExecutionStatus.RETRY_EXHAUSTED,
            message=str(last_error) if last_error else "Max retries exhausted",
            attempts=self.max_retries + 1,
            latency_ms=latency_ms,
        )

    def execute(
        self,
        order_fn: Callable[[], Any],
        order_id: Optional[str] = None,
    ) -> ExecutionResult:
        """
        Execute an order function with rate limiting and retry.

        This is the synchronous interface. For async usage, wrap with asyncio.to_thread().

        Args:
            order_fn: Callable that performs the order execution
            order_id: Optional order identifier for logging

        Returns:
            ExecutionResult with execution status and details
        """
        return self._execute_with_retry(order_fn, order_id)

    def execute_batch(
        self,
        orders: list[tuple[Callable[[], Any], Optional[str]]],
    ) -> list[ExecutionResult]:
        """
        Execute multiple orders in sequence with rate limiting.

        Args:
            orders: List of (order_fn, order_id) tuples

        Returns:
            List of ExecutionResult in same order as input
        """
        results = []
        for order_fn, order_id in orders:
            result = self.execute(order_fn, order_id)
            results.append(result)
        return results


# Module-level convenience function
def execute_order(
    order_fn: Callable[[], Any],
    order_id: Optional[str] = None,
    **kwargs,
) -> ExecutionResult:
    """
    Convenience function for single order execution.

    Args:
        order_fn: Callable that performs the order execution
        order_id: Optional order identifier
        **kwargs: Passed to OrderExecutor constructor

    Returns:
        ExecutionResult with execution status
    """
    executor = OrderExecutor(**kwargs)
    return executor.execute(order_fn, order_id)
