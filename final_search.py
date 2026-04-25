
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
best_all = {}

for coin in coins:
    t = yf.Ticker(coin)
    df = t.history(period="60d", interval="15m")
    p = np.asarray(df["Close"].values).flatten()
    rsi = calc_rsi(p)
    days = (df.index[-1] - df.index[0]).days
    
    best = None
    
    for rsi_buy in [25, 30, 35]:
        for rsi_sell in [65, 70, 75]:
            for stop in [0.03, 0.04, 0.05]:
                for target in [0.03, 0.04, 0.05, 0.06]:
                    for hold in [24, 48, 72]:
                        trades = []
                        pos = None
                        
                        for i in range(20, len(p) - hold - 1):
                            rv = float(rsi.iloc[i])
                            
                            if pos is None:
                                if rv < rsi_buy: pos = "long"; entry = p[i]
                                elif rv > rsi_sell: pos = "short"; entry = p[i]
                            else:
                                if pos == "long":
                                    ret = (p[i] - entry) / entry * 10
                                    if p[i] <= entry * (1 - stop) or rv > 50:
                                        trades.append(ret); pos = None
                                    elif p[i] >= entry * (1 + target):
                                        trades.append(ret); pos = None
                                else:
                                    ret = (entry - p[i]) / entry * 10
                                    if p[i] >= entry * (1 + stop) or rv < 50:
                                        trades.append(ret); pos = None
                                    elif p[i] <= entry * (1 - target):
                                        trades.append(ret); pos = None
                        
                        if len(trades) < 30: continue
                        
                        wins = [t for t in trades if t > 0]
                        losses = [t for t in trades if t < 0]
                        wr = len(wins) / len(trades)
                        if wr < 0.60: continue
                        
                        pf = abs(sum(wins)/sum(losses)) if losses else 999
                        equity = 1.0
                        for t in trades: equity *= (1 + t/100)
                        total = equity - 1
                        weekly = len(trades) / days * 7
                        
                        if weekly >= 3 and total > 0 and pf > 1.0:
                            if best is None or pf > best[7]:
                                best = [coin, rsi_buy, rsi_sell, stop, target, hold, weekly, pf, wr, total]
    
    best_all[coin] = best

print("="*70)
print("  汇总")
print("="*70)
for coin, b in best_all.items():
    if b:
        print(f"{coin}: RSI<{b[1]}/{b[2]} 止{b[3]:.0%} 目{b[4]:.0%} 持{b[5]}根 | 周{b[6]:.1f}次 胜率{b[8]:.1%} PF={b[7]:.2f} 收益{b[9]:+.1%}")
    else:
        print(f"{coin}: 无满足条件")
