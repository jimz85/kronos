#!/usr/bin/env python3
"""
exceptions.py - Unified Exception Hierarchy for Kronos
========================================================

Centralized exception handling for all Kronos modules.

Classes:
    KronosError - Base exception for all Kronos errors
    APIError - API-related errors with code and message
    RateLimitError - Rate limiting errors (subclass of APIError)
    NetworkError - Network connectivity errors
    TradingError - Trading operation errors
    ValidationError - Data validation errors

Decorators:
    with_retry - Retry decorator with exponential backoff

Version: v5.0.0
"""

import time
import functools
import logging
from typing import Optional, Type, Tuple, Callable, Any

logger = logging.getLogger('kronos.exceptions')


# ============================================================================
# Exception Hierarchy
# ============================================================================

class KronosError(Exception):
    """Base exception for all Kronos errors."""
    
    def __init__(self, message: str = "", **kwargs):
        super().__init__(message)
        self.message = message
        self.extra = kwargs
    
    def __str__(self) -> str:
        if self.extra:
            return f"{self.message} | {self.extra}"
        return self.message


class APIError(KronosError):
    """API-related errors with code and message."""
    
    def __init__(self, code: str, msg: str, **kwargs):
        super().__init__(f"APIError[{code}]: {msg}", **kwargs)
        self.code = code
        self.msg = msg
        self.extra = kwargs
    
    def __str__(self) -> str:
        extra_str = f" | {self.extra}" if self.extra else ""
        return f"APIError[{self.code}]: {self.msg}{extra_str}"


class RateLimitError(APIError):
    """Rate limiting errors from API endpoints."""
    
    def __init__(self, msg: str = "Rate limit exceeded", retry_after: Optional[float] = None, **kwargs):
        super().__init__(code="RATE_LIMIT", msg=msg, **kwargs)
        self.retry_after = retry_after
    
    def __str__(self) -> str:
        base = super().__str__()
        if self.retry_after:
            return f"{base} | retry_after={self.retry_after}s"
        return base


class NetworkError(KronosError):
    """Network connectivity and request errors."""
    
    def __init__(self, message: str = "", status_code: Optional[int] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.status_code = status_code
    
    def __str__(self) -> str:
        base = super().__str__()
        if self.status_code:
            return f"{base} | status={self.status_code}"
        return base


class TradingError(KronosError):
    """Trading operation errors (orders, positions, etc.)."""
    
    def __init__(self, message: str = "", symbol: Optional[str] = None, 
                 order_id: Optional[str] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.symbol = symbol
        self.order_id = order_id
    
    def __str__(self) -> str:
        base = super().__str__()
        parts = []
        if self.symbol:
            parts.append(f"symbol={self.symbol}")
        if self.order_id:
            parts.append(f"order_id={self.order_id}")
        if parts:
            return f"{base} | {', '.join(parts)}"
        return base


class ValidationError(KronosError):
    """Data validation and input errors."""
    
    def __init__(self, message: str = "", field: Optional[str] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.field = field
    
    def __str__(self) -> str:
        base = super().__str__()
        if self.field:
            return f"{base} | field={self.field}"
        return base


# ============================================================================
# Retry Decorator
# ============================================================================

def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[Exception, int], None]] = None,
) -> Callable:
    """
    Retry decorator with exponential backoff.
    
    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Initial delay between retries in seconds
        max_delay: Maximum delay between retries in seconds
        exponential_base: Base for exponential backoff calculation
        exceptions: Tuple of exception types to catch and retry
        on_retry: Optional callback(exception, attempt) for custom retry handling
    
    Returns:
        Decorated function with retry logic
    
    Example:
        @with_retry(max_attempts=3, exceptions=(NetworkError, RateLimitError))
        def fetch_data():
            return api.get()
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    
                    if attempt == max_attempts:
                        logger.error(
                            f"{func.__name__} failed after {max_attempts} attempts: {e}"
                        )
                        raise
                    
                    # Calculate delay with exponential backoff
                    delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)
                    
                    logger.warning(
                        f"{func.__name__} attempt {attempt}/{max_attempts} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    
                    if on_retry:
                        on_retry(e, attempt)
                    
                    time.sleep(delay)
            
            # Should not reach here, but just in case
            if last_exception:
                raise last_exception
        
        return wrapper
    return decorator


# ============================================================================
# Convenience Functions
# ============================================================================

def is_kronos_error(e: Exception) -> bool:
    """Check if an exception is a KronosError or subclass."""
    return isinstance(e, KronosError)


def safe_raise(ex: Exception, reraise: Optional[Type[KronosError]] = None) -> None:
    """
    Safely raise an exception, converting non-Kronos errors to KronosError if specified.
    
    Args:
        ex: The exception to raise
        reraise: Optional KronosError subclass to convert to
    """
    if reraise and not isinstance(ex, KronosError):
        raise reraise(str(ex)) from ex
    raise ex
