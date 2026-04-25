"""
盘感策略分析 - 分析每个市场状态的真实收益分布
不用指标，只用价格行为统计
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
    df['vol_ratio'] = vol / vol.rolling(24).mean()
    return df


def classify(f, idx):
    cum_48 = f['cum_48h'].loc[idx]
    pos_48 = f['pos_48h'].loc[idx]
    cum_168 = f['cum_168h'].loc[idx]
    cum_12 = f['cum_12h'].loc[idx]
    pos_12 = f['pos_12h'].loc[idx]
    cum_24 = f['cum_24h'].loc[idx]
    pos_24 = f['pos_24h'].loc[idx]
    
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
    elif cum_24 > 0.005 and pos_24 > 0.55:
        return 'momentum_up'
    elif cum_24 < -0.005 and pos_24 < 0.45:
        return 'momentum_down'
    else:
        return 'ranging'


def analyze_regime_returns(ohlc, coin):
    """分析每个状态下的未来收益率分布"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    vol = ohlc['vol']
    f = get_features(close, high, low, vol)
    
    dates = f.index.tolist()
    
    # 存储每个状态下，未来N小时的收益率
    results = {}
    regimes = ['strong_uptrend', 'uptrend', 'ranging_up', 'ranging', 'ranging_down', 'downtrend', 'strong_downtrend', 'momentum_up', 'momentum_down']
    
    for r in regimes:
        results[r] = {6: [], 12: [], 24: [], 48: []}
    
    for i in range(200, len(dates) - 48):
        dt = dates[i]
        regime = classify(f, dt)
        entry_px = close.loc[dt]
        
        # 计算未来各时间窗口的收益
        for h in [6, 12, 24, 48]:
            if i + h < len(dates):
                future_dt = dates[i + h]
                future_px = close.loc[future_dt]
                ret = (future_px - entry_px) / entry_px
                results[regime][h].append(ret)
    
    # 统计
    print(f'\n{coin} 各状态未来收益率分析:')
    print('=' * 80)
    
    all_summary = {}
    for r in regimes:
        h6 = results[r][6]
        h12 = results[r][12]
        h24 = results[r][24]
        h48 = results[r][48]
        
        if not h6:
            continue
        
        mean_6 = np.mean(h6) * 100
        mean_12 = np.mean(h12) * 100
        mean_24 = np.mean(h24) * 100
        mean_48 = np.mean(h48) * 100
        win_24 = (np.array(h24) > 0).mean() * 100
        win_48 = (np.array(h48) > 0).mean() * 100
        
        all_summary[r] = {
            'count': len(h6),
            'mean_6h': round(mean_6, 3),
            'mean_12h': round(mean_12, 3),
            'mean_24h': round(mean_24, 3),
            'mean_48h': round(mean_48, 3),
            'win_24h': round(win_24, 1),
            'win_48h': round(win_48, 1),
        }
        
        print(f'{r:20s} | n={len(h6):5d} | 6h={mean_6:+.3f}% | 12h={mean_12:+.3f}% | 24h={mean_24:+.3f}% | 48h={mean_48:+.3f}% | 胜24h={win_24:.0f}% | 胜48h={win_48:.0f}%')
    
    return all_summary


if __name__ == '__main__':
    for coin in ['BTC', 'ETH', 'AVAX', 'DOT']:
        analyze_regime_returns(load_ohlc(coin), coin)
