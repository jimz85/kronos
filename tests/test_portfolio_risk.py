#!/usr/bin/env python3
"""
test_portfolio_risk.py - Tests for Portfolio Risk Module
=======================================================

Tests for:
    - CorrelationMatrix class
    - check_portfolio_risk function
    - circuit_breaker integration
"""

import pytest
import numpy as np
import pandas as pd
from unittest.mock import MagicMock, patch
import sys
import os

# Add kronos to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.portfolio_risk import (
    CorrelationMatrix,
    check_portfolio_risk,
    format_correlation_report
)
from strategies.regime_classifier import (
    RegimeClassifier,
    RegimeType,
    RegimeMetrics
)
from risk.circuit_breaker import (
    check_portfolio_correlation_risk,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState
)


class TestCorrelationMatrix:
    """Tests for CorrelationMatrix class."""
    
    def test_initialization(self):
        """Test CorrelationMatrix initializes correctly."""
        cm = CorrelationMatrix(lookback_days=20, correlation_threshold=0.7)
        assert cm.lookback_days == 20
        assert cm.correlation_threshold == 0.7
        assert cm._correlation_matrix is None
        assert cm._btc_betas == {}
    
    def test_update_and_compute(self):
        """Test updating with price data and computing correlation matrix."""
        cm = CorrelationMatrix(lookback_days=20)
        
        # Create synthetic price data
        dates = pd.date_range('2024-01-01', periods=30, freq='1h')
        btc_price = 50000 + np.cumsum(np.random.randn(30) * 100)
        eth_price = 3000 + np.cumsum(np.random.randn(30) * 50)
        
        btc_df = pd.DataFrame({'close': btc_price}, index=dates)
        eth_df = pd.DataFrame({'close': eth_price}, index=dates)
        
        cm.update('BTC', btc_df)
        cm.update('ETH', eth_df)
        
        # Compute correlation
        corr_matrix = cm.compute_correlation_matrix(['BTC', 'ETH'])
        
        assert not corr_matrix.empty
        assert 'BTC' in corr_matrix.columns
        assert 'ETH' in corr_matrix.columns
        assert corr_matrix.loc['BTC', 'ETH'] == corr_matrix.loc['ETH', 'BTC']
    
    def test_btc_beta_calculation(self):
        """Test BTC Beta calculation."""
        cm = CorrelationMatrix(lookback_days=20)
        
        # Create correlated price data
        dates = pd.date_range('2024-01-01', periods=30, freq='1h')
        np.random.seed(42)
        
        # BTC goes up
        btc_price = 50000 + np.cumsum(np.random.randn(30) * 100)
        # ETH follows BTC with some correlation
        eth_price = 3000 + np.cumsum(np.random.randn(30) * 80)
        
        btc_df = pd.DataFrame({'close': btc_price}, index=dates)
        eth_df = pd.DataFrame({'close': eth_price}, index=dates)
        
        cm.update('ETH', eth_df)
        beta = cm.compute_btc_beta('ETH', btc_df)
        
        # Beta should be positive (positive correlation)
        assert beta > 0, f"Beta should be positive, got {beta}"
    
    def test_high_correlation_detection(self):
        """Test high correlation detection."""
        cm = CorrelationMatrix(correlation_threshold=0.7)
        
        # Create highly correlated data
        dates = pd.date_range('2024-01-01', periods=30, freq='1h')
        np.random.seed(42)
        prices = 100 + np.cumsum(np.random.randn(30))
        
        df1 = pd.DataFrame({'close': prices}, index=dates)
        # Create perfectly correlated second series (same returns)
        returns = np.diff(prices) / prices[:-1]
        prices2 = np.zeros(len(prices))
        prices2[0] = prices[0] * 1.5
        for i in range(len(returns)):
            prices2[i+1] = prices2[i] * (1 + returns[i])
        
        df2 = pd.DataFrame({'close': prices2}, index=dates)
        
        cm.update('COIN1', df1)
        cm.update('COIN2', df2)
        
        # Must compute correlation matrix first
        cm.compute_correlation_matrix(['COIN1', 'COIN2'])
        
        corr = cm.get_correlation('COIN1', 'COIN2')
        
        # Should have very high positive correlation
        assert abs(corr) > 0.5, f"Expected high correlation, got {corr}"


class TestCheckPortfolioRisk:
    """Tests for check_portfolio_risk function."""
    
    def test_no_existing_positions(self):
        """Test with no existing positions."""
        cm = CorrelationMatrix()
        
        allowed, reason = check_portfolio_risk(
            proposed_coin='ETH',
            proposed_direction='long',
            existing_positions={},
            correlation_matrix=cm
        )
        
        assert allowed is True
        assert "No conflicting positions" in reason
    
    def test_opposite_directions_allowed(self):
        """Test that opposite directions don't conflict."""
        cm = CorrelationMatrix()
        
        existing = {
            'BTC': {'direction': 'long', 'size': 0.5}
        }
        
        allowed, reason = check_portfolio_risk(
            proposed_coin='ETH',
            proposed_direction='short',  # Opposite direction
            existing_positions=existing,
            correlation_matrix=cm
        )
        
        # Should be allowed (hedging)
        assert allowed is True
    
    def test_high_correlation_same_direction_rejected(self):
        """Test that high correlation + same direction is rejected."""
        cm = CorrelationMatrix(correlation_threshold=0.7)
        
        # Setup with high correlation
        dates = pd.date_range('2024-01-01', periods=30, freq='1h')
        np.random.seed(42)
        prices = 100 + np.cumsum(np.random.randn(30))
        
        # Create perfectly correlated data
        returns = np.diff(prices) / prices[:-1]
        prices2 = np.zeros(len(prices))
        prices2[0] = 50  # Different starting price
        for i in range(len(returns)):
            prices2[i+1] = prices2[i] * (1 + returns[i])
        
        df1 = pd.DataFrame({'close': prices}, index=dates)
        df2 = pd.DataFrame({'close': prices2}, index=dates)
        
        cm.update('BTC', df1)
        cm.update('ETH', df2)
        
        # Compute correlation matrix
        cm.compute_correlation_matrix(['BTC', 'ETH'])
        
        existing = {
            'BTC': {'direction': 'long', 'size': 0.5}
        }
        
        allowed, reason = check_portfolio_risk(
            proposed_coin='ETH',
            proposed_direction='long',  # Same direction
            existing_positions=existing,
            correlation_matrix=cm
        )
        
        # High correlation + same direction should be rejected
        assert allowed is False, f"Expected rejection but got: {reason}"


class TestRegimeClassifierDynamicParams:
    """Tests for RegimeClassifier.get_dynamic_strategy_params."""
    
    def test_bull_regime_weights(self):
        """Test BULL regime returns trend-following weights."""
        classifier = RegimeClassifier()
        params = classifier.get_dynamic_strategy_params(RegimeType.BULL_TREND)
        
        assert params['trend_weight'] == 0.40
        assert params['momentum_weight'] == 0.35
        assert params['mean_reversion_weight'] == 0.10
        assert 'BULL' in params['description']
    
    def test_bear_regime_weights(self):
        """Test BEAR regime returns mean-reversion weights."""
        classifier = RegimeClassifier()
        params = classifier.get_dynamic_strategy_params(RegimeType.BEAR_TREND)
        
        assert params['mean_reversion_weight'] == 0.40
        assert params['trend_weight'] == 0.15
        assert 'BEAR' in params['description']
    
    def test_range_bound_weights(self):
        """Test SIDEWAYS regime returns range trading weights."""
        classifier = RegimeClassifier()
        params = classifier.get_dynamic_strategy_params(RegimeType.RANGE_BOUND)
        
        assert params['mean_reversion_weight'] == 0.45
        assert params['trend_weight'] == 0.15
        assert 'SIDEWAYS' in params['description']
    
    def test_high_volatility_weights(self):
        """Test HIGH_VOLATILITY regime prioritizes volatility protection."""
        classifier = RegimeClassifier()
        params = classifier.get_dynamic_strategy_params(RegimeType.HIGH_VOLATILITY)
        
        assert params['volatility_weight'] == 0.30
        assert 'HIGH VOL' in params['description']
    
    def test_weights_sum_to_one(self):
        """Test all weights sum to 1.0 for each regime."""
        classifier = RegimeClassifier()
        
        for regime in RegimeType:
            params = classifier.get_dynamic_strategy_params(regime)
            total = (
                params['momentum_weight'] +
                params['trend_weight'] +
                params['mean_reversion_weight'] +
                params['volatility_weight'] +
                params['volume_weight']
            )
            assert abs(total - 1.0) < 0.001, f"Weights for {regime} sum to {total}"


class TestCircuitBreakerIntegration:
    """Tests for circuit breaker portfolio risk integration."""
    
    def test_portfolio_risk_check_available(self):
        """Test that portfolio risk check function is available."""
        from risk.circuit_breaker import check_portfolio_correlation_risk
        
        # Should not raise
        allowed, reason = check_portfolio_correlation_risk(
            proposed_coin='ETH',
            proposed_direction='long',
            existing_positions={}
        )
        
        assert isinstance(allowed, bool)
        assert isinstance(reason, str)
    
    def test_portfolio_risk_with_correlation_matrix(self):
        """Test portfolio risk check with actual correlation matrix."""
        cm = CorrelationMatrix()
        
        # Setup high correlation data
        dates = pd.date_range('2024-01-01', periods=30, freq='1h')
        np.random.seed(42)
        prices = 100 + np.cumsum(np.random.randn(30))
        
        # Create perfectly correlated data
        returns = np.diff(prices) / prices[:-1]
        prices2 = np.zeros(len(prices))
        prices2[0] = 50
        for i in range(len(returns)):
            prices2[i+1] = prices2[i] * (1 + returns[i])
        
        df1 = pd.DataFrame({'close': prices}, index=dates)
        df2 = pd.DataFrame({'close': prices2}, index=dates)
        
        cm.update('BTC', df1)
        cm.update('ETH', df2)
        cm.compute_correlation_matrix(['BTC', 'ETH'])
        
        from risk.circuit_breaker import check_portfolio_correlation_risk
        
        existing = {'BTC': {'direction': 'long', 'size': 0.5}}
        
        allowed, reason = check_portfolio_correlation_risk(
            proposed_coin='ETH',
            proposed_direction='long',
            existing_positions=existing,
            correlation_matrix=cm
        )
        
        # High correlation + same direction should be rejected
        assert allowed is False, f"Expected rejection but got: {reason}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
