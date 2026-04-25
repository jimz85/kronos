#!/usr/bin/env python3
"""
多空趋势跟踪 - 历史回测验证
测试参数：RSI<35做多 / RSI>65做空 / ADX>20 / ATR止损1.5x / ATR止盈4.5x / 杠杆3-10x
"""
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'

def load_5m(coin):
    f = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    df = pd.read_csv(f)
    df = df.rename(columns={'datetime_utc': 'timestamp', df.columns[1]: 'dt2'})
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    cols = {c: c.lower() for c in df.columns if c.lower() in ['open','high','low','close','volume']}
    df = df.rename(columns=cols)
    return df[['open','high','low','close','volume']].astype(float)

def calc_rsi(prices, period=14):
    delta = np.diff(prices, prepend=prices[0])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    ag = pd.Series(gains).rolling(period).mean()
    al = pd.Series(losses).rolling(period).mean()
    rs = ag / (al + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_atr(high, low, close, period=14):
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(period).mean()

def calc_adx(high, low, close, period=14):
    high_d = np.diff(high, prepend=high[0])
    low_d = -np.diff(low, prepend=low[0])
    plus_dm = np.where(high_d > low_d, high_d, 0.0)
    minus_dm = np.where(low_d > high_d, low_d, 0.0)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(period).mean()
    plus_di = pd.Series(plus_dm).rolling(period).mean() / atr * 100
    minus_di = pd.Series(minus_dm).rolling(period).mean() / atr * 100
    dx = abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
    adx = dx.rolling(period).mean()
    return adx, plus_di, minus_di

def backtest(df, coin, cfg, timeframe='15min'):
    """回测多空趋势跟踪"""
    # 重采样
    df_tf = df.resample(timeframe).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()
    df_tf.index = df_tf.index.tz_localize(None)
    
    n = len(df_tf)
    if n < 100:
        return pd.DataFrame()
    
    close = df_tf['close'].values
    high = df_tf['high'].values
    low = df_tf['low'].values
    
    # 计算指标
    df_tf['rsi'] = calc_rsi(close, 14)
    df_tf['atr'] = calc_atr(high, low, close, 14)
    adx, plus_di, minus_di = calc_adx(high, low, close, 14)
    df_tf['adx'] = adx
    df_tf['atr_pct'] = df_tf['atr'] / df_tf['close']
    df_tf['atr_ratio'] = df_tf['atr'] / df_tf['atr'].rolling(20).mean()
    df_tf['vol_ratio'] = df_tf['volume'] / df_tf['volume'].rolling(20).mean()
    
    rsi = df_tf['rsi'].values
    adx_vals = df_tf['adx'].values
    atr_vals = df_tf['atr'].values
    atr_ratio = df_tf['atr_ratio'].values
    
    position = 0
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    pos_side = None
    entry_bar = 0
    entry_atr = 0
    fee = 0.0002
    
    trades = []
    
    for i in range(50, n):
        row_close = close[i]
        row_high = high[i]
        row_low = low[i]
        
        if position == 0:
            # 做多信号
            if (rsi[i] < cfg['rsi_buy'] and 
                adx_vals[i] > cfg['adx_min'] and
                atr_ratio[i] > cfg.get('atr_ratio_min', 1.0)):
                entry = row_close * (1 + fee)
                stop = entry - cfg['atr_stop'] * atr_vals[i]
                tp = entry + cfg['atr_tp'] * atr_vals[i]
                position = 1
                entry_price = entry
                stop_loss = stop
                take_profit = tp
                pos_side = 'long'
                entry_bar = i
                entry_atr = atr_vals[i]
            # 做空信号
            elif (rsi[i] > cfg['rsi_sell'] and 
                  adx_vals[i] > cfg['adx_min'] and
                  atr_ratio[i] > cfg.get('atr_ratio_min', 1.0)):
                entry = row_close * (1 - fee)
                stop = entry + cfg['atr_stop'] * atr_vals[i]
                tp = entry - cfg['atr_tp'] * atr_vals[i]
                position = -1
                entry_price = entry
                stop_loss = stop
                take_profit = tp
                pos_side = 'short'
                entry_bar = i
                entry_atr = atr_vals[i]
        else:
            pnl_pct = 0
            reason = ''
            
            if pos_side == 'long':
                if row_low <= stop_loss:
                    pnl_pct = (stop_loss / entry_price - 1) * 100 * cfg['lev']
                    reason = 'stop'
                elif row_high >= take_profit:
                    pnl_pct = (take_profit / entry_price - 1) * 100 * cfg['lev']
                    reason = 'tp'
            else:
                if row_high >= stop_loss:
                    pnl_pct = (entry_price / stop_loss - 1) * 100 * cfg['lev']
                    reason = 'stop'
                elif row_low <= take_profit:
                    pnl_pct = (entry_price / take_profit - 1) * 100 * cfg['lev']
                    reason = 'tp'
            
            if pnl_pct != 0:
                trades.append({
                    'date': df_tf.index[i],
                    'coin': coin,
                    'side': pos_side,
                    'pnl_pct': pnl_pct,
                    'raw_pnl': pnl_pct / cfg['lev'],
                    'leverage': cfg['lev'],
                    'reason': reason,
                    'atr_pct': entry_atr / entry_price * 100,
                    'hours': (df_tf.index[i] - df_tf.index[entry_bar]).total_seconds() / 3600,
                })
                position = 0
    
    return pd.DataFrame(trades)

print("="*70)
print("【多空趋势跟踪回测】15min | RSI+ADX+ATR动态止损止盈")
print("="*70)

# 测试参数
cfg_default = {
    'rsi_buy': 35,
    'rsi_sell': 65,
    'adx_min': 20,
    'atr_stop': 1.5,
    'atr_tp': 4.5,
    'lev': 5,
    'atr_ratio_min': 1.0,
}

print("\n--- 基准参数: RSI<35做多 RSI>65做空 ADX>20 ATR止损1.5x ATR止盈4.5x 杠杆5x ---\n")

for coin in ['BTC', 'ETH', 'AVAX']:
    df = load_5m(coin)
    print(f"\n{'='*50}")
    print(f"{coin} 15min回测")
    print(f"{'='*50}")
    
    t = backtest(df, coin, cfg_default, '15min')
    if len(t) == 0:
        print("  数据不足")
        continue
    
    t['year'] = pd.to_datetime(t['date']).dt.year
    total = t['pnl_pct'].sum()
    wr = (t['pnl_pct'] > 0).mean() * 100
    cum = t['pnl_pct'].cumsum()
    dd = (cum.cummax() - cum).max()
    
    longs = t[t['side']=='long']
    shorts = t[t['side']=='short']
    
    print(f"\n  总计: {len(t)}笔 总{total:+.0f}% 胜率{wr:.0f}% 最大回撤{dd:.0f}%")
    print(f"  多: {len(longs)}笔 {longs['pnl_pct'].sum():+.0f}% (胜率{(longs['pnl_pct']>0).mean()*100:.0f}%)")
    print(f"  空: {len(shorts)}笔 {shorts['pnl_pct'].sum():+.0f}% (胜率{(shorts['pnl_pct']>0).mean()*100:.0f}%)")
    print(f"\n  分年收益:")
    for yr in sorted(t['year'].unique()):
        g = t[t['year']==yr]
        bar = '+' * max(-30, min(30, int(g['pnl_pct'].sum()/2)))
        print(f"    {int(yr)}: {len(g):3d}笔 {g['pnl_pct'].sum():>+7.0f}% {bar}")
    
    # 原因分析
    print(f"\n  出场原因:")
    for reason, grp in t.groupby('reason'):
        print(f"    {reason}: {len(grp)}笔 {grp['pnl_pct'].sum():+.0f}%")

print("\n")
print("="*70)
print("【参数扫描】BTC 15min 最优参数")
print("="*70)

df_btc = load_5m('BTC')

best_params = None
best_score = -999

for rsi_buy in [30, 35, 40]:
    for rsi_sell in [60, 65, 70]:
        for adx_min in [15, 20, 25]:
            for atr_stop in [1.0, 1.5, 2.0]:
                for atr_tp in [3.0, 4.5, 6.0]:
                    for lev in [3, 5, 10]:
                        cfg = {
                            'rsi_buy': rsi_buy,
                            'rsi_sell': rsi_sell,
                            'adx_min': adx_min,
                            'atr_stop': atr_stop,
                            'atr_tp': atr_tp,
                            'lev': lev,
                            'atr_ratio_min': 1.0,
                        }
                        t = backtest(df_btc, 'BTC', cfg, '15min')
                        if len(t) < 10:
                            continue
                        
                        total = t['pnl_pct'].sum()
                        wr = (t['pnl_pct'] > 0).mean() * 100
                        cum = t['pnl_pct'].cumsum()
                        dd = (cum.cummax() - cum).max()
                        t['year'] = pd.to_datetime(t['date']).dt.year
                        r24 = t[t['year']>=2024]['pnl_pct'].sum()
                        
                        # 过滤：胜率>45%, DD<50%, 2024正收益
                        if wr < 45 or dd > 60 or r24 < -20:
                            continue
                        
                        # 综合评分
                        score = total - dd * 0.5
                        if score > best_score:
                            best_score = score
                            best_params = (cfg, len(t), wr, total, dd, r24)

if best_params:
    cfg, n, wr, total, dd, r24 = best_params
    print(f"\n最优参数:")
    print(f"  RSI<{cfg['rsi_buy']} RSI>{cfg['rsi_sell']}")
    print(f"  ADX>{cfg['adx_min']} ATR止损{cfg['atr_stop']}x ATR止盈{cfg['atr_tp']}x")
    print(f"  杠杆{cfg['lev']}x")
    print(f"  → {n}笔 总{total:+.0f}% 胜率{wr:.0f}% DD{dd:.0f}% 2024+{r24:+.0f}%")

print("\n")
print("="*70)
print("【关键问题】做空是否真的有效？")
print("="*70)

# 对比: 只做多 vs 只做空 vs 多空
for coin in ['BTC', 'ETH']:
    df = load_5m(coin)
    
    # 只做多
    cfg_long = cfg_default.copy()
    t_long = []
    for i in range(50, len(df)):
        df_tf = df.resample('15min').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna()
    
    print(f"\n{coin}:")
    print(f"  多空混合: {len(t)}笔 总{total:+.0f}%")
