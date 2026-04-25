#!/usr/bin/env python3
"""
AVAX 突破系统对比测试：20日 vs 30日 vs 10日 高点突破

基于2026-04-17吉总指令：48小时内完成对比测试
验证：胜率、盈亏比、最大回撤
手续费：0.25% + 0.1%滑点
"""

import pandas as pd
import numpy as np
import talib
import warnings
import json
warnings.filterwarnings('ignore')

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OUT_DIR = '/Users/jimingzhang/kronos/market_sense_results'
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


def backtest_breakout_n(ohlc, coin, breakout_days, hold_h=72, stop_pct=0.05):
    """
    N日突破回测：
    - 做多：突破N日高点
    - 做空：跌破N日低点
    - 止损：5%
    - 持仓：72h
    手续费：0.25% + 0.1%滑点
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
                ret = (close_px - entry_px) / entry_px - TOTAL_FEE
                if ret < -stop_pct or hold_hrs >= hold_h:
                    capital *= (1 + ret)
                    trades.append({'ret': ret, 'hold': hold_hrs, 'type': 'long'})
                    position = None
            elif pos_type == 'short':
                ret = (entry_px - close_px) / entry_px - TOTAL_FEE
                if ret < -stop_pct or hold_hrs >= hold_h:
                    capital *= (1 + ret)
                    trades.append({'ret': ret, 'hold': hold_hrs, 'type': 'short'})
                    position = None
        
        # 开仓
        if position is None:
            # 突破做多
            if close_px > h_n and prev_close <= h_n:
                position = (dt, close_px, 'long')
            # 跌破做空
            elif close_px < l_n and prev_close >= l_n:
                position = (dt, close_px, 'short')
    
    if position is not None:
        entry_time, entry_px, pos_type = position
        close_px = close.iloc[-1]
        if pos_type == 'long':
            ret = (close_px - entry_px) / entry_px - TOTAL_FEE
        else:
            ret = (entry_px - close_px) / entry_px - TOTAL_FEE
        capital *= (1 + ret)
    
    total_ret = (capital - 10000) / 10000 * 100
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    avg_win = np.mean([t['ret'] for t in wins]) * 100 if wins else 0
    avg_loss = abs(np.mean([t['ret'] for t in losses])) * 100 if losses else 0
    rr_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    
    # 最大回撤
    capital_curve = [10000]
    cap = 10000
    for t in trades:
        cap *= (1 + t['ret'])
        capital_curve.append(cap)
    peak = capital_curve[0]
    max_dd = 0
    for c in capital_curve:
        if c > peak:
            peak = c
        dd = (peak - c) / peak * 100
        if dd > max_dd:
            max_dd = dd
    
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
    }


def walkforward_n(ohlc, coin, breakout_days, hold_h=72, train_years=3, val_years=1):
    """Walk-Forward验证"""
    start_date = ohlc.index[0]
    end_date = ohlc.index[-1]
    
    results = []
    val_start = start_date + pd.DateOffset(years=train_years)
    window_idx = 0
    
    while val_start + pd.DateOffset(years=val_years) <= end_date:
        val_end = val_start + pd.DateOffset(years=val_years)
        val_data = ohlc[(ohlc.index >= val_start) & (ohlc.index < val_end)]
        
        if len(val_data) < 3000:
            val_start = val_start + pd.DateOffset(years=1)
            window_idx += 1
            continue
        
        r = backtest_breakout_n(val_data, coin, breakout_days, hold_h)
        r['period'] = f'{val_start.date()}~{val_end.date()}'
        results.append(r)
        
        val_start = val_start + pd.DateOffset(years=1)
        window_idx += 1
    
    return results


def bear_market_n(ohlc, coin, breakout_days):
    """熊市窗口测试"""
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
        r = backtest_breakout_n(data, coin, breakout_days)
        r['label'] = label
        results.append(r)
    return results


if __name__ == '__main__':
    coin = 'AVAX'
    ohlc = load_ohlc(coin)
    print(f'数据: {ohlc.index[0].date()} ~ {ohlc.index[-1].date()} ({len(ohlc)} bars)')
    print(f'手续费: {TOTAL_FEE*100:.2f}% ({FEE*100:.2f}% fee + {SLIPPAGE*100:.1f}% slippage)')
    print()
    
    # 全局回测：10/20/30日突破
    print('=' * 70)
    print('AVAX 突破系统全局回测')
    print('=' * 70)
    
    all_results = {}
    for n_days in [10, 20, 30]:
        print(f'\n--- {n_days}日突破 ---')
        r = backtest_breakout_n(ohlc, coin, n_days)
        print(f"  总收益: {r['total_ret']:+.1f}% | 交易: {r['trades']}笔 | 胜率: {r['win_rate']}%")
        print(f"  均赢: {r['avg_win']:.2f}% | 均亏: {r['avg_loss']:.2f}% | 盈亏比: {r['rr_ratio']}")
        print(f"  最大回撤: -{r['max_drawdown']:.1f}%")
        all_results[f'{n_days}d'] = r
    
    # Walk-Forward对比
    print('\n' + '=' * 70)
    print('Walk-Forward验证')
    print('=' * 70)
    
    wf_results = {}
    for n_days in [10, 20, 30]:
        print(f'\n--- {n_days}日突破 Walk-Forward ---')
        wf = walkforward_n(ohlc, coin, n_days)
        if not wf:
            print(f'  数据不足，无法做Walk-Forward')
            wf_results[f'{n_days}d'] = None
            continue
        
        for w in wf:
            print(f"  {w['period']}: {w['total_ret']:+.1f}% | 胜{w['win_rate']}% | 盈亏比{w['rr_ratio']} | 回撤-{w['max_drawdown']}%")
        
        avg = np.mean([w['total_ret'] for w in wf])
        pos = sum(1 for w in wf if w['total_ret'] > 0)
        avg_dd = np.mean([w['max_drawdown'] for w in wf])
        print(f"  → 均: {avg:+.1f}% | {pos}/{len(wf)}窗口正 | 平均回撤-{avg_dd:.1f}%")
        wf_results[f'{n_days}d'] = wf
    
    # 熊市测试
    print('\n' + '=' * 70)
    print('熊市压力测试')
    print('=' * 70)
    
    bear_results = {}
    for n_days in [10, 20, 30]:
        print(f'\n--- {n_days}日突破 熊市测试 ---')
        br = bear_market_n(ohlc, coin, n_days)
        for w in br:
            status = '✅' if w['total_ret'] > 0 else '❌'
            print(f"  {status} {w['label']}: {w['total_ret']:+.1f}% | 胜{w['win_rate']}% | 盈亏比{w['rr_ratio']}")
        
        positive = sum(1 for w in br if w['total_ret'] > 0)
        avg = np.mean([w['total_ret'] for w in br])
        bear_results[f'{n_days}d'] = {'windows': br, 'n_pos': positive, 'avg': avg}
    
    # 最终汇总
    print('\n' + '=' * 70)
    print('AVAX 突破系统对比总结')
    print('=' * 70)
    
    print(f'\n手续费: {TOTAL_FEE*100:.2f}% (0.25% fee + 0.1% slippage)')
    print()
    
    for n_days in [10, 20, 30]:
        gr = all_results.get(f'{n_days}d', None)
        wf = wf_results.get(f'{n_days}d', None)
        br = bear_results.get(f'{n_days}d', None)
        
        if gr is None:
            continue
        
        wf_summary = ''
        if wf:
            pos = sum(1 for w in wf if w['total_ret'] > 0)
            avg_wf = np.mean([w['total_ret'] for w in wf])
            wf_summary = f'WF {pos}/{len(wf)}窗({avg_wf:+.0f}%)'
        else:
            wf_summary = 'WF 无数据'
        
        bear_summary = ''
        if br:
            bear_summary = f'熊市{br["n_pos"]}/{len(br["windows"])}窗({br["avg"]:+.0f}%)'
        else:
            bear_summary = '熊市 未测'
        
        print(f'{n_days}日: 全局{r["total_ret"]:+.0f}% | {gr["win_rate"]}%WR | 盈亏比{gr["rr_ratio"]} | 回撤-{gr["max_drawdown"]}%')
        print(f'       {wf_summary} | {bear_summary}')
    
    # 推荐
    print('\n' + '=' * 70)
    print('推荐')
    print('=' * 70)
    
    # 简单评分
    scores = {}
    for n_days in [10, 20, 30]:
        gr = all_results.get(f'{n_days}d', None)
        wf = wf_results.get(f'{n_days}d', None)
        br = bear_results.get(f'{n_days}d', None)
        
        if gr is None:
            continue
        
        score = 0
        # 全局收益
        score += max(0, gr['total_ret']) * 0.2
        # 盈亏比
        score += gr['rr_ratio'] * 10
        # Walk-Forward
        if wf:
            wf_pos = sum(1 for w in wf if w['total_ret'] > 0)
            score += (wf_pos / len(wf)) * 30 if len(wf) >= 2 else 0
        # 熊市
        if br:
            score += (br['n_pos'] / len(br['windows'])) * 20
        # 最大回撤惩罚
        score -= gr['max_drawdown'] * 0.1
        
        scores[f'{n_days}d'] = round(score, 1)
    
    best = max(scores.items(), key=lambda x: x[1])
    print(f'综合评分: ' + ' | '.join([f'{k}: {v}分' for k, v in sorted(scores.items(), key=lambda x: -x[1])]))
    print(f'推荐: {best[0]} (评分{best[1]}分)')
    
    # 保存结果
    out = {
        'coin': coin,
        'fee': TOTAL_FEE,
        'global_results': all_results,
        'walkforward_results': {k: [{'period': w['period'], 'total_ret': w['total_ret'], 
                                      'win_rate': w['win_rate'], 'rr_ratio': w['rr_ratio'],
                                      'max_drawdown': w['max_drawdown']} for w in v] 
                                if v else None for k, v in wf_results.items()},
        'bear_results': {k: {'n_pos': v['n_pos'], 'avg': v['avg'],
                            'windows': [{'label': w['label'], 'total_ret': w['total_ret'],
                                        'win_rate': w['win_rate'], 'rr_ratio': w['rr_ratio']} 
                                       for w in v['windows']]}
                        for k, v in bear_results.items()},
        'scores': scores,
        'recommended': best[0]
    }
    
    out_path = f'{OUT_DIR}/avax_breakout_compare.json'
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    print(f'\n已保存: {out_path}')
