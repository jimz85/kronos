"""
Unified Technical Indicators Library for Kronos Project

Type-annotated technical indicators for cryptocurrency trading.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd


# Type aliases for clarity
PriceArray = npt.NDArray[np.floating]
OHLCData = dict[str, list[float] | npt.NDArray[np.floating]]


def calc_rsi(
    prices: PriceArray | list[float],
    period: int = 14,
) -> float:
    """
    Calculate Relative Strength Index (RSI)

    Args:
        prices: array-like, closing prices
        period: int, RSI period (default: 14)

    Returns:
        float: Latest RSI value (0-100)
    """
    prices_arr = np.asarray(prices, dtype=np.float64).flatten()
    deltas = np.diff(prices_arr, prepend=prices_arr[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = pd.Series(gains).rolling(period).mean()
    avg_loss = pd.Series(losses).rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi.iloc[-1])


def calc_ma(
    prices: PriceArray | list[float],
    period: int,
) -> float:
    """
    Calculate Simple Moving Average (SMA)

    Args:
        prices: array-like, closing prices
        period: int, MA period

    Returns:
        float: Latest SMA value
    """
    prices_arr = np.asarray(prices, dtype=np.float64).flatten()
    return float(pd.Series(prices_arr).rolling(period).mean().iloc[-1])


def calc_ema(
    prices: PriceArray | list[float],
    period: int = 20,
) -> np.ndarray:
    """
    Calculate Exponential Moving Average (EMA)

    Args:
        prices: array-like, closing prices
        period: int, EMA period (default: 20)

    Returns:
        np.ndarray: EMA values
    """
    prices_arr = np.asarray(prices, dtype=np.float64)
    ema = np.zeros(len(prices_arr), dtype=np.float64)
    ema[period - 1] = np.mean(prices_arr[:period])
    for i in range(period, len(prices_arr)):
        ema[i] = (ema[i - 1] * (period - 1) + prices_arr[i]) / period
    ema[: period - 1] = ema[period - 1]
    return ema


def calc_atr(
    high: PriceArray | list[float],
    low: PriceArray | list[float],
    close: PriceArray | list[float],
    period: int = 14,
) -> pd.Series:
    """
    Calculate Average True Range (ATR)

    Args:
        high: array-like, high prices
        low: array-like, low prices
        close: array-like, closing prices
        period: int, ATR period (default: 14)

    Returns:
        pd.Series: ATR values
    """
    high_arr = np.asarray(high, dtype=np.float64).flatten()
    low_arr = np.asarray(low, dtype=np.float64).flatten()
    close_arr = np.asarray(close, dtype=np.float64).flatten()
    prev_close = np.roll(close_arr, 1)
    prev_close[0] = close_arr[0]
    tr = np.maximum(
        high_arr - low_arr,
        np.maximum(np.abs(high_arr - prev_close), np.abs(low_arr - prev_close)),
    )
    return pd.Series(tr).rolling(period).mean()


def calc_bollinger(
    prices: PriceArray | list[float],
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculate Bollinger Bands

    Args:
        prices: array-like, closing prices
        period: int, Bollinger period (default: 20)
        std_mult: float, standard deviation multiplier (default: 2.0)

    Returns:
        tuple: (middle_band, upper_band, lower_band) as pd.Series
    """
    prices_arr = np.asarray(prices, dtype=np.float64).flatten()
    ma = pd.Series(prices_arr).rolling(period).mean()
    std = pd.Series(prices_arr).rolling(period).std()
    middle = ma
    upper = ma + std_mult * std
    lower = ma - std_mult * std
    return middle, upper, lower


def calc_macd(
    prices: PriceArray | list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Calculate MACD (Moving Average Convergence Divergence)

    Args:
        prices: array-like, closing prices
        fast: int, fast EMA period (default: 12)
        slow: int, slow EMA period (default: 26)
        signal: int, signal line period (default: 9)

    Returns:
        tuple: (macd_line, signal_line, histogram) as np.ndarray
    """
    prices_arr = np.asarray(prices, dtype=np.float64)
    # Calculate fast EMA
    ema_fast = np.zeros(len(prices_arr), dtype=np.float64)
    ema_fast[fast - 1] = np.mean(prices_arr[:fast])
    for i in range(fast, len(prices_arr)):
        ema_fast[i] = (ema_fast[i - 1] * (fast - 1) + prices_arr[i]) / fast
    ema_fast[: fast - 1] = ema_fast[fast - 1]

    # Calculate slow EMA
    ema_slow = np.zeros(len(prices_arr), dtype=np.float64)
    ema_slow[slow - 1] = np.mean(prices_arr[:slow])
    for i in range(slow, len(prices_arr)):
        ema_slow[i] = (ema_slow[i - 1] * (slow - 1) + prices_arr[i]) / slow
    ema_slow[: slow - 1] = ema_slow[slow - 1]

    macd = ema_fast - ema_slow
    # Signal line = EMA of MACD
    sig = np.zeros(len(macd), dtype=np.float64)
    sig[signal - 1] = np.mean(macd[:signal])
    for i in range(signal, len(macd)):
        sig[i] = (sig[i - 1] * (signal - 1) + macd[i]) / signal
    hist = macd - sig
    return macd, sig, hist


def calc_adx(
    high: PriceArray | list[float],
    low: PriceArray | list[float],
    close: PriceArray | list[float],
    n: int = 14,
) -> pd.Series:
    """
    Calculate Average Directional Index (ADX)

    Args:
        high: array-like, high prices
        low: array-like, low prices
        close: array-like, closing prices
        n: int, ADX period (default: 14)

    Returns:
        pd.Series: ADX values (0-100)
    """
    high_arr = np.asarray(high, dtype=np.float64)
    low_arr = np.asarray(low, dtype=np.float64)
    close_arr = np.asarray(close, dtype=np.float64)

    tr1 = high_arr - low_arr
    tr2 = np.abs(high_arr - np.roll(close_arr, 1))
    tr3 = np.abs(low_arr - np.roll(close_arr, 1))
    tr = pd.DataFrame({"tr1": tr1, "tr2": tr2, "tr3": tr3}).max(axis=1)

    up = np.diff(high_arr, prepend=high_arr[0])
    dn = -np.diff(low_arr, prepend=low_arr[0])

    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0))
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0))

    atr = tr.rolling(n).mean()
    pdi = 100.0 * (pdm.rolling(n).mean() / atr)
    mdi = 100.0 * (mdm.rolling(n).mean() / atr)
    dx = 100.0 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)
    return dx.rolling(n).mean()


def calc_cci(
    high: PriceArray | list[float],
    low: PriceArray | list[float],
    close: PriceArray | list[float],
    period: int = 20,
) -> pd.Series:
    """
    Calculate Commodity Channel Index (CCI)

    Args:
        high: array-like, high prices
        low: array-like, low prices
        close: array-like, closing prices
        period: int, CCI period (default: 20)

    Returns:
        pd.Series: CCI values (typically -100 to +100, can exceed)
    """
    high_arr = np.asarray(high, dtype=np.float64).flatten()
    low_arr = np.asarray(low, dtype=np.float64).flatten()
    close_arr = np.asarray(close, dtype=np.float64).flatten()
    typical_price = (high_arr + low_arr + close_arr) / 3.0
    sma = pd.Series(typical_price).rolling(period).mean()
    mean_deviation = pd.Series(typical_price).rolling(period).apply(
        lambda x: np.mean(np.abs(x - x.mean())), raw=True
    )
    return (typical_price - sma) / (0.015 * mean_deviation)


def calculate_indicators(candles: list[dict] | dict) -> dict[str, float]:
    """
    Calculate all technical indicators from OHLCV candles.

    Args:
        candles: List of dicts with 'open', 'high', 'low', 'close', 'volume' keys
                 or dict with lists: {'open': [...], 'high': [...], ...}

    Returns:
        Dict of indicator values as floats
    """
    import pandas as pd

    # Handle list of dicts format
    if isinstance(candles, list):
        df = pd.DataFrame(candles)
    else:
        df = pd.DataFrame(candles)

    close = df["close"].values
    high = df["high"].values if "high" in df.columns else close
    low = df["low"].values if "low" in df.columns else close

    # Calculate all indicators
    return {
        "rsi": calc_rsi(close, 14),
        "rsi_4h": calc_rsi(close, 56),  # 4H RSI proxy
        "ma_20": calc_ma(close, 20),
        "ma_50": calc_ma(close, 50),
        "ma_200": calc_ma(close, 200),
        "ema_12": float(calc_ema(close, 12)[-1]),
        "ema_26": float(calc_ema(close, 26)[-1]),
        "atr": float(calc_atr(high, low, close, 14).iloc[-1]),
        "bollinger_upper": float(calc_bollinger(close, 20)[1].iloc[-1]),
        "bollinger_middle": float(calc_bollinger(close, 20)[0].iloc[-1]),
        "bollinger_lower": float(calc_bollinger(close, 20)[2].iloc[-1]),
        "macd": float(calc_macd(close)[0][-1]),
        "macd_signal": float(calc_macd(close)[1][-1]),
        "macd_histogram": float(calc_macd(close)[2][-1]),
        "adx": float(calc_adx(high, low, close, 14).iloc[-1]),
        "cci": float(calc_cci(high, low, close, 20).iloc[-1]),
    }
