
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

def calc_rsi(p, n=14):
    d = np.diff(p, prepend=p[0])
    g = np.where(d>0, d, 0); l = np.where(d<0, -d, 0)
    ag = pd.Series(g).rolling(n).mean(); al = pd.Series(l).rolling(n).mean()
    return 100 - (100/(1 + ag/(al+1e-10)))

coins = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "DOGE-USD"]

for coin in coins:
    try:
        t = yf.Ticker(coin)
        df = t.history(period="60d", interval="15m")
        print(f"{coin}: {len(df)} rows, {df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')}")
    except Exception as e:
        print(f"{coin}: Error - {e}")
