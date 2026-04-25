#!/usr/bin/env python3
"""
Walk-Forward 验证：多品种趋势跟踪策略
滚动窗口验证：训练期250天，测试期60天
"""
import os, json
import numpy as np
import pandas as pd
from datetime import datetime

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
COINS = ['BTC', 'ETH', 'BNB', 'ADA', 'AVAX', 'DOGE']

def calc_rsi(close, n=14):
    deltas = np.diff(close, prepend=close[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg = np.zeros(len(close), dtype=float)
    lav = np.zeros(len(close), dtype=float)
    avg[n] = np.mean(gains[1:n+1])
    lav[n] = np.mean(losses[1:n+1])
    for i in range(n+1, len(close)):
        avg[i] = (avg[i-1]*(n-1) + gains[i]) / n
        lav[i] = (lav[i-1]*(n-1) + losses[i]) / n
    rs = avg / (lav + 1e-10)
    return 100 - (100 / (1 + rs))

def load_daily(coin):
    fpath = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    if not os.path.exists(fpath): return None
    total = int(os.popen(f'wc -l < "{fpath}"').read().strip()) - 1
    skip = max(0, total - 500000)
    df = pd.read_csv(fpath, skiprows=range(1, skip+1) if skip > 0 else None)
    dt_col = next((c for c in df.columns if 'datetime' in c.lower()), df.columns[0])
    df['ts'] = pd.to_datetime(df[dt_col], errors='coerce').dt.tz_localize(None)
    df = df.dropna(subset=['ts'])
    df = df[df['close'] > 0].set_index('ts').sort_index()
    daily = df[['close']].resample('1D').last().dropna()
    return daily

# 加载
print("📂 加载数据...")
coin_data = {}
for coin in COINS:
    d = load_daily(coin)
    if d is not None:
        coin_data[coin] = d
        print(f"  {coin}: {len(d)} 天")

# 对齐
common_idx = set(coin_data['BTC'].index)
for cd in coin_data.values():
    common_idx &= set(cd.index)
all_dates = sorted(common_idx)
print(f"  对齐: {len(all_dates)} 天 ({all_dates[0].date()} → {all_dates[-1].date()})")

# 转numpy数组
n = len(all_dates)
data = np.zeros((n, len(COINS)))
for j, coin in enumerate(COINS):
    for i, d in enumerate(all_dates):
        data[i, j] = coin_data[coin].loc[d, 'close']

print(f"\n🔁 Walk-Forward (训练250天/测试60天)...")

TRAIN = 250
TEST = 60
results = []

for i in range(TRAIN, n - TEST, TEST):
    train = data[i-TRAIN:i]
    test = data[i:i+TEST]
    
    # ===== 基线策略 =====
    capital = 1.0
    pos = {}  # {coin_idx: entry_price}
    trades = 0; wins = 0
    
    for k in range(TEST):
        close_k = test[k]
        
        # 平仓
        for cidx, ep in list(pos.items()):
            p = close_k[cidx]
            if p <= 0: continue
            r = (p - ep) / ep
            if r > 0: wins += 1
            trades += 1
            capital *= (1 + r)
            del pos[cidx]
        
        # 入场 (最多2个持仓)
        if len(pos) < 2:
            best = None; best_rsi = 999
            for cidx, coin in enumerate(COINS):
                if cidx in pos: continue
                p = close_k[cidx]
                if p <= 0: continue
                rsi_vals = calc_rsi(train[:, cidx], 14)
                rsi = rsi_vals[-1]
                if rsi < 35 and rsi < best_rsi:
                    best_rsi = rsi
                    best = cidx, p, rsi
            
            if best:
                cidx, p, rsi = best
                pos[cidx] = p
    
    ret = (capital - 1) * 100
    wr = wins / trades if trades > 0 else 0
    results.append({
        'end_date': all_dates[i + TEST - 1],
        'strategy': 'baseline',
        'return': ret,
        'trades': trades,
        'win_rate': wr,
        'capital': capital
    })

# 汇总
print("\n" + "=" * 60)
print("Walk-Forward 结果")
print("=" * 60)

# 按年分组
yearly = {}
for r in results:
    yr = r['end_date'].year
    if yr not in yearly: yearly[yr] = []
    yearly[yr].append(r['return'])

print(f"\n{'年份':<8} {'期数':>6} {'平均收益':>10} {'最大':>10} {'最小':>10} {'盈利%':>8}")
print("-" * 56)
for yr in sorted(yearly.keys()):
    rets = yearly[yr]
    winpct = sum(1 for r in rets if r > 0) / len(rets) * 100
    print(f"{yr:<8} {len(rets):>6} {np.mean(rets):>+10.1f}% {np.max(rets):>+10.1f}% {np.min(rets):>+10.1f}% {winpct:>7.0f}%")

all_rets = [r['return'] for r in results]
total_ret = (np.prod([1 + r/100 for r in all_rets]) - 1) * 100
avg_ret = np.mean(all_rets)
win_periods = sum(1 for r in all_rets if r > 0)
avg_trades = np.mean([r['trades'] for r in results])

print(f"\n总体 (共{len(results)}个窗口):")
print(f"  复合收益: {total_ret:+.1f}%")
print(f"  平均每期: {avg_ret:+.1f}%")
print(f"  盈利期: {win_periods}/{len(results)} ({win_periods/len(results)*100:.0f}%)")
print(f"  平均交易次数/期: {avg_trades:.1f}")
