#!/usr/bin/env python3
"""
AAVE 策略方向验证

数据：AAVE_USDT 5m → 1h (2020-10 ~ 2026-03, 5.5年)
记忆：DeFi类，平均+112%，趋势最强
"""

import pandas as pd
import numpy as np
import talib
import warnings
import gzip
import json
warnings.filterwarnings('ignore')

DATA_PATH = '/Users/jimingzhang/Desktop/crypto_data_Pre5m/AAVE_USDT_5m_from_20180101.csv'
OUT = '/Users/jimingzhang/kronos/market_sense_results/aave_direction.json'


def load_ohlc():
    df = pd.read_csv(DATA_PATH)
    df.columns = [c.lstrip('\ufeff') for c in df.columns]
    df['ts'] = pd.to_datetime(df['timestamp'], unit='ms')
    df = df.set_index('ts')
    df = df[['open', 'high', 'low', 'close', 'vol']]

    ohlc = df.resample('1h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'vol': 'sum'
    }).dropna()

    ohlc['rsi'] = talib.RSI(ohlc['close'].values, timeperiod=14)
    ohlc['adx'] = talib.ADX(ohlc['high'].values, ohlc['low'].values, ohlc['close'].values, timeperiod=14)
    return ohlc.dropna()


def run_bt(ohlc, rsi_lo, rsi_hi, adx_min, hold_h, cooldown_h=2, name=''):
    capital = 10000
    position = None
    last_exit = None
    trades = []

    rsi_arr = ohlc['rsi'].values
    adx_arr = ohlc['adx'].values
    close_arr = ohlc['close'].values

    for i in range(50, len(ohlc)):
        dt = ohlc.index[i]
        rsi, adx, close = rsi_arr[i], adx_arr[i], close_arr[i]

        if position is not None:
            entry_time, entry_px, size = position
            if (dt - entry_time).total_seconds() / 3600 >= hold_h:
                pnl = (close - entry_px) / entry_px - 0.002
                capital *= (1 + pnl)
                trades.append({'entry': str(entry_time), 'exit': str(dt),
                               'pnl': pnl, 'rsi_entry': rsi_arr[i-1], 'adx_entry': adx_arr[i-1]})
                last_exit = dt
                position = None

        if position is None:
            if last_exit is not None and (dt - last_exit).total_seconds() / 3600 < cooldown_h:
                continue
            if rsi_lo <= rsi < rsi_hi and adx > adx_min:
                position = (dt, close, capital / close)

    if position is not None:
        entry_time, entry_px, size = position
        pnl = (close_arr[-1] - entry_px) / entry_px - 0.002
        capital *= (1 + pnl)

    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    avg_win = np.mean([t['pnl'] for t in wins]) * 100 if wins else 0
    avg_loss = np.mean([t['pnl'] for t in losses]) * 100 if losses else 0
    wlr = abs(avg_win / avg_loss) if avg_loss else 0

    years = (ohlc.index[-1] - ohlc.index[0]).total_seconds() / (365.25 * 86400)
    total_ret = (capital - 10000) / 10000 * 100
    ann_ret = ((capital / 10000) ** (1 / years) - 1) * 100 if years > 0 else 0

    eq = [10000]
    for t in trades:
        eq.append(eq[-1] * (1 + t['pnl']))
    eq = pd.Series(eq)
    max_dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100

    return {
        'name': name,
        'total_trades': len(trades),
        'win_rate': round(wr, 1),
        'avg_win_pct': round(avg_win, 2),
        'avg_loss_pct': round(avg_loss, 2),
        'wlr': round(wlr, 2),
        'total_return_pct': round(total_ret, 1),
        'annualized_pct': round(ann_ret, 1),
        'max_drawdown_pct': round(max_dd, 1),
        'years': round(years, 1),
    }, trades


if __name__ == '__main__':
    ohlc = load_ohlc()
    print(f'AAVE 1h: {len(ohlc)} bars | {ohlc.index[0].date()} ~ {ohlc.index[-1].date()}')
    print()

    strategies = [
        ((0, 45), 15, 72, 'A: RSI<45 72h (记忆里的方向)'),
        ((0, 35), 15, 72, 'B: RSI<35 72h (严格抄底)'),
        ((35, 100), 15, 72, 'C: RSI>35 追涨'),
        ((55, 100), 15, 72, 'D: RSI>55 强趋势追涨'),
        ((0, 45), 15, 48, 'E: RSI<45 48h'),
        ((0, 45), 0, 72, 'F: RSI<45 无ADX 72h'),
    ]

    results = []
    for (rsi_lo, rsi_hi), adx_min, hold_h, name in strategies:
        res, trades = run_bt(ohlc, rsi_lo, rsi_hi, adx_min, hold_h, name=name)
        results.append(res)
        print(f'===== {name} =====')
        print(f'  交易: {res["total_trades"]}笔 | 胜率: {res["win_rate"]}%')
        print(f'  总收益: {res["total_return_pct"]}% | 年化: {res["annualized_pct"]}%')
        print(f'  最大回撤: {res["max_drawdown_pct"]}% | WLR: {res["wlr"]}')
        print()

    best = max(results, key=lambda r: r['total_return_pct'])
    print(f'===== 最佳策略: {best["name"]} =====')
    print(f'  总收益: {best["total_return_pct"]}% | 年化: {best["annualized_pct"]}%')

    output = {'results': results, 'period': f'{ohlc.index[0].date()} ~ {ohlc.index[-1].date()}'}
    with gzip.open(OUT, 'wt') as f:
        json.dump(output, f, indent=2, default=str)
    print(f'\n已保存: {OUT}')
