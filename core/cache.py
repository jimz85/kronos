#!/usr/bin/env python3
"""
cache.py - Indicator Caching Module for Kronos
================================================

In-memory cache for indicator values with TTL support,
LRU eviction, and thread-safe operations.

Key Features:
    - TTL (time-to-live) support for cached values
    - LRU eviction when max_size is reached
    - Thread-safe operations using locks
    - Cache invalidation on new data
    - Memory-efficient storage for indicator values

Version: 1.0.0
"""

import time
import threading
import logging
from collections import OrderedDict
from typing import Any, Optional, Dict, Tuple
from dataclasses import dataclass

logger = logging.getLogger('kronos.cache')


@dataclass
class CacheEntry:
    """Represents a single cache entry with metadata."""
    value: Any
    timestamp: float
    ttl: Optional[float] = None

    def is_expired(self) -> bool:
        """Check if the entry has expired based on TTL."""
        if self.ttl is None:
            return False
        return (time.time() - self.timestamp) > self.ttl


class IndicatorCache:
    """
    Thread-safe in-memory cache for indicator values.
    
    Features:
        - LRU eviction policy
        - TTL (time-to-live) support
        - Automatic expiration checking
        - Thread-safe operations
    
    Usage:
        cache = IndicatorCache(max_size=1000, default_ttl=300)
        cache.set('RSI_1h', 45.5, symbol='BTC', params={'period': 14})
        value = cache.get('RSI_1h', symbol='BTC', params={'period': 14})
    """

    def __init__(
        self,
        max_size: int = 1000,
        default_ttl: Optional[float] = 300.0,
        enable_lru: bool = True
    ):
        """
        Initialize the indicator cache.
        
        Args:
            max_size: Maximum number of entries before LRU eviction
            default_ttl: Default time-to-live in seconds (None = no expiration)
            enable_lru: Whether to use LRU eviction policy
        """
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._enable_lru = enable_lru
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        
        logger.info(
            f"IndicatorCache initialized: max_size={max_size}, "
            f"default_ttl={default_ttl}s, lru_enabled={enable_lru}"
        )

    def _make_key(self, indicator: str, symbol: str, timeframe: str = None, 
                  params: Dict = None) -> str:
        """
        Generate a unique cache key from indicator components.
        
        Args:
            indicator: Indicator name (e.g., 'RSI', 'MACD')
            symbol: Trading symbol (e.g., 'BTC')
            timeframe: Optional timeframe (e.g., '1h', '4h')
            params: Optional indicator parameters dict
        
        Returns:
            Canonical cache key string
        """
        parts = [indicator, symbol]
        if timeframe:
            parts.append(timeframe)
        if params:
            # Sort params for consistent key generation
            param_str = ','.join(f"{k}={v}" for k, v in sorted(params.items()))
            parts.append(param_str)
        return '|'.join(parts)

    def get(self, indicator: str, symbol: str, timeframe: str = None,
            params: Dict = None) -> Optional[Any]:
        """
        Retrieve a value from the cache.
        
        Args:
            indicator: Indicator name
            symbol: Trading symbol
            timeframe: Optional timeframe
            params: Optional indicator parameters
        
        Returns:
            Cached value if found and not expired, None otherwise
        """
        key = self._make_key(indicator, symbol, timeframe, params)
        
        with self._lock:
            entry = self._cache.get(key)
            
            if entry is None:
                self._misses += 1
                logger.debug(f"Cache MISS: {key}")
                return None
            
            if entry.is_expired():
                self._evict_locked(key)
                self._misses += 1
                logger.debug(f"Cache EXPIRED: {key}")
                return None
            
            # Move to end for LRU ordering
            if self._enable_lru:
                self._cache.move_to_end(key)
            
            self._hits += 1
            logger.debug(f"Cache HIT: {key}")
            return entry.value

    def set(self, indicator: str, symbol: str, value: Any,
            timeframe: str = None, params: Dict = None,
            ttl: Optional[float] = None) -> None:
        """
        Store a value in the cache.
        
        Args:
            indicator: Indicator name
            symbol: Trading symbol
            value: Value to cache
            timeframe: Optional timeframe
            params: Optional indicator parameters
            ttl: Optional TTL override (None uses default_ttl)
        """
        key = self._make_key(indicator, symbol, timeframe, params)
        ttl = ttl if ttl is not None else self._default_ttl
        
        with self._lock:
            # Evict if at capacity (and not updating existing key)
            if key not in self._cache and len(self._cache) >= self._max_size:
                self._evict_lru()
            
            self._cache[key] = CacheEntry(
                value=value,
                timestamp=time.time(),
                ttl=ttl
            )
            
            # Move to end for LRU ordering
            if self._enable_lru:
                self._cache.move_to_end(key)
            
            logger.debug(f"Cache SET: {key} (ttl={ttl}s)")

    def _evict_lru(self) -> None:
        """Evict the least recently used entry (must hold lock)."""
        if self._cache:
            key = next(iter(self._cache))
            self._evict_locked(key)

    def _evict_locked(self, key: str) -> None:
        """Evict a specific key (must hold lock)."""
        if key in self._cache:
            del self._cache[key]
            self._evictions += 1
            logger.debug(f"Cache EVICT: {key}")

    def invalidate(self, indicator: str = None, symbol: str = None,
                   timeframe: str = None) -> int:
        """
        Invalidate cache entries matching the criteria.
        
        Args:
            indicator: If provided, only invalidate entries for this indicator
            symbol: If provided, only invalidate entries for this symbol
            timeframe: If provided, only invalidate entries for this timeframe
        
        Returns:
            Number of entries invalidated
        """
        with self._lock:
            keys_to_delete = []
            
            for key in self._cache:
                parts = key.split('|')
                if len(parts) < 2:
                    continue
                
                key_indicator = parts[0]
                key_symbol = parts[1]
                key_timeframe = parts[2] if len(parts) > 2 else None
                
                match = True
                if indicator and key_indicator != indicator:
                    match = False
                if symbol and key_symbol != symbol:
                    match = False
                if timeframe and key_timeframe != timeframe:
                    match = False
                
                if match:
                    keys_to_delete.append(key)
            
            for key in keys_to_delete:
                self._evict_locked(key)
            
            if keys_to_delete:
                logger.info(f"Cache INVALIDATE: {len(keys_to_delete)} entries")
            
            return len(keys_to_delete)

    def invalidate_symbol(self, symbol: str) -> int:
        """
        Invalidate all cache entries for a specific symbol.
        
        Args:
            symbol: Trading symbol to invalidate
        
        Returns:
            Number of entries invalidated
        """
        return self.invalidate(symbol=symbol)

    def invalidate_indicator(self, indicator: str) -> int:
        """
        Invalidate all cache entries for a specific indicator.
        
        Args:
            indicator: Indicator name to invalidate
        
        Returns:
            Number of entries invalidated
        """
        return self.invalidate(indicator=indicator)

    def clear(self) -> int:
        """
        Clear all entries from the cache.
        
        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            logger.info(f"Cache CLEAR: {count} entries")
            return count

    def cleanup_expired(self) -> int:
        """
        Remove all expired entries from the cache.
        
        Returns:
            Number of expired entries removed
        """
        with self._lock:
            keys_to_delete = [
                key for key, entry in self._cache.items()
                if entry.is_expired()
            ]
            
            for key in keys_to_delete:
                self._evict_locked(key)
            
            if keys_to_delete:
                logger.info(f"Cache CLEANUP: {len(keys_to_delete)} expired entries")
            
            return len(keys_to_delete)

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dict with hits, misses, evictions, size, and hit_rate
        """
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            
            return {
                'hits': self._hits,
                'misses': self._misses,
                'evictions': self._evictions,
                'size': len(self._cache),
                'max_size': self._max_size,
                'hit_rate': hit_rate,
                'default_ttl': self._default_ttl,
            }

    def __len__(self) -> int:
        """Return the current number of entries in the cache."""
        with self._lock:
            return len(self._cache)

    def __contains__(self, key: Tuple[str, str, str, Dict]) -> bool:
        """
        Check if a key exists in the cache (without checking expiration).
        
        Args:
            key: Tuple of (indicator, symbol, timeframe, params)
        
        Returns:
            True if key exists, False otherwise
        """
        indicator, symbol, timeframe, params = key
        cache_key = self._make_key(indicator, symbol, timeframe, params)
        with self._lock:
            return cache_key in self._cache


# Module-level singleton for global access
_global_cache: Optional[IndicatorCache] = None
_global_lock = threading.Lock()


def get_cache() -> IndicatorCache:
    """
    Get the global cache instance (singleton).
    
    Returns:
        The global IndicatorCache instance
    """
    global _global_cache
    with _global_lock:
        if _global_cache is None:
            _global_cache = IndicatorCache()
        return _global_cache


def init_cache(max_size: int = 1000, default_ttl: float = 300.0) -> IndicatorCache:
    """
    Initialize the global cache with custom settings.
    
    Args:
        max_size: Maximum number of entries
        default_ttl: Default TTL in seconds
    
    Returns:
        The initialized global IndicatorCache instance
    """
    global _global_cache
    with _global_lock:
        _global_cache = IndicatorCache(max_size=max_size, default_ttl=default_ttl)
        return _global_cache
