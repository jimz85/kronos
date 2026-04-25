
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

def calc_ma(p, n): return pd.Series(np.asarray(p).flatten()).rolling(n).mean()

def test_rsi_strategy(p, rsi_buy, hold_days, stop_pct, target_pct):
    rsi = calc_rsi(p)
    trades = []
    pos = None
    
    for i in range(20, len(p) - hold_days - 1):
        if pos is None:
            if rsi.iloc[i] < rsi_buy:
                pos = i
        else:
            if i - pos >= hold_days:
                trades.append((p[i] - p[pos]) / p[pos])
                pos = None
            elif p[i] <= p[pos] * (1 - stop_pct):
                trades.append((p[i] - p[pos]) / p[pos])
                pos = None
            elif p[i] >= p[pos] * (1 + target_pct):
                trades.append((p[i] - p[pos]) / p[pos])
                pos = None
    
    return trades

def test_ma_trend(p, h, l, ma_fast, ma_slow, stop_atr_mult):
    ma_f = calc_ma(p, ma_fast)
    ma_s = calc_ma(p, ma_slow)
    
    tr = np.maximum(h - l, np.abs(h - np.roll(p, 1)))
    tr[0] = 0
    atr = pd.Series(tr).rolling(14).mean()
    
    trades = []
    pos = None
    
    for i in range(ma_slow, len(p) - 1):
        if pos is None:
            if p[i] > float(ma_f.iloc[i]) > float(ma_s.iloc[i]):
                pos = i
            elif p[i] < float(ma_f.iloc[i]) < float(ma_s.iloc[i]):
                pos = i
        else:
            atr_val = float(atr.iloc[pos]) if not np.isnan(float(atr.iloc[pos])) else p[pos] * 0.02
            if pos_type == "long" and p[i] <= p[pos] - stop_atr_mult * atr_val:
                trades.append((p[i] - p[pos]) / p[pos])
                pos = None
            elif pos_type == "short" and p[i] >= p[pos] + stop_atr_mult * atr_val:
                trades.append((p[pos] - p[i]) / p[pos])
                pos = None
            elif pos_type == "long" and p[i] < float(ma_f.iloc[i]):
                trades.append((p[i] - p[pos]) / p[pos])
                pos = None
            elif pos_type == "short" and p[i] > float(ma_f.iloc[i]):
                trades.append((p[pos] - p[i]) / p[pos])
                pos = None
    
    return trades

print("="*70)
print("  样本外验证：2018-2020年数据测试")
print("="*70)

# ETH 2018-2020
try:
    df = yf.download("ETH-USD", start="2018-01-01", end="2021-01-01", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df = df.loc[:, df.columns.get_level_values(0)]
    p = np.asarray(df["Close"].values).flatten()
    h = np.asarray(df["High"].values).flatten()
    l = np.asarray(df["Low"].values).flatten()
    
    buy_hold = (p[-1] - p[0]) / p[0]
    
    print(f"ETH 2018-2020: 持有收益={buy_hold:+.1%}")
    
    # 测试最优参数
    trades = test_rsi_strategy(p, 30, 14, 0.05, 0.10)
    if trades:
        wins = [t for t in trades if t>0]; losses = [t for t in trades if t<0]
        wr = len(wins)/len(trades)
        pf = abs(sum(wins)/sum(losses)) if losses else 999
        equity = [1.0]
        for t in trades: equity.append(equity[-1]*(1+t))
        total = equity[-1]-1
        alpha = total - buy_hold
        print(f"  RSI<30持14天 止5% 目10%: {len(trades)}笔 胜{wr:.0%} PF={pf:.2f} 总{total:+.1%} Alpha={alpha:+.1%}")
    
    # 更严格的测试
    trades2 = test_rsi_strategy(p, 30, 10, 0.05, 0.10)
    if trades2:
        wins = [t for t in trades2 if t>0]; losses = [t for t in trades2 if t<0]
        wr = len(wins)/len(trades2)
        pf = abs(sum(wins)/sum(losses)) if losses else 999
        equity = [1.0]
        for t in trades2: equity.append(equity[-1]*(1+t))
        total = equity[-1]-1
        alpha = total - buy_hold
        print(f"  RSI<30持10天 止5% 目10%: {len(trades2)}笔 胜{wr:.0%} PF={pf:.2f} 总{total:+.1%} Alpha={alpha:+.1%}")
        
except Exception as e:
    print(f"ETH错误: {e}")

print()

# SOL 2018-2020
try:
    df = yf.download("SOL-USD", start="2018-01-01", end="2021-01-01", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df = df.loc[:, df.columns.get_level_values(0)]
    p = np.asarray(df["Close"].values).flatten()
    
    buy_hold = (p[-1] - p[0]) / p[0]
    
    print(f"SOL 2018-2020: 持有收益={buy_hold:+.1%}")
    
    trades = test_rsi_strategy(p, 30, 7, 0.05, 0.10)
    if trades:
        wins = [t for t in trades if t>0]; losses = [t for t in trades if t<0]
        wr = len(wins)/len(trades)
        pf = abs(sum(wins)/sum(losses)) if losses else 999
        equity = [1.0]
        for t in trades: equity.append(equity[-1]*(1+t))
        total = equity[-1]-1
        alpha = total - buy_hold
        print(f"  RSI<30持7天 止5% 目10%: {len(trades)}笔 胜{wr:.0%} PF={pf:.2f} 总{total:+.1%} Alpha={alpha:+.1%}")
    
except Exception as e:
    print(f"SOL错误: {e}")

print()

# BTC 2018-2020
try:
    df = yf.download("BTC-USD", start="2018-01-01", end="2021-01-01", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df = df.loc[:, df.columns.get_level_values(0)]
    p = np.asarray(df["Close"].values).flatten()
    h = np.asarray(df["High"].values).flatten()
    l = np.asarray(df["Low"].values).flatten()
    
    buy_hold = (p[-1] - p[0]) / p[0]
    
    print(f"BTC 2018-2020: 持有收益={buy_hold:+.1%}")
    
    # BTC用趋势跟随
    ma_f = calc_ma(p, 50)
    ma_s = calc_ma(p, 100)
    
    trades = []
    pos = None
    tr = np.maximum(h - l, np.abs(h - np.roll(p, 1)))
    tr[0] = 0
    atr = pd.Series(tr).rolling(14).mean()
    
    for i in range(100, len(p) - 1):
        if pos is None:
            if p[i] > float(ma_f.iloc[i]) > float(ma_s.iloc[i]):
                pos = "long"; entry = p[i]
            elif p[i] < float(ma_f.iloc[i]) < float(ma_s.iloc[i]):
                pos = "short"; entry = p[i]
        else:
            atr_val = float(atr.iloc[pos]) if not np.isnan(float(atr.iloc[pos])) else entry * 0.02
            if pos == "long":
                if p[i] <= entry - 2.0 * atr_val:
                    trades.append((p[i] - entry) / entry); pos = None
                elif p[i] < float(ma_f.iloc[i]):
                    trades.append((p[i] - entry) / entry); pos = None
            else:
                if p[i] >= entry + 2.0 * atr_val:
                    trades.append((entry - p[i]) / entry); pos = None
                elif p[i] > float(ma_f.iloc[i]):
                    trades.append((entry - p[i]) / entry); pos = None
    
    if trades:
        wins = [t for t in trades if t>0]; losses = [t for t in trades if t<0]
        wr = len(wins)/len(trades)
        pf = abs(sum(wins)/sum(losses)) if losses else 999
        equity = [1.0]
        for t in trades: equity.append(equity[-1]*(1+t))
        total = equity[-1]-1
        alpha = total - buy_hold
        print(f"  MA趋势 50/100 ATR2.0: {len(trades)}笔 胜{wr:.0%} PF={pf:.2f} 总{total:+.1%} Alpha={alpha:+.1%}")
    
except Exception as e:
    print(f"BTC错误: {e}")

print()
print("★ 2018-2020是熊市，这是真正的样本外测试")
print("★ 如果策略在熊市也能跑赢，说明策略真的有效")
