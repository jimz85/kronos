#!/usr/bin/env python3
"""
strategy.py - Strategy Signal Generation Module
===============================================

Testable trading signal generation logic for Kronos.
Extracts strategy logic from kronos_pilot.py for unit testing.

Key Components:
    - Signal dataclass: Represents a trading signal
    - StrategyResult dataclass: Contains signal + metadata
    - generate_signals(): Main signal generation from multi-timeframe data
    - validate_signal(): Gemma4 validation placeholder

Version: 5.0.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd

logger = logging.getLogger('kronos.strategy')


class SignalType(Enum):
    """Trading signal types."""
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class SignalStrength(Enum):
    """Signal strength classification."""
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


@dataclass
class Signal:
    """
    Represents a trading signal for a symbol.
    
    Attributes:
        symbol: Trading pair symbol (e.g., 'BTC')
        signal_type: LONG, SHORT, or NEUTRAL
        strength: WEAK, MODERATE, or STRONG
        price: Reference price at signal generation
        confidence: Confidence score 0.0-1.0
        reason: Human-readable signal reason
        metadata: Additional signal metadata
    """
    symbol: str
    signal_type: SignalType
    strength: SignalStrength
    price: float
    confidence: float = 0.5
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate signal values."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be 0.0-1.0, got {self.confidence}")
        if self.signal_type not in SignalType:
            raise ValueError(f"Invalid signal_type: {self.signal_type}")
        if self.strength not in SignalStrength:
            raise ValueError(f"Invalid strength: {self.strength}")


@dataclass
class StrategyResult:
    """
    Result of strategy evaluation containing signal and analysis metadata.
    
    Attributes:
        signal: The trading signal (or None if no signal)
        symbol: Symbol evaluated
        timestamp: When the signal was generated
        indicators: Dictionary of computed indicators
        valid: Whether the result passes validation
        validation_errors: List of validation error messages
    """
    signal: Optional[Signal]
    symbol: str
    timestamp: pd.Timestamp = field(default_factory=pd.Timestamp.now)
    indicators: Dict[str, float] = field(default_factory=dict)
    valid: bool = True
    validation_errors: list = field(default_factory=list)
    
    def __post_init__(self):
        """Ensure validation_errors is a list."""
        if self.validation_errors is None:
            self.validation_errors = []


def compute_indicators(df: pd.DataFrame) -> Dict[str, float]:
    """
    Compute technical indicators from OHLCV data.
    
    Args:
        df: DataFrame with columns [open, high, low, close, volume]
    
    Returns:
        Dictionary of indicator values
    """
    if df is None or len(df) < 2:
        return {}
    
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    volume = df['volume'].values if 'volume' in df.columns else np.ones(len(close))
    
    indicators = {}
    
    # Price-based indicators
    if len(close) >= 14:
        # RSI (14-period)
        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = np.mean(gain[-14:])
        avg_loss = np.mean(loss[-14:])
        rs = avg_gain / (avg_loss + 1e-10)
        indicators['rsi'] = 100 - (100 / (1 + rs))
    else:
        indicators['rsi'] = 50.0
    
    # Simple returns
    if len(close) >= 2:
        indicators['return_1h'] = (close[-1] - close[-2]) / (close[-2] + 1e-10) * 100
    else:
        indicators['return_1h'] = 0.0
    
    # Volatility (std of returns)
    if len(close) >= 20:
        returns = np.diff(close) / close[:-1]
        indicators['volatility'] = np.std(returns[-20:]) * 100
    else:
        indicators['volatility'] = 0.0
    
    # Volume ratio (current vs average)
    if len(volume) >= 20:
        indicators['volume_ratio'] = volume[-1] / (np.mean(volume[-20:]) + 1e-10)
    else:
        indicators['volume_ratio'] = 1.0
    
    # Price position in range
    if len(high) >= 2 and len(low) >= 2:
        high_max = np.max(high[-20:]) if len(high) >= 20 else np.max(high)
        low_min = np.min(low[-20:]) if len(low) >= 20 else np.min(low)
        range_size = high_max - low_min
        if range_size > 0:
            indicators['price_position'] = (close[-1] - low_min) / range_size
        else:
            indicators['price_position'] = 0.5
    else:
        indicators['price_position'] = 0.5
    
    # Trend (simple moving average cross)
    if len(close) >= 50:
        sma_20 = np.mean(close[-20:])
        sma_50 = np.mean(close[-50:])
        indicators['trend'] = 1 if sma_20 > sma_50 else -1
    elif len(close) >= 20:
        indicators['trend'] = 1  # Bullish if only 20-period
    else:
        indicators['trend'] = 0
    
    return indicators


def generate_signals(
    symbol: str,
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    df_1d: pd.DataFrame
) -> StrategyResult:
    """
    Generate trading signals from multi-timeframe OHLCV data.
    
    This is the main signal generation function that combines analysis
    across 1-hour, 4-hour, and 1-day timeframes.
    
    Args:
        symbol: Trading pair symbol (e.g., 'BTC')
        df_1h: 1-hour OHLCV DataFrame
        df_4h: 4-hour OHLCV DataFrame
        df_1d: 1-day OHLCV DataFrame
    
    Returns:
        StrategyResult containing signal and metadata
    """
    timestamp = pd.Timestamp.now()
    validation_errors = []
    
    # Validate inputs
    if df_1h is None or len(df_1h) < 2:
        validation_errors.append("Insufficient 1h data")
        return StrategyResult(
            signal=None,
            symbol=symbol,
            timestamp=timestamp,
            valid=False,
            validation_errors=validation_errors
        )
    
    # Compute indicators for each timeframe
    ind_1h = compute_indicators(df_1h)
    ind_4h = compute_indicators(df_4h) if df_4h is not None and len(df_4h) >= 2 else {}
    ind_1d = compute_indicators(df_1d) if df_1d is not None and len(df_1d) >= 2 else {}
    
    all_indicators = {**ind_1h, **{f"4h_{k}": v for k, v in ind_4h.items()}, **{f"1d_{k}": v for k, v in ind_1d.items()}}
    
    # Get current price
    price = float(df_1h['close'].iloc[-1])
    
    # Signal generation logic
    signal_type = SignalType.NEUTRAL
    strength = SignalStrength.WEAK
    confidence = 0.5
    reason = "No clear signal"
    
    # Extract key indicators with defaults
    rsi = ind_1h.get('rsi', 50)
    trend = ind_1h.get('trend', 0)
    vol_ratio = ind_1h.get('volume_ratio', 1.0)
    price_pos = ind_1h.get('price_position', 0.5)
    
    # 4h and 1d trend (if available)
    trend_4h = ind_4h.get('trend', 0)
    trend_1d = ind_1d.get('trend', 0)
    
    # Multi-timeframe alignment scoring
    alignment_score = 0
    
    # RSI conditions
    rsi_oversold = rsi < 35
    rsi_overbought = rsi > 65
    rsi_neutral = 35 <= rsi <= 65
    
    # Trend alignment
    if trend == 1 and trend_4h == 1:
        alignment_score += 2
    elif trend == -1 and trend_4h == -1:
        alignment_score -= 2
    
    if trend_4h == 1 and trend_1d == 1:
        alignment_score += 2
    elif trend_4h == -1 and trend_1d == -1:
        alignment_score -= 2
    
    # Generate LONG signal
    if rsi_oversold and alignment_score >= 2:
        signal_type = SignalType.LONG
        strength = SignalStrength.STRONG if alignment_score >= 4 else SignalStrength.MODERATE
        confidence = min(0.5 + (alignment_score * 0.1) + (0.3 if rsi < 30 else 0), 0.95)
        reason = f"Oversold RSI({rsi:.1f}) with bullish alignment"
    elif rsi_oversold and alignment_score >= 0:
        signal_type = SignalType.LONG
        strength = SignalStrength.MODERATE if alignment_score >= 1 else SignalStrength.WEAK
        confidence = min(0.4 + (alignment_score * 0.1), 0.75)
        reason = f"Oversold RSI({rsi:.1f}) with weak bullish alignment"
    elif rsi_neutral and alignment_score >= 3:
        signal_type = SignalType.LONG
        strength = SignalStrength.MODERATE
        confidence = min(0.45 + (alignment_score * 0.08), 0.8)
        reason = f"Neutral RSI({rsi:.1f}) with strong bullish multi-timeframe alignment"
    
    # Generate SHORT signal
    elif rsi_overbought and alignment_score <= -2:
        signal_type = SignalType.SHORT
        strength = SignalStrength.STRONG if alignment_score <= -4 else SignalStrength.MODERATE
        confidence = min(0.5 + (abs(alignment_score) * 0.1) + (0.3 if rsi > 70 else 0), 0.95)
        reason = f"Overbought RSI({rsi:.1f}) with bearish alignment"
    elif rsi_overbought and alignment_score <= 0:
        signal_type = SignalType.SHORT
        strength = SignalStrength.MODERATE if alignment_score <= -1 else SignalStrength.WEAK
        confidence = min(0.4 + (abs(alignment_score) * 0.1), 0.75)
        reason = f"Overbought RSI({rsi:.1f}) with weak bearish alignment"
    elif rsi_neutral and alignment_score <= -3:
        signal_type = SignalType.SHORT
        strength = SignalStrength.MODERATE
        confidence = min(0.45 + (abs(alignment_score) * 0.08), 0.8)
        reason = f"Neutral RSI({rsi:.1f}) with strong bearish multi-timeframe alignment"
    
    # Build signal
    signal = Signal(
        symbol=symbol,
        signal_type=signal_type,
        strength=strength,
        price=price,
        confidence=confidence,
        reason=reason,
        metadata={
            'rsi': rsi,
            'trend': trend,
            'trend_4h': trend_4h,
            'trend_1d': trend_1d,
            'volume_ratio': vol_ratio,
            'alignment_score': alignment_score
        }
    )
    
    result = StrategyResult(
        signal=signal,
        symbol=symbol,
        timestamp=timestamp,
        indicators=all_indicators,
        valid=True,
        validation_errors=[]
    )
    
    logger.debug(f"Generated {signal_type.value} signal for {symbol}: {reason}")
    
    return result


def validate_signal(signal: Signal) -> tuple[bool, list[str]]:
    """
    Validate a trading signal using Gemma4 placeholder.
    
    This function is a placeholder for Gemma4 model validation.
    In the current implementation, it performs basic validation
    on signal structure and values.
    
    Args:
        signal: The Signal dataclass to validate
    
    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors = []
    
    if not isinstance(signal, Signal):
        errors.append("Signal must be a Signal dataclass instance")
        return False, errors
    
    # Basic field validation
    if not signal.symbol or len(signal.symbol) < 2:
        errors.append("Invalid symbol")
    
    if signal.signal_type not in SignalType:
        errors.append(f"Invalid signal_type: {signal.signal_type}")
    
    if signal.strength not in SignalStrength:
        errors.append(f"Invalid strength: {signal.strength}")
    
    if not 0.0 <= signal.confidence <= 1.0:
        errors.append(f"Confidence out of range: {signal.confidence}")
    
    if signal.price <= 0:
        errors.append(f"Invalid price: {signal.price}")
    
    # Gemma4 placeholder validation
    # In production, this would call the Gemma4 model for validation
    # For now, we accept all signals that pass basic validation
    
    # Placeholder: Reject extremely low confidence signals
    if signal.confidence < 0.3:
        errors.append(f"Confidence too low for Gemma4 validation: {signal.confidence}")
    
    is_valid = len(errors) == 0
    
    if not is_valid:
        logger.warning(f"Signal validation failed for {signal.symbol}: {errors}")
    else:
        logger.info(f"Signal validated for {signal.symbol}: {signal.signal_type.value} @ {signal.price}")
    
    return is_valid, errors


# Convenience function for quick signal generation
def quick_signal(symbol: str, df: pd.DataFrame) -> Optional[Signal]:
    """
    Generate a signal using single timeframe data.
    
    Convenience wrapper that uses the same dataframe for all timeframes.
    
    Args:
        symbol: Trading pair symbol
        df: OHLCV DataFrame
    
    Returns:
        Signal if generated, None otherwise
    """
    result = generate_signals(symbol, df, df, df)
    return result.signal
