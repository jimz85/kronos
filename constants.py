"""
================================================================================
constants.py - Trading System V5 Constants
================================================================================

Centralized constants for the Kronos V5 Trading System.
Used across core/strategies/models/risk/execution/data modules.

Version: 5.0.0
================================================================================
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, Any


# =============================================================================
# System Configuration
# =============================================================================

SYSTEM_NAME = "Kronos V5"
SYSTEM_VERSION = "5.0.0"
DEFAULT_DATA_DIR = "data"
LOG_LEVEL = "INFO"


# =============================================================================
# Market Configuration
# =============================================================================

class MarketRegime(Enum):
    """Market regime classifications."""
    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    RANGE_BOUND = "range_bound"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


class TrendDirection(Enum):
    """Trend direction for trading signals."""
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalType(Enum):
    """Trading signal types."""
    ENTRY_LONG = "entry_long"
    ENTRY_SHORT = "entry_short"
    EXIT = "exit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


# =============================================================================
# Risk Management Constants
# =============================================================================

@dataclass
class RiskConfig:
    """Risk management configuration."""
    max_position_size: float = 0.02  # 2% of portfolio per position
    max_drawdown: float = 0.15  # 15% max drawdown
    stop_loss_pct: float = 0.02  # 2% stop loss
    take_profit_pct: float = 0.06  # 6% take profit
    risk_per_trade: float = 0.01  # 1% risk per trade


# Default risk configuration
DEFAULT_RISK_CONFIG = RiskConfig()


# =============================================================================
# Strategy Constants
# =============================================================================

class StrategyType(Enum):
    """Available strategy types."""
    TREND_FOLLOWING = "trend_following"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    SCALPING = "scalping"


# =============================================================================
# Execution Configuration
# =============================================================================

class OrderType(Enum):
    """Order types for execution."""
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderSide(Enum):
    """Order side (buy/sell)."""
    BUY = "buy"
    SELL = "sell"


# =============================================================================
# Data Configuration
# =============================================================================

SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
DEFAULT_TIMEFRAME = "1h"

DATA_FEATURES = [
    "open", "high", "low", "close", "volume",
    "ema_fast", "ema_slow", "ema_signal",
    "atr", "adx", "rsi"
]


# =============================================================================
# Model Configuration
# =============================================================================

MODEL_CONFIG = {
    "default_model": "xgboost",
    "feature_window": 30,
    "prediction_horizon": 5,
    "confidence_threshold": 0.65,
}


# =============================================================================
# API Endpoints (Example)
# =============================================================================

API_CONFIG = {
    "base_url": "https://api.trading.com",
    "timeout": 30,
    "retry_attempts": 3,
}


# =============================================================================
# Environment Variables (for reference)
# =============================================================================

ENV_VARS = [
    "MINIMAX_API_KEY",
    "DASHSCOPE_API_KEY",
    "ANTHROPIC_API_KEY",
    "TRADING_API_KEY",
    "TRADING_SECRET_KEY",
]


# =============================================================================
# Migration Constants
# =============================================================================

V4_TO_V5_MIGRATION_VERSION = "5.0.0"
MIGRATION_REQUIRED_FIELDS = ["strategy_type", "risk_params", "execution_mode"]


def get_version() -> str:
    """Get the current system version."""
    return SYSTEM_VERSION


def get_risk_config() -> RiskConfig:
    """Get the default risk configuration."""
    return DEFAULT_RISK_CONFIG
