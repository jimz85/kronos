"""
快速回测：验证策略频率和信号分布
只跑信号统计，不做完整交易模拟
"""
import pandas as pd
import numpy as np
import os
from datetime import datetime

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'

# 策略参数
TF_ENTRY = '15min'
TF_TREND = '1h'
RSI_ENTRY_LONG = 45
RSI_ENTRY_SHORT = 55
RSI_EXTREME_LONG = 32
RSI_EXTREME_SHORT = 75
ATR_UPPER = 3.0
MIN_VOL_RATIO = 0.6
MIN_ATR_RATIO = 0.4
COINS = ['BTC', 'ETH', 'BNB', 'DOGE', 'ADA', 'AVAX']

def calc_rsi(close, n=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=n, adjust=False).mean()
    avg_loss = loss.ewm(span=n, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_atr(high, low, close, n=14):
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def load_data(coin):
    fpath = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    if not os.path.exists(fpath): return None
    df = pd.read_csv(fpath)
    cols = df.columns.tolist()
    col_map = {}
    for c in cols:
        cn = c.split('.')[0]
        if cn == 'vol': cn = 'volume'
        if cn not in col_map: col_map[c] = cn
    df = df.rename(columns=col_map)
    
    dt_col = None
    for c in df.columns:
        if c in ('datetime_utc', 'datetime_utc.1', 'datetime', 'date'):
            dt_col = c; break
    if dt_col is None:
        for c in df.columns:
            if c == 'timestamp': dt_col = c; break
    if dt_col is None: dt_col = cols[0]
    
    vol_col = 'volume' if 'volume' in df.columns else ('vol' if 'vol' in df.columns else None)
    
    result = pd.DataFrame()
    for col in ['open', 'high', 'low', 'close']:
        if col in df.columns: result[col] = df[col]
    if vol_col and vol_col in df.columns:
        result['volume'] = df[vol_col]
    else:
        result['volume'] = 0
    
    result['ts'] = pd.to_datetime(df[dt_col]).dt.tz_localize(None)
    result = result.set_index('ts').sort_index()
    return result[result['close'] > 0]

def resample(df, rule):
    return df[['open', 'high', 'low', 'close', 'volume']].resample(rule).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()

def calc_adx(ohlc, n=14):
    h, l, c = ohlc['high'], ohlc['low'], ohlc['close']
    tr1, tr2, tr3 = h-l, (h-c.shift()).abs(), (l-c.shift()).abs()
    tr = pd.concat([tr1,tr2,tr3], axis=1).max(axis=1)
    atr = tr.rolling(n).mean()
    up = h.diff(); dn = -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    plus_di = 100 * (plus_dm.rolling(n).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(n).mean() / atr)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    return dx.rolling(n).mean()

def count_signals(coin, start='2024-01-01', end='2025-12-31'):
    """统计信号数量和频率"""
    df = load_data(coin)
    if df is None: return None
    
    ohlc_entry = resample(df, TF_ENTRY)
    ohlc_trend = resample(df, TF_TREND)
    
    if len(ohlc_entry) < 100 or len(ohlc_trend) < 100: return None
    
    # 趋势
    c_trend = ohlc_trend['close']
    ema20_trend = c_trend.ewm(span=20, adjust=False).mean()
    ema50_trend = c_trend.ewm(span=50, adjust=False).mean()
    adx_trend = calc_adx(ohlc_trend)
    
    # 入场指标
    c = ohlc_entry['close']
    h = ohlc_entry['high']
    l = ohlc_entry['low']
    v = ohlc_entry['volume']
    
    rsi = calc_rsi(c, 14)
    rsi_ma = rsi.rolling(5).mean()
    atr = calc_atr(h, l, c, 14)
    atr_pct = atr / c
    atr_ma = atr_pct.rolling(20).mean()
    atr_ratio = atr_pct / (atr_ma + 1e-10)
    vol_ma = v.rolling(20).mean()
    vol_ratio = v / (vol_ma + 1e-10)
    
    # 过滤时间范围
    mask = (c.index >= start) & (c.index <= end)
    c = c[mask]
    h, l, v = h[mask], l[mask], v[mask]
    rsi = rsi[mask]; rsi_ma = rsi_ma[mask]
    atr_ratio = atr_ratio[mask]; atr_pct = atr_pct[mask]; vol_ratio = vol_ratio[mask]
    
    # 趋势对齐
    trend_up = (ema20_trend > ema50_trend).reindex(c.index, method='ffill')
    trend_dn = (ema20_trend < ema50_trend).reindex(c.index, method='ffill')
    adx = adx_trend.reindex(c.index, method='ffill')
    
    # 信号判定
    rsi_curr = rsi
    rsi_prev = rsi.shift(1)
    rsi_prev2 = rsi.shift(2)
    rsi_ma_curr = rsi_ma
    rsi_ma_prev = rsi_ma.shift(1)
    
    vol_blocked = ((atr_ratio > ATR_UPPER) | (atr_ratio < MIN_ATR_RATIO)).astype(bool)
    
    # 做多信号
    bounce = ((rsi_prev < rsi_ma_prev) & (rsi_curr >= rsi_prev)).astype(bool)
    extreme_long = (rsi_curr < RSI_EXTREME_LONG).astype(bool)
    long_cond = (
        trend_up.astype(bool) & 
        (~vol_blocked) & 
        (rsi_curr < RSI_ENTRY_LONG).astype(bool) & 
        (rsi_curr > 20).astype(bool) &
        (bounce | extreme_long) &
        ((vol_ratio >= MIN_VOL_RATIO) | extreme_long).astype(bool)
    )
    
    # 做空信号
    fall = ((rsi_prev > rsi_ma_prev) & (rsi_curr <= rsi_prev)).astype(bool)
    extreme_short = (rsi_curr > RSI_EXTREME_SHORT).astype(bool)
    short_cond = (
        trend_dn.astype(bool) &
        (~vol_blocked) &
        (rsi_curr > RSI_ENTRY_SHORT).astype(bool) &
        (rsi_curr < 80).astype(bool) &
        (fall | extreme_short) &
        ((vol_ratio >= MIN_VOL_RATIO) | extreme_short).astype(bool)
    )
    
    long_signals = long_cond[long_cond].index
    short_signals = short_cond[short_cond].index
    
    # 按天统计
    long_by_day = pd.Series(1, index=long_signals).resample('D').sum()
    short_by_day = pd.Series(1, index=short_signals).resample('D').sum()
    
    # 统计
    n_days = (pd.Timestamp(end) - pd.Timestamp(start)).days
    total_long = len(long_signals)
    total_short = len(short_signals)
    avg_long_per_day = total_long / n_days
    avg_short_per_day = total_short / n_days
    
    days_with_signal = ((long_by_day > 0) | (short_by_day > 0)).sum()
    
    # 趋势分布
    trend_up_days = trend_up.resample('D').last().sum()
    trend_dn_days = trend_dn.resample('D').last().sum()
    
    return {
        'coin': coin,
        'total_long': total_long,
        'total_short': total_short,
        'avg_long_per_day': avg_long_per_day,
        'avg_short_per_day': avg_short_per_day,
        'total_per_day': avg_long_per_day + avg_short_per_day,
        'days_with_signal': days_with_signal,
        'pct_days_with_signal': days_with_signal / n_days * 100,
        'trend_up_days': int(trend_up_days),
        'trend_dn_days': int(trend_dn_days),
    }

def main():
    print("="*70)
    print("策略信号频率回测 2024-2025")
    print("="*70)
    print(f"周期: 15min入场 / 1h趋势判断")
    print(f"信号条件: RSI<45回调+反弹 OR RSI<32极端超卖")
    print(f"过滤: ATR>3x禁止, ATR<0.4x禁止, 量比<0.6x禁止(极端除外)")
    print()
    
    all_results = []
    for coin in COINS:
        r = count_signals(coin)
        if r: all_results.append(r)
        print(f"  {coin}: {'OK' if r else 'FAIL'}")
    
    print()
    print("="*70)
    print("信号统计")
    print("="*70)
    print(f"{'币种':>5} {'LONG信号':>8} {'SHORT信号':>9} {'LONG/天':>8} {'SHORT/天':>9} {'合计/天':>8} {'有信号天数':>9} {'覆盖率':>7}")
    print("-"*70)
    
    total_long_all = 0
    total_short_all = 0
    total_per_day_all = []
    days_with_any = 0
    
    for r in all_results:
        pct = f"{r['pct_days_with_signal']:.0f}%"
        print(f"  {r['coin']:>4} {r['total_long']:>7} {r['total_short']:>8} {r['avg_long_per_day']:>7.2f} {r['avg_short_per_day']:>8.2f} {r['total_per_day']:>7.2f} {r['days_with_signal']:>8} {pct:>7}")
        total_long_all += r['total_long']
        total_short_all += r['total_short']
        total_per_day_all.append(r['total_per_day'])
    
    total_avg = np.mean(total_per_day_all)
    print()
    print(f"  汇总: LONG {total_long_all}笔 / SHORT {total_short_all}笔")
    print(f"  多币种合计日均信号: {total_avg:.1f}次")
    
    # 按年份细分
    print()
    print("="*70)
    print("按年份: 2024 vs 2025")
    print("="*70)
    for coin in COINS:
        r24 = count_signals(coin, '2024-01-01', '2024-12-31')
        r25 = count_signals(coin, '2025-01-01', '2025-12-31')
        if r24 and r25:
            print(f"  {coin}: 2024={r24['total_per_day']:.1f}次/天 2025={r25['total_per_day']:.1f}次/天")

if __name__ == '__main__':
    main()
