#!/usr/bin/env python3
"""
confidence_scorer.py
====================
ML-based confidence scoring module for Kronos trading signals.

This module scores the confidence of trading signals based on:
    - Technical indicator alignment
    - Volume confirmation
    - Regime alignment
    - Historical signal performance
    - Cross-timeframe confirmation

Author: Kronos Trading System
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import logging

import sys
sys.path.insert(0, '/Users/jimingzhang/kronos')
from strategies.regime_classifier import RegimeClassifier, RegimeType


logger = logging.getLogger(__name__)


@dataclass
class ConfidenceFactors:
    """Container for individual confidence factors."""
    indicator_alignment: float = 0.0  # 0-1: How many indicators agree
    volume_confirmation: float = 0.0   # 0-1: Volume supports the signal
    regime_alignment: float = 0.0      # 0-1: Signal matches regime
    timeframe_alignment: float = 0.0   # 0-1: Multiple timeframes agree
    momentum_alignment: float = 0.0     # 0-1: Momentum supports signal
    historical_accuracy: float = 0.5    # 0-1: Historical performance


@dataclass
class ScoredSignal:
    """Trading signal with confidence score."""
    signal_type: str  # 'long', 'short', 'neutral'
    base_confidence: float  # Original confidence from engine
    final_confidence: float  # Adjusted confidence after scoring
    factors: ConfidenceFactors
    adjustments: List[str]  # List of adjustments made
    timestamp: Optional[pd.Timestamp] = None
    metadata: Dict = field(default_factory=dict)


class ConfidenceScorer:
    """
    Scores and adjusts confidence of trading signals.
    
    Uses multiple factors to score signal confidence:
    - Technical indicator alignment
    - Volume confirmation
    - Regime alignment with signal direction
    - Multi-timeframe analysis
    - Historical signal performance
    """
    
    def __init__(self,
                 indicator_weights: Optional[Dict[str, float]] = None,
                 min_confidence_threshold: float = 0.4,
                 historical_window: int = 100):
        """
        Initialize the confidence scorer.
        
        Args:
            indicator_weights: Weights for different indicators
            min_confidence_threshold: Minimum confidence to act on
            historical_window: Window for historical accuracy calculation
        """
        self.indicator_weights = indicator_weights or {
            'rsi': 0.15,
            'macd': 0.15,
            'adx': 0.20,
            'bollinger': 0.15,
            'volume': 0.15,
            'momentum': 0.20
        }
        self.min_confidence_threshold = min_confidence_threshold
        self.historical_window = historical_window
        
        self.regime_classifier = RegimeClassifier()
        self._signal_history: List[ScoredSignal] = []
        self._correct_predictions: int = 0
        self._total_predictions: int = 0
    
    def score_signal(self, 
                   signal_type: str,
                   base_confidence: float,
                   df: pd.DataFrame,
                   regime: Optional[RegimeType] = None,
                   alpha_indicators: Optional[Dict] = None,
                   beta_indicators: Optional[Dict] = None) -> ScoredSignal:
        """
        Score a trading signal and return adjusted confidence.
        
        Args:
            signal_type: Type of signal ('long', 'short', 'neutral')
            base_confidence: Initial confidence from signal generator
            df: DataFrame with OHLCV data
            regime: Current market regime
            alpha_indicators: Indicators from alpha engine
            beta_indicators: Indicators from beta engine
            
        Returns:
            ScoredSignal with final confidence and factor breakdown
        """
        if df is None or len(df) < 60:
            return ScoredSignal(
                signal_type=signal_type,
                base_confidence=base_confidence,
                final_confidence=0.0,
                factors=ConfidenceFactors(),
                adjustments=["Insufficient data"],
                timestamp=df.index[-1] if df is not None and hasattr(df, 'index') else None
            )
        
        # Compute regime if not provided
        if regime is None:
            regime, reg_conf, _ = self.regime_classifier.classify(df)
        
        # Calculate confidence factors
        factors = self._calculate_factors(
            signal_type, df, regime, alpha_indicators, beta_indicators
        )
        
        # Calculate final confidence
        final_confidence, adjustments = self._compute_final_confidence(
            signal_type, base_confidence, factors, regime
        )
        
        scored_signal = ScoredSignal(
            signal_type=signal_type,
            base_confidence=base_confidence,
            final_confidence=final_confidence,
            factors=factors,
            adjustments=adjustments,
            timestamp=df.index[-1] if hasattr(df, 'index') else None
        )
        
        # Update historical accuracy tracking
        self._signal_history.append(scored_signal)
        
        return scored_signal
    
    def _calculate_factors(self,
                          signal_type: str,
                          df: pd.DataFrame,
                          regime: RegimeType,
                          alpha_indicators: Optional[Dict],
                          beta_indicators: Optional[Dict]) -> ConfidenceFactors:
        """Calculate all confidence factors."""
        factors = ConfidenceFactors()
        
        # Indicator alignment
        factors.indicator_alignment = self._calc_indicator_alignment(
            signal_type, df, alpha_indicators
        )
        
        # Volume confirmation
        factors.volume_confirmation = self._calc_volume_confirmation(df)
        
        # Regime alignment
        factors.regime_alignment = self._calc_regime_alignment(signal_type, regime)
        
        # Timeframe alignment (simplified)
        factors.timeframe_alignment = self._calc_timeframe_alignment(df)
        
        # Momentum alignment
        factors.momentum_alignment = self._calc_momentum_alignment(signal_type, df)
        
        # Historical accuracy
        factors.historical_accuracy = self._calc_historical_accuracy()
        
        return factors
    
    def _calc_indicator_alignment(self,
                                  signal_type: str,
                                  df: pd.DataFrame,
                                  alpha_indicators: Optional[Dict]) -> float:
        """Calculate how many indicators agree with the signal."""
        if alpha_indicators is None:
            # Compute basic indicators
            alpha_indicators = self._compute_basic_indicators(df)
        
        indicators_voting = 0
        total_weight = 0
        
        close = df['close'].values
        rsi = alpha_indicators.get('rsi', [50] * len(close))[-1]
        macd_hist = alpha_indicators.get('macd_histogram', [0] * len(close))[-1]
        adx = alpha_indicators.get('adx', 0)
        bb_upper = alpha_indicators.get('bb_upper', close[-1] * 1.02)
        bb_lower = alpha_indicators.get('bb_lower', close[-1] * 0.98)
        
        # RSI alignment
        if signal_type == 'long' and rsi < 50:
            indicators_voting += self.indicator_weights['rsi']
        elif signal_type == 'short' and rsi > 50:
            indicators_voting += self.indicator_weights['rsi']
        total_weight += self.indicator_weights['rsi']
        
        # MACD alignment
        if signal_type == 'long' and macd_hist > 0:
            indicators_voting += self.indicator_weights['macd']
        elif signal_type == 'short' and macd_hist < 0:
            indicators_voting += self.indicator_weights['macd']
        total_weight += self.indicator_weights['macd']
        
        # ADX alignment (stronger trend = better for directional signals)
        adx_factor = min(adx / 50, 1.0)  # Normalize ADX to 0-1
        if signal_type != 'neutral':
            indicators_voting += self.indicator_weights['adx'] * adx_factor
        total_weight += self.indicator_weights['adx']
        
        # Bollinger alignment
        if signal_type == 'long' and close[-1] < bb_upper:
            indicators_voting += self.indicator_weights['bollinger'] * 0.5
        elif signal_type == 'short' and close[-1] > bb_lower:
            indicators_voting += self.indicator_weights['bollinger'] * 0.5
        total_weight += self.indicator_weights['bollinger']
        
        return indicators_voting / total_weight if total_weight > 0 else 0.5
    
    def _calc_volume_confirmation(self, df: pd.DataFrame) -> float:
        """Calculate volume confirmation factor."""
        if 'volume' not in df.columns or len(df) < 20:
            return 0.5
        
        volume = df['volume'].values
        close = df['close'].values
        
        # Current volume vs average
        current_vol = np.mean(volume[-5:])
        avg_vol = np.mean(volume[-20:])
        
        if avg_vol <= 0:
            return 0.5
        
        vol_ratio = current_vol / avg_vol
        
        # Price change direction vs volume direction
        price_change = close[-1] - close[-5]
        recent_volume = volume[-5:]
        vol_direction = np.sum(np.diff(recent_volume) > 0) / 4  # Fraction of up-days
        
        # High volume with price move in same direction = confirmation
        if price_change > 0 and vol_ratio > 1.2:
            return min(0.5 + vol_direction * 0.3, 1.0)
        elif price_change < 0 and vol_ratio > 1.2:
            return min(0.5 + (1 - vol_direction) * 0.3, 1.0)
        
        return 0.5
    
    def _calc_regime_alignment(self, signal_type: str, regime: RegimeType) -> float:
        """Calculate how well signal aligns with current regime."""
        alignment_map = {
            RegimeType.BULL_TREND: {'long': 1.0, 'short': 0.2, 'neutral': 0.5},
            RegimeType.BEAR_TREND: {'long': 0.2, 'short': 1.0, 'neutral': 0.5},
            RegimeType.RANGE_BOUND: {'long': 0.6, 'short': 0.6, 'neutral': 0.8},
            RegimeType.HIGH_VOLATILITY: {'long': 0.5, 'short': 0.5, 'neutral': 0.7},
            RegimeType.LOW_VOLATILITY: {'long': 0.6, 'short': 0.6, 'neutral': 0.6},
            RegimeType.UNKNOWN: {'long': 0.4, 'short': 0.4, 'neutral': 0.5}
        }
        return alignment_map.get(regime, alignment_map[RegimeType.UNKNOWN]).get(
            signal_type, 0.5
        )
    
    def _calc_timeframe_alignment(self, df: pd.DataFrame) -> float:
        """Calculate multi-timeframe alignment (simplified)."""
        if len(df) < 100:
            return 0.5
        
        close = df['close'].values
        
        # Compare short-term vs long-term trends
        ma5 = np.mean(close[-5:])
        ma20 = np.mean(close[-20:])
        ma50 = np.mean(close[-50:]) if len(close) >= 50 else ma20
        
        # Strong alignment when all three are in agreement
        if ma5 > ma20 > ma50:
            return 0.8  # Strong uptrend across timeframes
        elif ma5 < ma20 < ma50:
            return 0.8  # Strong downtrend across timeframes
        elif ma5 > ma20 and ma20 < ma50:
            return 0.5  # Mixed
        else:
            return 0.5
    
    def _calc_momentum_alignment(self, signal_type: str, df: pd.DataFrame) -> float:
        """Calculate momentum factor."""
        if len(df) < 20:
            return 0.5
        
        close = df['close'].values
        
        # Various momentum periods
        mom_short = (close[-1] - close[-5]) / close[-5] if close[-5] != 0 else 0
        mom_medium = (close[-1] - close[-10]) / close[-10] if close[-10] != 0 else 0
        mom_long = (close[-1] - close[-20]) / close[-20] if close[-20] != 0 else 0
        
        # Normalize (assuming max 20% move per period)
        mom_short = np.clip(mom_short / 0.1, -1, 1)
        mom_medium = np.clip(mom_medium / 0.2, -1, 1)
        mom_long = np.clip(mom_long / 0.4, -1, 1)
        
        # Weighted average of momentum
        weighted_momentum = mom_short * 0.5 + mom_medium * 0.3 + mom_long * 0.2
        
        # Compare to signal direction
        if signal_type == 'long' and weighted_momentum > 0:
            return 0.5 + weighted_momentum * 0.5
        elif signal_type == 'short' and weighted_momentum < 0:
            return 0.5 + abs(weighted_momentum) * 0.5
        elif signal_type == 'neutral':
            return 0.6 - abs(weighted_momentum) * 0.3
        
        return 0.3
    
    def _calc_historical_accuracy(self) -> float:
        """Calculate historical accuracy of signals."""
        if self._total_predictions < 10:
            return 0.5  # Default until we have enough data
        
        accuracy = self._correct_predictions / self._total_predictions
        return max(0.4, min(accuracy, 0.9))  # Bound between 0.4 and 0.9
    
    def _compute_final_confidence(self,
                                  signal_type: str,
                                  base_confidence: float,
                                  factors: ConfidenceFactors,
                                  regime: RegimeType) -> Tuple[float, List[str]]:
        """Compute final confidence score from factors."""
        adjustments = []
        
        # Weighted average of factors
        weights = {
            'indicator_alignment': 0.25,
            'volume_confirmation': 0.15,
            'regime_alignment': 0.25,
            'timeframe_alignment': 0.15,
            'momentum_alignment': 0.10,
            'historical_accuracy': 0.10
        }
        
        final = (
            factors.indicator_alignment * weights['indicator_alignment'] +
            factors.volume_confirmation * weights['volume_confirmation'] +
            factors.regime_alignment * weights['regime_alignment'] +
            factors.timeframe_alignment * weights['timeframe_alignment'] +
            factors.momentum_alignment * weights['momentum_alignment'] +
            factors.historical_accuracy * weights['historical_accuracy']
        )
        
        # Adjustments
        if factors.regime_alignment < 0.3:
            final *= 0.7
            adjustments.append(f"Low regime alignment ({factors.regime_alignment:.2f})")
        
        if factors.volume_confirmation < 0.4:
            final *= 0.8
            adjustments.append(f"Weak volume confirmation ({factors.volume_confirmation:.2f})")
        
        if factors.indicator_alignment < 0.4:
            final *= 0.75
            adjustments.append(f"Low indicator alignment ({factors.indicator_alignment:.2f})")
        
        # Boost for strong regime alignment
        if factors.regime_alignment > 0.8:
            final = min(final * 1.1, 1.0)
            adjustments.append(f"Strong regime alignment boost")
        
        # Combine with base confidence (favor base if it's confident)
        final = (final * 0.4) + (base_confidence * 0.6)
        
        # Bound final confidence
        final = max(0.0, min(final, 1.0))
        
        if not adjustments:
            adjustments.append("No adjustments applied")
        
        return final, adjustments
    
    def _compute_basic_indicators(self, df: pd.DataFrame) -> Dict:
        """Compute basic indicators for scoring."""
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        
        # RSI
        deltas = np.diff(close, prepend=close[0])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        
        avg_g = np.zeros(len(close))
        avg_l = np.zeros(len(close))
        avg_g[14] = np.mean(gains[1:15])
        avg_l[14] = np.mean(losses[1:15])
        
        for i in range(15, len(close)):
            avg_g[i] = (avg_g[i-1] * 13 + gains[i]) / 14
            avg_l[i] = (avg_l[i-1] * 13 + losses[i]) / 14
        
        rs = avg_g / (avg_l + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        
        # MACD
        ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
        ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
        macd = ema12 - ema26
        signal = pd.Series(macd).ewm(span=9, adjust=False).mean().values
        macd_histogram = macd - signal
        
        # Bollinger Bands
        ma20 = pd.Series(close).rolling(20).mean().values
        std20 = pd.Series(close).rolling(20).std().values
        bb_upper = ma20 + 2 * std20
        bb_lower = ma20 - 2 * std20
        
        # ADX (simplified)
        tr1 = high[1:] - low[1:]
        tr2 = np.abs(high[1:] - close[:-1])
        tr3 = np.abs(low[1:] - close[:-1])
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        atr = np.zeros(len(close))
        atr[14] = np.mean(tr[:14])
        for i in range(15, len(close)):
            atr[i] = (atr[i-1] * 13 + tr[i-1]) / 14
        
        plus_dm = np.where(np.diff(high, prepend=high[0]) > 0, np.diff(high, prepend=high[0]), 0)
        minus_dm = np.where(-np.diff(low, prepend=low[0]) > 0, -np.diff(low, prepend=low[0]), 0)
        
        plus_di = np.zeros(len(close))
        minus_di = np.zeros(len(close))
        for i in range(14, len(close)):
            if atr[i] > 0:
                plus_di[i] = 100 * np.mean(plus_dm[i-14:i]) / atr[i]
                minus_di[i] = 100 * np.mean(minus_dm[i-14:i]) / atr[i]
        
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        adx = np.mean(dx[-14:]) if len(dx) >= 14 else 0
        
        return {
            'rsi': rsi,
            'macd': macd,
            'macd_histogram': macd_histogram,
            'bb_upper': bb_upper,
            'bb_lower': bb_lower,
            'adx': adx
        }
    
    def record_outcome(self, signal: ScoredSignal, was_correct: bool):
        """Record the outcome of a signal for historical accuracy tracking."""
        self._total_predictions += 1
        if was_correct:
            self._correct_predictions += 1
    
    def format_score_report(self, scored_signal: ScoredSignal) -> str:
        """Format a human-readable confidence score report."""
        f = scored_signal.factors
        return f"""
╔══════════════════════════════════════════════════════════════╗
║               SIGNAL CONFIDENCE REPORT                      ║
╠══════════════════════════════════════════════════════════════╣
║ Signal:        {scored_signal.signal_type.upper():<42} ║
║ Base Conf:     {scored_signal.base_confidence:.1%} ({(scored_signal.base_confidence*100):.0f}/100){" "*31}║
║ Final Conf:    {scored_signal.final_confidence:.1%} ({(scored_signal.final_confidence*100):.0f}/100){" "*31}║
╠══════════════════════════════════════════════════════════════╣
║ FACTOR BREAKDOWN                                            ║
║   Indicator Alignment:    {f.indicator_alignment:>6.1%}                         ║
║   Volume Confirmation:    {f.volume_confirmation:>6.1%}                         ║
║   Regime Alignment:       {f.regime_alignment:>6.1%}                         ║
║   Timeframe Alignment:    {f.timeframe_alignment:>6.1%}                         ║
║   Momentum Alignment:     {f.momentum_alignment:>6.1%}                         ║
║   Historical Accuracy:    {f.historical_accuracy:>6.1%}                         ║
╠══════════════════════════════════════════════════════════════╣
║ ADJUSTMENTS                                                 ║
{"".join(f"║   - {adj:<55} ║" for adj in scored_signal.adjustments)}
╚══════════════════════════════════════════════════════════════╝
"""


def create_confidence_scorer(config: Optional[Dict] = None) -> ConfidenceScorer:
    """Factory function to create ConfidenceScorer with optional config."""
    if config is None:
        return ConfidenceScorer()
    return ConfidenceScorer(**config)


# ========== Testing ==========
if __name__ == "__main__":
    import yfinance as yf
    
    print("Testing ConfidenceScorer...")
    
    try:
        data = yf.download("BTC-USD", start="2024-01-01", end="2026-04-01", progress=False)
        if len(data) > 0:
            df = data[['High', 'Low', 'Close', 'Volume']].copy()
            df.columns = ['high', 'low', 'close', 'volume']
            
            scorer = ConfidenceScorer()
            
            # Score a hypothetical long signal
            scored = scorer.score_signal(
                signal_type='long',
                base_confidence=0.7,
                df=df
            )
            
            print(scorer.format_score_report(scored))
            
    except Exception as e:
        print(f"Could not fetch test data: {e}")
        print("ConfidenceScorer module loaded successfully.")
