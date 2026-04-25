
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

print("="*70)
print("  10x杠杆策略设计原理")
print("="*70)
print("""
10x杠杆意味着：
- 价格波动1% = 账户波动10%
- 止损3% = 亏损30%!

要盈利需要的数学：
- 胜率60% + RR>0.67 → 期望值为正
- 胜率70% + RR>0.43 → 期望值为正

所以10x杠杆下：
- 止损3%太大会亏30%
- 目标3%只赚30%
- 盈亏比需要>止损/目标才能盈利
""")

# 验证BTC日线的实际RSI表现
coin = "BTC-USD"
t = yf.Ticker(coin)
df = t.history(period="5y", interval="1d")
p = np.asarray(df["Close"].values).flatten()
rsi = calc_rsi(p)
days = (df.index[-1] - df.index[0]).days

print(f"BTC {days}天日线数据 RSI分析:")
print()

# 测试不同止损止盈
for stop in [0.02, 0.03, 0.05, 0.07, 0.10]:
    for target in [0.02, 0.03, 0.05, 0.07, 0.10]:
        for hold in [3, 5, 7, 10, 14]:
            cfg = {"rsi_buy": 30, "rsi_sell": 70, "stop": stop, "target": target, "hold": hold}
            result = run_strategy(p, rsi, cfg, leverage=10)
            
            if result and result["trades"] >= 30:
                weekly = result["trades"] / days * 7
                
                if result["wr"] >= 0.60 and weekly >= 0.5 and result["total"] > 0 and result["pf"] > 1.0:
                    print(f"止{stop:.0%} 目{target:.0%} 持{hold}天: {result['trades']}笔 周{weekly:.1f}次 胜率{result['wr']:.1%} PF={result['pf']:.2f} 收益{result['total']:+.1%}")
