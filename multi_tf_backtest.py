#!/usr/bin/env python3
"""
multi_timeframe_backtest.py
多周期交易系统回测

验证：多周期确认 vs 单周期交易 谁的收益更好？
"""
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

def calc_bollinger(prices, period=20, std_mult=2.0):
    prices = np.asarray(prices).flatten()
    ma = pd.Series(prices).rolling(period).mean()
    std = pd.Series(prices).rolling(period).std()
    return ma, ma + std_mult * std, ma - std_mult * std

def get_tf_analysis(p, h, l, lookback):
    """分析单周期状态"""
    ma20 = calc_ma(p, 20)
    ma50 = calc_ma(p, 50)
    rsi = calc_rsi(p)
    atr = calc_atr(h, l, p)
    ma, bb_up, bb_low = calc_bollinger(p)
    
    i = -1  # 最新K线
    
    price = float(p[i])
    rsi_val = float(rsi.iloc[i])
    ma20_val = float(ma20.iloc[i])
    ma50_val = float(ma50.iloc[i]) if len(ma50) >= 50 else ma20_val
    bb_up_val = float(bb_up.iloc[i])
    bb_low_val = float(bb_low.iloc[i])
    atr_val = float(atr.iloc[i])
    
    # 趋势
    if price > ma20_val and ma20_val > ma50_val:
        trend = 1  # UP
    elif price < ma20_val and ma20_val < ma50_val:
        trend = -1  # DOWN
    else:
        trend = 0  # RANGE
    
    # 得分
    score = 0
    if trend == 1:
        score += 20
    elif trend == -1:
        score -= 20
    else:
        score += 0
    
    if rsi_val < 30:
        score += 15
    elif rsi_val < 40:
        score += 5
    elif rsi_val > 70:
        score -= 15
    elif rsi_val > 60:
        score -= 5
    
    bb_pos = (price - bb_low_val) / (bb_up_val - bb_low_val + 1e-10)
    if bb_pos < 0.2:
        score += 10
    elif bb_pos > 0.8:
        score -= 10
    
    return {
        "trend": trend,
        "score": max(-100, min(100, score)),
        "rsi": rsi_val,
        "bb_position": bb_pos,
        "atr_pct": atr_val / price
    }

def multi_tf_strategy(daily_data, h4_data, h1_data, m15_data):
    """
    多周期策略回测
    
    规则：
    1. 大周期(日线)确认方向
    2. 小周期(1H)等待回调入场
    3. ATR止损
    
    入场条件：
    - 日线趋势 UP + 小周期RSI<40 = 做多
    - 日线趋势 DOWN + 小周期RSI>60 = 做空
    
    出场：
    - ATR止损(2倍)
    - 日线趋势反转
    - 持满N根K线
    """
    trades = []
    pos = None
    entry_price = 0
    entry_i = 0
    stop_price = 0
    
    # 需要4个周期都对齐的长度
    min_len = min(len(daily_data), len(h4_data), len(h1_data), len(m15_data))
    
    for i in range(50, min_len - 1):
        # 获取各周期数据
        d = get_tf_analysis(
            daily_data["Close"].values[:i+1],
            daily_data["High"].values[:i+1],
            daily_data["Low"].values[:i+1],
            20
        )
        
        h4 = get_tf_analysis(
            h4_data["Close"].values[:i+1],
            h4_data["High"].values[:i+1],
            h4_data["Low"].values[:i+1],
            20
        )
        
        h1 = get_tf_analysis(
            h1_data["Close"].values[:i+1],
            h1_data["High"].values[:i+1],
            h1_data["Low"].values[:i+1],
            20
        )
        
        m15 = get_tf_analysis(
            m15_data["Close"].values[:i+1],
            m15_data["High"].values[:i+1],
            m15_data["Low"].values[:i+1],
            20
        )
        
        # 当前价格(1H周期的最新价格)
        curr_price = float(h1_data["Close"].values[i])
        
        if pos is None:
            # 入场条件：大周期趋势确认 + 小周期回调
            
            # 做多：日线UP + 1H RSI回调到40以下
            if d["trend"] == 1 and h1["rsi"] < 40:
                pos = "long"
                entry_price = curr_price
                entry_i = i
                stop_price = curr_price * (1 - d["atr_pct"] * 2)
            
            # 做空：日线DOWN + 1H RSI反弹到60以上
            elif d["trend"] == -1 and h1["rsi"] > 60:
                pos = "short"
                entry_price = curr_price
                entry_i = i
                stop_price = curr_price * (1 + d["atr_pct"] * 2)
        
        else:
            # 出场条件
            if pos == "long":
                # 止损
                if curr_price <= stop_price:
                    trades.append((curr_price - entry_price) / entry_price)
                    pos = None
                # 日线趋势反转
                elif d["trend"] == -1:
                    trades.append((curr_price - entry_price) / entry_price)
                    pos = None
                # RSI超买止盈
                elif h1["rsi"] > 70:
                    trades.append((curr_price - entry_price) / entry_price)
                    pos = None
            else:  # short
                if curr_price >= stop_price:
                    trades.append((entry_price - curr_price) / entry_price)
                    pos = None
                elif d["trend"] == 1:
                    trades.append((entry_price - curr_price) / entry_price)
                    pos = None
                elif h1["rsi"] < 30:
                    trades.append((entry_price - curr_price) / entry_price)
                    pos = None
    
    return trades

def simple_rsi_strategy(h1_data, rsi_buy=35, rsi_sell=65, stop_pct=0.03):
    """单周期RSI策略(对照)"""
    p = np.asarray(h1_data["Close"].values).flatten()
    h = np.asarray(h1_data["High"].values).flatten()
    l = np.asarray(h1_data["Low"].values).flatten()
    
    rsi = calc_rsi(p)
    atr = calc_atr(h, l, p)
    
    trades = []
    pos = None
    entry_price = 0
    
    for i in range(20, len(p) - 1):
        rsi_val = float(rsi.iloc[i])
        curr_price = p[i]
        atr_val = float(atr.iloc[i])
        
        if pos is None:
            if rsi_val < rsi_buy:
                pos = "long"
                entry_price = curr_price
        else:
            stop = entry_price * (1 - stop_pct)
            if curr_price <= stop:
                trades.append((curr_price - entry_price) / entry_price)
                pos = None
            elif rsi_val > rsi_sell:
                trades.append((curr_price - entry_price) / entry_price)
                pos = None
    
    return trades

def trend_follow_strategy(daily_data, stop_atr=2.0):
    """日线趋势跟随策略(对照)"""
    p = np.asarray(daily_data["Close"].values).flatten()
    h = np.asarray(daily_data["High"].values).flatten()
    l = np.asarray(daily_data["Low"].values).flatten()
    
    ma20 = calc_ma(p, 20)
    ma50 = calc_ma(p, 50)
    atr = calc_atr(h, l, p)
    
    trades = []
    pos = None
    entry_price = 0
    atr_at_entry = 0
    
    for i in range(50, len(p) - 1):
        ma20_val = float(ma20.iloc[i])
        ma50_val = float(ma50.iloc[i])
        curr_price = p[i]
        atr_val = float(atr.iloc[i])
        
        if pos is None:
            if curr_price > ma20_val and ma20_val > ma50_val:
                pos = "long"
                entry_price = curr_price
                atr_at_entry = atr_val
            elif curr_price < ma20_val and ma20_val < ma50_val:
                pos = "short"
                entry_price = curr_price
                atr_at_entry = atr_val
        else:
            stop = entry_price - stop_atr * atr_at_entry if pos == "long" else entry_price + stop_atr * atr_at_entry
            if (pos == "long" and curr_price <= stop) or (pos == "short" and curr_price >= stop):
                trades.append((curr_price - entry_price) / entry_price if pos == "long" else (entry_price - curr_price) / entry_price)
                pos = None
            elif (pos == "long" and curr_price < ma20_val) or (pos == "short" and curr_price > ma20_val):
                trades.append((curr_price - entry_price) / entry_price if pos == "long" else (entry_price - curr_price) / entry_price)
                pos = None
    
    return trades

def calc_stats(trades, label):
    if not trades:
        print(f"{label}: 无交易")
        return
    
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
    print(f"  交易次数: {len(trades)}")
    print(f"  胜率: {wr:.1%}")
    print(f"  平均盈利: {avg_win:+.2%}")
    print(f"  平均亏损: {avg_loss:+.2%}")
    print(f"  盈亏比: {rr:.2f}")
    print(f"  PF: {pf:.2f}")
    print(f"  总收益: {total:+.1%}")
    print(f"  最大回撤: {max_dd:.1%}")
    print()

print("="*70)
print("  多周期交易系统回测")
print("="*70)

# 获取数据
print("正在获取数据...")
t = yf.Ticker("BTC-USD")

try:
    # 日线
    df_1d = t.history(period="3y", interval="1d")
    print(f"日线: {len(df_1d)}根")
    
    # 4小时 - 用日线数据模拟(取每天的最后4根)
    # 实际应该用真实4H数据，这里用日线数据简化
    df_4h = t.history(period="1y", interval="1h")
    print(f"4H/1H: {len(df_4h)}根")
    
    # 用1H数据作为中周期
    df_1h = df_4h.copy()
    df_m15 = t.history(period="60d", interval="15m")
    print(f"15M: {len(df_m15)}根")
    
except Exception as e:
    print(f"数据获取失败: {e}")
    exit()

print()

# 回测各策略
print("="*70)
print("  回测结果对比")
print("="*70)
print()

# 1. 多周期策略(如果数据足够)
if len(df_1h) > 1000:
    # 用日线作为大周期，1H作为入场周期
    daily = df_1d
    h1 = df_1h[-2000:]  # 取最近2000根1H
    
    trades_multi = multi_tf_strategy(daily, h1, h1, h1)
    calc_stats(trades_multi, "多周期策略(大周期确认+小周期入场)")
else:
    print("多周期策略: 数据不足")

# 2. 单周期RSI策略(对照)
print("-"*70)
trades_rsi = simple_rsi_strategy(df_1h, rsi_buy=35, rsi_sell=65, stop_pct=0.03)
calc_stats(trades_rsi, "单周期RSI策略(RSI<35买, >65卖, 3%止损)")

# 3. 日线趋势跟随(对照)
print("-"*70)
trades_trend = trend_follow_strategy(df_1d, stop_atr=2.0)
calc_stats(trades_trend, "日线趋势跟随(MA排列, 2ATR止损)")

# 4. 买入持有(基准)
print("-"*70)
if len(df_1d) > 100:
    p = np.asarray(df_1d["Close"].values).flatten()
    buy_hold = (p[-1] - p[0]) / p[0]
    print(f"买入持有:")
    print(f"  总收益: {buy_hold:+.1%}")
    print(f"  持仓周期: {len(p)}天")

print()
print("="*70)
print("  对比总结")
print("="*70)
print()
print("策略                    | 交易次数 | 胜率   | PF    | 总收益  | 最大DD")
print("-"*70)

all_results = []

if trades_multi:
    wins = [t for t in trades_multi if t > 0]
    losses = [t for t in trades_multi if t < 0]
    wr = len(wins)/len(trades_multi) if trades_multi else 0
    pf = abs(sum(wins)/sum(losses)) if losses and sum(losses)!=0 else 999
    equity = [1.0]
    for t in trades_multi: equity.append(equity[-1]*(1+t))
    equity = np.array(equity)
    total = equity[-1]-1
    peak = np.maximum.accumulate(equity)
    dd = (equity-peak)/peak
    max_dd = abs(dd.min())
    all_results.append(("多周期策略", len(trades_multi), wr, pf, total, max_dd))

if trades_rsi:
    wins = [t for t in trades_rsi if t > 0]
    losses = [t for t in trades_rsi if t < 0]
    wr = len(wins)/len(trades_rsi) if trades_rsi else 0
    pf = abs(sum(wins)/sum(losses)) if losses and sum(losses)!=0 else 999
    equity = [1.0]
    for t in trades_rsi: equity.append(equity[-1]*(1+t))
    equity = np.array(equity)
    total = equity[-1]-1
    peak = np.maximum.accumulate(equity)
    dd = (equity-peak)/peak
    max_dd = abs(dd.min())
    all_results.append(("RSI单周期", len(trades_rsi), wr, pf, total, max_dd))

if trades_trend:
    wins = [t for t in trades_trend if t > 0]
    losses = [t for t in trades_trend if t < 0]
    wr = len(wins)/len(trades_trend) if trades_trend else 0
    pf = abs(sum(wins)/sum(losses)) if losses and sum(losses)!=0 else 999
    equity = [1.0]
    for t in trades_trend: equity.append(equity[-1]*(1+t))
    equity = np.array(equity)
    total = equity[-1]-1
    peak = np.maximum.accumulate(equity)
    dd = (equity-peak)/peak
    max_dd = abs(dd.min())
    all_results.append(("趋势跟随", len(trades_trend), wr, pf, total, max_dd))

p = np.asarray(df_1d["Close"].values).flatten()
buy_hold = (p[-1] - p[0]) / p[0]
all_results.append(("买入持有", 1, "-", "-", buy_hold, "-"))

for name, trades_cnt, wr, pf, total, max_dd in all_results:
    wr_str = f"{wr:.1%}" if isinstance(wr, float) else wr
    pf_str = f"{pf:.2f}" if isinstance(pf, float) else pf
    dd_str = f"{max_dd:.1%}" if isinstance(max_dd, float) else max_dd
    print(f"{name:<20} | {trades_cnt:>8} | {wr_str:>6} | {pf_str:>6} | {total:>+7.1%} | {dd_str:>6}")