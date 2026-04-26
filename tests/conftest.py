"""
Pytest Configuration for Kronos Test Suite
===========================================

Provides fixtures and shared test configuration for Kronos tests.
"""

import pytest
import numpy as np
import pandas as pd
from pathlib import Path


@pytest.fixture
def sample_prices():
    """Generate sample price data for indicator tests."""
    return np.array([100.0, 102.0, 101.0, 103.0, 105.0, 104.0, 106.0, 108.0, 107.0, 110.0])


@pytest.fixture
def sample_ohlc():
    """Generate sample OHLC data for indicator tests."""
    return {
        'high': np.array([102.0, 103.0, 102.5, 104.0, 106.0, 105.5, 107.0, 109.0, 108.5, 111.0]),
        'low': np.array([99.0, 100.5, 100.0, 101.5, 103.5, 102.5, 104.5, 106.0, 105.5, 108.0]),
        'close': np.array([100.0, 102.0, 101.0, 103.0, 105.0, 104.0, 106.0, 108.0, 107.0, 110.0])
    }


@pytest.fixture
def sample_dataframe(sample_ohlc):
    """Generate sample DataFrame with OHLC data."""
    dates = pd.date_range('2024-01-01', periods=10, freq='D')
    df = pd.DataFrame({
        'high': sample_ohlc['high'],
        'low': sample_ohlc['low'],
        'close': sample_ohlc['close'],
        'open': sample_ohlc['close'] - np.random.uniform(-1, 1, 10)
    }, index=dates)
    return df


@pytest.fixture
def kronos_root():
    """Return the Kronos project root directory."""
    return Path.home() / "kronos"


@pytest.fixture
def sample_gemma_output():
    """Sample Gemma4 model output for parser tests."""
    return """
    <thinking>
    Based on the technical analysis, the market shows bullish momentum.
    RSI is oversold at 35, which suggests a potential bounce.
    ADX is strong at 45, indicating a strong trend.
    </thinking>
    
    <output>
    BUY signal detected for BTC/USDT
    
    Confidence: 0.78
    Position: 5% of portfolio
    Risk: medium
    
    The price action suggests upward movement based on the oversold RSI
    and strong ADX confirmation. Buy entry recommended at current levels.
    </output>
    """


@pytest.fixture
def parser():
    """Create a GemmaOutputParser instance for testing."""
    from core.gemma4_parser import GemmaOutputParser
    return GemmaOutputParser()
