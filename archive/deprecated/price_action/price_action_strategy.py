"""
盘感策略 - 基于各状态真实收益率分布
策略设计：
- strong_uptrend: 追多，12-24h出场
- downtrend: 做空，24-48h出场
- ranging: 观望或超短(6-8h)
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'


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
    return df.resample('1h').agg({'open':'first','high':'max','low':'min','close':'last','vol':'sum'}).dropna()


def get_features(close, high, low, vol):
    rets = close.pct_change().fillna(0)
    df = pd.DataFrame(index=close.index)
    df['rets'] = rets
    df['cum_12h'] = rets.rolling(12).sum()
    df['cum_24h'] = rets.rolling(24).sum()
    df['cum_48h'] = rets.rolling(48).sum()
    df['cum_168h'] = rets.rolling(168).sum()
    df['pos_12h'] = rets.rolling(12).apply(lambda x: (x > 0).mean(), raw=True)
    df['pos_24h'] = rets.rolling(24).apply(lambda x: (x > 0).mean(), raw=True)
    df['pos_48h'] = rets.rolling(48).apply(lambda x: (x > 0).mean(), raw=True)
    return df


def classify(f, idx):
    cum_48 = f['cum_48h'].loc[idx]
    pos_48 = f['pos_48h'].loc[idx]
    cum_168 = f['cum_168h'].loc[idx]
    cum_12 = f['cum_12h'].loc[idx]
    pos_12 = f['pos_12h'].loc[idx]
    cum_24 = f['cum_24h'].loc[idx]
    
    if cum_48 > 0.015 and pos_48 > 0.55 and cum_168 > 0.03:
        return 'strong_uptrend'
    elif cum_48 < -0.015 and pos_48 < 0.45 and cum_168 < -0.03:
        return 'strong_downtrend'
    elif cum_48 > 0.005 and pos_48 > 0.52:
        return 'uptrend'
    elif cum_48 < -0.005 and pos_48 < 0.48:
        return 'downtrend'
    elif cum_12 > 0.005 and pos_12 > 0.55:
        return 'momentum_up'
    elif cum_12 < -0.005 and pos_12 < 0.45:
        return 'momentum_down'
    else:
        return 'ranging'


def backtest(ohlc, coin, coin_edge):
    """基于各币种edge的策略"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    f = get_features(close, high, low, ohlc['vol'])
    dates = f.index.tolist()
    
    capital = 10000
    position = None
    trades = []
    
    # 策略规则（根据各币种edge调整）
    strategy = {
        'strong_uptrend': {'action': 'long', 'min_hold': 12, 'max_hold': 48, 'stop': 0.03},
        'uptrend': {'action': 'long', 'min_hold': 12, 'max_hold': 36, 'stop': 0.02},
        'downtrend': {'action': 'short', 'min_hold': 12, 'max_hold': 48, 'stop': 0.02},
        'strong_downtrend': {'action': 'short', 'min_hold': 12, 'max_hold': 36, 'stop': 0.03},
        'momentum_up': {'action': 'long', 'min_hold': 6, 'max_hold': 24, 'stop': 0.02},
        'momentum_down': {'action': 'short', 'min_hold': 6, 'max_hold': 24, 'stop': 0.02},
        'ranging': None,  # 不交易
    }
    
    for i in range(200, len(dates) - 48):
        dt = dates[i]
        regime = classify(f, dt)
        
        if position is not None:
            entry_time, entry_px, pos_type, strat_name = position
            hold_h = (dt - entry_time).total_seconds() / 3600
            strat = strategy[strat_name]
            close_px = close.loc[dt]
            
            # 平仓条件
            exit_now = False
            
            if pos_type == 'long':
                ret = (close_px - entry_px) / entry_px
                stop_px = entry_px * (1 - strat['stop'])
                if close_px <= stop_px:
                    exit_now = True
                elif hold_h >= strat['min_hold'] and ret > 0.01:
                    exit_now = True
                elif hold_h >= strat['max_hold']:
                    exit_now = True
            elif pos_type == 'short':
                ret = (entry_px - close_px) / entry_px
                stop_px = entry_px * (1 + strat['stop'])
                if close_px >= stop_px:
                    exit_now = True
                elif hold_h >= strat['min_hold'] and ret > 0.01:
                    exit_now = True
                elif hold_h >= strat['max_hold']:
                    exit_now = True
            
            if exit_now:
                net = ret - 0.002
                capital *= (1 + net)
                trades.append({
                    'regime': strat_name, 'ret': net,
                    'gross': ret, 'hold_h': hold_h,
                    'type': pos_type
                })
                position = None
        
        # 开仓
        if position is None:
            strat_cfg = strategy.get(regime)
            if strat_cfg:
                close_px = close.loc[dt]
                position = (dt, close_px, strat_cfg['action'], regime)
    
    # 剩余持仓
    if position is not None:
        dt = dates[-1]
        close_px = close.loc[dt]
        entry_time, entry_px, pos_type, strat_name = position
        hold_h = (dt - entry_time).total_seconds() / 3600
        if pos_type == 'long':
            ret = (close_px - entry_px) / entry_px
        else:
            ret = (entry_px - close_px) / entry_px
        capital *= (1 + ret - 0.002)
    
    total_ret = (capital - 10000) / 10000 * 100
    
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    
    return {
        'coin': coin,
        'total_ret': round(total_ret, 1),
        'total_trades': len(trades),
        'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
        'avg_win': round(np.mean([t['ret'] for t in wins]) * 100, 2) if wins else 0,
        'avg_loss': round(np.mean([t['ret'] for t in losses]) * 100, 2) if losses else 0,
        'by_regime': {r: len([t for t in trades if t['regime'] == r]) for r in set(t['regime'] for t in trades)},
    }


def walkforward(ohlc, coin, val_years=1):
    """Walk-Forward验证"""
    start_date = ohlc.index[0]
    end_date = ohlc.index[-1]
    train_end = start_date + pd.DateOffset(years=3)
    
    results = []
    val_start = train_end
    
    while val_start + pd.DateOffset(years=val_years) <= end_date:
        val_end = val_start + pd.DateOffset(years=val_years)
        val_data = ohlc[(ohlc.index >= val_start) & (ohlc.index < val_end)]
        
        if len(val_data) < 3000:
            val_start = val_start + pd.DateOffset(years=val_years)
            continue
        
        r = backtest(val_data, coin, None)
        r['period'] = f'{val_start.date()}~{val_end.date()}'
        results.append(r)
        
        val_start = val_start + pd.DateOffset(years=val_years)
    
    return results


if __name__ == '__main__':
    coins = ['BTC', 'ETH', 'AVAX', 'DOT']
    
    print('=' * 70)
    print('盘感策略 Walk-Forward 验证')
    print('=' * 70)
    
    for coin in coins:
        ohlc = load_ohlc(coin)
        print(f'\n{coin}: {ohlc.index[0].date()} ~ {ohlc.index[-1].date()} ({len(ohlc)} bars)')
        
        wf_results = walkforward(ohlc, coin)
        
        for r in wf_results:
            print(f"  {r['period']}: 收益{r['total_ret']:+.1f}% | {r['total_trades']}笔 | 胜率{r['win_rate']}% | "
                  f"均赢{r['avg_win']}% | 均亏{r['avg_loss']}% | {r['by_regime']}")
        
        if wf_results:
            avg_ret = np.mean([r['total_ret'] for r in wf_results])
            positive = sum(1 for r in wf_results if r['total_ret'] > 0)
            print(f'  → 平均: {avg_ret:+.1f}% | {positive}/{len(wf_results)}窗口正')
