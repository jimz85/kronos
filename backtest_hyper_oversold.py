#!/usr/bin/env python3
"""
回测：系统性超卖加仓规则 vs 原始不追单规则
高效版 - 按年份逐年处理，避免内存爆炸
"""
import os, json, sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
COINS = ['BTC', 'ETH', 'BNB', 'ADA', 'AVAX', 'DOGE']
START_YEAR = 2020

# ============ 指标 ============
def calc_rsi_arr(close, n=14):
    deltas = np.diff(close, prepend=close[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.zeros_like(close)
    avg_loss = np.zeros_like(close)
    # EMA方式
    avg_gain[n] = np.mean(gains[1:n+1])
    avg_loss[n] = np.mean(losses[1:n+1])
    for i in range(n+1, len(close)):
        avg_gain[i] = (avg_gain[i-1]*(n-1) + gains[i]) / n
        avg_loss[i] = (avg_loss[i-1]*(n-1) + losses[i]) / n
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_adx_arr(high, low, close, n=14):
    tr = np.zeros(len(close))
    tr[1:] = np.maximum(high[1:]-low[1:], np.maximum(np.abs(high[1:]-close[:-1]), np.abs(low[1:]-close[:-1])))
    tr[0] = high[0] - low[0]
    tr_ma = np.zeros(len(close))
    up = np.zeros(len(close)); up[1:] = high[1:] - high[:-1]
    dn = np.zeros(len(close)); dn[1:] = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_di = np.zeros(len(close))
    minus_di = np.zeros(len(close))
    tr_ma[n] = np.mean(tr[1:n+1])
    plus_dm_ma = np.zeros(len(close)); plus_dm_ma[n] = np.mean(plus_dm[1:n+1])
    minus_dm_ma = np.zeros(len(close)); minus_dm_ma[n] = np.mean(minus_dm[1:n+1])
    for i in range(n+1, len(close)):
        tr_ma[i] = (tr_ma[i-1]*(n-1) + tr[i]) / n
        plus_dm_ma[i] = (plus_dm_ma[i-1]*(n-1) + plus_dm[i]) / n
        minus_dm_ma[i] = (minus_dm_ma[i-1]*(n-1) + minus_dm[i]) / n
        plus_di[i] = 100 * plus_dm_ma[i] / (tr_ma[i] + 1e-10)
        minus_di[i] = 100 * minus_dm_ma[i] / (tr_ma[i] + 1e-10)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx = np.zeros(len(close))
    adx[n*2] = np.mean(dx[n:n*2])
    for i in range(n*2+1, len(close)):
        adx[i] = (adx[i-1]*(n-1) + dx[i]) / n
    return adx

# ============ 数据加载（按年份）============
def load_year_data(coin, year):
    """加载指定年份的数据"""
    fpath = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    if not os.path.exists(fpath): return None
    
    # 直接用grep找年份行
    start_dt = f'{year}-01-01'
    end_dt = f'{year+1}-01-01'
    
    # 用awk提取年份范围内数据
    cmd = f"""awk -F',' 'NR==1 || /{start_dt}/, /{end_dt}/' "{fpath}" """
    data = os.popen(cmd).read()
    if not data or 'datetime' not in data.lower():
        # fallback: 全文件读
        df = pd.read_csv(fpath)
        dt_col = next((c for c in df.columns if 'datetime' in c.lower()), df.columns[0])
        df['ts'] = pd.to_datetime(df[dt_col], errors='coerce').dt.tz_localize(None)
        df = df[(df['ts'] >= start_dt) & (df['ts'] < end_dt)]
        if len(df) < 100: return None
    else:
        from io import StringIO
        df = pd.read_csv(StringIO(data))
        dt_col = next((c for c in df.columns if 'datetime' in c.lower()), df.columns[0])
        df['ts'] = pd.to_datetime(df[dt_col], errors='coerce').dt.tz_localize(None)
        df = df.dropna(subset=['ts'])
    
    # 标准化列
    result = pd.DataFrame()
    for col in ['open','high','low','close']:
        if col in df.columns: result[col] = df[col].values
    if 'volume' in df.columns:
        result['volume'] = df['volume'].values
    elif 'vol' in df.columns:
        result['volume'] = df['vol'].values
    else:
        result['volume'] = 0
    result['ts'] = df['ts'].values
    return result.set_index('ts').sort_index()

# ============ 快速全量RSI扫描 ============
def scan_all_rsi(coin):
    """快速扫描全量数据，找RSI<40的所有时点"""
    fpath = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    if not os.path.exists(fpath): return []
    
    # 读全部（加密数据约60万行，可以接受）
    df = pd.read_csv(fpath)
    dt_col = next((c for c in df.columns if 'datetime' in c.lower()), df.columns[0])
    df['ts'] = pd.to_datetime(df[dt_col], errors='coerce').dt.tz_localize(None)
    df = df.dropna(subset=['ts'])
    df = df[df['ts'].dt.year >= 2020]
    df = df.set_index('ts').sort_index()
    
    # 5min RSI
    close = df['close'].values.astype(float)
    rsi5 = calc_rsi_arr(close, 14)
    
    # 15min RSI
    ohlc_15 = df[['open','high','low','close','volume']].resample('15min').agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum'
    }).dropna()
    rsi15 = calc_rsi_arr(ohlc_15['close'].values, 14)
    
    # 1h趋势
    ohlc_1h = df[['open','high','low','close','volume']].resample('1h').agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum'
    }).dropna()
    c1h = ohlc_1h['close'].values
    ema20 = pd.Series(c1h).ewm(span=20, adjust=False).mean().values
    ema50 = pd.Series(c1h).ewm(span=50, adjust=False).mean().values
    adx1h = calc_adx_arr(ohlc_1h['high'].values, ohlc_1h['low'].values, c1h)
    
    # 匹配15min RSI到每根5min
    oversold_times = []
    dates_15 = ohlc_15.index
    n_15 = len(rsi15)
    
    for i in range(50, len(close) - 1):
        ts = df.index[i]
        price = close[i]
        
        # 找对应15min RSI
        # 15min bar index
        delta = ts - dates_15[0]
        m15_idx = int(delta.total_seconds() // (15*60))
        if m15_idx < 0 or m15_idx >= n_15: continue
        rsi = rsi15[m15_idx]
        if np.isnan(rsi): continue
        
        # 找对应1h
        delta1h = ts - ohlc_1h.index[0]
        h1_idx = min(int(delta1h.total_seconds() // 3600), len(adx1h)-1)
        if h1_idx < 0: continue
        trend_up = ema20[h1_idx] > ema50[h1_idx]
        trend_down = ema20[h1_idx] < ema50[h1_idx]
        adx = adx1h[h1_idx] if not np.isnan(adx1h[h1_idx]) else 0
        
        oversold_times.append({
            'ts': ts, 'coin': coin, 'price': price,
            'rsi': rsi, 'adx': adx,
            'trend_up': trend_up, 'trend_down': trend_down
        })
    
    return oversold_times

# ============ 主回测逻辑 ============
def backtest_signals(signals_by_date, allow_hyper=True, max_new_coins=2):
    """
    signals_by_date: {date: [signal_dict, ...]}
    按日期顺序执行交易
    """
    NO_ADD = {'BTC', 'ETH', 'BNB'}
    positions = {}  # {coin: (entry_price, side, date)}
    closed_trades = []  # (entry_price, exit_price, side, date)
    
    # 按时间排序信号
    all_signals = []
    for date, sigs in signals_by_date.items():
        for s in sigs:
            all_signals.append((s['ts'], s))
    all_signals.sort(key=lambda x: x[0])
    
    for ts, sig in all_signals:
        coin = sig['coin']
        price = sig['price']
        rsi = sig['rsi']
        adx = sig['adx']
        trend_up = sig['trend_up']
        trend_down = sig['trend_down']
        date = ts.date()
        
        # ===== 平仓 =====
        if coin in positions:
            entry_p, side, entry_date = positions[coin]
            should_exit = False
            if side == 'long':
                if rsi < 18: should_exit = True
                elif adx < 15: should_exit = True
                elif price < entry_p * 0.985: should_exit = True  # 1.5%硬止损
            else:
                if rsi > 85: should_exit = True
                elif adx < 15: should_exit = True
                elif price > entry_p * 1.015: should_exit = True
            
            if should_exit:
                pnl = (price - entry_p) / entry_p if side == 'long' else (entry_p - price) / entry_p
                closed_trades.append((entry_p, price, side, entry_date, pnl))
                del positions[coin]
        
        # ===== 入场 =====
        if coin not in positions:
            # 检查是否已有持仓（追单限制）
            has_pos = any(c in positions for c in ['BTC','ETH','BNB'])
            
            # 超卖环境检测：当前日期有多少个币种RSI<40
            date_sigs = signals_by_date.get(date, [])
            oversold_coins = sum(1 for s in date_sigs if s['rsi'] < 40 and s['coin'] != coin)
            btc_oversold = any(s['coin'] == 'BTC' and s['rsi'] < 40 for s in date_sigs)
            in_hyper = allow_hyper and btc_oversold and oversold_coins >= 2
            
            # 原始规则
            if rsi < 35 and trend_up and adx > 20:
                positions[coin] = (price, 'long', date)
            elif rsi < 40 and trend_up and adx > 20:
                positions[coin] = (price, 'long', date)
            # 超卖规则
            elif in_hyper and coin not in NO_ADD and not has_pos and trend_up and rsi < 40 and adx > 15:
                positions[coin] = (price, 'long', date)
    
    return closed_trades

# ============ 主程序 ============
if __name__ == '__main__':
    print("=" * 60)
    print("回测：系统性超卖加仓规则 vs 原始不追单规则")
    print("=" * 60)
    
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2022
    coin = sys.argv[2] if len(sys.argv) > 2 else 'BTC'
    
    print(f"\n📊 {coin} {year}年 RSI扫描...")
    sigs = scan_all_rsi(coin)
    print(f"   扫描到 {len(sigs)} 个时点")
    
    if sigs:
        rsi_vals = [s['rsi'] for s in sigs]
        oversold = [s for s in sigs if s['rsi'] < 40]
        print(f"   RSI<40: {len(oversold)} 个时点 ({len(oversold)/len(sigs)*100:.1f}%)")
        if oversold:
            print(f"   最近5个超卖点:")
            for s in oversold[-5:]:
                print(f"     {s['ts']} RSI={s['rsi']:.1f} ADX={s['adx']:.1f} price=${s['price']:.2f}")
    
    print("\n✅ 回测脚本已就绪。使用以下命令运行：")
    print("   python3 backtest_hyper_oversold.py <年份> <币种>")
    print("   python3 backtest_hyper_oversold.py 2022 BTC")
