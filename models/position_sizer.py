#!/usr/bin/env python3
"""
position_sizer.py
================
Dynamic position sizing module for Kronos trading system.

This module determines optimal position sizes based on:
    - Account risk parameters
    - Signal confidence
    - Market volatility (ATR)
    - Current market regime
    - Portfolio concentration limits
    - Correlation with existing positions

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
class PositionSizeResult:
    """Container for position sizing result."""
    size: float  # Position size as fraction of capital (0-1)
    size_dollars: float  # Position size in dollar terms
    num_units: float  # Number of units/contracts
    risk_amount: float  # Dollar risk amount
    risk_ratio: float  # Risk as fraction of capital
    stop_loss_price: float  # Calculated stop loss price
    take_profit_price: float  # Calculated take profit price
    adjusted_for: List[str]  # List of adjustments made
    confidence_adjusted: bool  # Whether confidence adjustment was applied
    regime_adjusted: bool  # Whether regime adjustment was applied
    volatility_adjusted: bool  # Whether volatility adjustment was applied


@dataclass
class RiskParameters:
    """Risk management parameters."""
    max_risk_per_trade: float = 0.02  # 2% max risk per trade
    max_portfolio_risk: float = 0.06  # 6% max total portfolio risk
    min_position_size: float = 0.01  # 1% minimum position
    max_position_size: float = 0.25  # 25% maximum position
    max_correlation: float = 0.7  # Max correlation between positions
    default_stop_pct: float = 0.02  # Default 2% stop loss


class PositionSizer:
    """
    Dynamic position sizing calculator.
    
    Calculates optimal position sizes considering:
    - Risk management rules
    - Signal confidence
    - Market volatility
    - Portfolio context
    - Regime-specific adjustments
    """
    
    def __init__(self,
                 risk_params: Optional[RiskParameters] = None,
                 account_balance: float = 10000.0):
        """
        Initialize the position sizer.
        
        Args:
            risk_params: Risk management parameters
            account_balance: Current account balance
        """
        self.risk_params = risk_params or RiskParameters()
        self.account_balance = account_balance
        
        self.regime_classifier = RegimeClassifier()
        self._position_history: List[Dict] = []
        self._current_positions: List[Dict] = []
        
    def calculate_size(self,
                      entry_price: float,
                      stop_loss: float,
                      take_profit: float,
                      signal_confidence: float,
                      regime: Optional[RegimeType] = None,
                      df: Optional[pd.DataFrame] = None,
                      existing_positions: Optional[List[Dict]] = None) -> PositionSizeResult:
        """
        Calculate optimal position size.
        
        Args:
            entry_price: Planned entry price
            stop_loss: Stop loss price
            take_profit: Take profit price
            signal_confidence: Confidence of the signal (0-1)
            regime: Current market regime
            df: DataFrame for additional analysis
            existing_positions: List of current positions for correlation check
            
        Returns:
            PositionSizeResult with calculated size and adjustments
        """
        adjustments = []
        confidence_adjusted = False
        regime_adjusted = False
        volatility_adjusted = False
        
        # Start with base size from risk parameters
        base_size = self.risk_params.max_risk_per_trade
        
        # Calculate risk per unit
        risk_per_unit = abs(entry_price - stop_loss) if stop_loss else entry_price * self.risk_params.default_stop_pct
        
        # Base position size from risk
        risk_amount = self.account_balance * self.risk_params.max_risk_per_trade
        size = risk_amount / risk_per_unit if risk_per_unit > 0 else 0
        
        # Convert to fraction of capital
        size_fraction = size / self.account_balance if self.account_balance > 0 else 0
        
        # Step 1: Confidence adjustment
        if signal_confidence < 0.5:
            size_fraction *= (signal_confidence / 0.5)
            adjustments.append(f"Low confidence ({signal_confidence:.1%} -> reduced)")
            confidence_adjusted = True
        elif signal_confidence > 0.8:
            size_fraction *= (0.9 + (signal_confidence - 0.8) * 0.5)  # Slight boost
            adjustments.append(f"High confidence ({signal_confidence:.1%} -> slight boost)")
            confidence_adjusted = True
        
        # Step 2: Regime adjustment
        if regime is None and df is not None:
            regime, _, _ = self.regime_classifier.classify(df)
        
        if regime:
            size_fraction, reg_adj = self._apply_regime_adjustment(size_fraction, regime)
            if reg_adj:
                adjustments.append(f"Regime adjustment ({regime.value})")
                regime_adjusted = True
        
        # Step 3: Volatility adjustment
        if df is not None and len(df) > 14:
            size_fraction, vol_adj = self._apply_volatility_adjustment(size_fraction, df)
            if vol_adj:
                adjustments.append("Volatility adjustment applied")
                volatility_adjusted = True
        
        # Step 4: Portfolio concentration check
        if existing_positions:
            size_fraction, conc_adj = self._apply_concentration_check(
                size_fraction, entry_price, existing_positions
            )
            if conc_adj:
                adjustments.append("Concentration limit applied")
        
        # Step 5: Apply hard limits
        size_fraction = max(
            self.risk_params.min_position_size,
            min(size_fraction, self.risk_params.max_position_size)
        )
        
        # Calculate final values
        final_size = size_fraction * self.account_balance
        num_units = final_size / entry_price if entry_price > 0 else 0
        final_risk = final_size * (risk_per_unit / entry_price) if entry_price > 0 else 0
        
        result = PositionSizeResult(
            size=size_fraction,
            size_dollars=final_size,
            num_units=num_units,
            risk_amount=final_risk,
            risk_ratio=final_risk / self.account_balance if self.account_balance > 0 else 0,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            adjusted_for=adjustments,
            confidence_adjusted=confidence_adjusted,
            regime_adjusted=regime_adjusted,
            volatility_adjusted=volatility_adjusted
        )
        
        return result
    
    def _apply_regime_adjustment(self, size: float, regime: RegimeType) -> Tuple[float, bool]:
        """Apply regime-specific position size adjustments."""
        adjustments = {
            RegimeType.BULL_TREND: 1.0,      # Full size in bull trend
            RegimeType.BEAR_TREND: 0.7,      # Reduced in bear trend
            RegimeType.RANGE_BOUND: 0.8,     # Slightly reduced in range
            RegimeType.HIGH_VOLATILITY: 0.5, # Significantly reduced in high vol
            RegimeType.LOW_VOLATILITY: 1.0,  # Full size in low vol
            RegimeType.UNKNOWN: 0.5          # Reduced when unknown
        }
        
        factor = adjustments.get(regime, 0.5)
        adjusted = size * factor
        return adjusted, factor != 1.0
    
    def _apply_volatility_adjustment(self, size: float, df: pd.DataFrame) -> Tuple[float, bool]:
        """Apply volatility-based position size adjustments."""
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        
        if len(close) < 14:
            return size, False
        
        # Calculate ATR
        tr1 = high[1:] - low[1:]
        tr2 = np.abs(high[1:] - close[:-1])
        tr3 = np.abs(low[1:] - close[:-1])
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        atr = np.zeros(len(close))
        atr[14] = np.mean(tr[:14])
        for i in range(15, len(close)):
            atr[i] = (atr[i-1] * 13 + tr[i-1]) / 14
        
        current_atr = atr[-1]
        avg_atr = np.mean(atr[-30:]) if len(atr) >= 30 else np.mean(atr)
        
        if avg_atr <= 0:
            return size, False
        
        atr_ratio = current_atr / avg_atr
        
        # High volatility = reduce size
        if atr_ratio > 1.5:
            factor = 0.6
        elif atr_ratio > 1.2:
            factor = 0.8
        elif atr_ratio < 0.5:
            factor = 1.2  # Low vol = can increase size
        elif atr_ratio < 0.8:
            factor = 1.1
        else:
            factor = 1.0
        
        return size * factor, factor != 1.0
    
    def _apply_concentration_check(self,
                                  size: float,
                                  entry_price: float,
                                  existing_positions: List[Dict]) -> Tuple[float, bool]:
        """Check and adjust for portfolio concentration."""
        if not existing_positions:
            return size, False
        
        # Calculate current portfolio exposure
        total_exposure = sum(pos.get('size', 0) for pos in existing_positions)
        
        # Check if adding new position would exceed limits
        new_exposure = total_exposure + size
        
        if new_exposure > self.risk_params.max_portfolio_risk:
            available = self.risk_params.max_portfolio_risk - total_exposure
            if available <= 0:
                return 0.0, True
            return min(size, available), True
        
        # Check correlation with existing positions
        for pos in existing_positions:
            if 'symbol' in pos and 'correlation' in pos:
                if pos.get('correlation', 0) > self.risk_params.max_correlation:
                    # Reduce size for highly correlated position
                    return size * 0.5, True
        
        return size, False
    
    def calculate_kelly_fraction(self,
                                win_rate: float,
                                avg_win: float,
                                avg_loss: float) -> float:
        """
        Calculate Kelly Criterion position fraction.
        
        Args:
            win_rate: Historical win rate (0-1)
            avg_win: Average win amount
            avg_loss: Average loss amount
            
        Returns:
            Kelly fraction for position sizing
        """
        if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
            return 0.0
        
        # Kelly formula: f* = (bp - q) / b
        # where b = avg_win/avg_loss, p = win_rate, q = 1 - p
        b = avg_win / avg_loss
        p = win_rate
        q = 1 - p
        
        kelly = (b * p - q) / b
        
        # Use half-Kelly for more conservative sizing
        return max(0, kelly * 0.5)
    
    def get_max_position(self, price: float) -> PositionSizeResult:
        """
        Get maximum position size given current risk parameters.
        
        Args:
            price: Asset price
            
        Returns:
            Maximum position size result
        """
        stop_loss = price * (1 - self.risk_params.default_stop_pct)
        return self.calculate_size(
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=price * (1 + self.risk_params.default_stop_pct * 2),
            signal_confidence=1.0,
            regime=RegimeType.UNKNOWN  # Conservative for max size
        )
    
    def update_position(self, position: Dict):
        """Track an open position for portfolio-level calculations."""
        self._current_positions.append(position)
    
    def close_position(self, position_id: str):
        """Remove a closed position from tracking."""
        self._current_positions = [
            p for p in self._current_positions if p.get('id') != position_id
        ]
    
    def get_portfolio_risk(self) -> float:
        """Calculate current total portfolio risk."""
        return sum(
            pos.get('risk_amount', 0) 
            for pos in self._current_positions
        )
    
    def format_size_report(self, result: PositionSizeResult) -> str:
        """Format position size result as readable string."""
        return f"""
╔══════════════════════════════════════════════════════════════╗
║               POSITION SIZING REPORT                      ║
╠══════════════════════════════════════════════════════════════╣
║ CALCULATED SIZE                                             ║
║   Position Size:      {result.size:>8.2%} (${result.size_dollars:,.2f})          ║
║   Number of Units:   {result.num_units:>12.2f}                       ║
║   Risk Amount:       ${result.risk_amount:>10,.2f}                       ║
║   Risk Ratio:        {result.risk_ratio:>8.2%}                        ║
╠══════════════════════════════════════════════════════════════╣
║ REFERENCE PRICES                                             ║
║   Stop Loss:        ${result.stop_loss_price:>12,.2f}                       ║
║   Take Profit:      ${result.take_profit_price:>12,.2f}                       ║
╠══════════════════════════════════════════════════════════════╣
║ ADJUSTMENTS                                                 ║
{"".join(f"║   ✓ {adj:<52} ║" for adj in result.adjusted_for)}
╠══════════════════════════════════════════════════════════════╣
║ MODIFIERS APPLIED                                           ║
║   Confidence:       {'Yes' if result.confidence_adjusted else 'No':<12}   Volatility:      {'Yes' if result.volatility_adjusted else 'No':<12}   ║
║   Regime:          {'Yes' if result.regime_adjusted else 'No':<12}                        ║
╚══════════════════════════════════════════════════════════════╝
"""


def create_position_sizer(config: Optional[Dict] = None) -> PositionSizer:
    """Factory function to create PositionSizer with optional config."""
    if config is None:
        return PositionSizer()
    
    risk_params = RiskParameters(**config.get('risk_params', {})) if 'risk_params' in config else None
    return PositionSizer(
        risk_params=risk_params,
        account_balance=config.get('account_balance', 10000.0)
    )


# ========== Testing ==========
if __name__ == "__main__":
    import yfinance as yf
    
    print("Testing PositionSizer...")
    
    try:
        data = yf.download("BTC-USD", start="2024-01-01", end="2026-04-01", progress=False)
        if len(data) > 0:
            df = data[['High', 'Low', 'Close', 'Volume']].copy()
            df.columns = ['high', 'low', 'close', 'volume']
            
            sizer = PositionSizer(account_balance=10000)
            
            entry = df['close'].iloc[-1]
            stop = entry * 0.98
            take = entry * 1.05
            
            result = sizer.calculate_size(
                entry_price=entry,
                stop_loss=stop,
                take_profit=take,
                signal_confidence=0.75,
                df=df
            )
            
            print(sizer.format_size_report(result))
            
            # Test Kelly calculation
            kelly = sizer.calculate_kelly_fraction(0.6, 100, 50)
            print(f"Kelly fraction for 60% win rate, 2:1 R:R: {kelly:.2%}")
            
    except Exception as e:
        print(f"Could not fetch test data: {e}")
        print("PositionSizer module loaded successfully.")
