
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

print("="*70)
print("  BTC 精细化参数搜索")
print("="*70)

t = yf.Ticker("BTC-USD")
df = t.history(period="90d", interval="15m")

p = np.asarray(df["Close"].values).flatten()
rsi = calc_rsi(p)
days = (df.index[-1] - df.index[0]).days

best = None

for rsi_buy in [25, 30, 35]:
    for stop_pct in [0.01, 0.015, 0.02, 0.025]:
        for target_pct in [0.015, 0.02, 0.025, 0.03, 0.04]:
            for hold_bars in [8, 12, 16, 24]:
                trades = []
                pos = None
                
                for i in range(20, len(p) - hold_bars - 1):
                    rsi_val = float(rsi.iloc[i])
                    
                    if pos is None:
                        if rsi_val < rsi_buy:
                            pos = "long"
                            entry_price = p[i]
                            entry_i = i
                    else:
                        exit_price = p[i]
                        
                        if exit_price <= entry_price * (1 - stop_pct):
                            trades.append((exit_price - entry_price) / entry_price * 10)
                            pos = None
                        elif exit_price >= entry_price * (1 + target_pct):
                            trades.append((exit_price - entry_price) / entry_price * 10)
                            pos = None
                        elif i - entry_i >= hold_bars:
                            trades.append((exit_price - entry_price) / entry_price * 10)
                            pos = None
                
                if len(trades) < 20:
                    continue
                
                wins = [t for t in trades if t > 0]
                losses = [t for t in trades if t < 0]
                wr = len(wins) / len(trades)
                
                if wr < 0.60:
                    continue
                
                avg_win = sum(wins)/len(wins) if wins else 0
                avg_loss = abs(sum(losses)/len(losses)) if losses else 0
                rr = avg_win / avg_loss if avg_loss > 0 else 999
                pf = abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999
                
                equity = [1.0]
                for t in trades: equity.append(equity[-1]*(1+t/100))
                total = equity[-1] - 1
                
                weekly = len(trades) / days * 7
                
                if weekly >= 3 and total > 0 and pf > 1.0:
                    if best is None or pf > best["pf"]:
                        best = {
                            "rsi_buy": rsi_buy,
                            "stop_pct": stop_pct,
                            "target_pct": target_pct,
                            "hold_bars": hold_bars,
                            "trades": len(trades),
                            "weekly": weekly,
                            "wr": wr,
                            "rr": rr,
                            "pf": pf,
                            "total": total
                        }

if best:
    print(f"BTC最优:")
    print(f"  RSI<{best['rsi_buy']} 止{best['stop_pct']:.1%} 目{best['target_pct']:.1%} 持{best['hold_bars']}根")
    print(f"  周{best['weekly']:.1f}次 胜率{best['wr']:.1%} RR={best['rr']:.2f} PF={best['pf']:.2f} 收益{best['total']:+.1%}")
else:
    print("无满足条件配置")
