#!/usr/bin/env python3
"""
engine_alpha.py
===============
Alpha trading engine - Primary strategy execution engine for Kronos.

This engine implements the main trading logic and signal generation,
focusing on trend-following and momentum-based strategies.

Components:
    - Signal generation from multiple indicators
    - Entry/exit logic
    - Risk management integration
    - Regime-aware strategy selection

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


class SignalType(Enum):
    """Trading signal types."""
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"


@dataclass
class TradingSignal:
    """Container for a trading signal."""
    signal_type: SignalType
    strength: float  # 0.0 to 1.0
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    timestamp: Optional[pd.Timestamp] = None
    metadata: Dict = field(default_factory=dict)


@dataclass
class Position:
    """Container for position state."""
    side: str  # 'long' or 'short'
    entry_price: float
    size: float
    current_price: float = 0.0
    pnl: float = 0.0
    unrealized_pnl: float = 0.0
    entry_time: Optional[pd.Timestamp] = None


class AlphaEngine:
    """
    Primary alpha generation engine.
    
    Implements the core trading strategy with:
    - Multi-indicator signal generation
    - Regime-adaptive position sizing
    - Dynamic stop-loss and take-profit levels
    """
    
    def __init__(self,
                 rsi_period: int = 14,
                 rsi_overbought: float = 70,
                 rsi_oversold: float = 30,
                 adx_period: int = 14,
                 adx_threshold: float = 25.0,
                 bb_period: int = 20,
                 bb_std: float = 2.0,
                 risk_per_trade: float = 0.02):
        """
        Initialize the Alpha engine.
        
        Args:
            rsi_period: RSI calculation period
            rsi_overbought: RSI level considered overbought
            rsi_oversold: RSI level considered oversold
            adx_period: ADX calculation period
            adx_threshold: Minimum ADX for trend confirmation
            bb_period: Bollinger Band period
            bb_std: Bollinger Band standard deviations
            risk_per_trade: Risk per trade as fraction of capital
        """
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.risk_per_trade = risk_per_trade
        
        self.regime_classifier = RegimeClassifier()
        self.current_position: Optional[Position] = None
        self._signal_history: List[TradingSignal] = []
        
    def analyze(self, df: pd.DataFrame, regime: Optional[RegimeType] = None) -> TradingSignal:
        """
        Analyze market data and generate trading signal.
        
        Args:
            df: DataFrame with OHLCV data
            regime: Optional pre-computed regime (will compute if not provided)
            
        Returns:
            TradingSignal with recommended action
        """
        if df is None or len(df) < max(self.rsi_period, self.bb_period, 60):
            return TradingSignal(
                signal_type=SignalType.NEUTRAL,
                strength=0.0,
                metadata={"reason": "insufficient_data"}
            )
        
        # Detect regime if not provided
        if regime is None:
            regime, _, _ = self.regime_classifier.classify(df)
        
        # Compute indicators
        indicators = self._compute_indicators(df)
        
        # Generate signal based on regime and indicators
        signal = self._generate_signal(indicators, regime, df)
        
        self._signal_history.append(signal)
        return signal
    
    def _compute_indicators(self, df: pd.DataFrame) -> Dict:
        """Compute all technical indicators."""
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        volume = df['volume'].values if 'volume' in df.columns else np.ones_like(close)
        
        # RSI
        rsi = self._calc_rsi(close)
        
        # ADX
        adx = self._calc_adx(high, low, close)
        
        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = self._calc_bollinger(close)
        
        # MACD
        macd, signal_line, histogram = self._calc_macd(close)
        
        # ATR for stops
        atr = self._calc_atr(high, low, close)
        
        # Volume SMA
        vol_sma = self._calc_volume_sma(volume)
        
        return {
            'rsi': rsi,
            'adx': adx,
            'bb_upper': bb_upper,
            'bb_middle': bb_middle,
            'bb_lower': bb_lower,
            'macd': macd,
            'macd_signal': signal_line,
            'macd_histogram': histogram,
            'atr': atr,
            'volume_sma': vol_sma,
            'close': close,
            'high': high,
            'low': low,
            'volume': volume
        }
    
    def _calc_rsi(self, closes: np.ndarray, period: int = None) -> np.ndarray:
        """Calculate RSI indicator."""
        period = period or self.rsi_period
        if len(closes) < period + 1:
            return np.zeros(len(closes))
        
        deltas = np.diff(closes, prepend=closes[0])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        
        avg_gains = np.zeros(len(closes))
        avg_losses = np.zeros(len(closes))
        avg_gains[period] = np.mean(gains[1:period+1])
        avg_losses[period] = np.mean(losses[1:period+1])
        
        for i in range(period + 1, len(closes)):
            avg_gains[i] = (avg_gains[i-1] * (period - 1) + gains[i]) / period
            avg_losses[i] = (avg_losses[i-1] * (period - 1) + losses[i]) / period
        
        rs = avg_gains / (avg_losses + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def _calc_adx(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, 
                  period: int = None) -> float:
        """Calculate ADX."""
        period = period or self.adx_period
        if len(close) < period + 1:
            return 0.0
        
        high_diff = np.diff(high, prepend=high[0])
        low_diff = -np.diff(low, prepend=low[0])
        
        plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
        minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0)
        
        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1])
            )
        )
        
        atr = np.zeros(len(close))
        atr[period] = np.mean(tr[:period])
        for i in range(period + 1, len(close)):
            atr[i] = (atr[i-1] * (period - 1) + tr[i-1]) / period
        
        plus_di = np.zeros(len(close))
        minus_di = np.zeros(len(close))
        
        for i in range(period, len(close)):
            if atr[i] > 0:
                plus_di[i] = 100 * np.mean(plus_dm[i-period+1:i+1]) / atr[i]
                minus_di[i] = 100 * np.mean(minus_dm[i-period+1:i+1]) / atr[i]
        
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        adx = np.mean(dx[-period:]) if len(dx) >= period else 0.0
        
        return adx
    
    def _calc_bollinger(self, closes: np.ndarray, period: int = None, 
                        num_std: float = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Calculate Bollinger Bands."""
        period = period or self.bb_period
        num_std = num_std or self.bb_std
        
        middle = pd.Series(closes).rolling(window=period).mean().values
        std = pd.Series(closes).rolling(window=period).std().values
        
        upper = middle + (std * num_std)
        lower = middle - (std * num_std)
        
        return upper, middle, lower
    
    def _calc_macd(self, closes: np.ndarray, fast: int = 12, slow: int = 26, 
                   signal_period: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Calculate MACD indicator."""
        if len(closes) < slow + signal_period:
            return np.zeros(3)
        
        ema_fast = pd.Series(closes).ewm(span=fast, adjust=False).mean().values
        ema_slow = pd.Series(closes).ewm(span=slow, adjust=False).mean().values
        
        macd = ema_fast - ema_slow
        signal = pd.Series(macd).ewm(span=signal_period, adjust=False).mean().values
        histogram = macd - signal
        
        return macd, signal, histogram
    
    def _calc_atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray,
                  period: int = 14) -> np.ndarray:
        """Calculate Average True Range."""
        if len(close) < 2:
            return np.zeros_like(close)
        
        tr1 = high[1:] - low[1:]
        tr2 = np.abs(high[1:] - close[:-1])
        tr3 = np.abs(low[1:] - close[:-1])
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        
        atr = np.zeros(len(close))
        atr[period] = np.mean(tr[:period])
        
        for i in range(period + 1, len(close)):
            atr[i] = (atr[i-1] * (period - 1) + tr[i-1]) / period
        
        return atr
    
    def _calc_volume_sma(self, volume: np.ndarray, period: int = 20) -> np.ndarray:
        """Calculate volume simple moving average."""
        return pd.Series(volume).rolling(window=period).mean().values
    
    def _generate_signal(self, indicators: Dict, regime: RegimeType,
                         df: pd.DataFrame) -> TradingSignal:
        """Generate trading signal based on indicators and regime."""
        rsi = indicators['rsi']
        adx = indicators['adx']
        bb_upper = indicators['bb_upper']
        bb_lower = indicators['bb_lower']
        macd_hist = indicators['macd_histogram']
        atr = indicators['atr']
        close = indicators['close']
        volume = indicators['volume']
        
        current_rsi = rsi[-1]
        current_close = close[-1]
        current_atr = atr[-1]
        current_macd = macd_hist[-1]
        
        # Regime-adaptive signal generation
        if regime == RegimeType.BULL_TREND:
            signal = self._bull_trend_signal(current_rsi, current_close, bb_upper[-1], 
                                            bb_lower[-1], adx, current_macd, current_atr)
        elif regime == RegimeType.BEAR_TREND:
            signal = self._bear_trend_signal(current_rsi, current_close, bb_upper[-1],
                                            bb_lower[-1], adx, current_macd, current_atr)
        elif regime == RegimeType.RANGE_BOUND:
            signal = self._range_bound_signal(current_rsi, current_close, bb_upper[-1],
                                              bb_lower[-1], current_atr)
        elif regime == RegimeType.HIGH_VOLATILITY:
            signal = self._high_vol_signal(current_rsi, current_close, adx, current_atr)
        elif regime == RegimeType.LOW_VOLATILITY:
            signal = self._low_vol_signal(current_rsi, current_close, adx, current_atr)
        else:
            signal = self._unknown_regime_signal(current_rsi, current_close, adx)
        
        # Add stop loss and take profit
        signal.stop_loss = self._calculate_stop_loss(signal, current_close, current_atr)
        signal.take_profit = self._calculate_take_profit(signal, current_close, current_atr)
        signal.entry_price = current_close
        signal.timestamp = df.index[-1] if hasattr(df, 'index') else None
        
        return signal
    
    def _bull_trend_signal(self, rsi: float, close: float, bb_upper: float,
                           bb_lower: float, adx: float, macd: float, atr: float) -> TradingSignal:
        """Generate signal for bull trend regime."""
        # Strong buy: RSI pullback in uptrend
        if 35 < rsi < 55 and adx > 25 and macd > 0:
            strength = min(0.7 + (adx / 100), 0.95)
            return TradingSignal(signal_type=SignalType.LONG, strength=strength)
        
        # Weak buy: RSI recovering from oversold
        if rsi < 40 and macd > 0:
            strength = 0.5 + (rsi / 100)
            return TradingSignal(signal_type=SignalType.LONG, strength=strength)
        
        # Sell signal: Overbought or trend reversal
        if rsi > 75 or (close > bb_upper and adx < 20):
            return TradingSignal(signal_type=SignalType.CLOSE_LONG, strength=0.7)
        
        return TradingSignal(signal_type=SignalType.NEUTRAL, strength=0.3)
    
    def _bear_trend_signal(self, rsi: float, close: float, bb_upper: float,
                           bb_lower: float, adx: float, macd: float, atr: float) -> TradingSignal:
        """Generate signal for bear trend regime."""
        # Short signal: RSI bounce in downtrend
        if 45 < rsi < 65 and adx > 25 and macd < 0:
            strength = min(0.7 + (adx / 100), 0.95)
            return TradingSignal(signal_type=SignalType.SHORT, strength=strength)
        
        # Buy cover: Oversold
        if rsi < 30 or close < bb_lower:
            return TradingSignal(signal_type=SignalType.CLOSE_SHORT, strength=0.7)
        
        return TradingSignal(signal_type=SignalType.NEUTRAL, strength=0.3)
    
    def _range_bound_signal(self, rsi: float, close: float, bb_upper: float,
                           bb_lower: float, atr: float) -> TradingSignal:
        """Generate signal for range-bound regime."""
        # Mean reversion at bands
        if close <= bb_lower and rsi < 35:
            strength = 0.6 + (30 - rsi) / 100
            return TradingSignal(signal_type=SignalType.LONG, strength=strength)
        
        if close >= bb_upper and rsi > 65:
            strength = 0.6 + (rsi - 70) / 100
            return TradingSignal(signal_type=SignalType.SHORT, strength=strength)
        
        return TradingSignal(signal_type=SignalType.NEUTRAL, strength=0.2)
    
    def _high_vol_signal(self, rsi: float, close: float, adx: float, 
                         atr: float) -> TradingSignal:
        """Generate signal for high volatility regime."""
        # In high vol, be more conservative
        if rsi < 25:
            return TradingSignal(signal_type=SignalType.LONG, strength=0.5)
        if rsi > 75:
            return TradingSignal(signal_type=SignalType.SHORT, strength=0.5)
        return TradingSignal(signal_type=SignalType.NEUTRAL, strength=0.2)
    
    def _low_vol_signal(self, rsi: float, close: float, adx: float,
                        atr: float) -> TradingSignal:
        """Generate signal for low volatility regime."""
        # Build positions slowly in low vol
        if rsi < 40:
            return TradingSignal(signal_type=SignalType.LONG, strength=0.6)
        if rsi > 60:
            return TradingSignal(signal_type=SignalType.SHORT, strength=0.6)
        return TradingSignal(signal_type=SignalType.NEUTRAL, strength=0.3)
    
    def _unknown_regime_signal(self, rsi: float, close: float, adx: float) -> TradingSignal:
        """Generate signal when regime is unknown."""
        if rsi < 30:
            return TradingSignal(signal_type=SignalType.LONG, strength=0.4)
        if rsi > 70:
            return TradingSignal(signal_type=SignalType.SHORT, strength=0.4)
        return TradingSignal(signal_type=SignalType.NEUTRAL, strength=0.2)
    
    def _calculate_stop_loss(self, signal: TradingSignal, close: float, atr: float) -> float:
        """Calculate stop loss price."""
        if signal.signal_type == SignalType.LONG:
            return close - (2.0 * atr)
        elif signal.signal_type == SignalType.SHORT:
            return close + (2.0 * atr)
        return None
    
    def _calculate_take_profit(self, signal: TradingSignal, close: float, atr: float) -> float:
        """Calculate take profit price."""
        if signal.signal_type == SignalType.LONG:
            return close + (3.0 * atr)
        elif signal.signal_type == SignalType.SHORT:
            return close - (3.0 * atr)
        return None
    
    def update_position(self, signal: TradingSignal, current_price: float,
                        timestamp: Optional[pd.Timestamp] = None) -> Position:
        """Update current position based on signal."""
        if signal.signal_type == SignalType.LONG and self.current_position is None:
            self.current_position = Position(
                side='long',
                entry_price=signal.entry_price,
                size=signal.strength * self.risk_per_trade,
                current_price=current_price,
                entry_time=timestamp
            )
        elif signal.signal_type == SignalType.SHORT and self.current_position is None:
            self.current_position = Position(
                side='short',
                entry_price=signal.entry_price,
                size=signal.strength * self.risk_per_trade,
                current_price=current_price,
                entry_time=timestamp
            )
        elif signal.signal_type in [SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT]:
            self.current_position = None
        
        if self.current_position:
            self.current_position.current_price = current_price
            self.current_position.unrealized_pnl = (
                (current_price - self.current_position.entry_price) 
                * self.current_position.size * (1 if self.current_position.side == 'long' else -1)
            )
        
        return self.current_position
    
    def get_status(self) -> Dict:
        """Get current engine status."""
        return {
            'has_position': self.current_position is not None,
            'position': self.current_position,
            'signal_history_len': len(self._signal_history),
            'last_signal': self._signal_history[-1] if self._signal_history else None
        }


def create_alpha_engine(config: Optional[Dict] = None) -> AlphaEngine:
    """Factory function to create AlphaEngine with optional config."""
    if config is None:
        return AlphaEngine()
    return AlphaEngine(**config)


# ========== Testing ==========
if __name__ == "__main__":
    import yfinance as yf
    
    print("Testing AlphaEngine...")
    
    try:
        data = yf.download("BTC-USD", start="2024-01-01", end="2026-04-01", progress=False)
        if len(data) > 0:
            df = data[['High', 'Low', 'Close', 'Volume']].copy()
            df.columns = ['high', 'low', 'close', 'volume']
            
            engine = AlphaEngine()
            
            # First get regime
            regime, confidence, metrics = engine.regime_classifier.classify(df)
            print(f"Regime: {regime.value} ({confidence:.1%})")
            
            # Then analyze
            signal = engine.analyze(df, regime)
            print(f"Signal: {signal.signal_type.value} (strength: {signal.strength:.2f})")
            print(f"Entry: {signal.entry_price}, SL: {signal.stop_loss}, TP: {signal.take_profit}")
            
    except Exception as e:
        print(f"Could not fetch test data: {e}")
        print("AlphaEngine module loaded successfully.")
