#!/usr/bin/env python3
"""
SOL 策略研究 - 基于2026-04-17吉总指令

优先级：高
测试内容：
1. SOL RSI<35 72h 回测
2. SOL 突破20日/30日 系统回测
3. Walk-Forward验证
4. 熊市压力测试
手续费: 0.25% + 0.1%滑点
"""

import pandas as pd
import numpy as np
import talib
import warnings
import json
warnings.filterwarnings('ignore')

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OUT_DIR = '/Users/jimax_sense_results'
FEE = 0.0025
SLIPPAGE = 0.001
TOTAL_FEE = FEE + SLIPPAGE  # 0.35%


def load_ohlc(coin):
    path = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    df = pd.read_csv(path)
    df.columns = [c.lstrip('\ufeff') for c in df.columns]
    ts_col = [c for c in df.columns if 'timestamp' in c.lower() or 'datetime' in c.lower()][0]
    df['ts'] = pd.to_datetime(df[ts_col], unit='ms', errors='coerce')
    if df['ts'].isna().all():
        df['ts'] = pd.to_datetime(df[ts_col], errors='coerce')
    df = df.set_index('ts')
    cols = [c for c in ['open', 'high', 'low', 'close', 'vol', 'volume'] if c in df.columns]
    df = df[cols].rename(columns={'volume': 'vol'})
    return df.resample('1h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'vol': 'sum'
    }).dropna()


def run_bt_rsi(ohlc, coin, rsi_lo, rsi_hi, adx_min, hold_h=72, fee=TOTAL_FEE):
    """RSI策略回测"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    rsi = pd.Series(talib.RSI(close.values, timeperiod=14), index=close.index)
    adx = pd.Series(talib.ADX(high.values, low.values, close.values, timeperiod=14), index=close.index)
    
    dates = close.index.tolist()
    capital = 10000
    position = None
    last_exit = None
    trades = []
    
    for i in range(50, len(dates) - hold_h):
        dt = dates[i]
        rsi_val = rsi.iloc[i]
        adx_val = adx.iloc[i]
        close_px = close.iloc[i]
        
        if position is not None:
            entry_time, entry_px = position
            hold_hrs = (dt - entry_time).total_seconds() / 3600
            if hold_hrs >= hold_h:
                ret = (close_px - entry_px) / entry_px - fee
                capital *= (1 + ret)
                trades.append({'ret': ret})
                last_exit = dt
                position = None
        
        if position is None:
            if last_exit is not None and (dt - last_exit).total_seconds() / 3600 < 2:
                continue
            if rsi_lo <= rsi_val < rsi_hi and adx_val > adx_min:
                position = (dt, close_px)
    
    if position is not None:
        ret = (close.iloc[-1] - position[1]) / position[1] - fee
        capital *= (1 + ret)
    
    total_ret = (capital - 10000) / 10000 * 100
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    
    return {
        'coin': coin,
        'strategy': f'RSI {rsi_lo}-{rsi_hi} ADX>{adx_min} {hold_h}h',
        'total_ret': round(total_ret, 1),
        'trades': len(trades),
        'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
        'avg_win': round(np.mean([t['ret'] for t in wins]) * 100, 2) if wins else 0,
        'avg_loss': round(abs(np.mean([t['ret'] for t in losses])) * 100, 2) if losses else 0,
        'fee': fee * 100,
    }


def run_bt_breakout(ohlc, coin, breakout_days, hold_h=72, fee=TOTAL_FEE):
    """突破策略回测"""
    close = ohlc['close']
    high_n = close.rolling(breakout_days * 24).max().shift(1)
    low_n = close.rolling(breakout_days * 24).min().shift(1)
    
    dates = close.index.tolist()
    capital = 10000
    position = None
    trades = []
    
    for i in range(breakout_days * 24 + 10, len(dates) - hold_h):
        dt = dates[i]
        close_px = close.iloc[i]
        prev_close = close.iloc[i-1]
        h_n = high_n.iloc[i]
        l_n = low_n.iloc[i]
        
        if position is not None:
            entry_time, entry_px, pos_type = position
            hold_hrs = (dt - entry_time).total_seconds() / 3600
            if pos_type == 'long':
                ret = (close_px - entry_px) / entry_px - fee
            else:
                ret = (entry_px - close_px) / entry_px - fee
            if hold_hrs >= hold_h:
                capital *= (1 + ret)
                trades.append({'ret': ret, 'type': pos_type})
                position = None
        
        if position is None:
            if close_px > h_n and prev_close <= h_n:
                position = (dt, close_px, 'long')
            elif close_px < l_n and prev_close >= l_n:
                position = (dt, close_px, 'short')
    
    if position is not None:
        entry_time, entry_px, pos_type = position
        close_px = close.iloc[-1]
        if pos_type == 'long':
            ret = (close_px - entry_px) / entry_px - fee
        else:
            ret = (entry_px - close_px) / entry_px - fee
        capital *= (1 + ret)
    
    total_ret = (capital - 10000) / 10000 * 100
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    
    return {
        'coin': coin,
        'strategy': f'突破{breakout_days}日 {hold_h}h',
        'total_ret': round(total_ret, 1),
        'trades': len(trades),
        'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
        'avg_win': round(np.mean([t['ret'] for t in wins]) * 100, 2) if wins else 0,
        'avg_loss': round(abs(np.mean([t['ret'] for t in losses])) * 100, 2) if losses else 0,
        'fee': fee * 100,
    }


def walkforward_bt(ohlc, coin, strategy_fn, strategy_name, train_years=3, val_years=1):
    """Walk-Forward验证"""
    start_date = ohlc.index[0]
    end_date = ohlc.index[-1]
    
    results = []
    val_start = start_date + pd.DateOffset(years=train_years)
    
    while val_start + pd.DateOffset(years=val_years) <= end_date:
        val_end = val_start + pd.DateOffset(years=val_years)
        val_data = ohlc[(ohlc.index >= val_start) & (ohlc.index < val_end)]
        
        if len(val_data) < 3000:
            val_start = val_start + pd.DateOffset(years=1)
            continue
        
        r = strategy_fn(val_data, coin)
        r['period'] = f'{val_start.date()}~{val_end.date()}'
        results.append(r)
        
        val_start = val_start + pd.DateOffset(years=1)
    
    return results


def bear_market_bt(ohlc, coin, strategy_fn, strategy_name):
    """熊市压力测试"""
    windows = [
        ('2021-05-01', '2021-11-01', '2021五月崩盘'),
        ('2022-05-01', '2023-05-01', '2022熊市(含FTX)'),
        ('2023-04-01', '2023-10-01', '2023四月崩盘'),
    ]
    
    results = []
    for start_str, end_str, label in windows:
        start = pd.to_datetime(start_str)
        end = pd.to_datetime(end_str)
        data = ohlc[(ohlc.index >= start) & (ohlc.index < end)]
        if len(data) < 500:
            continue
        r = strategy_fn(data, coin)
        r['label'] = label
        results.append(r)
    
    return results


if __name__ == '__main__':
    coin = 'SOL'
    
    print('=' * 70)
    print(f'SOL 策略研究 - 手续费: {TOTAL_FEE*100:.2f}%')
    print('=' * 70)
    
    # 检查数据
    try:
        ohlc = load_ohlc(coin)
        print(f'\n数据: {ohlc.index[0].date()} ~ {ohlc.index[-1].date()} ({len(ohlc)} bars)')
        years = (ohlc.index[-1] - ohlc.index[0]).total_seconds() / (365.25 * 86400)
        print(f'数据年数: {years:.1f}年')
    except Exception as e:
        print(f'\nSOL数据加载失败: {e}')
        print('SOL数据可能不存在或路径错误')
        exit(1)
    
    all_results = {}
    
    # 1. RSI策略全局回测
    print('\n--- RSI策略全局回测 ---')
    rsi_strategies = [
        (0, 35, 15, 72, 'RSI<35 72h'),
        (0, 45, 15, 72, 'RSI<45 72h'),
        (55, 100, 15, 72, 'RSI>55 追涨 72h'),
        (35, 100, 15, 72, 'RSI>35 追涨 72h'),
        (0, 35, 15, 48, 'RSI<35 48h'),
    ]
    
    for rsi_lo, rsi_hi, adx_min, hold_h, name in rsi_strategies:
        r = run_bt_rsi(ohlc, coin, rsi_lo, rsi_hi, adx_min, hold_h)
        print(f"  {name}: {r['total_ret']:+.1f}% | {r['trades']}笔 | 胜{r['win_rate']}% | 均赢{r['avg_win']}% | 均亏{r['avg_loss']}%")
        all_results[name] = r
    
    # 2. 突破策略全局回测
    print('\n--- 突破策略全局回测 ---')
    for n_days in [10, 20, 30]:
        r = run_bt_breakout(ohlc, coin, n_days)
        print(f"  {n_days}日突破: {r['total_ret']:+.1f}% | {r['trades']}笔 | 胜{r['win_rate']}% | 均赢{r['avg_win']}% | 均亏{r['avg_loss']}%")
        all_results[f'突破{n_days}d'] = r
    
    # 3. Walk-Forward验证（对最优策略）
    print('\n--- Walk-Forward验证 ---')
    
    best_rsi = max([(k, v) for k, v in all_results.items() if 'RSI' in k], 
                   key=lambda x: x[1]['total_ret'], default=(None, None))
    if best_rsi[0]:
        name = best_rsi[0]
        rsi_lo, rsi_hi, adx_min, hold_h = [(l, h, a, t, n) for l, h, a, t, n in rsi_strategies if n == name][0]
        fn = lambda df, c: run_bt_rsi(df, c, rsi_lo, rsi_hi, adx_min, hold_h)
        wf = walkforward_bt(ohlc, coin, fn, name)
        if wf:
            for w in wf:
                print(f"  {name} {w['period']}: {w['total_ret']:+.1f}% | 胜{w['win_rate']}%")
            pos = sum(1 for w in wf if w['total_ret'] > 0)
            avg = np.mean([w['total_ret'] for w in wf])
            print(f"  → WF均: {avg:+.1f}% | {pos}/{len(wf)}窗口正")
            all_results[f'{name}_WF'] = {'windows': wf, 'avg': avg, 'n_pos': pos}
    
    # 突破WF
    for n_days in [20, 30]:
        fn = lambda df, c, nd=n_days: run_bt_breakout(df, c, nd)
        wf = walkforward_bt(ohlc, coin, fn, f'突破{n_days}d')
        if wf:
            for w in wf:
                print(f"  突破{n_days}d {w['period']}: {w['total_ret']:+.1f}% | 胜{w['win_rate']}%")
            pos = sum(1 for w in wf if w['total_ret'] > 0)
            avg = np.mean([w['total_ret'] for w in wf])
            print(f"  → WF均: {avg:+.1f}% | {pos}/{len(wf)}窗口正")
            all_results[f'突破{n_days}d_WF'] = {'windows': wf, 'avg': avg, 'n_pos': pos}
    
    # 4. 熊市压力测试
    print('\n--- 熊市压力测试 ---')
    
    if best_rsi[0]:
        name = best_rsi[0]
        rsi_lo, rsi_hi, adx_min, hold_h = [(l, h, a, t, n) for l, h, a, t, n in rsi_strategies if n == name][0]
        fn = lambda df, c, l=rsi_lo, h=rsi_hi, a=adx_min, t=hold_h: run_bt_rsi(df, c, l, h, a, t)
        br = bear_market_bt(ohlc, coin, fn, name)
        for w in br:
            status = '✅' if w['total_ret'] > 0 else '❌'
            print(f"  {status} {name} {w['label']}: {w['total_ret']:+.1f}%")
    
    # 突破熊市
    for n_days in [20, 30]:
        fn = lambda df, c, nd=n_days: run_bt_breakout(df, c, nd)
        br = bear_market_bt(ohlc, coin, fn, f'突破{n_days}d')
        for w in br:
            status = '✅' if w['total_ret'] > 0 else '❌'
            print(f"  {status} 突破{n_days}d {w['label']}: {w['total_ret']:+.1f}%")
    
    # 保存
    out_path = f'/Users/jimingzhang/kronos/market_sense_results/sol_research.json'
    with open(out_path, 'w') as f:
        json.dump({k: v if not isinstance(v, dict) or 'windows' not in v else 
                   {kk: vv if not isinstance(vv, list) else 
                    [{kk2: vv2 for kk2, vv2 in w.items() if kk2 != 'ret'} for w in vv]
                    for kk, vv in v.items()}
                   for k, v in all_results.items()}, f, indent=2, default=str)
    print(f'\n已保存: {out_path}')
