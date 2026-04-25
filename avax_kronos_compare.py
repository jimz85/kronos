#!/usr/bin/env python3
"""
AVAX Kronos对比回测 v2（修复cooldown bug）

对比：
  1. 无Kronos：纯趋势信号（RSI<45, ADX>15, 72h持仓，2h cooldown）
  2. 有Kronos：趋势信号 + Kronos偏差<-2%才入场

数据：AVAX_USDT 1h (2021-01 ~ 2026-03, ~5年)
"""

import pandas as pd
import numpy as np
import talib
import warnings
import json
import gzip
from datetime import timedelta

warnings.filterwarnings('ignore')

# ===== 参数 =====
COIN = 'AVAX'
DATA_PATH = '/Users/jimingzhang/Desktop/crypto_data_Pre5m/AVAX_USDT_5m_from_20180101.csv'
OUT_PATH = '/Users/jimingzhang/kronos/market_sense_results/avax_kronos_compare.json'

RSI_ENTRY = 45
ADX_MIN = 15
HOLD_HOURS = 72
COOLDOWN_HOURS = 2  # 修复：加入cooldown防止连续信号
FEE = 0.002
INITIAL_CAPITAL = 10000
KRONOS_BIAS_THRESHOLD = -0.02


# ===== 加载数据 =====
def load_data():
    df = pd.read_csv(DATA_PATH)
    df.columns = [c.lstrip('\ufeff') for c in df.columns]
    df['ts'] = pd.to_datetime(df['timestamp'], unit='ms')
    df = df[df['ts'] >= '2021-01-01']
    df = df.set_index('ts')

    ohlc = df[['open','high','low','close','vol']].resample('1h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'vol': 'sum'
    }).dropna()

    ohlc['rsi'] = talib.RSI(ohlc['close'].values, timeperiod=14)
    ohlc['adx'] = talib.ADX(ohlc['high'].values, ohlc['low'].values, ohlc['close'].values, timeperiod=14)

    return ohlc.dropna()


# ===== 模拟Kronos偏差（基于RSI的近似） =====
def estimate_kronos_bias(rsi):
    if rsi < 25:
        return -0.04
    elif rsi < 35:
        return -0.025
    elif rsi < 45:
        return -0.01
    elif rsi < 55:
        return 0.0
    else:
        return 0.025


# ===== 回测引擎（修复cooldown） =====
def backtest(ohlc, use_kronos=False, name='strategy'):
    capital = INITIAL_CAPITAL
    position = None   # (entry_time, entry_price, size)
    last_exit_time = None  # cooldown tracker
    trades = []
    equity_curve = []

    rsi_arr = ohlc['rsi'].values
    adx_arr = ohlc['adx'].values
    close_arr = ohlc['close'].values

    for i in range(50, len(ohlc)):
        dt = ohlc.index[i]
        rsi = rsi_arr[i]
        adx = adx_arr[i]
        close = close_arr[i]

        kronos_bias = estimate_kronos_bias(rsi) if use_kronos else -999

        # ===== 出场检查 =====
        if position is not None:
            entry_time, entry_price, size = position
            hours_held = (dt - entry_time).total_seconds() / 3600

            # 时间到强制平仓
            if hours_held >= HOLD_HOURS:
                pnl_pct = (close - entry_price) / entry_price - FEE
                capital *= (1 + pnl_pct)
                trades.append({
                    'entry_time': str(entry_time),
                    'exit_time': str(dt),
                    'entry_price': entry_price,
                    'exit_price': close,
                    'pnl_pct': pnl_pct,
                    'hours_held': hours_held,
                    'rsi_entry': rsi_arr[i-1],
                    'adx_entry': adx_arr[i-1],
                    'kronos_bias_entry': kronos_bias if use_kronos else None,
                })
                last_exit_time = dt
                position = None

        # ===== 入场检查（仅当空仓时） =====
        if position is None:
            # Cooldown检查
            if last_exit_time is not None:
                hours_since_exit = (dt - last_exit_time).total_seconds() / 3600
                if hours_since_exit < COOLDOWN_HOURS:
                    equity_curve.append({'dt': dt, 'equity': capital})
                    continue

            # 趋势信号
            if rsi < RSI_ENTRY and adx > ADX_MIN:
                # Kronos过滤
                if use_kronos and kronos_bias >= KRONOS_BIAS_THRESHOLD:
                    pass  # 跳过
                else:
                    size = capital / close
                    position = (dt, close, size)

        equity_curve.append({'dt': dt, 'equity': capital})

    # 强制平仓最后持仓
    if position is not None:
        entry_time, entry_price, size = position
        final_close = close_arr[-1]
        pnl_pct = (final_close - entry_price) / entry_price - FEE
        capital *= (1 + pnl_pct)

    # ===== 统计 =====
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]

    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    wr = len(wins) / len(trades) * 100 if trades else 0
    avg_win = np.mean([t['pnl_pct'] for t in wins]) * 100 if wins else 0
    avg_loss = np.mean([t['pnl_pct'] for t in losses]) * 100 if losses else 0
    wlr = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    eq = pd.Series([e['equity'] for e in equity_curve])
    cummax = eq.cummax()
    drawdown = (eq - cummax) / cummax
    max_dd = drawdown.min() * 100

    years = (ohlc.index[-1] - ohlc.index[0]).total_seconds() / (365.25 * 86400)
    annualized = ((capital / INITIAL_CAPITAL) ** (1/years) - 1) * 100 if years > 0 else 0

    result = {
        'strategy': name,
        'coin': COIN,
        'total_trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(wr, 1),
        'avg_win_pct': round(avg_win, 2),
        'avg_loss_pct': round(avg_loss, 2),
        'wlr': round(wlr, 2),
        'total_return_pct': round(total_return, 1),
        'annualized_pct': round(annualized, 1),
        'max_drawdown_pct': round(max_dd, 1),
        'final_capital': round(capital, 2),
        'years': round(years, 1),
        'avg_hold_hours': round(np.mean([t['hours_held'] for t in trades]), 1) if trades else 0,
    }
    return result, trades


if __name__ == '__main__':
    print('加载AVAX数据...')
    ohlc = load_data()
    print(f'数据: {len(ohlc)} 根1h K线 | {ohlc.index[0].date()} ~ {ohlc.index[-1].date()}')

    print('\n===== 无Kronos baseline =====')
    res_no, trades_no = backtest(ohlc, use_kronos=False, name='no_kronos')
    print(f'  交易次数: {res_no["total_trades"]}')
    print(f'  胜率: {res_no["win_rate"]}%')
    print(f'  总收益: {res_no["total_return_pct"]}%')
    print(f'  年化: {res_no["annualized_pct"]}%')
    print(f'  最大回撤: {res_no["max_drawdown_pct"]}%')

    print('\n===== 有Kronos过滤 =====')
    res_kronos, trades_kronos = backtest(ohlc, use_kronos=True, name='with_kronos')
    print(f'  交易次数: {res_kronos["total_trades"]}')
    print(f'  胜率: {res_kronos["win_rate"]}%')
    print(f'  总收益: {res_kronos["total_return_pct"]}%')
    print(f'  年化: {res_kronos["annualized_pct"]}%')
    print(f'  最大回撤: {res_kronos["max_drawdown_pct"]}%')

    print('\n===== 对比总结 =====')
    delta_return = res_kronos['total_return_pct'] - res_no['total_return_pct']
    trades_lost = res_no['total_trades'] - res_kronos['total_trades']
    print(f'  Kronos过滤掉交易: {trades_lost} 笔')
    print(f'  收益差异: {delta_return:+.1f}%')
    print(f'  胜率变化: {res_kronos["win_rate"] - res_no["win_rate"]:+.1f}%')
    print(f'  Kronos过滤掉交易的胜率:', end=' ')
    kronos_rejected = [t for t in trades_no if estimate_kronos_bias(t['rsi_entry']) >= KRONOS_BIAS_THRESHOLD]
    if kronos_rejected:
        kr_wins = [t for t in kronos_rejected if t['pnl_pct'] > 0]
        print(f'{len(kr_wins)}/{len(kronos_rejected)} = {len(kr_wins)/len(kronos_rejected)*100:.1f}%')
    else:
        print('0笔')

    output = {
        'no_kronos': res_no,
        'with_kronos': res_kronos,
        'delta_return_pct': round(delta_return, 1),
        'trades_lost_to_kronos': trades_lost,
    }
    with gzip.open(OUT_PATH, 'wt') as f:
        json.dump(output, f, indent=2, default=str)
    print(f'\n结果已保存: {OUT_PATH}')
