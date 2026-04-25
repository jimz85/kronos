#!/usr/bin/env python3
"""
regime_classifier.py
====================
Advanced market regime classification module for Kronos trading system.

This module classifies market conditions into distinct regimes to enable
regime-appropriate strategy selection.

Regimes:
    BULL_TREND     - Strong upward trending market
    BEAR_TREND     - Strong downward trending market
    RANGE_BOUND    - Sideways/consolidating market  
    HIGH_VOLATILITY - Elevated volatility regime
    LOW_VOLATILITY  - Low volatility regime
    UNKNOWN        - Unable to determine regime

Author: Kronos Trading System
"""

import numpy as np
import pandas as pd
from enum import Enum
from typing import Tuple, Dict, Optional, List
from dataclasses import dataclass


class RegimeType(Enum):
    """Market regime types."""
    UNKNOWN = "unknown"
    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"


@dataclass
class RegimeMetrics:
    """Container for regime classification metrics."""
    adx: float
    atr_ratio: float
    bb_width: float
    trend_strength: float
    momentum: float
    volume_profile: float


class RegimeClassifier:
    """
    Multi-factor market regime classifier.
    
    Uses a combination of technical indicators to classify
    the current market regime.
    """
    
    def __init__(self, 
                 adx_threshold: float = 25.0,
                 atr_lookback: int = 14,
                 bb_lookback: int = 20,
                 vol_threshold: float = 1.5):
        """
        Initialize the regime classifier.
        
        Args:
            adx_threshold: ADX value above which trend is considered strong
            atr_lookback: Period for ATR calculation
            bb_lookback: Period for Bollinger Bands
            vol_threshold: Multiplier for high/low volatility detection
        """
        self.adx_threshold = adx_threshold
        self.atr_lookback = atr_lookback
        self.bb_lookback = bb_lookback
        self.vol_threshold = vol_threshold
        self._cache: Dict = {}
    
    def classify(self, df: pd.DataFrame) -> Tuple[RegimeType, float, RegimeMetrics]:
        """
        Classify the current market regime.
        
        Args:
            df: DataFrame with OHLCV data (requires: high, low, close, volume)
            
        Returns:
            Tuple of (regime, confidence, metrics)
        """
        if df is None or len(df) < 60:
            return RegimeType.UNKNOWN, 0.0, None
        
        metrics = self._compute_metrics(df)
        if metrics is None:
            return RegimeType.UNKNOWN, 0.0, None
        
        regime, confidence = self._determine_regime(metrics, df)
        return regime, confidence, metrics
    
    def _compute_metrics(self, df: pd.DataFrame) -> Optional[RegimeMetrics]:
        """Compute all regime indicators."""
        try:
            high = df['high'].values
            low = df['low'].values
            close = df['close'].values
            volume = df['volume'].values if 'volume' in df.columns else np.ones_like(close)
            
            # ADX calculation
            adx = self._calc_adx(high, low, close)
            
            # ATR ratio (current vs historical)
            atr_ratio = self._calc_atr_ratio(high, low, close)
            
            # Bollinger Band width
            bb_width = self._calc_bb_width(close)
            
            # Trend strength
            trend_strength = self._calc_trend_strength(close)
            
            # Momentum
            momentum = self._calc_momentum(close)
            
            # Volume profile
            volume_profile = self._calc_volume_profile(volume)
            
            return RegimeMetrics(
                adx=adx,
                atr_ratio=atr_ratio,
                bb_width=bb_width,
                trend_strength=trend_strength,
                momentum=momentum,
                volume_profile=volume_profile
            )
        except Exception as e:
            return None
    
    def _calc_adx(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
        """Calculate Average Directional Index."""
        if len(close) < period + 1:
            return 0.0
        
        high_diff = np.diff(high, prepend=high[0])
        low_diff = -np.diff(low, prepend=low[0])
        
        plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
        minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0)
        
        atr = self._calc_atr(high, low, close, period)
        
        plus_di = np.zeros(len(close))
        minus_di = np.zeros(len(close))
        
        for i in range(period, len(close)):
            if atr[i] > 0:
                plus_di[i] = 100 * np.mean(plus_dm[i-period+1:i+1]) / atr[i]
                minus_di[i] = 100 * np.mean(minus_dm[i-period+1:i+1]) / atr[i]
        
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        adx = np.mean(dx[-period:]) if len(dx) >= period else 0.0
        
        return adx
    
    def _calc_atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        """Calculate Average True Range."""
        if len(close) < 2:
            return np.zeros_like(close)
        
        tr1 = high[1:] - low[1:]
        tr2 = np.abs(high[1:] - close[:-1])
        tr3 = np.abs(low[1:] - close[:-1])
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        
        atr = np.zeros_like(close)
        atr[period] = np.mean(tr[:period])
        
        for i in range(period + 1, len(close)):
            atr[i] = (atr[i-1] * (period - 1) + tr[i-1]) / period
        
        return atr
    
    def _calc_atr_ratio(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> float:
        """Calculate current ATR as ratio to historical average."""
        atr = self._calc_atr(high, low, close, self.atr_lookback)
        if len(atr) < self.atr_lookback * 2:
            return 1.0
        
        current_atr = np.mean(atr[-5:])
        historical_atr = np.mean(atr[-self.atr_lookback*2:-self.atr_lookback])
        
        if historical_atr <= 0:
            return 1.0
        return current_atr / historical_atr
    
    def _calc_bb_width(self, close: np.ndarray) -> float:
        """Calculate normalized Bollinger Band width."""
        if len(close) < self.bb_lookback:
            return 0.5
        
        recent = close[-self.bb_lookback:]
        ma = np.mean(recent)
        std = np.std(recent)
        
        if ma <= 0:
            return 0.5
        return (2 * std) / ma
    
    def _calc_trend_strength(self, close: np.ndarray, lookback: int = 60) -> float:
        """Calculate trend strength from -1 to 1."""
        if len(close) < lookback:
            lookback = len(close)
        
        prices = close[-lookback:]
        ma20 = np.mean(prices[-20:]) if len(prices) >= 20 else np.mean(prices)
        ma60 = np.mean(prices) if len(prices) >= 60 else ma20
        
        current = prices[-1]
        
        if ma20 > ma60 and current > ma20:
            return 1.0  # Strong uptrend
        elif ma20 < ma60 and current < ma20:
            return -1.0  # Strong downtrend
        elif current > ma20 > ma60:
            return 0.5  # Weak uptrend
        elif current < ma20 < ma60:
            return -0.5  # Weak downtrend
        else:
            return 0.0  # No trend
    
    def _calc_momentum(self, close: np.ndarray, period: int = 10) -> float:
        """Calculate price momentum from -1 to 1."""
        if len(close) < period + 1:
            return 0.0
        
        recent_return = (close[-1] - close[-period-1]) / close[-period-1] if close[-period-1] != 0 else 0.0
        
        # Normalize to roughly -1 to 1 range (assuming max 20% move)
        return np.clip(recent_return / 0.2, -1.0, 1.0)
    
    def _calc_volume_profile(self, volume: np.ndarray, lookback: int = 20) -> float:
        """Calculate volume profile relative to average."""
        if len(volume) < lookback:
            return 0.5
        
        current_vol = np.mean(volume[-5:])
        avg_vol = np.mean(volume[-lookback:-5]) if lookback > 5 else np.mean(volume)
        
        if avg_vol <= 0:
            return 0.5
        return np.clip(current_vol / avg_vol, 0.0, 2.0) / 2.0
    
    def _determine_regime(self, metrics: RegimeMetrics, df: pd.DataFrame) -> Tuple[RegimeType, float]:
        """Determine regime from computed metrics."""
        close = df['close'].values
        
        # Check volatility first
        if metrics.atr_ratio > self.vol_threshold:
            return RegimeType.HIGH_VOLATILITY, 0.75
        elif metrics.atr_ratio < 1 / self.vol_threshold:
            return RegimeType.LOW_VOLATILITY, 0.70
        
        # Check trend
        if metrics.adx > self.adx_threshold:
            if metrics.trend_strength > 0.3:
                return RegimeType.BULL_TREND, min(0.80 + metrics.adx/100, 0.95)
            elif metrics.trend_strength < -0.3:
                return RegimeType.BEAR_TREND, min(0.80 + metrics.adx/100, 0.95)
        
        # Check for range-bound (low ADX + moderate BB width)
        if metrics.adx < 20 and 0.3 < metrics.bb_width < 0.8:
            return RegimeType.RANGE_BOUND, 0.70
        
        # Default - insufficient signals
        return RegimeType.UNKNOWN, 0.50
    
    def get_signal(self, regime: RegimeType) -> Dict:
        """
        Get trading signal parameters for a given regime.
        
        Returns:
            Dict with recommended strategy parameters
        """
        signals = {
            RegimeType.BULL_TREND: {
                "action": "long",
                "position_size": 1.0,
                "stop_loss": "2ATR",
                "take_profit": "3ATR",
                "strategy": "trend_following"
            },
            RegimeType.BEAR_TREND: {
                "action": "short",
                "position_size": 0.8,
                "stop_loss": "1.5ATR",
                "take_profit": "2ATR",
                "strategy": "momentum"
            },
            RegimeType.RANGE_BOUND: {
                "action": "mean_reversion",
                "position_size": 0.6,
                "stop_loss": "2%",
                "take_profit": "3%",
                "strategy": "range_trading"
            },
            RegimeType.HIGH_VOLATILITY: {
                "action": "reduce",
                "position_size": 0.5,
                "stop_loss": "3ATR",
                "take_profit": "4ATR",
                "strategy": "volatility_adjusted"
            },
            RegimeType.LOW_VOLATILITY: {
                "action": "build",
                "position_size": 0.7,
                "stop_loss": "1.5%",
                "take_profit": "2%",
                "strategy": "accumulation"
            },
            RegimeType.UNKNOWN: {
                "action": "wait",
                "position_size": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "strategy": "no_trade"
            }
        }
        return signals.get(regime, signals[RegimeType.UNKNOWN])
    
    def format_analysis(self, regime: RegimeType, confidence: float, 
                       metrics: RegimeMetrics) -> str:
        """Format regime analysis as a readable string."""
        if metrics is None:
            return "Insufficient data for regime analysis"
        
        signal = self.get_signal(regime)
        return f"""
╔══════════════════════════════════════════════════════════════╗
║               MARKET REGIME ANALYSIS                        ║
╠══════════════════════════════════════════════════════════════╣
║ Regime:        {regime.value.upper():<42} ║
║ Confidence:    {confidence:.1%} ({(confidence*100):.0f}/100){" "*31}║
╠══════════════════════════════════════════════════════════════╣
║ METRICS                                                       ║
║   ADX (trend):          {metrics.adx:>6.2f}{" "*36}║
║   ATR Ratio (vol):      {metrics.atr_ratio:>6.2f}{" "*36}║
║   BB Width:             {metrics.bb_width:>6.4f}{" "*36}║
║   Trend Strength:       {metrics.trend_strength:>6.2f}{" "*36}║
║   Momentum:             {metrics.momentum:>6.2f}{" "*36}║
║   Volume Profile:       {metrics.volume_profile:>6.2f}{" "*36}║
╠══════════════════════════════════════════════════════════════╣
║ RECOMMENDED SIGNAL                                            ║
║   Action:         {signal['action']:<40}║
║   Strategy:       {signal['strategy']:<40}║
║   Position Size:  {signal['position_size']:.0%}{" "*36}║
║   Stop Loss:      {signal['stop_loss'] or 'None':<40}║
║   Take Profit:    {signal['take_profit'] or 'None':<40}║
╚══════════════════════════════════════════════════════════════╝
"""


def detect_regime(df: pd.DataFrame) -> Tuple[RegimeType, float, RegimeMetrics]:
    """Convenience function for quick regime detection."""
    classifier = RegimeClassifier()
    return classifier.classify(df)


# ========== Testing ==========
if __name__ == "__main__":
    import yfinance as yf
    
    print("Testing RegimeClassifier...")
    
    # Test with BTC data
    try:
        data = yf.download("BTC-USD", start="2024-01-01", end="2026-04-01", progress=False)
        if len(data) > 0:
            df = data[['High', 'Low', 'Close', 'Volume']].copy()
            df.columns = ['high', 'low', 'close', 'volume']
            
            classifier = RegimeClassifier()
            regime, confidence, metrics = classifier.classify(df)
            
            print(classifier.format_analysis(regime, confidence, metrics))
    except Exception as e:
        print(f"Could not fetch test data: {e}")
        print("RegimeClassifier module loaded successfully.")
