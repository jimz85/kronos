#!/usr/bin/env python3
"""
multi_timeframe_backtest_v2.py
正确的多周期策略回测

逻辑：
- 日线UP + 1H突破20日高点 → 做多
- 日线DOWN + 1H跌破20日低点 → 做空
- ATR止损(2倍)
- 日线趋势反转出场
"""

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

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

def multi_tf_breakout(daily_p, daily_h, daily_l, h1_p, h1_h, h1_l, stop_atr=2.0):
    """
    正确的多周期突破策略
    
    入场：
    - 日线UP(收盘>MA20>MA50) AND 1H突破20日高点 → 做多
    - 日线DOWN(收盘<MA20<MA50) AND 1H跌破20日低点 → 做空
    
    出场：
    - ATR止损(2倍)
    - 日线趋势反转
    """
    
    # 计算日线趋势
    daily_ma20 = calc_ma(daily_p, 20)
    daily_ma50 = calc_ma(daily_p, 50)
    
    # 1H数据需要对齐到日线
    # 取每个日线对应位置的1H数据
    # 简化：用日线数据中的对应1H周期数据
    
    trades = []
    pos = None
    entry_price = 0
    entry_atr = 0
    
    min_len = min(len(daily_p), len(h1_p))
    
    for i in range(50, min_len - 1):
        # 日线趋势
        daily_price = daily_p[i]
        daily_ma20_val = float(daily_ma20.iloc[i])
        daily_ma50_val = float(daily_ma50.iloc[i])
        
        # 1H近20根的趋势判断
        h1_start = max(0, i * 24 - 24)  # 大致对应
        h1_end = i * 24 + 24
        
        if h1_end > len(h1_p):
            continue
        
        h1_recent = h1_p[h1_start:h1_end]
        h1_high_recent = h1_h[h1_start:h1_end]
        h1_low_recent = h1_l[h1_start:h1_end]
        
        if len(h1_recent) < 20:
            continue
        
        # 1H的20日高低点
        h1_high_20 = np.max(h1_recent[-20:])
        h1_low_20 = np.min(h1_recent[-20:])
        
        # 当前1H价格
        h1_price = h1_p[i * 24] if i * 24 < len(h1_p) else h1_p[-1]
        
        # 日线趋势
        is_daily_up = daily_price > daily_ma20_val > daily_ma50_val
        is_daily_down = daily_price < daily_ma20_val < daily_ma50_val
        
        # 1H的ATR
        h1_atr_start = max(0, i * 24 - 24)
        h1_atr_end = min(len(h1_h), i * 24 + 24)
        if h1_atr_end - h1_atr_start >= 14:
            h1_atr = calc_atr(
                h1_h[h1_atr_start:h1_atr_end],
                h1_l[h1_atr_start:h1_atr_end],
                h1_p[h1_atr_start:h1_atr_end]
            )
            current_atr = float(h1_atr.iloc[-1]) if not h1_atr.empty else h1_price * 0.01
        else:
            current_atr = h1_price * 0.01
        
        if pos is None:
            # 入场条件
            if is_daily_up and h1_price > h1_high_20:
                pos = "long"
                entry_price = h1_price
                entry_atr = current_atr
            elif is_daily_down and h1_price < h1_low_20:
                pos = "short"
                entry_price = h1_price
                entry_atr = current_atr
        
        else:
            # 出场
            stop_mult = stop_atr * entry_atr / entry_price
            
            if pos == "long":
                # 止损
                if h1_price <= entry_price * (1 - stop_mult):
                    trades.append((h1_price - entry_price) / entry_price)
                    pos = None
                # 日线趋势反转
                elif not is_daily_up:
                    trades.append((h1_price - entry_price) / entry_price)
                    pos = None
            
            else:  # short
                if h1_price >= entry_price * (1 + stop_mult):
                    trades.append((entry_price - h1_price) / entry_price)
                    pos = None
                elif not is_daily_down:
                    trades.append((entry_price - h1_price) / entry_price)
                    pos = None
    
    return trades

def simple_trend_follow(daily_p, daily_h, daily_l, stop_atr=2.0):
    """
    简单的日线趋势跟随
    """
    ma20 = calc_ma(daily_p, 20)
    ma50 = calc_ma(daily_p, 50)
    atr = calc_atr(daily_h, daily_l, daily_p)
    
    trades = []
    pos = None
    entry_price = 0
    entry_atr = 0
    
    for i in range(50, len(daily_p) - 1):
        price = daily_p[i]
        ma20_val = float(ma20.iloc[i])
        ma50_val = float(ma50.iloc[i])
        atr_val = float(atr.iloc[i])
        
        if pos is None:
            if price > ma20_val and ma20_val > ma50_val:
                pos = "long"
                entry_price = price
                entry_atr = atr_val
            elif price < ma20_val and ma20_val < ma50_val:
                pos = "short"
                entry_price = price
                entry_atr = atr_val
        else:
            stop_mult = stop_atr * entry_atr / entry_price
            
            if pos == "long":
                if price <= entry_price * (1 - stop_mult):
                    trades.append((price - entry_price) / entry_price)
                    pos = None
                elif price < ma20_val:
                    trades.append((price - entry_price) / entry_price)
                    pos = None
            
            else:
                if price >= entry_price * (1 + stop_mult):
                    trades.append((entry_price - price) / entry_price)
                    pos = None
                elif price > ma20_val:
                    trades.append((entry_price - price) / entry_price)
                    pos = None
    
    return trades

def calc_stats(trades, label):
    if not trades:
        print(f"{label}: 无交易")
        return None
    
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    wr = len(wins) / len(trades)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    rr = avg_win / avg_loss if avg_loss > 0 else 999
    pf = abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999
    
    equity = [1.0]
    for t in trades:
        equity.append(equity[-1] * (1 + t))
    equity = np.array(equity)
    total = equity[-1] - 1
    
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = abs(dd.min())
    
    print(f"{label}:")
    print(f"  交易: {len(trades)}笔")
    print(f"  胜率: {wr:.1%}")
    print(f"  盈亏比: {rr:.2f}")
    print(f"  PF: {pf:.2f}")
    print(f"  总收益: {total:+.1%}")
    print(f"  最大DD: {max_dd:.1%}")
    print()
    
    return {
        "label": label, "trades": len(trades), "wr": wr,
        "rr": rr, "pf": pf, "total": total, "max_dd": max_dd
    }

print("="*70)
print("  多周期突破策略 vs 日线趋势跟随 回测")
print("="*70)

# 获取数据
print("获取数据...")
t = yf.Ticker("BTC-USD")

df_1d = t.history(period="3y", interval="1d")
df_1h = t.history(period="1y", interval="1h")

print(f"日线: {len(df_1d)}根")
print(f"1H: {len(df_1h)}根")

daily_p = np.asarray(df_1d["Close"].values).flatten()
daily_h = np.asarray(df_1d["High"].values).flatten()
daily_l = np.asarray(df_1d["Low"].values).flatten()

h1_p = np.asarray(df_1h["Close"].values).flatten()
h1_h = np.asarray(df_1h["High"].values).flatten()
h1_l = np.asarray(df_1h["Low"].values).flatten()

print()

# 回测
print("-"*70)

# 多周期突破策略
print("多周期突破策略(需1H突破确认):")
trades1 = multi_tf_breakout(daily_p, daily_h, daily_l, h1_p, h1_h, h1_l, stop_atr=2.0)
s1 = calc_stats(trades1, "  结果")

# 日线趋势跟随(对照)
print("-"*70)
print("日线趋势跟随(直接入场):")
trades2 = simple_trend_follow(daily_p, daily_h, daily_l, stop_atr=2.0)
s2 = calc_stats(trades2, "  结果")

# 买入持有(基准)
print("-"*70)
buy_hold = (daily_p[-1] - daily_p[0]) / daily_p[0]
print(f"买入持有: {buy_hold:+.1%}")

# 对比
print()
print("="*70)
print("  对比总结")
print("="*70)
print()
print(f"{'策略':<25} | {'交易':>6} | {'胜率':>8} | {'盈亏比':>8} | {'PF':>8} | {'总收益':>10} | {'DD':>8}")
print("-"*85)

results = []

if s1:
    results.append(s1)
if s2:
    results.append(s2)

results.append({
    "label": "买入持有",
    "trades": 1,
    "wr": "-",
    "rr": "-",
    "pf": "-",
    "total": buy_hold,
    "max_dd": "-"
})

for r in results:
    wr_str = f"{r['wr']:.1%}" if isinstance(r['wr'], float) else r['wr']
    rr_str = f"{r['rr']:.2f}" if isinstance(r['rr'], float) else r['rr']
    pf_str = f"{r['pf']:.2f}" if isinstance(r['pf'], float) else r['pf']
    dd_str = f"{r['max_dd']:.1%}" if isinstance(r['max_dd'], float) else r['max_dd']
    
    marker = "✅" if isinstance(r['total'], float) and r['total'] > buy_hold * 0.8 else "🟡" if isinstance(r['total'], float) and r['total'] > 0 else "❌"
    
    print(f"{marker}{r['label']:<23} | {r['trades']:>6} | {wr_str:>8} | {rr_str:>8} | {pf_str:>8} | {r['total']:>+10.1%} | {dd_str:>8}")