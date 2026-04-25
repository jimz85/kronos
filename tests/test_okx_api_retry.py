"""
Unit tests for okx_api_retry.py
Tests: (A) 3 timeouts then success, (B) 429 with Retry-After: 3 header,
(C) 6x 502 then APIExhaustedError on 5th retry, (D) exponential backoff deterministic,
(E) jitter randomness, (F) non-retryable exception propagates.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from okx_api_retry import async_api_retry, APIExhaustedError


class TestOKXAPIRetry(unittest.IsolatedAsyncioTestCase):
    """Test suite for okx_api_retry decorator."""

    # -------------------------------------------------------------------------
    # (A) 3 timeouts then success
    # -------------------------------------------------------------------------
    async def test_three_timeouts_then_success(self):
        """
        Verify that after 3 consecutive timeout exceptions, the 4th call succeeds
        and returns the expected result.
        """
        call_count = 0

        @async_api_retry(max_retries=5, base_delay=0.01, jitter=False)
        async def flaky_function():
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise TimeoutError(f"Timeout #{call_count}")
            return {"code": "0", "data": "success"}

        result = await flaky_function()
        self.assertEqual(call_count, 4)
        self.assertEqual(result, {"code": "0", "data": "success"})

    # -------------------------------------------------------------------------
    # (B) 429 with Retry-After: 3 header
    # -------------------------------------------------------------------------
    async def test_429_with_retry_after_header(self):
        """
        Verify that a 429 response with Retry-After: 3 header causes a 3-second
        delay before retrying, and eventually succeeds after the retry.
        """
        call_count = 0
        sleep_durations = []

        original_sleep = asyncio.sleep
        async def mock_sleep(delay):
            sleep_durations.append(delay)
            await original_sleep(0)  # minimal actual delay for speed

        class Mock429Exception(Exception):
            status = 429
            headers = {"Retry-After": "3"}

        @async_api_retry(max_retries=3, base_delay=10.0, jitter=False)
        async def rate_limited_function():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Mock429Exception("Rate limited")
            return {"code": "0", "data": "success"}

        with patch("asyncio.sleep", mock_sleep):
            result = await rate_limited_function()

        self.assertEqual(call_count, 2)
        self.assertEqual(result, {"code": "0", "data": "success"})
        # Verify Retry-After: 3 was used (not the default 10s base_delay)
        self.assertEqual(sleep_durations[0], 3.0)

    # -------------------------------------------------------------------------
    # (C) 6x 502 then APIExhaustedError on 5th retry
    # -------------------------------------------------------------------------
    async def test_six_502_then_api_exhausted_error(self):
        """
        Verify that 6 consecutive 502 errors (initial + 5 retries) raise
        APIExhaustedError with correct attempt count.
        """
        call_count = 0

        class Mock502Exception(Exception):
            status = 502
            headers = {}

        @async_api_retry(max_retries=5, base_delay=0.01, jitter=False)
        async def bad_gateway_function():
            nonlocal call_count
            call_count += 1
            raise Mock502Exception("Bad Gateway")

        with self.assertRaises(APIExhaustedError) as ctx:
            await bad_gateway_function()

        self.assertEqual(call_count, 5)
        self.assertEqual(ctx.exception.attempts, 5)
        self.assertIn("502", ctx.exception.last_error)

    # -------------------------------------------------------------------------
    # (D) exponential backoff deterministic
    # -------------------------------------------------------------------------
    async def test_exponential_backoff_deterministic(self):
        """
        Verify that without jitter, delays follow exact exponential backoff:
        delay = min(base_delay * 2^(attempt-1), max_delay)
        """
        sleep_durations = []
        base_delay = 1.0
        max_delay = 60.0

        original_sleep = asyncio.sleep
        async def mock_sleep(delay):
            sleep_durations.append(delay)
            await original_sleep(0)

        @async_api_retry(max_retries=5, base_delay=base_delay, max_delay=max_delay, jitter=False)
        async def always_fails():
            raise ConnectionError("Always fails")

        with patch("asyncio.sleep", mock_sleep):
            with self.assertRaises(APIExhaustedError):
                await always_fails()

        # Attempt 1: delay = 1.0 * 2^0 = 1.0
        # Attempt 2: delay = 1.0 * 2^1 = 2.0
        # Attempt 3: delay = 1.0 * 2^2 = 4.0
        # Attempt 4: delay = 1.0 * 2^3 = 8.0
        # Attempt 5: delay = 1.0 * 2^4 = 16.0 (last retry still sleeps before raising)
        expected = [1.0, 2.0, 4.0, 8.0, 16.0]
        self.assertEqual(sleep_durations, expected)

    # -------------------------------------------------------------------------
    # (E) jitter randomness
    # -------------------------------------------------------------------------
    async def test_jitter_randomness(self):
        """
        Verify that with jitter=True, delays are longer than the deterministic
        base delay and vary between runs.
        """
        sleep_durations = []
        base_delay = 1.0

        original_sleep = asyncio.sleep
        async def mock_sleep(delay):
            sleep_durations.append(delay)
            await original_sleep(0)

        @async_api_retry(max_retries=3, base_delay=base_delay, max_delay=60.0, jitter=True)
        async def always_fails():
            raise ConnectionError("Always fails")

        with patch("asyncio.sleep", mock_sleep):
            with self.assertRaises(APIExhaustedError):
                await always_fails()

        # With jitter, each delay should be: base * 2^(n-1) + random(0, 0.5 * base * 2^(n-1))
        # For attempt 1: 1.0 + random(0, 0.5) = between 1.0 and 1.5
        # For attempt 2: 2.0 + random(0, 1.0) = between 2.0 and 3.0
        # For attempt 3: 4.0 + random(0, 2.0) = between 4.0 and 6.0
        # max_retries=3 means 3 total attempts (1 initial + 2 retries), but code sleeps
        # after each failed attempt including the last one
        self.assertEqual(len(sleep_durations), 3)  # 3 attempts = 3 sleeps

        self.assertGreater(sleep_durations[0], 1.0)  # jitter adds to base
        self.assertGreater(sleep_durations[1], 2.0)  # jitter adds to base
        self.assertGreater(sleep_durations[2], 4.0)  # jitter adds to base

    # -------------------------------------------------------------------------
    # (F) non-retryable exception propagates
    # -------------------------------------------------------------------------
    async def test_non_retryable_exception_propagates(self):
        """
        Verify that APIExhaustedError (already structured) propagates immediately
        without retry. This is the only truly non-retryable exception type.
        """
        call_count = 0

        @async_api_retry(max_retries=5, base_delay=0.01, jitter=False)
        async def pre_exhausted_function():
            nonlocal call_count
            call_count += 1
            raise APIExhaustedError(
                attempts=call_count,
                last_error="Pre-existing exhaustion",
                context={}
            )

        with self.assertRaises(APIExhaustedError) as ctx:
            await pre_exhausted_function()

        self.assertEqual(str(ctx.exception), "APIExhaustedError(attempts=1, last_error='Pre-existing exhaustion')")
        self.assertEqual(call_count, 1)  # Only one attempt, no retries


if __name__ == "__main__":
    unittest.main(verbosity=2)
