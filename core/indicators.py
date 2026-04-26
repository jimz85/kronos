"""
Unified Technical Indicators Library for Kronos Project
"""

import numpy as np
import pandas as pd


def calc_rsi(prices, period=14):
    """
    Calculate Relative Strength Index (RSI)
    
    Args:
        prices: array-like, closing prices
        period: int, RSI period (default: 14)
    
    Returns:
        float: Latest RSI value
    """
    prices = np.asarray(prices).flatten()
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = pd.Series(gains).rolling(period).mean()
    avg_loss = pd.Series(losses).rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def calc_ma(prices, period):
    """
    Calculate Simple Moving Average (SMA)
    
    Args:
        prices: array-like, closing prices
        period: int, MA period
    
    Returns:
        float: Latest SMA value
    """
    return float(pd.Series(np.asarray(prices).flatten()).rolling(period).mean().iloc[-1])


def calc_ema(prices, period=20):
    """
    Calculate Exponential Moving Average (EMA)
    
    Args:
        prices: array-like, closing prices
        period: int, EMA period (default: 20)
    
    Returns:
        float: Latest EMA value
    """
    prices = np.asarray(prices)
    ema = np.zeros(len(prices))
    ema[period - 1] = np.mean(prices[:period])
    for i in range(period, len(prices)):
        ema[i] = (ema[i - 1] * (period - 1) + prices[i]) / period
    ema[:period - 1] = ema[period - 1]
    return float(ema[-1])


def calc_atr(high, low, close, period=14):
    """
    Calculate Average True Range (ATR)
    
    Args:
        high: array-like, high prices
        low: array-like, low prices
        close: array-like, closing prices
        period: int, ATR period (default: 14)
    
    Returns:
        float: Latest ATR value
    """
    high = np.asarray(high).flatten()
    low = np.asarray(low).flatten()
    close = np.asarray(close).flatten()
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return float(pd.Series(tr).rolling(period).mean().iloc[-1])


def calc_bollinger(prices, period=20, std_mult=2.0):
    """
    Calculate Bollinger Bands
    
    Args:
        prices: array-like, closing prices
        period: int, Bollinger period (default: 20)
        std_mult: float, standard deviation multiplier (default: 2.0)
    
    Returns:
        tuple: (middle_band, upper_band, lower_band) as floats
    """
    prices = np.asarray(prices).flatten()
    ma = pd.Series(prices).rolling(period).mean()
    std = pd.Series(prices).rolling(period).std()
    middle = float(ma.iloc[-1])
    upper = float((ma + std_mult * std).iloc[-1])
    lower = float((ma - std_mult * std).iloc[-1])
    return middle, upper, lower


def calc_macd(prices, fast=12, slow=26, signal=9):
    """
    Calculate MACD (Moving Average Convergence Divergence)
    
    Args:
        prices: array-like, closing prices
        fast: int, fast EMA period (default: 12)
        slow: int, slow EMA period (default: 26)
        signal: int, signal line period (default: 9)
    
    Returns:
        tuple: (macd_line, signal_line, histogram) as floats
    """
    prices = np.asarray(prices)
    # Calculate fast EMA
    ema_fast = np.zeros(len(prices))
    ema_fast[fast - 1] = np.mean(prices[:fast])
    for i in range(fast, len(prices)):
        ema_fast[i] = (ema_fast[i - 1] * (fast - 1) + prices[i]) / fast
    ema_fast[:fast - 1] = ema_fast[fast - 1]
    
    # Calculate slow EMA
    ema_slow = np.zeros(len(prices))
    ema_slow[slow - 1] = np.mean(prices[:slow])
    for i in range(slow, len(prices)):
        ema_slow[i] = (ema_slow[i - 1] * (slow - 1) + prices[i]) / slow
    ema_slow[:slow - 1] = ema_slow[slow - 1]
    
    macd = ema_fast - ema_slow
    # Signal line = EMA of MACD
    sig = np.zeros(len(macd))
    sig[signal - 1] = np.mean(macd[:signal])
    for i in range(signal, len(macd)):
        sig[i] = (sig[i - 1] * (signal - 1) + macd[i]) / signal
    hist = macd - sig
    return float(macd[-1]), float(sig[-1]), float(hist[-1])


def calc_adx(high, low, close, n=14):
    """
    Calculate Average Directional Index (ADX)
    
    Args:
        high: array-like, high prices
        low: array-like, low prices
        close: array-like, closing prices
        n: int, ADX period (default: 14)
    
    Returns:
        float: Latest ADX value
    """
    high = np.asarray(high)
    low = np.asarray(low)
    close = np.asarray(close)
    
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
    
    up = np.diff(high, prepend=high[0])
    dn = -np.diff(low, prepend=low[0])
    
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0))
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0))
    
    atr = tr.rolling(n).mean()
    pdi = 100 * (pdm.rolling(n).mean() / atr)
    mdi = 100 * (mdm.rolling(n).mean() / atr)
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)
    return float(dx.rolling(n).mean().iloc[-1])


def calc_cci(high, low, close, period=20):
    """
    Calculate Commodity Channel Index (CCI)
    
    Args:
        high: array-like, high prices
        low: array-like, low prices
        close: array-like, closing prices
        period: int, CCI period (default: 20)
    
    Returns:
        float: Latest CCI value
    """
    high = np.asarray(high).flatten()
    low = np.asarray(low).flatten()
    close = np.asarray(close).flatten()
    typical_price = (high + low + close) / 3
    sma = pd.Series(typical_price).rolling(period).mean()
    mean_deviation = pd.Series(typical_price).rolling(period).apply(
        lambda x: np.mean(np.abs(x - x.mean())), raw=True
    )
    return float(((typical_price - sma) / (0.015 * mean_deviation)).iloc[-1])


def calculate_indicators(candles):
    """
    Calculate all technical indicators from OHLCV candles.
    
    Args:
        candles: List of dicts with 'open', 'high', 'low', 'close', 'volume' keys
                 or dict with lists: {'open': [...], 'high': [...], ...}
    
    Returns:
        Dict of indicator values
    """
    import pandas as pd
    
    # Handle list of dicts format
    if isinstance(candles, list):
        df = pd.DataFrame(candles)
    else:
        df = pd.DataFrame(candles)
    
    close = df['close'].values
    high = df['high'].values if 'high' in df.columns else close
    low = df['low'].values if 'low' in df.columns else close
    open_prices = df['open'].values if 'open' in df.columns else close
    
    # Calculate all indicators
    return {
        'rsi': calc_rsi(close, 14),
        'rsi_4h': calc_rsi(close, 56),  # 4H RSI proxy
        'ma_20': calc_ma(close, 20),
        'ma_50': calc_ma(close, 50),
        'ma_200': calc_ma(close, 200),
        'ema_12': calc_ema(close, 12),
        'ema_26': calc_ema(close, 26),
        'atr': calc_atr(high, low, close, 14),
        'bollinger_upper': calc_bollinger(close, 20)[0],
        'bollinger_middle': calc_bollinger(close, 20)[1],
        'bollinger_lower': calc_bollinger(close, 20)[2],
        'macd': calc_macd(close)[0],
        'macd_signal': calc_macd(close)[1],
        'macd_histogram': calc_macd(close)[2],
        'adx': calc_adx(high, low, close, 14),
        'cci': calc_cci(high, low, close, 20),
    }
