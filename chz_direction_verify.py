#!/usr/bin/env python3
"""
CHZ 策略方向验证
"""

import pandas as pd
import numpy as np
import talib
import warnings
import gzip
import json
warnings.filterwarnings('ignore')

DATA_PATH = '/Users/jimingzhang/Desktop/crypto_data_Pre5m/CHZ_USDT_5m_from_20180101.csv'
OUT = '/Users/jimingzhang/kronos/market_sense_results/chz_direction.json'


def load_ohlc():
    df = pd.read_csv(DATA_PATH)
    df.columns = [c.lstrip('\ufeff') for c in df.columns]
    ts_col = [c for c in df.columns if 'timestamp' in c.lower()][0]
    df['ts'] = pd.to_datetime(df[ts_col], unit='ms')
    df = df.set_index('ts')
    df = df[['open', 'high', 'low', 'close', 'vol']]
    ohlc = df.resample('1h').agg({'open':'first','high':'max','low':'min','close':'last','vol':'sum'}).dropna()
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
                trades.append({'pnl': pnl})
                last_exit = dt
                position = None
        if position is None:
            if last_exit is not None and (dt - last_exit).total_seconds() / 3600 < cooldown_h:
                continue
            if rsi_lo <= rsi < rsi_hi and adx > adx_min:
                position = (dt, close, capital / close)
    if position is not None:
        pnl = (close_arr[-1] - position[1]) / position[1] - 0.002
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
        'name': name, 'total_trades': len(trades), 'win_rate': round(wr, 1),
        'avg_win_pct': round(avg_win, 2), 'avg_loss_pct': round(avg_loss, 2),
        'wlr': round(wlr, 2), 'total_return_pct': round(total_ret, 1),
        'annualized_pct': round(ann_ret, 1), 'max_drawdown_pct': round(max_dd, 1),
        'years': round(years, 1),
    }


if __name__ == '__main__':
    ohlc = load_ohlc()
    print(f'CHZ 1h: {len(ohlc)} bars | {ohlc.index[0].date()} ~ {ohlc.index[-1].date()}')
    strategies = [
        ((0, 45), 15, 72, 'A: RSI<45 72h'),
        ((0, 35), 15, 72, 'B: RSI<35 72h'),
        ((55, 100), 15, 72, 'C: RSI>55 追涨'),
        ((0, 45), 15, 48, 'D: RSI<45 48h'),
        ((35, 100), 15, 72, 'E: RSI>35 追涨'),
        ((0, 45), 0, 72, 'F: RSI<45 无ADX 72h'),
    ]
    results = []
    for (rsi_lo, rsi_hi), adx_min, hold_h, name in strategies:
        res = run_bt(ohlc, rsi_lo, rsi_hi, adx_min, hold_h, name=name)
        results.append(res)
        print(f'[{name}] {res["total_trades"]}笔 | 胜率{res["win_rate"]}% | 总{res["total_return_pct"]}% | 年化{res["annualized_pct"]}% | MDD{res["max_drawdown_pct"]}%')
    best = max(results, key=lambda r: r['total_return_pct'])
    print(f'最佳: {best["name"]} 总{best["total_return_pct"]}% 年化{best["annualized_pct"]}%')
    with gzip.open(OUT, 'wt') as f:
        json.dump({'results': results, 'period': f'{ohlc.index[0].date()} ~ {ohlc.index[-1].date()}'}, f, indent=2, default=str)
    print(f'已保存: {OUT}')
