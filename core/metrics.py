#!/usr/bin/env python3
"""
metrics.py - Prometheus metrics for trading system
====================================================

Prometheus metrics for monitoring Kronos trading system.

Metrics:
    Trade:  trade_count, trade_pnl, position_size
    System: circuit_breaker_status, last_trade_time
    Market: signal_generated, signal_executed, signal_rejected

Version: 1.0.0
"""

from prometheus_client import Counter, Histogram, Gauge

# Trade metrics
trade_count = Counter(
    'kronos_trade_count',
    'Total number of trades executed',
    ['coin', 'side']
)

trade_pnl = Histogram(
    'kronos_trade_pnl',
    'Profit/loss distribution per trade',
    ['coin'],
    buckets=[-100, -50, -20, -10, -5, -1, 0, 1, 5, 10, 20, 50, 100]
)

position_size = Gauge(
    'kronos_position_size',
    'Current position size in base currency',
    ['coin']
)

# System metrics
circuit_breaker_status = Gauge(
    'kronos_circuit_breaker_status',
    'Circuit breaker status (0=ok, 1=triggered)'
)

last_trade_time = Gauge(
    'kronos_last_trade_time',
    'Unix timestamp of last trade'
)

# Market metrics
signal_generated = Counter(
    'kronos_signal_generated',
    'Total signals generated',
    ['coin', 'signal_type']
)

signal_executed = Counter(
    'kronos_signal_executed',
    'Total signals executed',
    ['coin', 'signal_type']
)

signal_rejected = Counter(
    'kronos_signal_rejected',
    'Total signals rejected',
    ['coin', 'signal_type', 'reason']
)
