
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

def run_strategy(p, rsi, cfg, leverage=10):
    trades = []
    pos = None
    
    for i in range(20, len(p) - cfg["hold"] - 1):
        rv = float(rsi.iloc[i])
        
        if pos is None:
            if rv < cfg["rsi_buy"]:
                pos = "long"; entry = p[i]
            elif rv > cfg["rsi_sell"]:
                pos = "short"; entry = p[i]
        else:
            if pos == "long":
                ret = (p[i] - entry) / entry * leverage
                if p[i] <= entry * (1 - cfg["stop"]) or rv > 50:
                    trades.append(ret); pos = None
                elif p[i] >= entry * (1 + cfg["target"]):
                    trades.append(ret); pos = None
            else:
                ret = (entry - p[i]) / entry * leverage
                if p[i] >= entry * (1 + cfg["stop"]) or rv < 50:
                    trades.append(ret); pos = None
                elif p[i] <= entry * (1 - cfg["target"]):
                    trades.append(ret); pos = None
    
    if not trades:
        return None
    
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    wr = len(wins) / len(trades)
    pf = abs(sum(wins)/sum(losses)) if losses else 999
    
    equity = 1.0
    for t in trades:
        equity *= (1 + t/100)
    total = equity - 1
    
    return {"trades": len(trades), "wr": wr, "pf": pf, "total": total}

# DOGE精细搜索
coin = "DOGE-USD"
t = yf.Ticker(coin)
df = t.history(period="180d", interval="1h")
p = np.asarray(df["Close"].values).flatten()
rsi = calc_rsi(p)
days = (df.index[-1] - df.index[0]).days

best = None

for rsi_buy in [20, 25, 30, 35, 40, 45]:
    for rsi_sell in [55, 60, 65, 70, 75]:
        for stop in [0.02, 0.025, 0.03, 0.035, 0.04]:
            for target in [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]:
                for hold in [12, 24, 36, 48]:
                    cfg = {"rsi_buy": rsi_buy, "rsi_sell": rsi_sell, "stop": stop, "target": target, "hold": hold}
                    result = run_strategy(p, rsi, cfg)
                    
                    if result and result["trades"] >= 20:
                        weekly = result["trades"] / days * 7
                        
                        if result["wr"] >= 0.60 and weekly >= 3 and result["pf"] >= 1.0:
                            if best is None or result["pf"] > best[6]:
                                best = [rsi_buy, rsi_sell, stop, target, hold, weekly, result["pf"], result["wr"], result["total"], result["trades"]]

if best:
    print(f"DOGE最优: RSI<{best[0]}/{best[1]} 止{best[2]:.1%} 目{best[3]:.1%} 持{best[4]}h")
    print(f"  周{best[5]:.1f}次 胜率{best[7]:.1%} PF={best[6]:.2f} 收益{best[8]:+.1%}")
else:
    print("DOGE无满足条件配置")
