"""
纯趋势跟踪策略 - EMA + ATR
方向: 多空双向
周期: 1H + 4H
核心: 三重EMA过滤 + ATR止损/移动止盈
目标: PF>1.5, 夏普>1.0, 全周期正期望
"""
import vectorbt as vbt
import pandas as pd
import numpy as np
import json, os
from datetime import datetime

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OUT_DIR = '/Users/jimingzhang/kronos/trend_following'
os.makedirs(OUT_DIR, exist_ok=True)

# 约束
MIN_PF = 1.5
MIN_SHARPE = 1.0
MAX_DD = 0.30
FEES = 0.001
SLIPPAGE = 0.0005
SIZE = 0.10  # 10%仓位

COINS = ['BTC', 'ETH', 'DOGE', 'BCH', 'ADA']

def load_data(coin):
    """加载5分钟数据，聚合到1H/4H"""
    df = pd.read_csv(f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv')
    df['timestamp'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    df = df.set_index('timestamp').sort_index()
    df = df[['open', 'high', 'low', 'close', 'vol']].rename(columns={'vol': 'volume'})
    df = df[(df['close'] > 0) & (df['volume'] > 0)]
    return df

def aggregate(df, freq='1h'):
    """5min聚合到大周期"""
    resampled = df.resample(freq).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()
    return resampled

def split_data(df):
    train = df.loc[:'2023-12-31']
    val = df.loc['2024-01-01':'2025-06-30']
    test = df.loc['2025-07-01':]
    return train, val, test

def calc_atr(high, low, close, n=14):
    tr = np.maximum(high - low, np.abs(high - close.shift()), np.abs(low - close.shift()))
    return tr.rolling(n).mean()

def calc_ema(c, n):
    return c.ewm(span=n, adjust=False).mean()

def run_backtest(close, high, low, entries, exits, sl_atr, tp_atr, trailing_atr=None):
    """运行回测，返回stats"""
    atr = calc_atr(high, low, close, 14)
    sl_val = (atr * sl_atr).replace(0, np.nan).ffill().bfill().fillna(0.001)
    tp_val = (atr * tp_atr).replace(0, np.nan).ffill().bfill().fillna(0.003)
    
    kwargs = {
        'sl_stop': sl_val,
        'tp_stop': tp_val,
        'fees': FEES,
        'slippage': SLIPPAGE,
        'size': SIZE,
        'direction': 'longonly'
    }
    
    pf = vbt.Portfolio.from_signals(close, entries, exits, **kwargs)
    s = pf.stats()
    trades = s.get('Total Trades', 0)
    if trades < 10:
        return None
    
    wr = s.get('Win Rate [%]', 0) / 100
    aw = s.get('Avg Winning Trade [%]', 0) / 100
    al = abs(s.get('Avg Losing Trade [%]', 0) / 100)
    pf_ratio = aw / al if al > 1e-10 else 0
    dd = s.get('Max Drawdown [%]', 0) / 100
    sharpe = s.get('Sharpe Ratio', 0)
    ret = s.get('Total Return [%]', 0) / 100
    n_days = (close.index[-1] - close.index[0]).days
    daily = trades / max(n_days, 1)
    
    return {
        'trades': trades, 'wr': wr, 'pf': pf_ratio,
        'dd': dd, 'sharpe': sharpe, 'ret': ret,
        'daily': daily, 'aw': aw, 'al': al,
        'n_days': n_days,
    }

def trend_follow_entries(close, ema_fast, ema_mid, ema_slow):
    """三重EMA趋势跟踪入场信号"""
    # 只做顺大周期趋势的方向
    # 大周期向上(ema_slow在ema_mid上方) → 只做多
    # 大周期向下(ema_slow在ema_mid下方) → 只做空
    bull_trend = ema_fast > ema_mid
    bear_trend = ema_fast < ema_mid
    confirmed_bull = bull_trend & (ema_mid > ema_slow)
    confirmed_bear = bear_trend & (ema_mid < ema_slow)
    
    # 入场: 快速EMA从下方穿越中速EMA(金叉做多，死叉做空)
    entries_long = (bull_trend & (ema_fast.shift(1) <= ema_mid.shift(1))).astype(int)
    entries_short = (bear_trend & (ema_fast.shift(1) >= ema_mid.shift(1))).astype(int)
    
    return entries_long, entries_short

def trend_follow_exits(close, ema_fast, ema_mid, ema_slow, trailing_atr=None):
    """趋势跟踪出场: 逆穿EMA"""
    exits_long = (ema_fast < ema_mid).astype(int)
    exits_short = (ema_fast > ema_mid).astype(int)
    return exits_long, exits_short

def scan_ema_params(df_train, period='1h'):
    """网格搜索最佳EMA参数组合"""
    train, val, test = split_data(df_train)
    close = train['close']
    high = train['high']
    low = train['low']
    
    results = []
    
    # EMA参数网格
    fast_range = [5, 8, 10, 12, 15]
    mid_range = [20, 30, 50]
    slow_range = [50, 100, 200]
    
    for fast in fast_range:
        for mid in mid_range:
            if fast >= mid:
                continue
            for slow in slow_range:
                if mid >= slow:
                    continue
                
                ema_fast = calc_ema(close, fast)
                ema_mid = calc_ema(close, mid)
                ema_slow = calc_ema(close, slow)
                
                # 只做顺大周期趋势的方向
                bull_trend = ema_fast > ema_mid
                confirmed_bull = bull_trend & (ema_mid > ema_slow)
                confirmed_bear = ~confirmed_bull & (ema_fast < ema_mid)
                
                # 金叉做多
                entries_long = (bull_trend & (ema_fast.shift(1) <= ema_mid.shift(1))).astype(int)
                exits_long = (ema_fast < ema_mid).astype(int)
                
                for sl_atr in [1.0, 1.5, 2.0]:
                    for tp_atr in [2.0, 3.0, 4.0, 5.0]:
                        r = run_backtest(close, high, low, entries_long, exits_long, sl_atr, tp_atr)
                        if r:
                            label = f'LONG EMA({fast}/{mid}/{slow}) SL{sl_atr}_TP{tp_atr}'
                            results.append({
                                'type': 'LONG',
                                'ema': (fast, mid, slow),
                                'sl_atr': sl_atr,
                                'tp_atr': tp_atr,
                                'train': r,
                                'label': label
                            })
    
    # 按PF排序
    results.sort(key=lambda x: x['train']['pf'], reverse=True)
    return results

# ============================================================
# 主流程
# ============================================================
if __name__ == '__main__':
    print('='*70)
    print('纯趋势跟踪 - EMA+ATR 全方位验证')
    print('='*70)
    
    for coin in COINS:
        print(f'\n加载 {coin}...')
        df_5m = load_data(coin)
        
        for freq in ['1h', '4h']:
            df = aggregate(df_5m, freq)
            train, val, test = split_data(df)
            print(f'  {freq}: 训练集 {len(train)}根, 验证集 {len(val)}根, 测试集 {len(test)}根')
            
            results = scan_ema_params(df, freq)
            
            print(f'\n  === {coin} {freq} 训练集 Top10 (按PF排序) ===')
            for r in results[:10]:
                t = r['train']
                pf_str = f"PF={t['pf']:.2f} WR={t['wr']*100:.1f}% Sharpe={t['sharpe']:.2f} DD={t['dd']*100:.1f}% 日均={t['daily']:.1f} 总收益={t['ret']*100:.1f}%"
                passed = '✅' if (t['pf'] >= MIN_PF and t['sharpe'] >= MIN_SHARPE and t['dd'] <= MAX_DD) else '❌'
                print(f"  {passed} {r['label']}: {pf_str}")
            
            # 保存结果
            with open(f'{OUT_DIR}/results_{coin}_{freq}.json', 'w') as f:
                json.dump({coin: [{**r, 'train': dict(r['train'])} for r in results]}, f, default=str, indent=2)
    
    print(f'\n结果已保存到 {OUT_DIR}/')
