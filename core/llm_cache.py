#!/usr/bin/env python3
"""
llm_cache.py - Redis-based LLM Request Caching for Kronos
========================================================

A Redis caching layer to cache LLM API requests, reducing API calls
and improving response times for repeated or similar prompts.

Key Features:
    - SHA256 hash-based cache keys for efficient lookup
    - Configurable TTL per LLM provider (MiniMax, Ollama, etc.)
    - Cache hit/miss statistics tracking
    - Thread-safe operations
    - Graceful fallback when Redis is unavailable

Version: 1.0.0
"""

import os
import json
import hashlib
import logging
import threading
import time
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass, field
from functools import wraps

logger = logging.getLogger('kronos.llm_cache')

# Redis availability flag
REDIS_AVAILABLE = False
_redis_client = None

# Try to import redis
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    logger.warning("redis package not found, LLM caching will be disabled")


@dataclass
class CacheStats:
    """Statistics for LLM cache operations."""
    hits: int = 0
    misses: int = 0
    errors: int = 0
    total_latency_saved_ms: float = 0.0  # Estimated latency saved from cache hits
    
    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class LLMRequestCache:
    """
    Redis-based cache for LLM API requests.
    
    This class caches LLM responses based on a hash of the prompt,
    significantly reducing API calls for repeated or similar prompts.
    
    Usage:
        cache = LLMRequestCache()
        
        # Try to get cached response
        cached = cache.get(prompt, provider="minimax")
        if cached:
            return cached
        
        # Call LLM and cache the response
        response = call_llm(prompt)
        cache.set(prompt, response, provider="minimax")
        return response
    
    Cache Key Format:
        llm_cache:{provider}:{sha256_hash_of_prompt}
    """
    
    # Default TTLs per provider (in seconds)
    DEFAULT_TTL = {
        "minimax": 300,      # 5 minutes for MiniMax
        "ollama": 600,       # 10 minutes for local Ollama
        "openai": 300,       # 5 minutes for OpenAI
        "default": 300,       # 5 minutes default
    }
    
    # Key prefix for all LLM cache entries
    KEY_PREFIX = "llm_cache"
    
    def __init__(
        self,
        redis_url: Optional[str] = None,
        default_ttl: Optional[int] = None,
        enabled: bool = True,
        stats_enabled: bool = True,
    ):
        """
        Initialize the LLM request cache.
        
        Args:
            redis_url: Redis connection URL. Defaults to REDIS_URL env var or localhost.
            default_ttl: Default TTL in seconds for cached responses.
            enabled: Whether caching is enabled. If False, get/set become no-ops.
            stats_enabled: Whether to track cache statistics.
        """
        self._enabled = enabled and REDIS_AVAILABLE
        self._stats_enabled = stats_enabled
        self._stats = CacheStats()
        self._lock = threading.RLock()
        self._ttl_overrides: Dict[str, int] = {}
        
        # Determine Redis URL
        if redis_url is None:
            redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        
        # Initialize Redis client
        self._redis_url = redis_url
        self._client = None
        
        if self._enabled:
            try:
                self._client = redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                # Test connection
                self._client.ping()
                logger.info(f"LLM cache initialized with Redis: {redis_url}")
            except Exception as e:
                logger.warning(f"Failed to connect to Redis: {e}. LLM caching disabled.")
                self._enabled = False
                self._client = None
        
        # Set default TTL
        if default_ttl is not None:
            self.DEFAULT_TTL["default"] = default_ttl
    
    def _make_key(self, prompt: str, provider: str = "default") -> str:
        """
        Generate a cache key from prompt and provider.
        
        Uses SHA256 hash of the prompt for efficient fixed-length keys.
        
        Args:
            prompt: The LLM prompt text
            provider: The LLM provider name (minimax, ollama, openai, etc.)
        
        Returns:
            Cache key string in format: llm_cache:{provider}:{hash}
        """
        # Normalize prompt: strip whitespace and normalize newlines
        normalized = ' '.join(prompt.split())
        prompt_hash = hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]
        return f"{self.KEY_PREFIX}:{provider}:{prompt_hash}"
    
    def get(self, prompt: str, provider: str = "default") -> Optional[str]:
        """
        Retrieve a cached LLM response.
        
        Args:
            prompt: The prompt that was used for the LLM call
            provider: The LLM provider name
        
        Returns:
            Cached response string if found, None otherwise
        """
        if not self._enabled or not self._client:
            return None
        
        key = self._make_key(prompt, provider)
        
        try:
            cached = self._client.get(key)
            if cached is not None:
                if self._stats_enabled:
                    with self._lock:
                        self._stats.hits += 1
                logger.debug(f"LLM cache HIT: provider={provider}, key={key[:50]}...")
                return cached
            else:
                if self._stats_enabled:
                    with self._lock:
                        self._stats.misses += 1
                logger.debug(f"LLM cache MISS: provider={provider}, key={key[:50]}...")
                return None
        except Exception as e:
            if self._stats_enabled:
                with self._lock:
                    self._stats.errors += 1
            logger.warning(f"LLM cache GET error: {e}")
            return None
    
    def set(
        self,
        prompt: str,
        response: str,
        provider: str = "default",
        ttl: Optional[int] = None,
    ) -> bool:
        """
        Cache an LLM response.
        
        Args:
            prompt: The prompt that was used
            response: The LLM response to cache
            provider: The LLM provider name
            ttl: Optional TTL override in seconds
        
        Returns:
            True if cached successfully, False otherwise
        """
        if not self._enabled or not self._client:
            return False
        
        key = self._make_key(prompt, provider)
        
        # Determine TTL
        if ttl is None:
            ttl = self._ttl_overrides.get(provider, self.DEFAULT_TTL.get(provider, self.DEFAULT_TTL["default"]))
        
        try:
            # Store as JSON with metadata for potential future use
            cache_value = json.dumps({
                "response": response,
                "cached_at": time.time(),
                "provider": provider,
                "prompt_hash": key.split(":")[-1],
            })
            
            self._client.setex(key, ttl, cache_value)
            logger.debug(f"LLM cache SET: provider={provider}, key={key[:50]}..., ttl={ttl}s")
            return True
        except Exception as e:
            if self._stats_enabled:
                with self._lock:
                    self._stats.errors += 1
            logger.warning(f"LLM cache SET error: {e}")
            return False
    
    def delete(self, prompt: str, provider: str = "default") -> bool:
        """
        Delete a cached response.
        
        Args:
            prompt: The prompt key
            provider: The LLM provider name
        
        Returns:
            True if deleted successfully, False otherwise
        """
        if not self._enabled or not self._client:
            return False
        
        key = self._make_key(prompt, provider)
        
        try:
            self._client.delete(key)
            logger.debug(f"LLM cache DELETE: provider={provider}, key={key[:50]}...")
            return True
        except Exception as e:
            logger.warning(f"LLM cache DELETE error: {e}")
            return False
    
    def clear_provider(self, provider: str) -> int:
        """
        Clear all cached responses for a specific provider.
        
        Args:
            provider: The LLM provider name
        
        Returns:
            Number of keys deleted
        """
        if not self._enabled or not self._client:
            return 0
        
        pattern = f"{self.KEY_PREFIX}:{provider}:*"
        deleted = 0
        
        try:
            for key in self._client.scan_iter(pattern):
                self._client.delete(key)
                deleted += 1
            logger.info(f"LLM cache CLEARED {deleted} entries for provider={provider}")
            return deleted
        except Exception as e:
            logger.warning(f"LLM cache CLEAR error: {e}")
            return deleted
    
    def clear_all(self) -> int:
        """
        Clear all LLM cache entries.
        
        Returns:
            Number of keys deleted
        """
        if not self._enabled or not self._client:
            return 0
        
        pattern = f"{self.KEY_PREFIX}:*"
        deleted = 0
        
        try:
            for key in self._client.scan_iter(pattern):
                self._client.delete(key)
                deleted += 1
            logger.info(f"LLM cache CLEARED ALL: {deleted} entries")
            return deleted
        except Exception as e:
            logger.warning(f"LLM cache CLEAR ALL error: {e}")
            return deleted
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dict with hits, misses, errors, hit_rate, and enabled status
        """
        with self._lock:
            return {
                "hits": self._stats.hits,
                "misses": self._stats.misses,
                "errors": self._stats.errors,
                "hit_rate": self._stats.hit_rate,
                "enabled": self._enabled,
                "redis_available": REDIS_AVAILABLE,
                "redis_url": self._redis_url,
            }
    
    def set_ttl(self, provider: str, ttl: int) -> None:
        """
        Set custom TTL for a specific provider.
        
        Args:
            provider: The LLM provider name
            ttl: TTL in seconds
        """
        self._ttl_overrides[provider] = ttl
        logger.info(f"LLM cache TTL set: provider={provider}, ttl={ttl}s")
    
    def reset_stats(self) -> None:
        """Reset cache statistics."""
        with self._lock:
            self._stats = CacheStats()
        logger.debug("LLM cache stats reset")
    
    @property
    def is_enabled(self) -> bool:
        """Check if caching is enabled."""
        return self._enabled
    
    def __len__(self) -> int:
        """Get approximate number of cached entries."""
        if not self._enabled or not self._client:
            return 0
        
        try:
            pattern = f"{self.KEY_PREFIX}:*"
            return sum(1 for _ in self._client.scan_iter(pattern))
        except Exception:
            return 0


# Module-level singleton
_global_cache: Optional[LLMRequestCache] = None
_global_lock = threading.Lock()


def get_llm_cache() -> LLMRequestCache:
    """
    Get the global LLM cache instance (singleton).
    
    Returns:
        The global LLMRequestCache instance
    """
    global _global_cache
    with _global_lock:
        if _global_cache is None:
            _global_cache = LLMRequestCache()
        return _global_cache


def init_llm_cache(
    redis_url: Optional[str] = None,
    default_ttl: int = 300,
    enabled: bool = True,
) -> LLMRequestCache:
    """
    Initialize the global LLM cache with custom settings.
    
    Args:
        redis_url: Redis connection URL
        default_ttl: Default TTL in seconds
        enabled: Whether caching is enabled
    
    Returns:
        The initialized global LLMRequestCache instance
    """
    global _global_cache
    with _global_lock:
        _global_cache = LLMRequestCache(
            redis_url=redis_url,
            default_ttl=default_ttl,
            enabled=enabled,
        )
        return _global_cache


def cached_llm_call(provider: str = "default", ttl: Optional[int] = None):
    """
    Decorator to cache LLM function calls.
    
    Usage:
        @cached_llm_call(provider="minimax", ttl=300)
        def call_minimax(prompt: str) -> str:
            # Your LLM call logic here
            return response
    
    Args:
        provider: The LLM provider name
        ttl: Optional TTL override in seconds
    
    Returns:
        Decorated function with caching
    """
    def decorator(func):
        @wraps(func)
        def wrapper(prompt: str, *args, **kwargs):
            cache = get_llm_cache()
            
            # Try to get from cache first
            cached_response = cache.get(prompt, provider=provider)
            if cached_response is not None:
                # Parse the cached JSON
                try:
                    cached_data = json.loads(cached_response)
                    return cached_data.get("response", cached_response)
                except json.JSONDecodeError:
                    return cached_response
            
            # Call the actual function
            response = func(prompt, *args, **kwargs)
            
            # Cache the response
            if response:
                cache.set(prompt, response, provider=provider, ttl=ttl)
            
            return response
        return wrapper
    return decorator


# Convenience functions for direct use
def get_cached_response(prompt: str, provider: str = "default") -> Optional[str]:
    """
    Get a cached LLM response.
    
    Args:
        prompt: The LLM prompt
        provider: The LLM provider name
    
    Returns:
        Cached response if found, None otherwise
    """
    cache = get_llm_cache()
    cached = cache.get(prompt, provider)
    if cached:
        try:
            data = json.loads(cached)
            return data.get("response")
        except json.JSONDecodeError:
            return cached
    return None


def cache_response(
    prompt: str,
    response: str,
    provider: str = "default",
    ttl: Optional[int] = None,
) -> bool:
    """
    Cache an LLM response.
    
    Args:
        prompt: The LLM prompt
        response: The LLM response to cache
        provider: The LLM provider name
        ttl: Optional TTL override
    
    Returns:
        True if cached successfully
    """
    cache = get_llm_cache()
    return cache.set(prompt, response, provider=provider, ttl=ttl)


if __name__ == "__main__":
    # Simple test when run directly
    logging.basicConfig(level=logging.DEBUG)
    
    cache = LLMRequestCache()
    print(f"Cache enabled: {cache.is_enabled}")
    print(f"Stats: {cache.get_stats()}")
    
    if cache.is_enabled:
        # Test set/get
        test_prompt = "What is the current BTC price?"
        test_response = "The current BTC price is $45,000."
        
        print(f"Setting cache for prompt: {test_prompt[:30]}...")
        cache.set(test_prompt, test_response, provider="test")
        
        print(f"Getting cached response...")
        cached = cache.get(test_prompt, provider="test")
        print(f"Cached: {cached}")
        
        # Test stats
        print(f"Stats after test: {cache.get_stats()}")
