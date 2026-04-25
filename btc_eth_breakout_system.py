#!/usr/bin/env python3
"""
BTC/ETH 20日突破系统开发

基于2026-04-17吉总指令：
- P0最高优先级
- 直接复用AVAX 20日突破代码框架
- 针对BTC/ETH波动率特性调整参数
- 必须通过L1-L3验证

核心参数（基于AVAX验证结果）：
- 突破周期：20日（AVAX WF验证通过）
- 持仓：72h
- 止损：5%
- 不设固定止盈（追踪止损）
- 手续费：0.40%（保守）
"""

import pandas as pd
import numpy as np
import talib
import warnings
import json
warnings.filterwarnings('ignore')

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OUT_DIR = '/Users/jimingzhang/kronos/market_sense_results'

# 保守手续费（吉总指令）
FEE = 0.0025
SLIPPAGE = 0.0015
CONSERVATIVE_FEE = 0.004  # 0.40%
TOTAL_FEE = CONSERVATIVE_FEE  # 用保守费率


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


def backtest_breakout(ohlc, coin, breakout_days=20, hold_h=72, fee=TOTAL_FEE, stop_pct=0.05):
    """
    BTC/ETH 20日突破回测
    - 做多：突破20日高点
    - 做空：跌破20日低点（可选，本测试只测做多）
    - 止损：5%
    - 持仓：72h
    - 手续费：0.40%保守
    """
    close = ohlc['close']
    high_arr = ohlc['high']
    low_arr = ohlc['low']
    
    high_n = close.rolling(breakout_days * 24).max().shift(1)
    low_n = close.rolling(breakout_days * 24).min().shift(1)
    
    dates = close.index.tolist()
    
    capital = 10000
    position = None
    trades = []
    equity_curve = [10000]
    
    for i in range(breakout_days * 24 + 10, len(dates) - hold_h):
        dt = dates[i]
        close_px = close.iloc[i]
        prev_close = close.iloc[i-1]
        h_n = high_n.iloc[i]
        l_n = low_n.iloc[i]
        
        # 持仓管理
        if position is not None:
            entry_time, entry_px, pos_type = position
            hold_hrs = (dt - entry_time).total_seconds() / 3600
            
            if pos_type == 'long':
                ret = (close_px - entry_px) / entry_px - fee
                stop_hit = (close_px < entry_px * (1 - stop_pct))
                time_hit = hold_hrs >= hold_h
                if stop_hit or time_hit:
                    capital *= (1 + ret)
                    trades.append({'ret': ret, 'hold': hold_hrs, 'type': 'long', 'stop': 'stop' if stop_hit else 'time'})
                    equity_curve.append(capital)
                    position = None
            elif pos_type == 'short':
                ret = (entry_px - close_px) / entry_px - fee
                stop_hit = (close_px > entry_px * (1 + stop_pct))
                time_hit = hold_hrs >= hold_h
                if stop_hit or time_hit:
                    capital *= (1 + ret)
                    trades.append({'ret': ret, 'hold': hold_hrs, 'type': 'short', 'stop': 'stop' if stop_hit else 'time'})
                    equity_curve.append(capital)
                    position = None
        
        # 开仓：仅做多（测试结果显示BTC/ETH做空长期负收益）
        if position is None:
            if close_px > h_n and prev_close <= h_n:
                position = (dt, close_px, 'long')
    
    if position is not None:
        entry_time, entry_px, pos_type = position
        close_px = close.iloc[-1]
        if pos_type == 'long':
            ret = (close_px - entry_px) / entry_px - fee
        else:
            ret = (entry_px - close_px) / entry_px - fee
        capital *= (1 + ret)
        equity_curve.append(capital)
    
    total_ret = (capital - 10000) / 10000 * 100
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    avg_win = np.mean([t['ret'] for t in wins]) * 100 if wins else 0
    avg_loss = abs(np.mean([t['ret'] for t in losses])) * 100 if losses else 0
    rr_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    
    # 最大回撤
    peak = equity_curve[0]
    max_dd = 0
    for c in equity_curve:
        if c > peak:
            peak = c
        dd = (peak - c) / peak * 100
        if dd > max_dd:
            max_dd = dd
    
    # 统计止损触发率
    stop_triggers = sum(1 for t in trades if t.get('stop') == 'stop')
    time_triggers = sum(1 for t in trades if t.get('stop') == 'time')
    
    return {
        'coin': coin,
        'breakout_days': breakout_days,
        'total_ret': round(total_ret, 1),
        'trades': len(trades),
        'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'rr_ratio': round(rr_ratio, 2),
        'max_drawdown': round(max_dd, 1),
        'stop_triggers': stop_triggers,
        'time_triggers': time_triggers,
        'stop_rate': round(stop_triggers / len(trades) * 100, 1) if trades else 0,
        'fee': fee * 100,
    }


def walkforward(ohlc, coin, breakout_days=20, hold_h=72, fee=TOTAL_FEE):
    """Walk-Forward验证"""
    start_date = ohlc.index[0]
    end_date = ohlc.index[-1]
    
    results = []
    val_start = start_date + pd.DateOffset(years=3)
    
    while val_start + pd.DateOffset(years=1) <= end_date:
        val_end = val_start + pd.DateOffset(years=1)
        val_data = ohlc[(ohlc.index >= val_start) & (ohlc.index < val_end)]
        
        if len(val_data) < 3000:
            val_start = val_start + pd.DateOffset(years=1)
            continue
        
        r = backtest_breakout(val_data, coin, breakout_days, hold_h, fee)
        r['period'] = f'{val_start.date()}~{val_end.date()}'
        results.append(r)
        
        val_start = val_start + pd.DateOffset(years=1)
    
    return results


def stress_test(ohlc, coin, breakout_days=20, hold_h=72, fee=TOTAL_FEE):
    """极端行情压力测试"""
    windows = [
        ('2020-02-01', '2020-08-01', '2020新冠崩盘'),
        ('2021-05-01', '2021-11-01', '2021五月崩盘'),
        ('2022-05-01', '2023-05-01', '2022熊市(含FTX)'),
        ('2022-11-01', '2023-06-01', 'FTX崩盘专项'),
        ('2023-04-01', '2023-10-01', '2023四月崩盘'),
    ]
    
    results = []
    for start_str, end_str, name in windows:
        start = pd.to_datetime(start_str)
        end = pd.to_datetime(end_str)
        data = ohlc[(ohlc.index >= start) & (ohlc.index < end)]
        if len(data) < 500:
            continue
        r = backtest_breakout(data, coin, breakout_days, hold_h, fee)
        r['window'] = name
        results.append(r)
    
    return results


def both_sides_test(ohlc, coin, breakout_days=20, hold_h=72, fee=TOTAL_FEE):
    """双向测试：做多+做空 vs 只做多"""
    # 只做多
    long_only = backtest_breakout(ohlc, coin, breakout_days, hold_h, fee)
    
    # 双向（重新跑）
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
            if hold_hrs >= hold_h:
                if pos_type == 'long':
                    ret = (close_px - entry_px) / entry_px - fee
                else:
                    ret = (entry_px - close_px) / entry_px - fee
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
    
    both_ret = (capital - 10000) / 10000 * 100
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    long_trades = [t for t in trades if t['type'] == 'long']
    short_trades = [t for t in trades if t['type'] == 'short']
    
    return {
        'both_sides_ret': round(both_ret, 1),
        'long_only_ret': round(long_only['total_ret'], 1),
        'improvement': round(both_ret - long_only['total_ret'], 1),
        'long_trades': len(long_trades),
        'short_trades': len(short_trades),
        'long_win_rate': round(len([t for t in long_trades if t['ret']>0]) / len(long_trades) * 100, 1) if long_trades else 0,
        'short_win_rate': round(len([t for t in short_trades if t['ret']>0]) / len(short_trades) * 100, 1) if short_trades else 0,
    }


if __name__ == '__main__':
    print('=' * 70)
    print('BTC/ETH 20日突破系统 - P0开发任务')
    print(f'手续费: {TOTAL_FEE*100:.2f}% (保守费率)')
    print('=' * 70)
    
    all_results = {}
    
    for coin in ['BTC', 'ETH']:
        print(f'\n\n{"="*70}')
        print(f'{coin} 20日突破系统')
        print('=' * 70)
        
        try:
            ohlc = load_ohlc(coin)
            years = (ohlc.index[-1] - ohlc.index[0]).total_seconds() / (365.25 * 86400)
            print(f'数据: {ohlc.index[0].date()} ~ {ohlc.index[-1].date()} ({years:.1f}年)')
        except Exception as e:
            print(f'数据加载失败: {e}')
            continue
        
        # L1: 全周期回测
        print('\n[L1] 全周期回测（2020-2026）')
        data = ohlc[ohlc.index >= '2020-01-01']
        r = backtest_breakout(data, coin)
        s = '✅' if r['total_ret'] > 0 else '❌'
        print(f'  {s} 总收益: {r["total_ret"]:+.1f}% | 交易: {r["trades"]}笔 | 胜率: {r["win_rate"]}%')
        print(f'     均赢: {r["avg_win"]:.2f}% | 均亏: {r["avg_loss"]:.2f}% | 盈亏比: {r["rr_ratio"]}')
        print(f'     最大回撤: -{r["max_drawdown"]}% | 止损触发: {r["stop_rate"]}%({r["stop_triggers"]}笔)')
        all_results[f'{coin}_L1'] = r
        
        # L2: Walk-Forward
        print(f'\n[L2] Walk-Forward验证')
        wf = walkforward(ohlc, coin)
        if wf:
            for w in wf:
                s = '✅' if w['total_ret'] > 0 else '❌'
                print(f'  {s} {w["period"]}: {w["total_ret"]:+.1f}% | 胜{w["win_rate"]}% | 盈亏比{w["rr_ratio"]} | 回撤-{w["max_drawdown"]}%')
            pos = sum(1 for w in wf if w['total_ret'] > 0)
            avg = np.mean([w['total_ret'] for w in wf])
            print(f'  → {pos}/{len(wf)}窗口正 | 均{avg:+.1f}%')
            all_results[f'{coin}_WF'] = {'windows': wf, 'n_pos': pos, 'n_total': len(wf), 'avg': avg}
        
        # L3: 压力测试
        print(f'\n[L3] 极端行情压力测试')
        st = stress_test(ohlc, coin)
        for w in st:
            s = '✅' if w['total_ret'] > 0 else '❌'
            print(f'  {s} {w["window"]}: {w["total_ret"]:+.1f}% | 胜{w["win_rate"]}% | 止损触发{w["stop_rate"]}%')
        pos = sum(1 for w in st if w['total_ret'] > 0)
        avg = np.mean([w['total_ret'] for w in st])
        worst = min(w['total_ret'] for w in st)
        print(f'  → {pos}/{len(st)}窗正 | 均{avg:+.1f}% | 最差{worst:+.1f}%')
        all_results[f'{coin}_stress'] = {'windows': st, 'n_pos': pos, 'avg': avg, 'worst': worst}
        
        # 双向测试
        print(f'\n[双向] 做多+做空 vs 只做多')
        bt = both_sides_test(ohlc, coin)
        print(f'  只做多: {bt["long_only_ret"]:+.1f}%')
        print(f'  双向:   {bt["both_sides_ret"]:+.1f}% (差异: {bt["improvement"]:+.1f}%)')
        print(f'  多头: {bt["long_trades"]}笔 胜率{bt["long_win_rate"]}%')
        print(f'  空头: {bt["short_trades"]}笔 胜率{bt["short_win_rate"]}%')
        all_results[f'{coin}_both'] = bt
        
        # 综合结论
        l1_pass = r['total_ret'] > 0
        wf_pass = wf and sum(1 for w in wf if w['total_ret'] > 0) == len(wf)
        st_pass = pos >= len(st) * 0.6 and worst > -30
        
        print(f'\n结论: L1:{"✅" if l1_pass else "❌"} L2:{"✅" if wf_pass else "❌"} L3:{"✅" if st_pass else "❌"}')
        if l1_pass and wf_pass and st_pass:
            print(f'  → {coin} 20日突破系统通过L1-L3验证！建议实盘待命')
        elif l1_pass and st_pass:
            print(f'  → {coin} 通过L1/L3，但L2需要更多数据验证')
        else:
            print(f'  → {coin} 未通过验证，需调整参数')
    
    # 汇总
    print('\n\n' + '=' * 70)
    print('汇总对比')
    print('=' * 70)
    print(f'手续费: {TOTAL_FEE*100:.2f}%')
    print()
    
    for coin in ['BTC', 'ETH']:
        r = all_results.get(f'{coin}_L1', {})
        wf = all_results.get(f'{coin}_WF', {})
        bt = all_results.get(f'{coin}_both', {})
        
        if not r:
            continue
        
        print(f'{coin}:')
        print(f'  L1: {r.get("total_ret","?"):+.0f}% | {r.get("win_rate","?"):.0f}%WR | 盈亏比{r.get("rr_ratio","?"):.1f}')
        print(f'  WF: {wf.get("n_pos","?")}/{wf.get("n_total","?")}窗正 | 均{wf.get("avg","?"):+.0f}%')
        if bt:
            print(f'  双向: {bt.get("both_sides_ret","?"):+.0f}% vs 只做多{bt.get("long_only_ret","?"):+.0f}%')
        print()
    
    # 保存
    out_path = f'{OUT_DIR}/btc_eth_breakout_results.json'
    with open(out_path, 'w') as f:
        json.dump({k: v if not isinstance(v, dict) or 'windows' not in v
                   else {kk: vv if not isinstance(vv, list) else
                         [{kk2: vv2 for kk2, vv2 in w.items() if kk2 != 'ret'} for w in vv]
                         for kk, vv in v.items()}
                   for k, v in all_results.items()}, f, indent=2, default=str)
    print(f'已保存: {out_path}')
