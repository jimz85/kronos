"""
OKX API Retry Decorator with Survival Tier Integration.
Implements async retry with exponential backoff and HTTP 429 Retry-After support.
"""
import asyncio
import functools
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

import sys as _sys
from pathlib import Path as _Path

# Allow running as standalone script or imported module
_root = _Path(__file__).parent.parent
if str(_root) not in _sys.path:
    _sys.path.insert(0, str(_root))

try:
    from kronos.real_monitor import RealMonitor, TreasuryTier
except ImportError:
    # Standalone test / no kronos package installed
    RealMonitor = None  # type: ignore
    TreasuryTier = None  # type: ignore

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class APIExhaustedError(Exception):
    """
    Raised when an API call exhausts all retry attempts.
    Contains structured data about the failure.
    """
    attempts: int
    last_error: str
    context: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"APIExhaustedError(attempts={self.attempts}, "
            f"last_error={self.last_error!r})"
        )


def _find_monitor() -> Optional[RealMonitor]:
    """
    Locate the global RealMonitor instance by searching
    standard kronos state file paths.
    """
    search_paths = [
        os.path.join(os.path.dirname(__file__), "data", "kronos_treasury.json"),
        os.path.expanduser("~/.kronos/data/kronos_treasury.json"),
        "data/kronos_treasury.json",
    ]
    for path in search_paths:
        if os.path.exists(path):
            try:
                return RealMonitor(path)
            except Exception:
                pass
    return None


def _downgrade_tier(monitor: Optional[RealMonitor], reason: str) -> None:
    """
    Downgrade survival tier toward EMERGENCY on API exhaustion.
    
    Progression: NORMAL -> CAUTION -> CRITICAL -> EMERGENCY -> SUSPENDED
    """
    if monitor is None:
        logger.warning("No RealMonitor found for tier downgrade: %s", reason)
        return

    try:
        current = monitor.get_tier()
        if current == TreasuryTier.SUSPENDED:
            return  # Already at lowest tier

        downgrade_map = {
            TreasuryTier.NORMAL:   TreasuryTier.CAUTION,
            TreasuryTier.CAUTION:  TreasuryTier.CRITICAL,
            TreasuryTier.CRITICAL: TreasuryTier.EMERGENCY,
            TreasuryTier.EMERGENCY: TreasuryTier.SUSPENDED,
        }
        next_tier = downgrade_map.get(current, TreasuryTier.CRITICAL)

        logger.warning(
            "API exhaustion forcing tier downgrade: %s -> %s (%s)",
            current.value, next_tier.value, reason
        )

        # Apply the tier downgrade directly to state
        if next_tier == TreasuryTier.EMERGENCY:
            monitor._handle_emergency()
        elif next_tier == TreasuryTier.SUSPENDED:
            monitor.state.manual_suspend = True
            monitor.state.tier = TreasuryTier.SUSPENDED.value
        else:
            monitor.state.tier = next_tier.value

        monitor.state.save(monitor.state_file)
    except Exception as exc:
        logger.error("Failed to downgrade survival tier: %s", exc)


def async_api_retry(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    monitor_path: Optional[str] = None,
):
    """
    Async retry decorator for OKX API calls with exponential backoff.

    Features:
        - Max 5 retries with exponential backoff (asyncio.sleep only)
        - HTTP 429 responses honour the Retry-After header
        - Network exceptions trigger retry with backoff
        - On exhaustion: raise APIExhaustedError with structured data
          and downgrade the survival tier

    Args:
        max_retries: Maximum number of retry attempts (default 5)
        base_delay:  Initial backoff delay in seconds (default 1.0)
        max_delay:   Maximum delay cap in seconds (default 60.0)
        jitter:      Add random jitter to delays (default True)
        monitor_path: Optional path to RealMonitor state file;
                     if omitted the decorator searches standard paths

    Raises:
        APIExhaustedError: After all retries are exhausted, containing
                           attempts count and the last error message.
        The decorated function's own exceptions are NOT caught on success.

    Example:
        @async_api_retry(max_retries=5, base_delay=2.0)
        async def get_balances():
            return await okx_client.get("/api/v5/account/balance")
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            _monitor: Optional[RealMonitor] = None
            if monitor_path:
                _monitor = RealMonitor(monitor_path)
            else:
                _monitor = _find_monitor()

            last_error: str = "unknown"
            last_exception: Optional[Exception] = None

            for attempt in range(1, max_retries + 1):
                try:
                    result = await func(*args, **kwargs)

                    # Treat common HTTP error status codes as retryable
                    # (the underlying client may return response dicts)
                    if isinstance(result, dict):
                        code = result.get("code", "")
                        # OKX success code is "0"
                        if code == "0":
                            return result
                        # Rate-limit / circuit-breaker codes
                        if code in ("50198", "58102", "58103"):
                            last_error = f"OKX rate-limit code={code}"
                            raise APIExhaustedError(
                                attempts=attempt,
                                last_error=last_error,
                                context={"code": code}
                            )
                        # Other non-zero codes are returned as-is
                        # (caller should handle)
                        return result

                    return result

                except APIExhaustedError:
                    # Already structured — re-raise immediately
                    raise

                except asyncio.CancelledError:
                    # Propagate cancellation without retry
                    raise

                except Exception as exc:  # noqa: BLE001
                    last_exception = exc
                    last_error = f"{type(exc).__name__}: {exc}"
                    http_code: Optional[int] = None
                    retry_after: Optional[float] = None

                    # Extract HTTP status code if available
                    if hasattr(exc, "status") and isinstance(exc.status, int):
                        http_code = exc.status
                    elif hasattr(exc, "response") and hasattr(exc.response, "status"):
                        http_code = exc.response.status

                    # Extract Retry-After from 429 responses
                    if http_code == 429:
                        headers: dict = {}
                        if hasattr(exc, "headers"):
                            headers = getattr(exc, "headers", {}) or {}
                        elif hasattr(exc, "response") and hasattr(exc.response, "headers"):
                            headers = exc.response.headers or {}

                        raw_retry_after = headers.get("Retry-After") or headers.get("retry-after")
                        if raw_retry_after:
                            try:
                                retry_after = float(raw_retry_after)
                            except ValueError:
                                pass

                        if retry_after is None:
                            # Default OKX Retry-After when header absent
                            retry_after = 10.0

                        logger.warning(
                            "HTTP 429 received [attempt %d/%d], Retry-After=%.1fs",
                            attempt, max_retries, retry_after
                        )
                    else:
                        # Network or other HTTP error — compute backoff
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        if jitter:
                            delay += random.uniform(0.0, 0.5 * delay)

                        logger.warning(
                            "API call failed [attempt %d/%d] %s, retrying in %.1fs",
                            attempt, max_retries, last_error, delay
                        )

                        # Sleep using asyncio.sleep only
                        await asyncio.sleep(delay)
                        continue

                    # 429 path — sleep for Retry-After then retry
                    if retry_after and attempt < max_retries:
                        await asyncio.sleep(retry_after)

            # All retries exhausted
            _downgrade_tier(_monitor, last_error)

            final_error = last_error
            if last_exception and final_error == "unknown":
                final_error = f"{type(last_exception).__name__}: {last_exception}"

            raise APIExhaustedError(
                attempts=max_retries,
                last_error=final_error,
                context={"last_exception_type": type(last_exception).__name__}
                if last_exception else {}
            )

        return wrapper  # type: ignore[return-value]
    return decorator
