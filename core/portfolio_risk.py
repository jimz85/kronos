#!/usr/bin/env python3
"""
portfolio_risk.py - Portfolio Risk Management Module
====================================================

Multi-coin correlation matrix and portfolio risk checking.
Prevents overexposure to correlated positions.

Key Components:
    - CorrelationMatrix: 20-day rolling correlation matrix + BTC Beta
    - check_portfolio_risk(): High-correlation same-direction exposure check
    - integrate with circuit_breaker for risk-prevented openings

Author: Kronos Trading System
Version: 5.0.0
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import logging

logger = logging.getLogger('kronos.portfolio_risk')


@dataclass
class CorrelationMatrix:
    """
    Rolling correlation matrix for multi-coin portfolio risk.
    
    Tracks 20-day rolling correlations between coins and BTC,
    computes BTC Beta, and detects correlated exposures.
    """
    
    lookback_days: int = 20
    btc_symbol: str = "BTC"
    correlation_threshold: float = 0.70  # Threshold for "high correlation"
    beta_threshold: float = 1.20          # Threshold for "high beta"
    
    def __post_init__(self):
        self._correlation_matrix: Optional[pd.DataFrame] = None
        self._btc_betas: Dict[str, float] = {}
        self._last_update: Optional[pd.Timestamp] = None
        self._price_data: Dict[str, pd.DataFrame] = {}
    
    def update(self, coin: str, df: pd.DataFrame) -> None:
        """
        Update price data for a coin.
        
        Args:
            coin: Coin symbol (e.g., 'ETH')
            df: DataFrame with 'close' column
        """
        if df is None or 'close' not in df.columns or len(df) < self.lookback_days:
            return
        
        self._price_data[coin] = df[['close']].copy()
        self._last_update = pd.Timestamp.now()
    
    def compute_correlation_matrix(self, coins: List[str]) -> pd.DataFrame:
        """
        Compute rolling correlation matrix for given coins.
        
        Args:
            coins: List of coin symbols
            
        Returns:
            DataFrame with correlation coefficients
        """
        if not coins or len(coins) < 2:
            return pd.DataFrame()
        
        # Collect price data
        price_dict = {}
        min_len = float('inf')
        
        for coin in coins:
            if coin in self._price_data and len(self._price_data[coin]) >= self.lookback_days:
                prices = self._price_data[coin]['close'].iloc[-self.lookback_days:]
                price_dict[coin] = prices.values
                min_len = min(min_len, len(prices))
        
        if len(price_dict) < 2:
            return pd.DataFrame()
        
        # Trim to common length
        for coin in price_dict:
            price_dict[coin] = price_dict[coin][-int(min_len):]
        
        # Compute correlation matrix
        price_df = pd.DataFrame(price_dict)
        self._correlation_matrix = price_df.corr()
        
        return self._correlation_matrix
    
    def compute_btc_beta(self, coin: str, btc_df: pd.DataFrame) -> float:
        """
        Compute BTC Beta for a coin.
        
        Beta = Cov(coin, BTC) / Var(BTC)
        - Beta > 1: Coin is more volatile than BTC
        - Beta < 1: Coin is less volatile than BTC
        - Beta < 0: Coin moves opposite to BTC
        
        Args:
            coin: Coin symbol
            btc_df: DataFrame with 'close' column for BTC
            
        Returns:
            BTC Beta value
        """
        if coin not in self._price_data or btc_df is None:
            return 1.0
        
        coin_prices = self._price_data[coin]['close'].iloc[-self.lookback_days:].values
        btc_prices = btc_df['close'].iloc[-self.lookback_days:].values
        
        min_len = min(len(coin_prices), len(btc_prices))
        if min_len < 10:
            return 1.0
        
        coin_prices = coin_prices[-min_len:]
        btc_prices = btc_prices[-min_len:]
        
        # Compute returns
        coin_returns = np.diff(coin_prices) / coin_prices[:-1]
        btc_returns = np.diff(btc_prices) / btc_prices[:-1]
        
        if len(coin_returns) < 5 or len(btc_returns) < 5:
            return 1.0
        
        # Compute Beta
        cov = np.cov(coin_returns, btc_returns)[0, 1]
        var_btc = np.var(btc_returns)
        
        if var_btc <= 0:
            return 1.0
        
        beta = cov / var_btc
        self._btc_betas[coin] = beta
        
        return beta
    
    def get_btc_beta(self, coin: str) -> float:
        """Get cached BTC Beta for a coin."""
        return self._btc_betas.get(coin, 1.0)
    
    def get_correlation(self, coin1: str, coin2: str) -> float:
        """Get correlation between two coins."""
        if self._correlation_matrix is None:
            return 0.0
        try:
            return self._correlation_matrix.loc[coin1, coin2]
        except KeyError:
            return 0.0
    
    def is_high_correlation(self, coin1: str, coin2: str) -> bool:
        """Check if two coins are highly correlated (above threshold)."""
        return abs(self.get_correlation(coin1, coin2)) >= self.correlation_threshold
    
    def is_high_beta(self, coin: str) -> bool:
        """Check if coin has high BTC beta (above threshold)."""
        return self.get_btc_beta(coin) >= self.beta_threshold


def check_portfolio_risk(
    proposed_coin: str,
    proposed_direction: str,
    existing_positions: Dict[str, dict],
    correlation_matrix: CorrelationMatrix,
    btc_df: Optional[pd.DataFrame] = None
) -> Tuple[bool, str]:
    """
    Check if a proposed trade would create dangerous correlated exposure.
    
    High correlation same-direction exposure = increased systemic risk.
    If BTC is correlated at 0.7+ with proposed coin AND both are LONG,
    this creates concentrated directional risk.
    
    Args:
        proposed_coin: Coin symbol being considered
        proposed_direction: 'long' or 'short'
        existing_positions: Dict of {symbol: {'direction': str, 'size': float}}
        correlation_matrix: CorrelationMatrix instance
        btc_df: Optional BTC price data for beta calculation
        
    Returns:
        Tuple of (is_allowed: bool, reason: str)
    """
    if not existing_positions or proposed_direction == 'neutral':
        return True, "No conflicting positions"
    
    # Compute correlation with existing positions
    btc_correlation = 0.0
    conflicting_coins = []
    
    for existing_coin, position in existing_positions.items():
        existing_direction = position.get('direction', '').lower()
        
        # Skip if directions don't match (long vs short is actually hedging)
        if existing_direction != proposed_direction.lower():
            continue
        
        # Skip the proposed coin itself
        if existing_coin == proposed_coin:
            continue
        
        corr = correlation_matrix.get_correlation(proposed_coin, existing_coin)
        
        # Check correlation threshold
        if abs(corr) >= correlation_matrix.correlation_threshold:
            btc_correlation = max(btc_correlation, abs(corr))
            conflicting_coins.append((existing_coin, corr))
    
    # Check BTC Beta for LONG positions (high beta = correlated with market)
    if proposed_direction.lower() == 'long' and btc_df is not None:
        beta = correlation_matrix.compute_btc_beta(proposed_coin, btc_df)
        if beta >= correlation_matrix.beta_threshold:
            return False, (
                f"REJECTED: {proposed_coin} has HIGH BTC Beta ({beta:.2f}). "
                f"Correlated exposure risk. "
                f"Conflicting coins: {conflicting_coins}"
            )
    
    # Reject if highly correlated same-direction positions exist
    if btc_correlation >= correlation_matrix.correlation_threshold:
        return False, (
            f"REJECTED: {proposed_coin} correlation={btc_correlation:.2f} "
            f"with {conflicting_coins}. "
            f"High correlation + same direction = concentrated risk. "
            f"Reduce existing exposure first."
        )
    
    return True, f"OK: {proposed_coin} passes portfolio risk check"


def format_correlation_report(
    correlation_matrix: CorrelationMatrix,
    coins: List[str]
) -> str:
    """Format correlation matrix as readable string."""
    corr_matrix = correlation_matrix.compute_correlation_matrix(coins)
    if corr_matrix.empty:
        return "Insufficient data for correlation analysis"
    
    # Format as string
    lines = ["\n╔══════════════════════════════════════════════════════════════╗",
             "║              PORTFOLIO CORRELATION MATRIX                  ║",
             "╠══════════════════════════════════════════════════════════════╣"]
    
    # Header
    header = "║  "
    for coin in corr_matrix.columns:
        header += f"{coin[:6]:>8}"
    header += "  ║"
    lines.append(header)
    lines.append("╠══════════════════════════════════════════════════════════════╣")
    
    # Data
    for coin in corr_matrix.index:
        row = f"║  {coin[:6]:<6}"
        for val in corr_matrix.loc[coin]:
            if abs(val) >= 0.7:
                row += f"  {val:>6.2f}*"  # Mark high correlation
            else:
                row += f"  {val:>6.2f}"
        row += "  ║"
        lines.append(row)
    
    lines.append("╚══════════════════════════════════════════════════════════════╝")
    lines.append("(* = high correlation ≥0.70)")
    
    # Add BTC Betas
    lines.append("\n╔══════════════════════════════════════════════════════════════╗")
    lines.append("║                    BTC BETA                               ║")
    lines.append("╠══════════════════════════════════════════════════════════════╣")
    
    for coin in coins:
        beta = correlation_matrix.get_btc_beta(coin)
        if beta >= 1.2:
            lines.append(f"║  {coin[:6]:<6}: {beta:>6.2f}  HIGH BETA ⚠️                    ║")
        elif beta <= 0.8:
            lines.append(f"║  {coin[:6]:<6}: {beta:>6.2f}  LOW BETA                             ║")
        else:
            lines.append(f"║  {coin[:6]:<6}: {beta:>6.2f}  NEUTRAL                              ║")
    
    lines.append("╚══════════════════════════════════════════════════════════════╝")
    
    return "\n".join(lines)


# ========== Testing ==========
if __name__ == "__main__":
    import yfinance as yf
    
    print("Testing CorrelationMatrix...")
    
    # Test with crypto data
    try:
        coins = ['BTC', 'ETH', 'BNB', 'SOL']
        data = {}
        
        for coin in coins:
            symbol = f"{coin}-USD"
            df = yf.download(symbol, start="2024-01-01", end="2026-04-01", progress=False)
            if len(df) > 0:
                data[coin] = df[['Close']].rename(columns={'Close': 'close'})
        
        if len(data) >= 2:
            # Create correlation matrix
            cm = CorrelationMatrix(lookback_days=20)
            
            # Update with data
            for coin, df in data.items():
                cm.update(coin, df)
            
            # Compute and display
            corr_matrix = cm.compute_correlation_matrix(list(data.keys()))
            print(format_correlation_report(cm, list(data.keys())))
            
            # Test BTC Beta for ETH
            eth_beta = cm.compute_btc_beta('ETH', data['BTC'])
            print(f"\nETH BTC Beta: {eth_beta:.2f}")
            
            # Test portfolio risk check
            existing = {
                'BTC': {'direction': 'long', 'size': 0.5},
                'ETH': {'direction': 'long', 'size': 0.3}
            }
            
            allowed, reason = check_portfolio_risk(
                proposed_coin='BNB',
                proposed_direction='long',
                existing_positions=existing,
                correlation_matrix=cm,
                btc_df=data.get('BTC')
            )
            print(f"\nPortfolio Risk Check (BNB LONG): allowed={allowed}, reason={reason}")
            
        else:
            print("Could not fetch enough test data")
            
    except Exception as e:
        print(f"Test error: {e}")
    
    print("\nCorrelationMatrix module loaded successfully.")
