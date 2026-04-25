#!/usr/bin/env python3
"""Walk-Forward年度对比（精简版）"""
import yfinance as yf, numpy as np, pandas as pd
from scipy.stats import spearmanr

def rsi(s, p=14):
    d = s.diff(); g = d.clip(lower=0).rolling(p).mean(); l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - (100/(1 + g/l.replace(0,np.nan)))

def adx(h, lo, c, p=14):
    pdm = h.diff(); mdm = -lo.diff(); pdm[pdm < 0] = 0; mdm[mdm < 0] = 0
    tr = np.maximum(h - lo, np.maximum(abs(h - c.shift(1)), abs(lo - c.shift(1))))
    atr = tr.rolling(p).mean(); pdi = 100*(pdm.rolling(p).mean()/atr); mdi = 100*(mdm.rolling(p).mean()/atr)
    dx = 100*abs(pdi-mdi)/(pdi+mdi); return dx.rolling(p).mean()

def load_df(ticker):
    df = yf.download(ticker, period='5y', interval='1d', progress=False, auto_adjust=True)
    if df.empty: return None
    if isinstance(df.columns, pd.MultiIndex): df.columns = [c[0].lower() for c in df.columns]
    else: df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index)
    return df

def bt_ic(df, ic_thresh=0.05):
    df = df.copy(); df['rsi'] = rsi(df['close']); df['adx'] = adx(df['high'], df['low'], df['close'])
    df['rsi_inv'] = 100 - df['rsi']; df['ret_next'] = df['close'].pct_change().shift(-1)
    df['ic'] = np.nan
    for i in range(60, len(df)-1):
        w = df.iloc[i-60:i]
        if w['rsi_inv'].std() > 1e-10 and w['ret_next'].std() > 1e-10:
            ic, _ = spearmanr(w['rsi_inv'], w['ret_next'])
            df.iloc[i, df.columns.get_loc('ic')] = 0 if np.isnan(ic) else ic
    cap = 100000; posn = 0; ep = 0; wins = []; losses = []; yr_end = {}
    for i in range(65, len(df)-1):
        price = df.iloc[i]['close']; rsi_v = df.iloc[i]['rsi']; adx_v = df.iloc[i]['adx']; ic_v = df.iloc[i]['ic']; yr = df.iloc[i].name.year
        if posn != 0:
            pnl = cap * 0.10 * (posn * (price - ep) / ep * 3)
            (wins if pnl > 0 else losses).append(pnl); cap += pnl; posn = 0
        if ic_v is not None and not np.isnan(ic_v) and abs(ic_v) > ic_thresh:
            if rsi_v < 30 and adx_v > 20 and posn == 0: posn = 1; ep = price
            elif rsi_v > 70 and adx_v > 20 and posn == 0: posn = -1; ep = price
        if posn == 1:
            if price <= ep*(1-0.05/3): cap += cap*0.10*(-0.05); losses.append(cap*0.10*(-0.05)); posn = 0
            elif price >= ep*(1+0.20/3): cap += cap*0.10*0.20; wins.append(cap*0.10*0.20); posn = 0
        elif posn == -1:
            if price >= ep*(1+0.05/3): cap += cap*0.10*(-0.05); losses.append(cap*0.10*(-0.05)); posn = 0
            elif price <= ep*(1-0.20/3): cap += cap*0.10*0.20; wins.append(cap*0.10*0.20); posn = 0
        yr_end[yr] = cap
    return cap, len(wins), len(losses), yr_end

def bt_fixed(df):
    df = df.copy(); df['rsi'] = rsi(df['close']); df['adx'] = adx(df['high'], df['low'], df['close'])
    cap = 100000; posn = 0; ep = 0; wins = []; losses = []; yr_end = {}
    for i in range(20, len(df)-1):
        price = df.iloc[i]['close']; rsi_v = df.iloc[i]['rsi']; adx_v = df.iloc[i]['adx']; yr = df.iloc[i].name.year
        if posn != 0:
            pnl = cap * 0.10 * (posn * (price - ep) / ep * 3)
            (wins if pnl > 0 else losses).append(pnl); cap += pnl; posn = 0
        if rsi_v < 30 and adx_v > 20 and posn == 0: posn = 1; ep = price
        elif rsi_v > 70 and adx_v > 20 and posn == 0: posn = -1; ep = price
        if posn == 1:
            if price <= ep*(1-0.05/3): cap += cap*0.10*(-0.05); losses.append(cap*0.10*(-0.05)); posn = 0
            elif price >= ep*(1+0.20/3): cap += cap*0.10*0.20; wins.append(cap*0.10*0.20); posn = 0
        elif posn == -1:
            if price >= ep*(1+0.05/3): cap += cap*0.10*(-0.05); losses.append(cap*0.10*(-0.05)); posn = 0
            elif price <= ep*(1-0.20/3): cap += cap*0.10*0.20; wins.append(cap*0.10*0.20); posn = 0
        yr_end[yr] = cap
    return yr_end

coins = [('BTC-USD','BTC'), ('ETH-USD','ETH'), ('ADA-USD','ADA'), ('DOGE-USD','DOGE'), ('AVAX-USD','AVAX'), ('DOT-USD','DOT')]

print('='*75)
print('Walk-Forward年度对比: IC自适应(蓝) vs 固定策略(灰) [单位=K美元]')
print('='*75)

all_yr_ic = {}
all_yr_fx = {}
total_ic = 0

for ticker, coin in coins:
    df = load_df(ticker)
    if df is None: continue
    cap_ic, w, l, yr_ic = bt_ic(df)
    yr_fx = bt_fixed(df)
    
    all_yr_ic[coin] = yr_ic
    all_yr_fx[coin] = yr_fx
    
    diff = cap_ic - 100000
    arrow = '+' if diff > 0 else ''
    print(f'{coin:4s}: 最终=${cap_ic/1000:.0f}K | IC自适应 {arrow}{diff/1000:.0f}K | {w+l}笔交易')
    total_ic += diff

print()
print(f'6币种合计: IC自适应总盈利 = ${total_ic/1000:.0f}K')

# 熊市检验
print()
print('2022熊市检验:')
for coin in ['BTC', 'ETH', 'ADA', 'DOGE', 'AVAX', 'DOT']:
    yr_ic = all_yr_ic.get(coin, {})
    yr_fx = all_yr_fx.get(coin, {})
    ic_2022 = yr_ic.get(2022, 100000) - 100000
    fx_2022 = yr_fx.get(2022, 100000) - 100000
    if ic_2022 != 0 or fx_2022 != 0:
        print(f'  {coin}: IC=${ic_2022/1000:+.0f}K | 固定=${fx_2022/1000:+.0f}K | 改善={(ic_2022-fx_2022)/1000:+.0f}K')
