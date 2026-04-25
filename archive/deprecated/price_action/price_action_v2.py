"""
盘感策略 v2 - 只做有真实edge的状态

已验证的edge：
- BTC strong_uptrend: 48h +0.77% 胜55%
- ETH strong_uptrend: 48h +0.82% 胜53%
- AVAX strong_uptrend: 48h +2.36% 胜54% ← 最强
- AVAX momentum_down: 48h +1.02% (逆势多)
- DOT momentum_up: 48h +1.07% 胜51%
- ETH downtrend: 48h -0.10% 胜52% (做空)

设计：
- 只做上述5个状态，其他全部跳过
- 止损2%
- 持仓12-24h（快进快出）
- 不频繁交易
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


# 各币种的有效状态（只交易这些）
VALID_REGIMES = {
    'BTC': ['strong_uptrend'],
    'ETH': ['strong_uptrend', 'downtrend'],
    'AVAX': ['strong_uptrend', 'momentum_down'],
    'DOT': ['momentum_up'],
}


def backtest_v2(ohlc, coin):
    """只做有效状态的策略"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    f = get_features(close, high, low, ohlc['vol'])
    dates = f.index.tolist()
    
    valid = VALID_REGIMES.get(coin, [])
    if not valid:
        return None
    
    capital = 10000
    position = None
    trades = []
    
    for i in range(200, len(dates) - 48):
        dt = dates[i]
        regime = classify(f, dt)
        close_px = close.loc[dt]
        
        # 持仓管理
        if position is not None:
            entry_time, entry_px, pos_type = position
            hold_h = (dt - entry_time).total_seconds() / 3600
            
            if pos_type == 'long':
                ret = (close_px - entry_px) / entry_px
                # 止损
                if ret < -0.02:
                    capital *= (1 + ret - 0.002)
                    trades.append({'regime': regime, 'ret': ret - 0.002, 'hold_h': hold_h, 'exit': 'stop'})
                    position = None
                elif hold_h >= 24 and ret > 0:
                    capital *= (1 + ret - 0.002)
                    trades.append({'regime': regime, 'ret': ret - 0.002, 'hold_h': hold_h, 'exit': 'time'})
                    position = None
            elif pos_type == 'short':
                ret = (entry_px - close_px) / entry_px
                if ret < -0.02:
                    capital *= (1 + ret - 0.002)
                    trades.append({'regime': regime, 'ret': ret - 0.002, 'hold_h': hold_h, 'exit': 'stop'})
                    position = None
                elif hold_h >= 24 and ret > 0:
                    capital *= (1 + ret - 0.002)
                    trades.append({'regime': regime, 'ret': ret - 0.002, 'hold_h': hold_h, 'exit': 'time'})
                    position = None
        
        # 开仓：只在有效状态
        if position is None and regime in valid:
            if regime == 'strong_uptrend':
                position = (dt, close_px, 'long')
            elif regime in ['downtrend', 'momentum_down']:
                position = (dt, close_px, 'short')
            elif regime == 'momentum_up':
                position = (dt, close_px, 'long')
    
    # 最终结算
    if position is not None:
        dt = dates[-1]
        entry_time, entry_px, pos_type = position
        close_px = close.loc[dt]
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
        'trades': len(trades),
        'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
        'avg_win': round(np.mean([t['ret'] for t in wins]) * 100, 2) if wins else 0,
        'avg_loss': round(np.mean([t['ret'] for t in losses]) * 100, 2) if losses else 0,
        'valid_regimes': valid,
    }


def walkforward_v2(ohlc, coin, val_years=1):
    """Walk-Forward"""
    start_date = ohlc.index[0]
    end_date = ohlc.index[-1]
    
    results = []
    val_start = start_date + pd.DateOffset(years=3)
    
    while val_start + pd.DateOffset(years=val_years) <= end_date:
        val_end = val_start + pd.DateOffset(years=val_years)
        val_data = ohlc[(ohlc.index >= val_start) & (ohlc.index < val_end)]
        
        if len(val_data) < 3000:
            val_start = val_start + pd.DateOffset(years=val_years)
            continue
        
        r = backtest_v2(val_data, coin)
        if r:
            r['period'] = f'{val_start.date()}~{val_end.date()}'
            results.append(r)
        
        val_start = val_start + pd.DateOffset(years=val_years)
    
    return results


if __name__ == '__main__':
    print('=' * 70)
    print('盘感策略 v2 - 只做有效状态 Walk-Forward')
    print('=' * 70)
    
    for coin in ['BTC', 'ETH', 'AVAX', 'DOT']:
        ohlc = load_ohlc(coin)
        valid = VALID_REGIMES.get(coin, [])
        print(f'\n{coin}: {ohlc.index[0].date()} ~ {ohlc.index[-1].date()} | 交易状态: {valid}')
        
        wf = walkforward_v2(ohlc, coin)
        
        for r in wf:
            print(f"  {r['period']}: {r['total_ret']:+.1f}% | {r['trades']}笔 | "
                  f"胜{r['win_rate']}% | 均赢{r['avg_win']}% | 均亏{r['avg_loss']}%")
        
        if wf:
            avg_ret = np.mean([r['total_ret'] for r in wf])
            positive = sum(1 for r in wf if r['total_ret'] > 0)
            print(f'  → 平均: {avg_ret:+.1f}% | {positive}/{len(wf)}窗口正收益')
