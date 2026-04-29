# Kronos Performance Optimization Guide

## Overview

This document describes the critical performance optimization paths in the Kronos trading system to ensure low-latency trade execution and efficient resource utilization.

## Critical Performance Paths

### 1. Trade Execution Pipeline

```
Signal Generation → Risk Check → Order Submission → Execution → Confirmation
        ↓              ↓              ↓              ↓           ↓
      ~10ms         ~5ms          ~20ms          ~50ms       ~10ms
```

**Key Optimization Targets:**

| Stage | Target Latency | Optimization Strategy |
|-------|---------------|---------------------|
| Signal Generation | <10ms | Pre-computed indicators, caching |
| Risk Check | <5ms | Circuit breaker pre-flight checks |
| Order Submission | <20ms | Connection pooling, async HTTP |
| Execution | <50ms | Exchange API latency + network |
| Confirmation | <10ms | WebSocket streaming |

### 2. Data Pipeline (Real-time)

```
Exchange → WebSocket → Local Buffer → Feature Engineering → Model Inference
    ↓           ↓            ↓              ↓                  ↓
  ~5ms        ~2ms         ~1ms           ~10ms              ~20ms
```

**Key Optimization Points:**

- **WebSocket Connection**: Keep-alive, single connection per endpoint
- **Local Buffer**: Ring buffer for O(1) push/pop operations
- **Feature Engineering**: NumPy vectorized operations, no Python loops
- **Model Inference**: Batch processing, GPU acceleration when available

### 3. Memory-Critical Operations

#### 3.1 Data Cache (core/cache.py)

The LRU cache is used for frequently accessed market data.

```python
# Configuration
CACHE_CONFIG = {
    'max_size': 10_000,      # Max entries
    'ttl_seconds': 60,        # Time-to-live
    'eviction_policy': 'lru'  # Least Recently Used
}
```

**Optimization Tips:**
- Keep cache sizes within L3 cache bounds (~10MB working set)
- Use `cache.get_or_compute()` for atomic get/set operations
- Monitor hit rate via `cache.hit_rate` metric

#### 3.2 Candle Buffer

Circular buffer for efficient OHLCV storage:

```python
class CandleBuffer:
    def __init__(self, capacity: int = 1000):
        self._buf = np.zeros((capacity, 5), dtype=np.float32)  # OHLCV
        self._head = 0
        self._count = 0
```

### 4. Computation Hotspots

#### 4.1 Indicators (core/indicators.py)

**Critical Functions (executed every tick):**

| Function | Complexity | Target Time |
|----------|------------|-------------|
| `calculate_rsi()` | O(n) | <1ms |
| `calculate_atr()` | O(n) | <1ms |
| `calculate_adx()` | O(n) | <2ms |
| `calculate_bollinger()` | O(n) | <1ms |

**Optimization:** All indicators use NumPy broadcasting - no Python loops over price data.

#### 4.2 Feature Engineering

Vectorized feature computation:

```python
# CORRECT: Vectorized
returns = np.diff(prices) / prices[:-1]

# WRONG: Python loop
returns = []
for i in range(1, len(prices)):
    returns.append((prices[i] - prices[i-1]) / prices[i-1])
```

#### 4.3 Model Inference (gemma4_signal_validator.py)

**Optimization Strategies:**
1. **Batch Size**: Process signals in batches of 32-64 for GPU efficiency
2. **Precision**: Use FP16 for inference when acceptable
3. **Caching**: Cache tokenizer outputs for repeated symbols

### 5. I/O Optimization

#### 5.1 File I/O

- Use memory-mapped files for large datasets: `np.memmap()`
- Async file writes for logging: `aiofiles`
- Compress historical data: Parquet > CSV

#### 5.2 Network I/O

- **Connection Pooling**: Reuse HTTP connections (requests.Session)
- **Request Batching**: Batch multiple requests where API supports
- **Retry with Exponential Backoff**: Base 1.5, max 5 retries

#### 5.3 Database

- **Write Buffering**: Batch inserts (100 rows per transaction)
- **Index Optimization**: Composite indexes on (symbol, timestamp)
- **Connection Pool**: Min 5, Max 20 connections

### 6. Concurrency Model

Kronos uses asyncio for I/O-bound operations:

```python
# Web UI Server (AsyncIO)
async def handle_request():
    data = await fetch_market_data()      # Non-blocking
    features = compute_features(data)     # CPU-bound
    result = await model.predict(features) # Non-blocking
    return result

# Thread Pool for CPU-bound tasks
loop.run_in_executor(None, cpu_intensive_function)
```

**Key Principles:**
1. Never block the event loop with CPU-bound work
2. Use `asyncio.gather()` for parallel I/O operations
3. Limit concurrent operations to prevent memory exhaustion

### 7. Profiling & Monitoring

#### 7.1 Performance Metrics

Key metrics to monitor in Prometheus:

```
kronos_signal_latency_seconds{stage="generation"}
kronos_signal_latency_seconds{stage="validation"}
kronos_signal_latency_seconds{stage="execution"}
kronos_cache_hit_ratio
kronos_order_fill_latency_seconds
```

#### 7.2 Profiling Tools

```bash
# CPU Profiling
python -m cProfile -o profile.stats kronos_pilot.py

# Memory Profiling  
python -m memory_profiler kronos_pilot.py

# Line-by-line timing
pip install line_profiler
python -m kernprof -l -v kronos_pilot.py
```

#### 7.3 Latency Budget

| Operation | P50 | P95 | P99 |
|-----------|-----|-----|-----|
| Signal Gen | 5ms | 15ms | 50ms |
| Risk Check | 2ms | 5ms | 10ms |
| API Call | 10ms | 50ms | 200ms |
| Full Trade | 100ms | 300ms | 500ms |

### 8. Optimization Checklist

- [ ] **Indicators**: All use NumPy, no Python loops
- [ ] **Cache**: LRU with TTL, hit rate >80%
- [ ] **Connection Pool**: Max 10 connections, keep-alive enabled
- [ ] **Batch Operations**: Batch where possible (exchanges, DB)
- [ ] **Async**: All I/O is non-blocking
- [ ] **Memory**: Object pooling for frequent allocations
- [ ] **GC**: Minimize allocations in hot paths, use __slots__

### 9. Common Performance Pitfalls

| Pitfall | Impact | Solution |
|---------|--------|----------|
| Synchronous DB calls | Blocks event loop | Use async DB driver |
| Large JSON parsing | CPU spike | Use orjson, parse incrementally |
| Disk sync on every log | I/O bottleneck | Async logging with ring buffer |
| N+1 queries | Latency accumulation | Batch queries |
| Unbounded queues | Memory growth | Set maxsize, use backpressure |

### 10. Performance Test Suite

Run performance tests:

```bash
# Latency benchmarks
pytest tests/benchmarks/test_latency.py -v

# Memory benchmarks  
pytest tests/benchmarks/test_memory.py -v

# Throughput benchmarks
pytest tests/benchmarks/test_throughput.py -v
```

**Benchmarks must pass before deployment:**

| Metric | Threshold |
|--------|-----------|
| Signal latency P99 | <100ms |
| Memory per instance | <512MB |
| Throughput | >1000 signals/sec |
