"""
backtest - Kronos Backtest Module
==================================

Unified backtesting framework for Kronos trading strategies.

Classes:
    BacktestEngine: Alias for UnifiedBacktester providing core backtesting engine
    HyperOptimizedBacktest: Hyperparameter-optimized backtest runner
    MultiDirectionBacktest: Multi-directional (long/short) backtest engine
    RSIADXPatternBacktest: RSI + ADX pattern-based backtest engine

Fee Structure:
    - FEE_AND_SLIPPAGE = 0.002 (0.2% total including maker/taker/slippage)
    - Dynamic exit: 1.5xATR stop loss / 3xATR trigger → breakeven trailing / 24h force exit
    - Signal deduplication: 2h cooldown (no re-entry during position hold)

Version: 5.0.0
"""

from .engine import (
    BacktestEngine,
    UnifiedBacktester,
    WLRTracker,
    HyperOptimizedBacktest,
    MultiDirectionBacktest,
    RSIADXPatternBacktest,
)

# Re-export aliases for backward compatibility
__all__ = [
    "BacktestEngine",
    "UnifiedBacktester",
    "WLRTracker",
    # Stubs for future expansion
    "HyperOptimizedBacktest",
    "MultiDirectionBacktest",
    "RSIADXPatternBacktest",
]
