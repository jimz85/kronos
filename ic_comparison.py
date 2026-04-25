#!/usr/bin/env python3
import yfinance as yf, numpy as np, pandas as pd
from scipy.stats import spearmanr

def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def adx(h, lo, c, p=14):
    pdm = h.diff(); mdm = -lo.diff()
    pdm[pdm < 0] = 0; mdm[mdm < 0] = 0
    tr = np.maximum(h - lo, np.maximum(abs(h - c.shift(1)), abs(lo - c.shift(1))))
    atr = tr.rolling(p).mean()
    pdi = 100 * (pdm.rolling(p).mean() / atr)
    mdi = 100 * (mdm.rolling(p).mean() / atr)
    dx = 100 * abs(pdi - mdi) / (pdi + mdi)
    return dx.rolling(p).mean()

def bt_fixed(df):
    df2 = df.copy()
    df2['rsi'] = rsi(df2['close'])
    df2['adx'] = adx(df2['high'], df2['low'], df2['close'])
    cap = 100000; posn = 0; ep = 0; wins = []; losses = []
    for i in range(20, len(df2) - 1):
        price = df2.iloc[i]['close']
        rsi_v = df2.iloc[i]['rsi']
        adx_v = df2.iloc[i]['adx']
        if posn != 0:
            pnl = cap * 0.10 * (posn * (price - ep) / ep * 3)
            if pnl > 0: wins.append(pnl)
            else: losses.append(pnl)
            cap += pnl; posn = 0
        if rsi_v < 30 and adx_v > 20 and posn == 0:
            posn = 1; ep = price
        elif rsi_v > 70 and adx_v > 20 and posn == 0:
            posn = -1; ep = price
        if posn == 1:
            if price <= ep * (1 - 0.05/3):
                cap += cap * 0.10 * (-0.05); losses.append(cap * 0.10 * (-0.05)); posn = 0
            elif price >= ep * (1 + 0.20/3):
                cap += cap * 0.10 * 0.20; wins.append(cap * 0.10 * 0.20); posn = 0
        elif posn == -1:
            if price >= ep * (1 + 0.05/3):
                cap += cap * 0.10 * (-0.05); losses.append(cap * 0.10 * (-0.05)); posn = 0
            elif price <= ep * (1 - 0.20/3):
                cap += cap * 0.10 * 0.20; wins.append(cap * 0.10 * 0.20); posn = 0
    wlr = abs(sum(wins) / sum(losses)) if losses else 99
    return cap, wlr, len(wins), len(losses)

def bt_ic(df, ic_thresh):
    df2 = df.copy()
    df2['rsi'] = rsi(df2['close'])
    df2['adx'] = adx(df2['high'], df2['low'], df2['close'])
    df2['rsi_inv'] = 100 - df2['rsi']
    df2['ret_next'] = df2['close'].pct_change().shift(-1)
    window = 60
    ic_vals = [0] * len(df2)
    for i in range(window, len(df2)):
        w = df2.iloc[i-window:i]
        s1 = w['rsi_inv'].std(); s2 = w['ret_next'].std()
        if s1 > 1e-10 and s2 > 1e-10:
            ic, _ = spearmanr(w['rsi_inv'], w['ret_next'])
            ic_vals[i] = 0 if np.isnan(ic) else ic
    df2['ic_rsi'] = ic_vals
    cap = 100000; posn = 0; ep = 0; wins = []; losses = []
    for i in range(window + 1, len(df2) - 1):
        price = df2.iloc[i]['close']
        rsi_v = df2.iloc[i]['rsi']
        adx_v = df2.iloc[i]['adx']
        ic_v = df2.iloc[i]['ic_rsi']
        if posn != 0:
            pnl = cap * 0.10 * (posn * (price - ep) / ep * 3)
            if pnl > 0: wins.append(pnl)
            else: losses.append(pnl)
            cap += pnl; posn = 0
        ic_ok = abs(ic_v) > ic_thresh
        if rsi_v < 30 and adx_v > 20 and ic_ok and posn == 0:
            posn = 1; ep = price
        elif rsi_v > 70 and adx_v > 20 and ic_ok and posn == 0:
            posn = -1; ep = price
        if posn == 1:
            if price <= ep * (1 - 0.05/3):
                cap += cap * 0.10 * (-0.05); losses.append(cap * 0.10 * (-0.05)); posn = 0
            elif price >= ep * (1 + 0.20/3):
                cap += cap * 0.10 * 0.20; wins.append(cap * 0.10 * 0.20); posn = 0
        elif posn == -1:
            if price >= ep * (1 + 0.05/3):
                cap += cap * 0.10 * (-0.05); losses.append(cap * 0.10 * (-0.05)); posn = 0
            elif price <= ep * (1 - 0.20/3):
                cap += cap * 0.10 * 0.20; wins.append(cap * 0.10 * 0.20); posn = 0
    wlr = abs(sum(wins) / sum(losses)) if losses else 99
    return cap, wlr, len(wins), len(losses)

if __name__ == '__main__':
    tickers = [
        ('BTC-USD', 'BTC'),
        ('ETH-USD', 'ETH'),
        ('ADA-USD', 'ADA'),
        ('DOGE-USD', 'DOGE'),
        ('AVAX-USD', 'AVAX'),
        ('DOT-USD', 'DOT'),
    ]

    print('=== IC过滤 vs 固定策略（yfinance 1d，5年）===')
    for ticker, coin in tickers:
        df = yf.download(ticker, period='5y', interval='1d', progress=False)
        if df.empty:
            continue
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        if len(df) < 100:
            continue

        c1, wlr1, w1, l1 = bt_fixed(df)
        c2, wlr2, w2, l2 = bt_ic(df, 0.05)
        c3, wlr3, w3, l3 = bt_ic(df, 0.10)

        print(f'{coin}:')
        print(f'  固定RSI30/70 ADX>20: ${c1-100000:+,.0f} WLR={wlr1:.2f} ({w1+l1} trades)')
        print(f'  IC>|0.05|过滤:        ${c2-100000:+,.0f} WLR={wlr2:.2f} ({w2+l2} trades)')
        print(f'  IC>|0.10|过滤:        ${c3-100000:+,.0f} WLR={wlr3:.2f} ({w3+l3} trades)')
        print()
