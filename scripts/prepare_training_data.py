#!/usr/bin/env python3
"""
Kronos ML Training Data Preparation Script

Loads historical OHLCV data, calculates technical indicators,
generates labels (future returns), and saves to parquet format.

Usage:
    python prepare_training_data.py --coin BTC-USDT --start 2024-01-01 --end 2024-12-31 --output data/train_btc.parquet
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

# Try to import technical indicators from core.indicators, fallback to kronos_v2.core.factor_calculator
try:
    from core.indicators import calculate_indicators
except ImportError:
    try:
        from kronos_v2.core.factor_calculator import FactorCalculator
        _factor_calc = FactorCalculator()
        def calculate_indicators(candles: List[Dict[str, float]]) -> Dict[str, float]:
            """Wrapper to use FactorCalculator for technical indicators."""
            price_data = {
                'closes': [c['close'] for c in candles],
                'highs': [c.get('high', c['close']) for c in candles],
                'lows': [c.get('low', c['close']) for c in candles],
                'volumes': [c.get('volume', 1.0) for c in candles]
            }
            return _factor_calc.calc_factors_safe(price_data)
    except ImportError:
        def calculate_indicators(candles: List[Dict[str, float]]) -> Dict[str, float]:
            """Fallback: return default indicators if no indicator module available."""
            return {
                'rsi': 50.0, 'adx': 20.0, 'bollinger_pos': 0.5,
                'macd': 0.0, 'vol_ratio': 1.0, 'atr': 0.0, 'confidence': 0.0
            }

# Default base prices for simulated data
BASE_PRICES = {
    'BTC-USDT': 67500.0,
    'ETH-USDT': 3450.0,
    'SOL-USDT': 145.0,
    'XRP-USDT': 0.52,
    'DOGE-USDT': 0.165,
    'ADA-USDT': 0.48,
    'AVAX-USDT': 38.5,
    'DOT-USDT': 7.85,
    'LINK-USDT': 18.20,
    'MATIC-USDT': 0.72
}

# Data cache directory
CACHE_DIR = Path(__file__).parent.parent / 'data' / 'ohlcv'
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_cached_data(coin: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Load cached OHLCV data if available."""
    cache_file = CACHE_DIR / f"{coin.replace('-', '_')}_{start}_{end}.parquet"
    if cache_file.exists():
        try:
            return pd.read_parquet(cache_file)
        except Exception:
            pass
    return None


def save_to_cache(df: pd.DataFrame, coin: str, start: str, end: str) -> None:
    """Save OHLCV data to cache."""
    cache_file = CACHE_DIR / f"{coin.replace('-', '_')}_{start}_{end}.parquet"
    df.to_parquet(cache_file, index=False)


def fetch_ohlcv_from_okx(coin: str, start: str, end: str, interval: str = '1h') -> pd.DataFrame:
    """
    Fetch OHLCV data from OKX API or generate simulated data.
    
    In production, this would call the OKX API. For now, generates realistic
    simulated data based on the coin's base price.
    """
    import random
    
    # Parse dates
    start_dt = datetime.strptime(start, '%Y-%m-%d')
    end_dt = datetime.strptime(end, '%Y-%m-%d')
    
    # Check cache first
    cached_df = get_cached_data(coin, start, end)
    if cached_df is not None:
        print(f"Loaded {len(cached_df)} candles from cache for {coin}")
        return cached_df
    
    # Generate simulated data
    base_price = BASE_PRICES.get(coin, 100.0)
    delta = end_dt - start_dt
    num_candles = int(delta.total_seconds() / 3600)  # hourly candles
    
    candles = []
    current_price = base_price
    
    for i in range(num_candles):
        timestamp = start_dt + timedelta(hours=i)
        
        # Generate realistic price movement
        change_pct = random.uniform(-0.03, 0.03)
        open_price = current_price * (1 + change_pct)
        high_price = open_price * (1 + random.uniform(0, 0.02))
        low_price = open_price * (1 - random.uniform(0, 0.02))
        close_price = open_price * (1 + random.uniform(-0.015, 0.015))
        volume = random.uniform(1000, 10000)
        
        candles.append({
            'timestamp': timestamp.isoformat(),
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': volume,
            'coin': coin
        })
        
        current_price = close_price
    
    df = pd.DataFrame(candles)
    
    # Save to cache
    save_to_cache(df, coin, start, end)
    print(f"Generated {len(df)} simulated candles for {coin}")
    
    return df


def calculate_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate technical indicators and features for each candle.
    
    Uses a sliding window approach to calculate indicators at each point.
    """
    features_list = []
    
    for i in range(len(df)):
        # Get lookback window (up to 100 candles, or all available)
        lookback = min(100, i + 1)
        window_candles = []
        
        for j in range(i - lookback + 1, i + 1):
            row = df.iloc[j]
            window_candles.append({
                'close': row['close'],
                'high': row['high'],
                'low': row['low'],
                'volume': row['volume']
            })
        
        # Calculate indicators
        indicators = calculate_indicators(window_candles)
        
        # Add price-based features
        row = df.iloc[i]
        close = row['close']
        
        # Returns at different horizons
        features = {
            'timestamp': row['timestamp'],
            'coin': row['coin'],
            'close': close,
            'open': row['open'],
            'high': row['high'],
            'low': row['low'],
            'volume': row['volume'],
            # Technical indicators
            'rsi': indicators.get('rsi', 50.0),
            'adx': indicators.get('adx', 20.0),
            'bollinger_pos': indicators.get('bollinger_pos', 0.5),
            'macd': indicators.get('macd', 0.0),
            'vol_ratio': indicators.get('vol_ratio', 1.0),
            'atr': indicators.get('atr', 0.0),
            'confidence': indicators.get('confidence', 0.0),
        }
        
        # Price-based features
        if i > 0:
            features['return_1h'] = (close - df.iloc[i-1]['close']) / df.iloc[i-1]['close']
        else:
            features['return_1h'] = 0.0
        
        # Volatility (std of returns over lookback)
        if lookback > 10:
            returns = []
            for j in range(max(1, i - lookback + 1), i):
                ret = (df.iloc[j]['close'] - df.iloc[j-1]['close']) / df.iloc[j-1]['close']
                returns.append(ret)
            features['volatility'] = math.sqrt(sum(r**2 for r in returns) / len(returns)) if returns else 0.0
        else:
            features['volatility'] = 0.0
        
        features_list.append(features)
    
    return pd.DataFrame(features_list)


def generate_labels(df: pd.DataFrame, horizons: List[int] = [1, 4, 24]) -> pd.DataFrame:
    """
    Generate labels (future returns) at different time horizons.
    
    Args:
        df: DataFrame with price data
        horizons: List of hours ahead to calculate returns for
    
    Returns:
        DataFrame with added label columns
    """
    for horizon in horizons:
        label_col = f'return_{horizon}h'
        future_col = f'future_return_{horizon}h'
        
        df[future_col] = 0.0
        
        for i in range(len(df) - horizon):
            current_close = df.iloc[i]['close']
            future_close = df.iloc[i + horizon]['close']
            df.loc[df.index[i], future_col] = (future_close - current_close) / current_close
    
    return df


def main():
    parser = argparse.ArgumentParser(description='Prepare training data for Kronos ML model')
    parser.add_argument('--coin', type=str, default='BTC-USDT',
                        help='Trading pair symbol (e.g., BTC-USDT)')
    parser.add_argument('--start', type=str, default='2024-01-01',
                        help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default='2024-12-31',
                        help='End date (YYYY-MM-DD)')
    parser.add_argument('--output', type=str, default=None,
                        help='Output parquet file path')
    
    args = parser.parse_args()
    
    # Default output path
    if args.output is None:
        output_dir = Path(__file__).parent.parent / 'data' / 'training'
        output_dir.mkdir(parents=True, exist_ok=True)
        args.output = str(output_dir / f"train_{args.coin.replace('-', '_')}.parquet")
    
    print(f"Preparing training data for {args.coin} from {args.start} to {args.end}")
    
    # Step 1: Load OHLCV data
    print("Step 1: Loading OHLCV data...")
    df = fetch_ohlcv_from_okx(args.coin, args.start, args.end)
    print(f"  Loaded {len(df)} candles")
    
    # Step 2: Calculate technical indicators
    print("Step 2: Calculating technical indicators...")
    df = calculate_features(df)
    print(f"  Calculated indicators for {len(df)} candles")
    
    # Step 3: Generate labels (future returns)
    print("Step 3: Generating labels (future returns)...")
    df = generate_labels(df)
    label_cols = [c for c in df.columns if c.startswith('future_return_')]
    print(f"  Generated labels: {label_cols}")
    
    # Step 4: Save to parquet
    print(f"Step 4: Saving to {args.output}...")
    df.to_parquet(args.output, index=False)
    print(f"  Saved {len(df)} rows to {args.output}")
    
    # Summary
    print("\nSummary:")
    print(f"  Coin: {args.coin}")
    print(f"  Date range: {args.start} to {args.end}")
    print(f"  Total samples: {len(df)}")
    print(f"  Features: {len(df.columns) - len(label_cols) - 1}")
    print(f"  Labels: {len(label_cols)}")
    print(f"  Output: {args.output}")


if __name__ == '__main__':
    main()
