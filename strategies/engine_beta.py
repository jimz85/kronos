#!/usr/bin/env python3
"""
engine_beta.py
==============
Beta trading engine - Secondary strategy execution engine for Kronos.

This engine provides alternative strategy approaches that complement
the Alpha engine. It focuses on mean-reversion, breakout, and 
counter-trend strategies.

Components:
    - Alternative signal generation methods
    - Breakout detection
    - Mean-reversion signals
    - Strategy blending with Alpha

Author: Kronos Trading System
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import logging

import sys
sys.path.insert(0, '/Users/jimingzhang/kronos')
from strategies.regime_classifier import RegimeClassifier, RegimeType


logger = logging.getLogger(__name__)


class BetaSignalType(Enum):
    """Beta-specific signal types."""
    BREAKOUT_LONG = "breakout_long"
    BREAKOUT_SHORT = "breakout_short"
    MEAN_REVERT_LONG = "mean_revert_long"
    MEAN_REVERT_SHORT = "mean_revert_short"
    MOMENTUM_LONG = "momentum_long"
    MOMENTUM_SHORT = "momentum_short"
    NEUTRAL = "neutral"


@dataclass
class BetaSignal:
    """Container for beta trading signal."""
    signal_type: BetaSignalType
    confidence: float  # 0.0 to 1.0
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward_ratio: float = 0.0
    timestamp: Optional[pd.Timestamp] = None
    metadata: Dict = field(default_factory=dict)


class BetaEngine:
    """
    Secondary beta generation engine.
    
    Provides alternative strategy approaches:
    - Breakout detection and trading
    - Mean-reversion strategies
    - Momentum-based counter-trend
    - Volume-weighted signals
    """
    
    def __init__(self,
                 breakout_window: int = 20,
                 breakout_threshold: float = 0.02,
                 mean_revert_period: int = 20,
                 volume_threshold: float = 1.5,
                 momentum_period: int = 10):
        """
        Initialize the Beta engine.
        
        Args:
            breakout_window: Window for breakout detection
            breakout_threshold: Minimum price change for breakout (as fraction)
            mean_revert_period: Period for mean-reversion calculation
            volume_threshold: Volume multiplier for breakout confirmation
            momentum_period: Period for momentum calculation
        """
        self.breakout_window = breakout_window
        self.breakout_threshold = breakout_threshold
        self.mean_revert_period = mean_revert_period
        self.volume_threshold = volume_threshold
        self.momentum_period = momentum_period
        
        self.regime_classifier = RegimeClassifier()
        self._signal_history: List[BetaSignal] = []
        self._breakout_levels: Dict = {'resistance': 0, 'support': 0}
        
    def analyze(self, df: pd.DataFrame, regime: Optional[RegimeType] = None) -> BetaSignal:
        """
        Analyze market data and generate beta trading signal.
        
        Args:
            df: DataFrame with OHLCV data
            regime: Optional pre-computed regime
            
        Returns:
            BetaSignal with recommended action
        """
        if df is None or len(df) < self.breakout_window + 10:
            return BetaSignal(
                signal_type=BetaSignalType.NEUTRAL,
                confidence=0.0,
                metadata={"reason": "insufficient_data"}
            )
        
        # Compute all beta indicators
        indicators = self._compute_indicators(df)
        
        # Detect regime if not provided
        if regime is None:
            regime, _, _ = self.regime_classifier.classify(df)
        
        # Generate signal based on multiple methods
        signal = self._generate_signal(indicators, regime, df)
        
        self._signal_history.append(signal)
        return signal
    
    def _compute_indicators(self, df: pd.DataFrame) -> Dict:
        """Compute beta-specific indicators."""
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        volume = df['volume'].values if 'volume' in df.columns else np.ones_like(close)
        
        # Breakout levels
        resistance, support = self._calc_breakout_levels(high, low, close)
        self._breakout_levels = {'resistance': resistance, 'support': support}
        
        # Mean reversion indicators
        z_score = self._calc_z_score(close)
        mean_price = self._calc_mean_reversion_level(close)
        
        # Volume indicators
        volume_ratio = self._calc_volume_ratio(volume)
        
        # Momentum
        momentum = self._calc_momentum(close)
        
        # Price channels
        channel_high, channel_low, channel_mid = self._calc_price_channels(high, low, close)
        
        # VWAP if available
        vwap = self._calc_vwap(high, low, close, volume)
        
        return {
            'resistance': resistance,
            'support': support,
            'z_score': z_score,
            'mean_price': mean_price,
            'volume_ratio': volume_ratio,
            'momentum': momentum,
            'channel_high': channel_high,
            'channel_low': channel_low,
            'channel_mid': channel_mid,
            'vwap': vwap,
            'close': close,
            'high': high,
            'low': low,
            'volume': volume
        }
    
    def _calc_breakout_levels(self, high: np.ndarray, low: np.ndarray,
                             close: np.ndarray) -> Tuple[float, float]:
        """Calculate breakout resistance and support levels."""
        if len(close) < self.breakout_window:
            return close[-1], close[-1]
        
        # Use highest high and lowest low in the window
        lookback = min(self.breakout_window, len(close) - 1)
        resistance = np.max(high[-lookback:])
        support = np.min(low[-lookback:])
        
        return resistance, support
    
    def _calc_z_score(self, closes: np.ndarray, period: int = None) -> float:
        """Calculate z-score of current price vs rolling mean."""
        period = period or self.mean_revert_period
        if len(closes) < period:
            return 0.0
        
        recent = closes[-period:]
        mean = np.mean(recent)
        std = np.std(recent)
        
        if std == 0:
            return 0.0
        
        return (closes[-1] - mean) / std
    
    def _calc_mean_reversion_level(self, closes: np.ndarray, period: int = None) -> float:
        """Calculate mean reversion level (rolling mean)."""
        period = period or self.mean_revert_period
        if len(closes) < period:
            return closes[-1]
        return np.mean(closes[-period:])
    
    def _calc_volume_ratio(self, volume: np.ndarray, period: int = 20) -> float:
        """Calculate current volume vs average volume."""
        if len(volume) < period:
            return 1.0
        
        current_vol = np.mean(volume[-5:])
        avg_vol = np.mean(volume[-period:])
        
        if avg_vol <= 0:
            return 1.0
        return current_vol / avg_vol
    
    def _calc_momentum(self, closes: np.ndarray, period: int = None) -> float:
        """Calculate price momentum."""
        period = period or self.momentum_period
        if len(closes) < period + 1:
            return 0.0
        
        return (closes[-1] - closes[-period-1]) / closes[-period-1] if closes[-period-1] != 0 else 0.0
    
    def _calc_price_channels(self, high: np.ndarray, low: np.ndarray,
                             close: np.ndarray, period: int = 20) -> Tuple[float, float, float]:
        """Calculate price channel (Donchian-style)."""
        if len(close) < period:
            return close[-1], close[-1], close[-1]
        
        lookback = min(period, len(close))
        channel_high = np.max(high[-lookback:])
        channel_low = np.min(low[-lookback:])
        channel_mid = (channel_high + channel_low) / 2
        
        return channel_high, channel_low, channel_mid
    
    def _calc_vwap(self, high: np.ndarray, low: np.ndarray,
                  close: np.ndarray, volume: np.ndarray) -> float:
        """Calculate Volume Weighted Average Price."""
        if len(close) < 2 or np.sum(volume) == 0:
            return close[-1]
        
        typical_price = (high + low + close) / 3
        return np.sum(typical_price * volume) / np.sum(volume)
    
    def _generate_signal(self, indicators: Dict, regime: RegimeType,
                        df: pd.DataFrame) -> BetaSignal:
        """Generate beta trading signal from indicators."""
        close = indicators['close']
        current_close = close[-1]
        current_atr = self._calc_atr(indicators['high'], indicators['low'], close)
        
        signals = []
        
        # Check breakout signals
        breakout_signal = self._check_breakout(indicators, current_close)
        if breakout_signal:
            signals.append(breakout_signal)
        
        # Check mean reversion signals
        mean_revert_signal = self._check_mean_reversion(indicators, current_close)
        if mean_revert_signal:
            signals.append(mean_revert_signal)
        
        # Check momentum signals
        momentum_signal = self._check_momentum(indicators, current_close)
        if momentum_signal:
            signals.append(momentum_signal)
        
        # If multiple signals agree, use the strongest
        if len(signals) > 1:
            # Prefer signals that match the regime
            regime_aligned = [s for s in signals if self._signal_matches_regime(s, regime)]
            if regime_aligned:
                signals = regime_aligned
        
        # Return strongest signal or neutral
        if signals:
            best = max(signals, key=lambda s: s.confidence)
            best.stop_loss = self._calc_stop_loss(best, current_close, current_atr)
            best.take_profit = self._calc_take_profit(best, current_close, current_atr)
            best.risk_reward_ratio = self._calc_rr_ratio(best, current_close, current_atr)
            best.entry_price = current_close
            best.timestamp = df.index[-1] if hasattr(df, 'index') else None
            return best
        
        return BetaSignal(
            signal_type=BetaSignalType.NEUTRAL,
            confidence=0.3,
            entry_price=current_close,
            timestamp=df.index[-1] if hasattr(df, 'index') else None
        )
    
    def _check_breakout(self, indicators: Dict, current_close: float) -> Optional[BetaSignal]:
        """Check for breakout signals."""
        resistance = indicators['resistance']
        support = indicators['support']
        volume_ratio = indicators['volume_ratio']
        
        # Bullish breakout
        if current_close > resistance * (1 + self.breakout_threshold) and volume_ratio > self.volume_threshold:
            confidence = min(0.6 + (volume_ratio - 1) * 0.2, 0.9)
            return BetaSignal(
                signal_type=BetaSignalType.BREAKOUT_LONG,
                confidence=confidence,
                metadata={'breakout_price': resistance, 'volume_ratio': volume_ratio}
            )
        
        # Bearish breakdown
        if current_close < support * (1 - self.breakout_threshold) and volume_ratio > self.volume_threshold:
            confidence = min(0.6 + (volume_ratio - 1) * 0.2, 0.9)
            return BetaSignal(
                signal_type=BetaSignalType.BREAKOUT_SHORT,
                confidence=confidence,
                metadata={'breakout_price': support, 'volume_ratio': volume_ratio}
            )
        
        return None
    
    def _check_mean_reversion(self, indicators: Dict, current_close: float) -> Optional[BetaSignal]:
        """Check for mean reversion signals."""
        z_score = indicators['z_score']
        mean_price = indicators['mean_price']
        
        # Strong deviation from mean suggests reversion
        if z_score < -2.0:  # Price significantly below mean
            confidence = min(abs(z_score) / 4, 0.85)
            return BetaSignal(
                signal_type=BetaSignalType.MEAN_REVERT_LONG,
                confidence=confidence,
                metadata={'z_score': z_score, 'mean_price': mean_price}
            )
        
        if z_score > 2.0:  # Price significantly above mean
            confidence = min(abs(z_score) / 4, 0.85)
            return BetaSignal(
                signal_type=BetaSignalType.MEAN_REVERT_SHORT,
                confidence=confidence,
                metadata={'z_score': z_score, 'mean_price': mean_price}
            )
        
        return None
    
    def _check_momentum(self, indicators: Dict, current_close: float) -> Optional[BetaSignal]:
        """Check for momentum signals."""
        momentum = indicators['momentum']
        vwap = indicators['vwap']
        
        # Strong momentum with VWAP confirmation
        if momentum > 0.05 and current_close > vwap:
            confidence = min(0.5 + momentum * 5, 0.85)
            return BetaSignal(
                signal_type=BetaSignalType.MOMENTUM_LONG,
                confidence=confidence,
                metadata={'momentum': momentum, 'vwap': vwap}
            )
        
        if momentum < -0.05 and current_close < vwap:
            confidence = min(0.5 + abs(momentum) * 5, 0.85)
            return BetaSignal(
                signal_type=BetaSignalType.MOMENTUM_SHORT,
                confidence=confidence,
                metadata={'momentum': momentum, 'vwap': vwap}
            )
        
        return None
    
    def _signal_matches_regime(self, signal: BetaSignal, regime: RegimeType) -> bool:
        """Check if signal is aligned with current regime."""
        if regime == RegimeType.BULL_TREND:
            return signal.signal_type in [BetaSignalType.BREAKOUT_LONG, 
                                         BetaSignalType.MEAN_REVERT_LONG,
                                         BetaSignalType.MOMENTUM_LONG]
        elif regime == RegimeType.BEAR_TREND:
            return signal.signal_type in [BetaSignalType.BREAKOUT_SHORT,
                                         BetaSignalType.MEAN_REVERT_SHORT,
                                         BetaSignalType.MOMENTUM_SHORT]
        elif regime == RegimeType.RANGE_BOUND:
            return signal.signal_type in [BetaSignalType.MEAN_REVERT_LONG,
                                         BetaSignalType.MEAN_REVERT_SHORT]
        return True  # Neutral regime matches all
    
    def _calc_atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray,
                  period: int = 14) -> float:
        """Calculate ATR for stop/take profit."""
        if len(close) < 2:
            return 0.0
        
        tr1 = high[-1] - low[-1]
        tr2 = abs(high[-1] - close[-2])
        tr3 = abs(low[-1] - close[-2])
        tr = max(tr1, max(tr2, tr3))
        
        return tr
    
    def _calc_stop_loss(self, signal: BetaSignal, close: float, atr: float) -> float:
        """Calculate stop loss for signal."""
        if signal.signal_type in [BetaSignalType.BREAKOUT_LONG, 
                                  BetaSignalType.MEAN_REVERT_LONG,
                                  BetaSignalType.MOMENTUM_LONG]:
            return close - (2.0 * atr)
        elif signal.signal_type in [BetaSignalType.BREAKOUT_SHORT,
                                   BetaSignalType.MEAN_REVERT_SHORT,
                                   BetaSignalType.MOMENTUM_SHORT]:
            return close + (2.0 * atr)
        return None
    
    def _calc_take_profit(self, signal: BetaSignal, close: float, atr: float) -> float:
        """Calculate take profit for signal."""
        if signal.signal_type in [BetaSignalType.BREAKOUT_LONG,
                                  BetaSignalType.MEAN_REVERT_LONG,
                                  BetaSignalType.MOMENTUM_LONG]:
            return close + (3.0 * atr)
        elif signal.signal_type in [BetaSignalType.BREAKOUT_SHORT,
                                   BetaSignalType.MEAN_REVERT_SHORT,
                                   BetaSignalType.MOMENTUM_SHORT]:
            return close - (3.0 * atr)
        return None
    
    def _calc_rr_ratio(self, signal: BetaSignal, close: float, atr: float) -> float:
        """Calculate risk-reward ratio."""
        if signal.stop_loss and signal.take_profit:
            risk = abs(close - signal.stop_loss)
            reward = abs(signal.take_profit - close)
            return reward / risk if risk > 0 else 0
        return 0
    
    def combine_with_alpha(self, beta_signal: BetaSignal, 
                          alpha_signal_type: 'SignalType') -> BetaSignal:
        """
        Combine beta signal with alpha signal for blended decision.
        
        Args:
            beta_signal: Signal from beta engine
            alpha_signal_type: Signal type from alpha engine
            
        Returns:
            Combined/adjusted beta signal
        """
        if alpha_signal_type.value == "neutral":
            # Reduce confidence if alpha says neutral
            beta_signal.confidence *= 0.5
        
        return beta_signal
    
    def get_status(self) -> Dict:
        """Get current beta engine status."""
        return {
            'signal_history_len': len(self._signal_history),
            'last_signal': self._signal_history[-1] if self._signal_history else None,
            'breakout_levels': self._breakout_levels
        }


def create_beta_engine(config: Optional[Dict] = None) -> BetaEngine:
    """Factory function to create BetaEngine with optional config."""
    if config is None:
        return BetaEngine()
    return BetaEngine(**config)


# ========== Testing ==========
if __name__ == "__main__":
    import yfinance as yf
    
    print("Testing BetaEngine...")
    
    try:
        data = yf.download("BTC-USD", start="2024-01-01", end="2026-04-01", progress=False)
        if len(data) > 0:
            df = data[['High', 'Low', 'Close', 'Volume']].copy()
            df.columns = ['high', 'low', 'close', 'volume']
            
            engine = BetaEngine()
            
            # Get regime
            regime, confidence, metrics = engine.regime_classifier.classify(df)
            print(f"Regime: {regime.value} ({confidence:.1%})")
            
            # Analyze
            signal = engine.analyze(df, regime)
            print(f"Beta Signal: {signal.signal_type.value}")
            print(f"Confidence: {signal.confidence:.2f}")
            print(f"RR Ratio: {signal.risk_reward_ratio:.2f}")
            
    except Exception as e:
        print(f"Could not fetch test data: {e}")
        print("BetaEngine module loaded successfully.")
