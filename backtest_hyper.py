#!/usr/bin/env python3
"""
简洁回测：系统性超卖加仓 vs 不追单
按日线级别回测，快速得到结论
"""
import os, json
import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
COINS = ['BTC', 'ETH', 'BNB', 'ADA', 'AVAX', 'DOGE']
START_YEAR = 2020

def calc_rsi(close, n=14):
    deltas = np.diff(close, prepend=close[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.zeros_like(deltas, dtype=float)
    avg_loss = np.zeros_like(deltas, dtype=float)
    avg_gain[n] = np.mean(gains[1:n+1])
    avg_loss[n] = np.mean(losses[1:n+1])
    for i in range(n+1, len(deltas)):
        avg_gain[i] = (avg_gain[i-1]*(n-1) + gains[i]) / n
        avg_loss[i] = (avg_loss[i-1]*(n-1) + losses[i]) / n
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def load_coin_daily(coin):
    """加载日线数据用于快速回测"""
    fpath = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    if not os.path.exists(fpath): return None
    
    total = int(os.popen(f'wc -l < "{fpath}"').read().strip()) - 1
    skip = max(0, total - 200000)  # 最近2000根5min线
    df = pd.read_csv(fpath, skiprows=range(1, skip+1) if skip > 0 else None)
    
    dt_col = next((c for c in df.columns if 'datetime' in c.lower()), df.columns[0])
    df['ts'] = pd.to_datetime(df[dt_col], errors='coerce').dt.tz_localize(None)
    df = df.dropna(subset=['ts'])
    df = df[df['ts'].dt.year >= START_YEAR]
    
    # 转日线
    df = df.set_index('ts').sort_index()
    
    # 标准化列名
    vol_col = next((c for c in df.columns if 'vol' in c.lower()), None)
    keep_cols = ['open', 'high', 'low', 'close']
    if vol_col: keep_cols.append(vol_col)
    df = df[[c for c in keep_cols if c in df.columns]].copy()
    if vol_col and vol_col != 'volume':
        df = df.rename(columns={vol_col: 'volume'})
    if 'volume' not in df.columns:
        df['volume'] = 0
    
    daily = df.resample('1D').agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum'
    }).dropna()
    return daily

def load_coin_4h(coin):
    """加载4H数据"""
    fpath = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    if not os.path.exists(fpath): return None
    
    total = int(os.popen(f'wc -l < "{fpath}"').read().strip()) - 1
    skip = max(0, total - 200000)
    df = pd.read_csv(fpath, skiprows=range(1, skip+1) if skip > 0 else None)
    
    dt_col = next((c for c in df.columns if 'datetime' in c.lower()), df.columns[0])
    df['ts'] = pd.to_datetime(df[dt_col], errors='coerce').dt.tz_localize(None)
    df = df.dropna(subset=['ts'])
    df = df[df['ts'].dt.year >= START_YEAR]
    
    df = df.set_index('ts').sort_index()
    
    vol_col = next((c for c in df.columns if 'vol' in c.lower()), None)
    keep_cols = ['open', 'high', 'low', 'close']
    if vol_col: keep_cols.append(vol_col)
    df = df[[c for c in keep_cols if c in df.columns]].copy()
    if vol_col and vol_col != 'volume':
        df = df.rename(columns={vol_col: 'volume'})
    if 'volume' not in df.columns:
        df['volume'] = 0
    
    h4 = df.resample('4h').agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum'
    }).dropna()
    return h4

print("=" * 60)
print("回测：系统性超卖加仓规则 vs 原始不追单规则")
print("=" * 60)

# 加载所有数据
print("\n📂 加载数据...")
coin_data = {}
for coin in COINS:
    daily = load_coin_daily(coin)
    h4 = load_coin_4h(coin)
    if daily is not None and h4 is not None:
        coin_data[coin] = {'daily': daily, '4h': h4}
        print(f"  {coin}: {len(daily)} 天, {len(h4)} 个4H")

# 对齐日期
all_dates = set()
for cd in coin_data.values():
    all_dates.update(cd['daily'].index)
all_dates = sorted([d for d in all_dates if d.year >= START_YEAR])

print(f"  对齐后: {len(all_dates)} 个共同日期")

# ===== 生成每日信号 =====
print("\n📊 计算日线RSI和趋势...")
daily_signals = {}  # {date: {coin: {'rsi': x, 'trend_up': bool, 'close': price}}}

for date in all_dates:
    day_sigs = {}
    for coin, cd in coin_data.items():
        d = cd['daily']
        h = cd['4h']
        
        # 4H趋势
        h_before = h[h.index < date]
        if len(h_before) < 60: continue
        c4h = h_before['close'].values
        ema20 = pd.Series(c4h).ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = pd.Series(c4h).ewm(span=50, adjust=False).mean().iloc[-1]
        trend_up = ema20 > ema50
        
        # 日线RSI(14)
        c_d = d[d.index <= date]
        if len(c_d) < 20: continue
        rsi = calc_rsi(c_d['close'].values, 14)[-1]
        close = c_d['close'].iloc[-1]
        
        day_sigs[coin] = {'rsi': rsi, 'trend_up': trend_up, 'close': close}
    
    daily_signals[date] = day_sigs

# ===== 模拟交易 =====
print("\n🔁 模拟交易...")
NO_ADD = {'BTC', 'ETH', 'BNB'}

def simulate(hyper_mode):
    capital = 100000  # 10万模拟资金
    pos = {}  # {coin: (entry_price, size)}
    trades = []  # (date, coin, side, entry, exit, pnl_pct)
    daily_returns = []
    
    for date in all_dates:
        sigs = daily_signals.get(date, {})
        if not sigs: continue
        
        btc_rsi = sigs.get('BTC', {}).get('rsi', 99)
        oversold_count = sum(1 for c, s in sigs.items() if s['rsi'] < 40)
        in_hyper = hyper_mode and btc_rsi < 40 and oversold_count >= 3
        
        # 平仓检查
        for coin, (entry_p, size) in list(pos.items()):
            if coin not in sigs: continue
            sig = sigs[coin]
            price = sig['close']
            rsi = sig['rsi']
            trend_up = sig['trend_up']
            
            # 止损
            sl = 0.015 if in_hyper else 0.02
            if price < entry_p * (1 - sl):
                pnl = (price / entry_p - 1)
                trades.append((date, coin, 'long', entry_p, price, pnl))
                capital *= (1 + pnl * size)
                del pos[coin]
            # 止盈
            elif price > entry_p * 1.045:
                pnl = (price / entry_p - 1)
                trades.append((date, coin, 'long', entry_p, price, pnl))
                capital *= (1 + pnl * size)
                del pos[coin]
            # RSI极端
            elif rsi < 18:
                pnl = (price / entry_p - 1)
                trades.append((date, coin, 'long', entry_p, price, pnl))
                capital *= (1 + pnl * size)
                del pos[coin]
        
        # 入场检查
        new_coins_this_day = 0
        for coin in COINS:
            if coin in pos: continue
            if coin in NO_ADD and not in_hyper: continue
            if coin not in sigs: continue
            sig = sigs[coin]
            if sig['rsi'] >= 40: continue
            if not sig['trend_up']: continue
            
            # 超卖模式：最多2个新币
            if in_hyper and new_coins_this_day >= 2: continue
            
            # 原仓位50%（1%×0.5=0.5%）
            pct = 0.005 if in_hyper else 0.01
            price = sig['close']
            size = pct  # 资金比例
            pos[coin] = (price, size)
            new_coins_this_day += 1
    
    return trades, capital, pos

t_baseline, cap_b, pos_b = simulate(hyper_mode=False)
t_hyper, cap_h, pos_h = simulate(hyper_mode=True)

def analyze(trades, initial_cap=100000):
    if not trades: return 0, 0, 0, 0, 0
    pnls = [t[5] for t in trades]
    wins = [p for p in pnls if p > 0]
    total = sum(pnls)
    count = len(pnls)
    win_rate = len(wins) / count if count > 0 else 0
    max_dd = 0
    peak = initial_cap
    equity = initial_cap
    for p in pnls:
        equity *= (1 + p)
        if equity > peak: peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd: max_dd = dd
    avg_win = np.mean(wins) if wins else 0
    return total * 100, count, win_rate * 100, max_dd * 100, avg_win * 100

print("\n" + "=" * 60)
print("结果对比")
print("=" * 60)
b = analyze(t_baseline)
h = analyze(t_hyper)
print(f"\n{'策略':<20} {'总收益率':>10} {'交易次数':>8} {'胜率':>8} {'最大DD':>10} {'均盈':>8}")
print("-" * 60)
print(f"{'基线(不追单)':<20} {b[0]:>+9.1f}% {b[1]:>8} {b[2]:>7.1f}% {b[3]:>9.1f}% {b[4]:>7.1f}%")
print(f"{'超卖加仓':<20} {h[0]:>+9.1f}% {h[1]:>8} {h[2]:>7.1f}% {h[3]:>9.1f}% {h[4]:>7.1f}%")
print("-" * 60)
print(f"{'差异':<20} {h[0]-b[0]:>+9.1f}% {h[1]-b[1]:>+8} {h[2]-b[2]:>+7.1f}% {h[3]-b[3]:>+9.1f}%")

print("\n💡 解读：")
if h[0] > b[0] and h[3] - b[3] < 10:
    print("  ✅ 超卖加仓规则有效：收益提升且回撤可控")
elif h[0] > b[0] and h[3] > b[3]:
    print("  ⚠️ 超卖加仓规则收益更高但回撤也更大，需进一步分析")
else:
    print("  ❌ 超卖加仓规则未能提升收益，不建议采用")
