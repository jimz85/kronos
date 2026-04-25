#!/usr/bin/env python3
"""
趋势早期探测器 + 分状态 Walk-Forward 分析
基于吉总提供的三个趋势确认条件:
1. 价格突破过去20根K线最高价（唐奇安通道突破）
2. ADX(14) > 25 且 +DI > -DI
3. 成交量 > 过去20根均量的1.5倍
"""
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 指标计算
# ============================================================

def calc_rsi(prices, period=14):
    d = np.diff(prices, prepend=prices[0])
    g = np.where(d > 0, d, 0)
    l = np.where(d < 0, -d, 0)
    ag = pd.Series(g).rolling(period).mean()
    al = pd.Series(l).rolling(period).mean()
    return 100 - (100 / (1 + ag / (al + 1e-10)))

def calc_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calc_atr(high, low, close, period=14):
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(period).mean()

def calc_adx_dmi(high, low, close, period=14):
    """计算 ADX 和 DMI"""
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]

    # True Range
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))

    # Directional Movement
    up_move = high - np.roll(high, 1)
    down_move = np.roll(low, 1) - low
    up_move[0] = 0
    down_move[0] = 0

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

    # Smooth
    atr = pd.Series(tr).rolling(period).mean()
    plus_di = pd.Series(plus_dm).rolling(period).mean() / (atr + 1e-10) * 100
    minus_di = pd.Series(minus_dm).rolling(period).mean() / (atr + 1e-10) * 100

    dx = abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
    adx = dx.rolling(period).mean()

    return adx.values, plus_di.values, minus_di.values

def calc_donchian(high, low, period=20):
    """唐奇安通道"""
    return pd.Series(high).rolling(period).max().shift(1).values, \
           pd.Series(low).rolling(period).min().shift(1).values

def calc_volume_ma(volume, period=20):
    """成交量移动平均"""
    return pd.Series(volume).rolling(period).mean().shift(1).values

# ============================================================
# 市场状态分类（增强版）
# ============================================================

def classify_regime_enhanced(row, lookback=20, adx_thresh=25):
    """
    增强版市场状态分类
    - bull_trend → active_bull (强趋势) / weak_bull (EMA200上方但动量不足)
    - bear_trend → active_bear (强趋势) / weak_bear
    - range: EMA20/50间距<2%
    """
    close  = row['close']
    ema20  = row['ema20']
    ema50  = row['ema50']
    ema200 = row['ema200']
    adx    = row['adx']
    plus_di = row['plus_di']
    minus_di = row['minus_di']

    # 基础趋势判断
    if close > ema200:
        base_trend = 'bull'
    elif close < ema200:
        base_trend = 'bear'
    else:
        return 'transition'

    # 震荡判断
    if abs(ema20 - ema50) / close < 0.02:
        return 'range'

    # 趋势强度判断
    trend_active = (adx > adx_thresh) and (plus_di > minus_di if base_trend == 'bull' else minus_di > plus_di)

    if base_trend == 'bull':
        if trend_active:
            return 'active_bull'
        else:
            return 'weak_bull'
    else:  # bear
        if trend_active:
            return 'active_bear'
        else:
            return 'weak_bear'

# ============================================================
# 数据加载
# ============================================================

print("加载BTC 1H数据...")
df = pd.read_csv("/tmp/btc_1h_processed.csv", index_col=0, parse_dates=True)
close = df['close'].astype(float).values
high  = df['high'].astype(float).values
low   = df['low'].astype(float).values
volume = df['volume'].astype(float).values

print(f"原始数据: {len(df)} bars")

# 计算所有指标
df['close']  = close
df['rsi']    = calc_rsi(close, 14).values
df['ema20']  = calc_ema(pd.Series(close), 20).values
df['ema50']  = calc_ema(pd.Series(close), 50).values
df['ema200'] = calc_ema(pd.Series(close), 200).values
df['atr']    = calc_atr(high, low, close, 14).values
df['atr_ma'] = pd.Series(df['atr'].values).rolling(24).mean().shift(1).values
df['adx'], df['plus_di'], df['minus_di'] = calc_adx_dmi(high, low, close, 14)

# 唐奇安通道
df['donchian_high'], df['donchian_low'] = calc_donchian(high, low, 20)
# 成交量MA
df['volume_ma'] = calc_volume_ma(volume, 20)

# 趋势确认信号
df['price_breakout'] = (df['close'] > df['donchian_high']).values
df['volume_surge']   = (df['volume'] > df['volume_ma'] * 1.5).values
df['adx_trend']      = (df['adx'] > 25).values
df['di_bull']        = (df['plus_di'] > df['minus_di']).values
df['di_bear']        = (df['minus_di'] > df['plus_di']).values

# 三个条件同时满足 = 强趋势
df['strong_bull'] = df['price_breakout'] & df['volume_surge'] & df['adx_trend'] & df['di_bull']
df['strong_bear'] = (df['close'] < df['donchian_low']) & df['volume_surge'] & df['adx_trend'] & df['di_bear']

# 分类市场状态
regimes = []
for i in range(len(df)):
    regimes.append(classify_regime_enhanced(df.iloc[i]))
df['regime'] = regimes

# ============================================================
# 统计
# ============================================================
print(f"\n{'='*60}")
print("增强版市场状态分布")
print("="*60)
rc = df['regime'].value_counts()
for r, cnt in rc.items():
    pct = cnt/len(df)*100
    hours_per_year = pct/100*8766
    print(f"  {r}: {cnt}h ({pct:.1f}%) ≈ {hours_per_year:.0f}h/年")

print(f"\n趋势确认条件统计:")
print(f"  唐奇安突破:    {df['price_breakout'].sum()} 次 ({df['price_breakout'].mean()*100:.1f}%)")
print(f"  成交量放大1.5x: {df['volume_surge'].sum()} 次 ({df['volume_surge'].mean()*100:.1f}%)")
print(f"  ADX>25:        {df['adx_trend'].sum()} 次 ({df['adx_trend'].mean()*100:.1f}%)")
print(f"  强多信号:      {df['strong_bull'].sum()} 次 ({df['strong_bull'].mean()*100:.2f}%)")
print(f"  强空信号:      {df['strong_bear'].sum()} 次 ({df['strong_bear'].mean()*100:.2f}%)")

# ============================================================
# 分状态 RSI<35 信号分析
# ============================================================

def analyze_regime_signals(df, regime, label, min_signals=3):
    subset = df[df['regime'] == regime].copy()
    if len(subset) < 20:
        return None

    rsi_arr = subset['rsi'].values
    close_arr = subset['close'].values
    idxs = subset.index.tolist()

    # RSI<35
    signal_idxs = [i for i, r in enumerate(rsi_arr) if r < 35]
    if len(signal_idxs) < min_signals:
        return {'label': label, 'signals': len(signal_idxs), 'win_rate': None, 'avg': None, 'pf': None}

    # 未来2根K线收益
    rets = []
    for si in signal_idxs:
        if si + 3 < len(close_arr):
            ret = (close_arr[si+2] - close_arr[si]) / close_arr[si]
            rets.append(ret)

    if len(rets) < min_signals:
        return {'label': label, 'signals': len(rets), 'win_rate': None, 'avg': None, 'pf': None}

    wins   = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    wr = len(wins) / len(rets)
    avg = np.mean(rets)
    pf  = abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999

    return {'label': label, 'signals': len(rets), 'win_rate': wr, 'avg': avg, 'pf': pf,
            'wins': len(wins), 'losses': len(losses)}

print(f"\n{'='*60}")
print("分状态 RSI<35 信号表现（增强版）")
print("="*60)

regime_order = ['active_bull', 'weak_bull', 'active_bear', 'weak_bear', 'range', 'transition']
regime_labels = {
    'active_bull': '【强多头】趋势确认',
    'weak_bull':   '【弱多头】EMA200上方',
    'active_bear': '【强空头】趋势确认',
    'weak_bear':   '【弱空头】EMA200下方',
    'range':       '【震荡市】',
    'transition':  '【过渡】'
}

results = {}
for regime in regime_order:
    r = analyze_regime_signals(df, regime, regime_labels.get(regime, regime))
    results[regime] = r
    if r and r['win_rate'] is not None:
        print(f"\n{regime_labels.get(regime, regime)}:")
        print(f"  信号: {r['signals']} 胜率: {r['win_rate']:.1%} 均收益: {r['avg']:+.2%} PF: {r['pf']:.2f}")
        print(f"  盈/亏: {r['wins']}/{r['losses']}")
    else:
        print(f"\n{regime_labels.get(regime, regime)}: 信号不足")

# ============================================================
# 强趋势状态下 加 ATR 过滤的效果
# ============================================================

print(f"\n{'='*60}")
print("强趋势状态 + ATR过滤测试")
print("="*60)

for regime in ['active_bull', 'active_bear']:
    subset = df[df['regime'] == regime].copy()
    if len(subset) < 20:
        continue

    rsi_arr = subset['rsi'].values
    close_arr = subset['close'].values
    atr_arr = subset['atr'].values
    atr_ma_arr = subset['atr_ma'].values
    idxs = subset.index.tolist()

    # RSI<35 且 ATR收缩
    mask = (rsi_arr < 35) & (atr_arr < atr_ma_arr * 0.70)
    signal_idxs = np.where(mask)[0]

    if len(signal_idxs) < 3:
        print(f"\n{regime} + ATR收缩: 信号太少 ({len(signal_idxs)})")
        continue

    rets = []
    for si in signal_idxs:
        if si + 3 < len(close_arr):
            ret = (close_arr[si+2] - close_arr[si]) / close_arr[si]
            rets.append(ret)

    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    wr = len(wins)/len(rets)
    avg = np.mean(rets)
    pf = abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999
    print(f"\n{regime} + ATR收缩:")
    print(f"  信号: {len(rets)} 胜率: {wr:.1%} 均收益: {avg:+.2%} PF: {pf:.2f}")

# ============================================================
# 各时间段状态分布
# ============================================================

print(f"\n{'='*60}")
print("各时间段状态分布（增强版）")
print("="*60)

periods = [
    ("训练集 2024-04→12", "2024-04-15", "2024-12-31"),
    ("验证集 2025-01→03", "2025-01-01", "2025-03-31"),
    ("测试集1 2025-04→06", "2025-04-01", "2025-06-30"),
    ("测试集2 2025-07→10", "2025-07-01", "2025-10-31"),
    ("测试集3 2025-11→now", "2025-11-01", "2026-04-14"),
]

regime_cols = ['active_bull', 'weak_bull', 'active_bear', 'weak_bear', 'range', 'transition']

for pname, start, end in periods:
    mask = (df.index >= start) & (df.index <= end)
    subset = df[mask]
    if len(subset) == 0:
        continue
    counts = subset['regime'].value_counts()
    total = len(subset)
    row = " | ".join(f"{r}:{counts.get(r,0)/total*100:.0f}%" for r in regime_cols if counts.get(r,0) > 0)
    print(f"  {pname}: {row}")

# ============================================================
# Walk-Forward: 在active_bull状态下测试
# ============================================================

print(f"\n{'='*60}")
print("Walk-Forward 精神: active_bull状态下的参数验证")
print("="*60)

# 训练集
train = df[df.index < '2025-01-01']
val   = df[(df.index >= '2025-01-01') & (df.index < '2025-04-01')]
test  = df[df.index >= '2025-04-01']

# 仅在active_bull状态下的RSI<35信号
def active_bull_backtest(data, rsi_thresh=35):
    subset = data[data['regime'] == 'active_bull']
    if len(subset) < 20:
        return None

    rsi_arr = subset['rsi'].values
    close_arr = subset['close'].values
    atr_arr = subset['atr'].values
    atr_ma_arr = subset['atr_ma'].values

    # RSI信号
    signal_idxs = np.where(rsi_arr < rsi_thresh)[0]
    if len(signal_idxs) < 5:
        return None

    rets = []
    for si in signal_idxs:
        if si + 3 < len(close_arr):
            ret = (close_arr[si+2] - close_arr[si]) / close_arr[si]
            rets.append(ret)

    if len(rets) < 5:
        return None

    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    return {
        'signals': len(rets), 'win_rate': len(wins)/len(rets),
        'avg': np.mean(rets), 'pf': abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999
    }

train_result = active_bull_backtest(train, 35)
val_result   = active_bull_backtest(val, 35)
test_result  = active_bull_backtest(test, 35)

for name, r, period_df in [("训练集", train_result, train),
                             ("验证集", val_result, val),
                             ("测试集", test_result, test)]:
    active_count = (period_df['regime'] == 'active_bull').sum()
    pct = active_count/len(period_df)*100
    print(f"\n{name} (active_bull时间占比: {pct:.1f}%):")
    if r:
        print(f"  RSI<35信号: {r['signals']} 胜率: {r['win_rate']:.1%} 均收益: {r['avg']:+.2%} PF: {r['pf']:.2f}")
    else:
        print(f"  信号不足")

# 证伪检查
if train_result and val_result:
    wr_decay = (train_result['win_rate'] - val_result['win_rate']) / train_result['win_rate']
    avg_decay = (train_result['avg'] - val_result['avg']) / train_result['avg'] if train_result['avg'] > 0 else 999
    falsified = wr_decay > 0.5 or val_result['win_rate'] < 0.40
    print(f"\n证伪检查:")
    print(f"  训练胜率: {train_result['win_rate']:.1%}")
    print(f"  验证胜率: {val_result['win_rate']:.1%}")
    print(f"  胜率衰减: {wr_decay:.1%}")
    print(f"  均收益衰减: {avg_decay:.1%}")
    print(f"  证伪: {'🚫 YES' if falsified else '✅ NO'}")

# ============================================================
# 平滑性测试: RSI阈值微调
# ============================================================

print(f"\n{'='*60}")
print("active_bull RSI平滑性测试")
print("="*60)

for rsi_thresh in [25, 30, 35, 40, 45]:
    r = active_bull_backtest(train, rsi_thresh)
    if r:
        print(f"  RSI<{rsi_thresh}: 信号={r['signals']} 胜率={r['win_rate']:.1%} 均收益={r['avg']:+.2%} PF={r['pf']:.2f}")

print(f"\n✅ 分析完成")
