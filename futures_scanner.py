"""
高频率双边合约策略 - 多策略横向对比
目标: 每日至少1个信号, 赔率优先, 3-5x杠杆
测试: RSI / BB / EMA 多个 timeframe 组合
"""
import vectorbt as vbt
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'

def calc_rsi(close, n=14):
    d = np.diff(close, prepend=close.iloc[0])
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = pd.Series(g).rolling(n).mean()
    al = pd.Series(l).rolling(n).mean()
    return 100 - (100 / (1 + ag / (al + 1e-10)))

def calc_adx(high, low, close, n=14):
    tr1 = high - low
    tr2 = np.abs(high - close.shift())
    tr3 = np.abs(low - close.shift())
    tr = pd.DataFrame({'tr1':tr1,'tr2':tr2,'tr3':tr3}).max(axis=1)
    up = high.diff()
    dn = -low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=low.index)
    atr = tr.rolling(n).mean()
    pdi = 100 * (pdm.rolling(n).mean() / atr)
    mdi = 100 * (mdm.rolling(n).mean() / atr)
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)
    return dx.rolling(n).mean()

def calc_bb(close, n=20, std=2):
    mid = close.rolling(n).mean()
    upper = mid + std * close.rolling(n).std()
    lower = mid - std * close.rolling(n).std()
    return mid, upper, lower

def load_5m(coin):
    df = pd.read_csv(f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv')
    cols = df.columns.tolist()
    new_cols = []
    seen = {}
    for c in cols:
        cn = c.split('.')[0]
        if cn not in seen:
            new_cols.append(c)
            seen[cn] = cn
    df = df[new_cols][['datetime_utc','open','high','low','close','volume']]
    df['ts'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    df = df.set_index('ts').sort_index()
    df = df[df['close'] > 0]
    return df

def resample_tf(df, tf):
    return df[['open','high','low','close']].resample(tf).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    }).dropna()

def run_backtest(close, high, low, entries, exits, size_pct=0.01, lev=3, fee=0.0004):
    """
    双向杠杆回测
    size_pct: 每笔风险占总资金百分比
    lev: 杠杆倍数
    fee: 手续费 (双边开平 ~0.04% = 0.0004)
    """
    atr_pct = ((high - low).rolling(14).mean()) / close
    sl_stop = (atr_pct * 1.0).fillna(0.005)
    
    # 强制止损
    exits = (exits | (atr_pct > 0)).astype(int)  # 用止损退出
    
    pf = vbt.Portfolio.from_signals(
        close, entries, exits,
        fees=fee, slippage=0, size=size_pct * lev,  # 实际仓位 = 保证金 * 杠杆
        leverage=lev,  # 新版vectorbt支持leverage参数
        leverage_direction='both',
        sl_stop=sl_stop,
        direction='both'
    )
    return pf

def strategy_rsi_meanrev(ohlc, rsi_th_long=35, rsi_th_short=65):
    """RSI均值回归策略"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    
    rsi = calc_rsi(close, 14)
    rsi_ma = rsi.rolling(5).mean()
    
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema30 = close.ewm(span=30, adjust=False).mean()
    
    # LONG: RSI超卖 + RSI反弹 + 价格站上EMA10
    rsi_bounce = (rsi > rsi_ma) & (rsi.shift(1) <= rsi_ma)
    entries_long = ((rsi < rsi_th_long) & rsi_bounce & (close > ema10)).astype(int)
    
    # SHORT: RSI超买 + RSI死叉 + 价格跌破EMA10
    rsi_drop = (rsi < rsi_ma) & (rsi.shift(1) >= rsi_ma)
    entries_short = ((rsi > rsi_th_short) & rsi_drop & (close < ema10)).astype(int)
    
    # 固定持有3根K线后退出
    exits_long = entries_long.shift(3).fillna(0).astype(int)
    exits_short = entries_short.shift(3).fillna(0).astype(int)
    
    return entries_long, exits_long, entries_short, exits_short

def strategy_bb_squeeze(ohlc, bb_std=2):
    """布林带挤压突破策略"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    
    mid, upper, lower = calc_bb(close, 20, bb_std)
    
    # 计算带宽判断挤压
    bandwidth = (upper - lower) / mid
    squeeze = bandwidth < bandwidth.rolling(50).quantile(0.2)
    squeeze_prev = squeeze.shift(1)
    
    # 突破: 向上/向下
    entries_long = ((close > upper) & squeeze_prev).astype(int)
    entries_short = ((close < lower) & squeeze_prev).astype(int)
    
    # 持有到反向信号
    exits_long = ((close < mid)).astype(int)
    exits_short = ((close > mid)).astype(int)
    
    return entries_long, exits_long, entries_short, exits_short

def strategy_ema_cross(ohlc, fast=10, slow=30):
    """EMA交叉策略"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    
    bull_cross = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1))
    bear_cross = (ema_fast < ema_slow) & (ema_fast.shift(1) >= ema_slow.shift(1))
    
    entries_long = bull_cross.astype(int)
    entries_short = bear_cross.astype(int)
    
    # 反向交叉退出
    exits_long = bear_cross.astype(int)
    exits_short = bull_cross.astype(int)
    
    return entries_long, exits_long, entries_short, exits_short

def strategy_rsi_adx(ohlc, adx_th=25):
    """RSI + ADX组合策略"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    
    rsi = calc_rsi(close, 14)
    adx = calc_adx(high, low, close, 14)
    adx_avg = adx.rolling(3).mean()
    
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema30 = close.ewm(span=30, adjust=False).mean()
    
    # ADX确认趋势
    bull_adx = adx_avg > adx_th
    bear_adx = adx_avg > adx_th
    
    bull_cross = (ema10 > ema30) & (ema10.shift(1) <= ema30.shift(1))
    bear_cross = (ema10 < ema30) & (ema10.shift(1) >= ema30.shift(1))
    
    # RSI极端值 + ADX确认
    entries_long = ((rsi < 35) & bull_adx & bull_cross).astype(int)
    entries_short = ((rsi > 65) & bear_adx & bear_cross).astype(int)
    
    # 反向退出
    exits_long = ((rsi > 60) | (ema10 < ema30)).astype(int)
    exits_short = ((rsi < 40) | (ema10 > ema30)).astype(int)
    
    return entries_long, exits_long, entries_short, exits_short

def strategy_pure_short(ohlc, adx_th=20):
    """只做空的策略(专门测试做空效果)"""
    close = ohlc['close']
    high = ohlc['high']
    low = ohlc['low']
    
    adx = calc_adx(high, low, close, 14)
    adx_avg = adx.rolling(3).mean()
    
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema30 = close.ewm(span=30, adjust=False).mean()
    
    bear_cross = (ema10 < ema30) & (ema10.shift(1) >= ema30.shift(1))
    
    entries_short = (bear_cross & (adx_avg > adx_th)).astype(int)
    entries_long = pd.Series(0, index=close.index).astype(int)
    
    exits_short = (ema10 > ema30).astype(int)
    exits_long = pd.Series(0, index=close.index).astype(int)
    
    return entries_long, exits_long, entries_short, exits_short

def evaluate(pf, label, lev):
    if pf is None: return None
    s = pf.stats()
    trades = s.get('Total Trades', 0)
    if trades < 10: return None
    
    val = pf.value()
    ret = (val.iloc[-1] / val.iloc[0] - 1)
    peak = val.cummax()
    dd = ((val - peak) / peak).min()
    rets = val.pct_change().dropna()
    
    # 年化
    ann_ret = rets.mean() * 365 * 24  # 假设1H频率
    ann_vol = rets.std() * np.sqrt(365 * 24)
    sharpe = ann_ret / ann_vol if ann_vol > 1e-10 else 0
    
    aw = s.get('Avg Winning Trade [%]', 0)
    al = abs(s.get('Avg Losing Trade [%]', 0))
    pf_ratio = aw / al if al > 1e-10 else 0
    wr = s.get('Win Rate [%]', 0)
    
    return {
        'label': label,
        'trades': trades,
        'ret': ret * 100,
        'dd': dd * 100,
        'sharpe': sharpe,
        'pf': pf_ratio,
        'wr': wr,
        'lev': lev,
        'daily_signals': trades / (len(val) / 24) if len(val) > 0 else 0,  # 1H bars
    }

print("="*72)
print("高频率双边合约策略 - 多策略横向对比")
print("="*72)

# 加载数据
df = load_5m('BTC')
close_full = df['close']
high_full = df['high']
low_full = df['low']

# 划分训练期和验证期
train_end = '2023-12-31'
test_start = '2024-01-01'
test_end = '2025-12-31'

# 时间框架
timeframes = ['1h', '4h']

strategies = [
    ('RSI均值回归(1H)', lambda ohlc: strategy_rsi_meanrev(ohlc, 35, 65), '1h'),
    ('RSI均值回归(4H)', lambda ohlc: strategy_rsi_meanrev(ohlc, 35, 65), '4h'),
    ('RSI均值回归严格(1H)', lambda ohlc: strategy_rsi_meanrev(ohlc, 30, 70), '1h'),
    ('BB挤压(1H)', lambda ohlc: strategy_bb_squeeze(ohlc, 2), '1h'),
    ('EMA交叉(1H)', lambda ohlc: strategy_ema_cross(ohlc, 5, 20), '1h'),
    ('EMA交叉(4H)', lambda ohlc: strategy_ema_cross(ohlc, 10, 30), '4h'),
    ('RSI+ADX(1H)', lambda ohlc: strategy_rsi_adx(ohlc, 25), '1h'),
    ('RSI+ADX(4H)', lambda ohlc: strategy_rsi_adx(ohlc, 25), '4h'),
    ('纯做空(4H)', lambda ohlc: strategy_pure_short(ohlc, 20), '4h'),
]

# 只用训练期测试
train_close = close_full.loc[:train_end]
train_high = high_full.loc[:train_end]
train_low = low_full.loc[:train_end]

results = []

for label, strat_fn, tf in strategies:
    ohlc = resample_tf(df.loc[:train_end], tf)
    c = ohlc['close']
    h = ohlc['high']
    l = ohlc['low']
    
    eL, xL, eS, xS = strat_fn(ohlc)
    
    # 过滤太少的信号
    total_signals = int(eL.sum()) + int(eS.sum())
    if total_signals < 20:
        continue
    
    # 用vectorbt双向回测
    for lev in [3, 5]:
        for size_pct in [0.01, 0.02]:
            fee = 0.0004 * 2  # 双边开平
            
            pf = vbt.Portfolio.from_signals(
                c,
                entries=eL, exits=xL,
                short_entries=eS, short_exits=xS,
                fees=fee, slippage=0,
                size=size_pct * lev,
            )
            
            stats = evaluate(pf, label, lev)
            if stats:
                stats['size_pct'] = size_pct
                results.append(stats)

# 排序
if results:
    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values('ret', ascending=False)
    
    print(f"\n{'策略':<25} {'杠杆':>4} {'收益':>8} {'DD':>7} {'夏普':>6} {'PF':>6} {'胜率':>6} {'交易':>5} {'日均信号':>8}")
    print("-"*80)
    
    for _, r in results_df.iterrows():
        mark = '🏆' if r['ret'] > 500 else ('✅' if r['ret'] > 100 else ('⚠️' if r['ret'] > 0 else '❌'))
        print(f"  {r['label']:<23} {r['lev']:>3}x {r['ret']:>+7.0f}% {r['dd']:>6.1f}% {r['sharpe']:>6.2f} {r['pf']:>6.2f} {r['wr']:>5.0f}% {r['trades']:>5} {r['daily_signals']:>7.1f}/天 {mark}")

print()
print("="*72)
print("最优策略详细回测")
print("="*72)

# 对最优策略做详细年份分析
best_strategies = [
    ('RSI均值回归(1H)', lambda ohlc: strategy_rsi_meanrev(ohlc, 35, 65), '1h'),
    ('RSI均值回归严格(1H)', lambda ohlc: strategy_rsi_meanrev(ohlc, 30, 70), '1h'),
    ('RSI+ADX(1H)', lambda ohlc: strategy_rsi_adx(ohlc, 25), '1h'),
]

for label, strat_fn, tf in best_strategies:
    ohlc_train = resample_tf(df.loc[:train_end], tf)
    ohlc_test = resample_tf(df.loc[test_start:test_end], tf)
    
    c_train = ohlc_train['close']
    h_train = ohlc_train['high']
    l_train = ohlc_train['low']
    c_test = ohlc_test['close']
    h_test = ohlc_test['high']
    l_test = ohlc_test['low']
    
    eL_train, xL_train, eS_train, xS_train = strat_fn(ohlc_train)
    eL_test, xL_test, eS_test, xS_test = strat_fn(ohlc_test)
    
    print(f"\n{label} ({tf})")
    print(f"{'数据集':>8} {'收益':>8} {'DD':>7} {'夏普':>6} {'PF':>6} {'胜率':>6} {'交易':>5} {'信号':>5}")
    print("-"*60)
    
    for data_label, c, h, l, eL, xL, eS, xS in [
        ('训练集', c_train, h_train, l_train, eL_train, xL_train, eS_train, xS_train),
        ('验证集', c_test, h_test, l_test, eL_test, xL_test, eS_test, xS_test),
    ]:
        pf = vbt.Portfolio.from_signals(
            c,
            entries=eL, exits=xL,
            short_entries=eS, short_exits=xS,
            fees=0.0008, size=0.10,
        )
        stats = evaluate(pf, label, 5)
        if stats:
            print(f"  {data_label:<6} {stats['ret']:>+7.0f}% {stats['dd']:>6.1f}% {stats['sharpe']:>6.2f} {stats['pf']:>6.2f} {stats['wr']:>5.0f}% {stats['trades']:>5} {int(eL.sum())+int(eS.sum()):>5}")
