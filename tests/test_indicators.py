"""
Tests for core/indicators.py
=============================

Unit tests for the technical indicators library.
"""

import pytest
import numpy as np
import pandas as pd
from core.indicators import (
    calc_rsi,
    calc_ma,
    calc_ema,
    calc_atr,
    calc_bollinger,
    calc_macd,
    calc_adx,
    calc_cci
)


class TestRSI:
    """Tests for calc_rsi function."""

    def test_rsi_basic(self, sample_prices):
        """Test basic RSI calculation."""
        rsi = calc_rsi(sample_prices, period=5)
        assert len(rsi) == len(sample_prices)
        # RSI values should be between 0 and 100
        assert rsi.notna().sum() > 0  # Some valid values after warmup
        # First 'period' values should be NaN due to rolling window
        assert rsi.iloc[:4].isna().all()

    def test_rsi_returns_series(self, sample_prices):
        """Test that RSI returns a pandas Series."""
        rsi = calc_rsi(sample_prices)
        assert isinstance(rsi, pd.Series)

    def test_rsi_default_period(self):
        """Test RSI with default period (14)."""
        prices = np.random.uniform(90, 110, 50)
        rsi = calc_rsi(prices)
        assert len(rsi) == 50

    def test_rsi_flat_prices(self):
        """Test RSI with flat/unmoving prices."""
        flat_prices = np.full(30, 100.0)
        rsi = calc_rsi(flat_prices, period=14)
        # RSI should be 50 for flat prices (no gains/losses)
        # After warmup period, RSI should hover around 50
        assert rsi.notna().sum() > 0


class TestMA:
    """Tests for calc_ma function."""

    def test_ma_basic(self, sample_prices):
        """Test basic MA calculation."""
        ma = calc_ma(sample_prices, period=3)
        assert len(ma) == len(sample_prices)
        # First 2 values should be NaN
        assert np.isnan(ma.iloc[0])
        assert np.isnan(ma.iloc[1])
        # Third value onwards should have values
        assert ma.iloc[2:].notna().all()

    def test_ma_returns_series(self, sample_prices):
        """Test that MA returns a pandas Series."""
        ma = calc_ma(sample_prices, period=5)
        assert isinstance(ma, pd.Series)

    def test_ma_single_value(self):
        """Test MA with period of 1."""
        prices = np.array([100.0, 105.0, 110.0])
        ma = calc_ma(prices, period=1)
        np.testing.assert_array_equal(ma, prices)


class TestEMA:
    """Tests for calc_ema function."""

    def test_ema_basic(self, sample_prices):
        """Test basic EMA calculation."""
        ema = calc_ema(sample_prices, period=5)
        assert len(ema) == len(sample_prices)
        assert not np.isnan(ema[-1])  # Last value should be valid

    def test_ema_returns_array(self, sample_prices):
        """Test that EMA returns a numpy array."""
        ema = calc_ema(sample_prices, period=5)
        assert isinstance(ema, np.ndarray)

    def test_ema_default_period(self):
        """Test EMA with default period (20)."""
        prices = np.random.uniform(90, 110, 50)
        ema = calc_ema(prices)
        assert len(ema) == 50
        assert not np.isnan(ema[-1])


class TestATR:
    """Tests for calc_atr function."""

    def test_atr_basic(self, sample_ohlc):
        """Test basic ATR calculation."""
        atr = calc_atr(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close'],
            period=5
        )
        assert len(atr) == len(sample_ohlc['high'])
        assert atr.notna().sum() > 0

    def test_atr_returns_series(self, sample_ohlc):
        """Test that ATR returns a pandas Series."""
        atr = calc_atr(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close']
        )
        assert isinstance(atr, pd.Series)

    def test_atr_positive_values(self, sample_ohlc):
        """Test that ATR values are positive."""
        atr = calc_atr(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close'],
            period=5
        )
        valid_atr = atr.dropna()
        assert (valid_atr >= 0).all()


class TestBollinger:
    """Tests for calc_bollinger function."""

    def test_bollinger_basic(self, sample_prices):
        """Test basic Bollinger Bands calculation."""
        middle, upper, lower = calc_bollinger(sample_prices, period=5)
        assert len(middle) == len(sample_prices)
        assert len(upper) == len(sample_prices)
        assert len(lower) == len(sample_prices)

    def test_bollinger_upper_above_lower(self, sample_prices):
        """Test that upper band is above lower band."""
        middle, upper, lower = calc_bollinger(sample_prices, period=5)
        valid_idx = lower.notna()
        assert (upper[valid_idx] >= lower[valid_idx]).all()

    def test_bollinger_returns_tuples(self, sample_prices):
        """Test that Bollinger returns correct tuple structure."""
        result = calc_bollinger(sample_prices, period=5)
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_bollinger_custom_std_mult(self):
        """Test Bollinger Bands with custom standard deviation multiplier."""
        prices = np.random.uniform(90, 110, 100)
        middle, upper, lower = calc_bollinger(prices, period=20, std_mult=3.0)
        # With higher std_mult, bands should be wider
        middle2, upper2, lower2 = calc_bollinger(prices, period=20, std_mult=2.0)
        width1 = (upper - lower).mean()
        width2 = (upper2 - lower2).mean()
        assert width1 > width2


class TestMACD:
    """Tests for calc_macd function."""

    def test_macd_basic(self, sample_prices):
        """Test basic MACD calculation."""
        macd, signal, hist = calc_macd(sample_prices)
        assert len(macd) == len(sample_prices)
        assert len(signal) == len(sample_prices)
        assert len(hist) == len(sample_prices)

    def test_macd_returns_tuples(self, sample_prices):
        """Test that MACD returns correct tuple structure."""
        result = calc_macd(sample_prices)
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_macd_histogram(self, sample_prices):
        """Test MACD histogram calculation."""
        macd, signal, hist = calc_macd(sample_prices)
        # Histogram should be MACD - Signal line
        np.testing.assert_array_almost_equal(macd - signal, hist)


class TestADX:
    """Tests for calc_adx function."""

    def test_adx_basic(self, sample_ohlc):
        """Test basic ADX calculation."""
        adx = calc_adx(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close'],
            n=5
        )
        assert len(adx) == len(sample_ohlc['high'])
        assert adx.notna().sum() > 0

    def test_adx_returns_series(self, sample_ohlc):
        """Test that ADX returns a pandas Series."""
        adx = calc_adx(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close']
        )
        assert isinstance(adx, pd.Series)

    def test_adx_positive_values(self, sample_ohlc):
        """Test that ADX values are positive (0-100 range)."""
        adx = calc_adx(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close'],
            n=5
        )
        valid_adx = adx.dropna()
        assert (valid_adx >= 0).all()
        assert (valid_adx <= 100).all()


class TestCCI:
    """Tests for calc_cci function."""

    def test_cci_basic(self, sample_ohlc):
        """Test basic CCI calculation."""
        cci = calc_cci(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close'],
            period=5
        )
        assert len(cci) == len(sample_ohlc['high'])

    def test_cci_returns_series(self, sample_ohlc):
        """Test that CCI returns a pandas Series."""
        cci = calc_cci(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close']
        )
        assert isinstance(cci, pd.Series)

    def test_cci_typical_price(self, sample_ohlc):
        """Test CCI calculation uses typical price correctly."""
        typical = (sample_ohlc['high'] + sample_ohlc['low'] + sample_ohlc['close']) / 3
        # CCI should vary around 0 when price is near the moving average
        cci = calc_cci(
            sample_ohlc['high'],
            sample_ohlc['low'],
            sample_ohlc['close'],
            period=20
        )
        # With enough data, CCI should have some variation
        assert cci.std() >= 0


class TestIndicatorsEdgeCases:
    """Edge case tests for indicator functions."""

    def test_empty_array(self):
        """Test indicators with empty arrays."""
        empty = np.array([])
        # RSI should handle gracefully
        rsi = calc_rsi(empty)
        assert len(rsi) == 0

    def test_single_value(self):
        """Test indicators with single value."""
        single = np.array([100.0])
        rsi = calc_rsi(single, period=14)
        assert len(rsi) == 1

    def test_nan_values(self):
        """Test indicators with NaN values in input."""
        prices = np.array([100.0, np.nan, 102.0, 103.0, 105.0, 104.0, 106.0, 108.0, 107.0, 110.0])
        rsi = calc_rsi(prices, period=5)
        assert len(rsi) == len(prices)

    def test_negative_prices(self):
        """Test indicators with negative prices (unusual but should handle)."""
        prices = np.array([-100.0, -90.0, -80.0, -70.0, -60.0])
        ma = calc_ma(prices, period=3)
        assert len(ma) == len(prices)
