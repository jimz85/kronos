"""
盘感策略回测 - 验证纯价格行为判断是否有效

策略逻辑：
- strong_uptrend: 持仓不动，止损-3%
- strong_downtrend: 空仓
- ranging_up/ranging_down: 快进快出（6-12h），止损-2%
- ranging: 观望
- 小趋势反向时不追

验证：各市场状态下的真实收益
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
    df['cum_12h'] = rets.rolling(12).sum()
    df['cum_24h'] = rets.rolling(24).sum()
    df['cum_48h'] = rets.rolling(48).sum()
    df['cum_168h'] = rets.rolling(168).sum()
    df['pos_12h'] = rets.rolling(12).apply(lambda x: (x > 0).mean(), raw=True)
    df['pos_24h'] = rets.rolling(24).apply(lambda x: (x > 0).mean(), raw=True)
    df['pos_48h'] = rets.rolling(48).apply(lambda x: (x > 0).mean(), raw=True)
    df['std_48h'] = rets.rolling(48).std()
    df['vol_ratio'] = vol / vol.rolling(24).mean()
    return df


def classify(f, idx):
    cum_48 = f['cum_48h'].loc[idx]
    pos_48 = f['pos_48h'].loc[idx]
    cum_168 = f['cum_168h'].loc[idx]
    cum_12 = f['cum_12h'].loc[idx]
    pos_12 = f['pos_12h'].loc[idx]
    
    if cum_48 > 0.015 and pos_48 > 0.55 and cum_168 > 0.03:
        return 'strong_uptrend'
    elif cum_48 < -0.015 and pos_48 < 0.45 and cum_168 < -0.03:
        return 'strong_downtrend'
    elif cum_48 > 0.005 and pos_48 > 0.52:
        return 'uptrend'
    elif cum_48 < -0.005 and pos_48 < 0.48:
        return 'downtrend'
    elif cum_12 > 0.005 and pos_12 > 0.55:
        return 'ranging_up'
    elif cum_12 < -0.005 and pos_12 < 0.45:
        return 'ranging_down'
    else:
        return 'ranging'


def backtest_regime_strategy(ohlc, coin, train_years=3, val_years=1):
    """Walk-Forward回测"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    vol = ohlc['vol']
    
    f = get_features(close, high, low, vol)
    
    results = []
    start_date = ohlc.index[0]
    end_date = ohlc.index[-1]
    
    val_start = start_date + pd.DateOffset(years=train_years)
    window_idx = 0
    
    while val_start + pd.DateOffset(years=val_years) <= end_date:
        val_end = val_start + pd.DateOffset(years=val_years)
        
        train_data = ohlc[(ohlc.index >= start_date) & (ohlc.index < train_years)]
        val_data = ohlc[(ohlc.index >= val_start) & (ohlc.index < val_end)]
        val_f = f[(f.index >= val_start) & (f.index < val_end)]
        
        if len(val_data) < 1000:
            window_idx += 1
            val_start = val_start + pd.DateOffset(years=val_years)
            continue
        
        # 统计各状态的收益
        regime_stats = {}
        
        capital = 10000
        position = None
        regime_counts = {r: 0 for r in ['strong_uptrend', 'uptrend', 'ranging', 'downtrend', 'strong_downtrend', 'ranging_up', 'ranging_down']}
        
        for i in range(48, len(val_data)):
            dt = val_data.index[i]
            regime = classify(val_f.iloc[i])
            
            if regime_counts.get(regime, 0) is not None:
                regime_counts[regime] = regime_counts.get(regime, 0) + 1
            
            if position is not None:
                entry_time, entry_px, pos_type, hold_start = position
                hold_h = (dt - entry_time).total_seconds() / 3600
                close_px = close.iloc[i]
                high_px = high.iloc[i]
                low_px = low.iloc[i]
                
                # 止损
                stop_hit = False
                if pos_type == 'long':
                    ret = (close_px - entry_px) / entry_px - 0.002
                    if hold_h >= 48:
                        capital *= (1 + ret)
                        position = None
                    elif ret < -0.03:
                        capital *= (1 + ret)
                        position = None
                        stop_hit = True
                elif pos_type == 'short':
                    ret = (entry_px - close_px) / entry_px - 0.002
                    if hold_h >= 48:
                        capital *= (1 + ret)
                        position = None
                    elif ret < -0.03:
                        capital *= (1 + ret)
                        position = None
                        stop_hit = True
                
                if position is None:
                    regime_stats[pos_type] = regime_stats.get(pos_type, {'ret': 0, 'count': 0})
                    regime_stats[pos_type]['count'] += 1
                    regime_stats[pos_type]['ret'] += ret if 'ret' in dir() else 0
            
            # 开仓信号
            if position is None:
                if regime == 'strong_uptrend':
                    position = (dt, close.iloc[i], 'long', dt)
                elif regime == 'strong_downtrend':
                    position = (dt, close.iloc[i], 'short', dt)
                elif regime == 'ranging_up' and hold_h >= 6:
                    position = (dt, close.iloc[i], 'long', dt)
                elif regime == 'ranging_down' and hold_h >= 6:
                    position = (dt, close.iloc[i], 'short', dt)
        
        total_ret = (capital - 10000) / 10000 * 100
        
        results.append({
            'window': window_idx,
            'val_start': str(val_start.date()),
            'val_end': str(val_end.date()),
            'final_capital': round(capital, 2),
            'total_ret': round(total_ret, 1),
            'regime_counts': {k: v for k, v in regime_counts.items() if v > 0},
        })
        
        window_idx += 1
        val_start = val_start + pd.DateOffset(years=val_years)
    
    return results


def simple_backtest(ohlc, coin):
    """简单回测：只用ranging市场快进快出"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    vol = ohlc['vol']
    f = get_features(close, high, low, vol)
    
    capital = 10000
    position = None
    trades = []
    
    dates = ohlc.index.tolist()
    
    for i in range(100, len(dates)):
        dt = dates[i]
        close_px = close.loc[dt]
        high_px = high.loc[dt]
        low_px = low.loc[dt]
        
        regime = classify(f, dt)
        
        # 止损
        if position is not None:
            entry_time, entry_px, pos_type = position
            hold_h = (dt - entry_time).total_seconds() / 3600
            
            if pos_type == 'long':
                ret = (close_px - entry_px) / entry_px - 0.002
                stop_loss = entry_px * 0.97
                target = entry_px * 1.03
                if hold_h >= 12 or close_px <= stop_loss or close_px >= target:
                    capital *= (1 + ret)
                    trades.append({'ret': ret, 'regime': regime, 'hold_h': hold_h, 'type': 'long'})
                    position = None
            elif pos_type == 'short':
                ret = (entry_px - close_px) / entry_px - 0.002
                stop_loss = entry_px * 1.03
                target = entry_px * 0.97
                if hold_h >= 12 or close_px >= stop_loss or close_px <= target:
                    capital *= (1 + ret)
                    trades.append({'ret': ret, 'regime': regime, 'hold_h': hold_h, 'type': 'short'})
                    position = None
        
        # 开仓：只在有明确方向时开
        if position is None:
            if regime == 'ranging_up':
                position = (dt, close_px, 'long')
            elif regime == 'ranging_down':
                position = (dt, close_px, 'short')
            elif regime == 'strong_uptrend':
                position = (dt, close_px, 'long')
            elif regime == 'strong_downtrend':
                position = (dt, close_px, 'short')
    
    total_ret = (capital - 10000) / 10000 * 100
    
    # 统计
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    avg_win = np.mean([t['ret'] for t in wins]) * 100 if wins else 0
    avg_loss = np.mean([t['ret'] for t in losses]) * 100 if losses else 0
    
    return {
        'coin': coin,
        'total_trades': len(trades),
        'win_rate': round(wr, 1),
        'avg_win_pct': round(avg_win, 2),
        'avg_loss_pct': round(avg_loss, 2),
        'total_ret': round(total_ret, 1),
        'regime_trades': {r: len([t for t in trades if t['regime'] == r]) for r in set(t['regime'] for t in trades)}
    }


if __name__ == '__main__':
    coins = ['BTC', 'ETH', 'AVAX', 'DOT', 'AAVE']
    
    print('=' * 60)
    print('盘感策略回测 - 纯价格行为验证')
    print('=' * 60)
    
    for coin in coins:
        try:
            ohlc = load_ohlc(coin)
            print(f'\n{coin}: {ohlc.index[0].date()} ~ {ohlc.index[-1].date()} ({len(ohlc)} bars)')
            
            result = simple_backtest(ohlc, coin)
            print(f'  总交易: {result["total_trades"]}笔 | 胜率: {result["win_rate"]}%')
            print(f'  平均赢: {result["avg_win_pct"]}% | 平均亏: {result["avg_loss_pct"]}%')
            print(f'  总收益: {result["total_ret"]}%')
            print(f'  各状态交易: {result["regime_trades"]}')
        except Exception as e:
            print(f'\n{coin}: 错误 - {e}')
            import traceback
            traceback.print_exc()
