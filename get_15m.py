
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

def calc_rsi(prices, period=14):
    prices = np.asarray(prices).flatten()
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = pd.Series(gains).rolling(period).mean()
    avg_loss = pd.Series(losses).rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_ma(prices, period):
    return pd.Series(np.asarray(prices).flatten()).rolling(period).mean()

def calc_atr(high, low, close, period=14):
    high = np.asarray(high).flatten()
    low = np.asarray(low).flatten()
    close = np.asarray(close).flatten()
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(period).mean()

print("="*70)
print("  获取BTC 15分钟K线数据")
print("="*70)

# yfinance 15min data
t = yf.Ticker("BTC-USD")

# Get 60 days of 15min data
df = t.history(period="60d", interval="15m")
print(f"数据量: {len(df)} 根K线")
print(f"时间范围: {df.index[0]} 到 {df.index[-1]}")
print(f"覆盖天数: {(df.index[-1] - df.index[0]).days} 天")

if len(df) < 100:
    print("数据不足，尝试其他方法...")
    # Try different approach
    df = yf.download("BTC-USD", period="60d", interval="15m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df = df.loc[:, df.columns.get_level_values(0)]
    print(f"数据量: {len(df)} 根K线")

print()
print(df.tail())
