#!/usr/bin/env python3
"""
方向一验证: 波动率弹簧策略
使用 strategy_validator 验证器
"""
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, '/Users/jimingzhang/kronos')

from strategy_validator import (
    StrategyValidator,
    calc_rsi, calc_ema, calc_atr, calc_dmi,
    make_volatility_spring_strategy
)
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 加载数据 + 计算指标
# ============================================================
print("加载BTC 1H数据...")
df = pd.read_csv('/tmp/btc_1h_processed.csv', index_col=0, parse_dates=True)
close = df['close'].astype(float).values
high  = df['high'].astype(float).values
low   = df['low'].astype(float).values
volume = df['volume'].astype(float).values

df['close']  = close
df['high']   = high
df['low']    = low
df['volume'] = volume
df['rsi']    = calc_rsi(close, 14)
df['ema20']  = calc_ema(pd.Series(close), 20)
df['ema50']  = calc_ema(pd.Series(close), 50)
df['ema200'] = calc_ema(pd.Series(close), 200)
df['atr']    = calc_atr(high, low, close, 14)
df['atr_ma'] = pd.Series(df['atr'].values).rolling(20).mean().shift(1).values
df['atr_ma20'] = pd.Series(df['atr'].values).rolling(20).mean().shift(1).values
df['volume_ma'] = pd.Series(volume).rolling(20).mean().shift(1).values

# 24小时区间
df['range_low']  = pd.Series(close).rolling(24).min().shift(1).values
df['range_high'] = pd.Series(close).rolling(24).max().shift(1).values

# ADX
df['adx'], df['plus_di'], df['minus_di'] = calc_dmi(high, low, close, 14)

print(f"数据: {len(df)} bars ({df.index[0].date()} → {df.index[-1].date()})")

# ============================================================
# 策略定义
# ============================================================

def volatility_spring_long(df, atr_squeeze=0.50, lookback=24):
    """波动率弹簧做多策略"""
    squeeze = df['atr'].values < df['atr_ma'].values * atr_squeeze
    at_lower = df['close'].values <= df['range_low'].values * 1.01
    sig = (squeeze & at_lower).astype(int)
    return {'signal': sig, 'direction': 'long'}

def volatility_spring_short(df, atr_squeeze=0.50, lookback=24):
    """波动率弹簧做空策略"""
    squeeze = df['atr'].values < df['atr_ma'].values * atr_squeeze
    at_upper = df['close'].values >= df['range_high'].values * 0.99
    sig = (squeeze & at_upper).astype(int)
    return {'signal': sig, 'direction': 'short'}

# ============================================================
# Walk-Forward: 方向一（弹簧做多）
# ============================================================
print("\n" + "="*65)
print("方向一: 波动率弹簧策略 (做多)")
print("="*65)

# 先手工跑Walk-Forward看数字
train = df[df.index < '2025-01-01'].copy()
val   = df[(df.index >= '2025-01-01') & (df.index < '2025-04-01')].copy()
test  = df[df.index >= '2025-04-01'].copy()

def backtest_spring(data, squeeze_thresh, lookback=24, direction='long'):
    if len(data) < 50:
        return None
    sig = volatility_spring_long(data, squeeze_thresh, lookback) if direction == 'long' \
          else volatility_spring_short(data, squeeze_thresh, lookback)
    sig_arr = sig['signal']
    close_arr = data['close'].values.astype(float)

    trades = []
    pos = None
    for i in range(30, len(sig_arr) - 5):
        if sig_arr[i] == 1 and pos is None:
            pos = i
            entry = float(close_arr[i])
        elif pos is not None and sig_arr[i] == 0:
            exit_p = float(close_arr[i])
            hold = i - pos
            if direction == 'long':
                ret = (exit_p - entry) / entry
            else:
                ret = (entry - exit_p) / entry
            trades.append(ret)
            pos = None

    if not trades or len(trades) < 5:
        return None

    wins = [r for r in trades if r > 0]
    losses = [r for r in trades if r < 0]
    return {
        'trades': len(trades),
        'win_rate': len(wins)/len(trades),
        'avg': np.mean(trades),
        'pf': abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999,
        'total': sum(trades),
    }

print("\n--- ATR收缩阈值扫参 ---")
for squeeze in [0.30, 0.40, 0.50, 0.60]:
    for lookback in [12, 24, 48]:
        r = backtest_spring(train, squeeze, lookback, 'long')
        if r:
            print(f"  ATR<{squeeze:.0%} lookback={lookback}: 交易={r['trades']} 胜率={r['win_rate']:.1%} 均={r['avg']:+.2%} PF={r['pf']:.2f}")

# ============================================================
# 最优参数Walk-Forward
# ============================================================
print("\n--- 最优参数 Walk-Forward ---")
best_squeeze = 0.50
best_lookback = 24

for name, data in [("训练集", train), ("验证集", val), ("测试集", test)]:
    r = backtest_spring(data, best_squeeze, best_lookback, 'long')
    if r:
        print(f"  {name}: 交易={r['trades']} 胜率={r['win_rate']:.1%} 均={r['avg']:+.2%} PF={r['pf']:.2f}")
    else:
        print(f"  {name}: 无有效结果")

# ============================================================
# 波动率弹簧 vs RSI 对比（两种策略对比）
# ============================================================
print("\n" + "="*65)
print("对比: 波动率弹簧 vs RSI均值回归")
print("="*65)

def backtest_rsi(data, rsi_thresh=35):
    if len(data) < 50:
        return None
    sig_arr = (data['rsi'].values < rsi_thresh).astype(int)
    close_arr = data['close'].values.astype(float)
    trades = []
    pos = None
    for i in range(30, len(sig_arr) - 5):
        if sig_arr[i] == 1 and pos is None:
            pos = i
            entry = float(close_arr[i])
        elif pos is not None and sig_arr[i] == 0:
            ret = (float(close_arr[i]) - entry) / entry
            trades.append(ret)
            pos = None
    if not trades or len(trades) < 5:
        return None
    wins = [r for r in trades if r > 0]
    losses = [r for r in trades if r < 0]
    return {'trades': len(trades), 'win_rate': len(wins)/len(trades),
            'avg': np.mean(trades), 'pf': abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999}

for name, data in [("训练集", train), ("验证集", val), ("测试集", test)]:
    spring = backtest_spring(data, 0.50, 24, 'long')
    rsi = backtest_rsi(data, 35)
    print(f"\n{name}:")
    if spring:
        print(f"  弹簧策略: 交易={spring['trades']} 胜率={spring['win_rate']:.1%} 均={spring['avg']:+.2%} PF={spring['pf']:.2f}")
    else:
        print(f"  弹簧策略: 无结果")
    if rsi:
        print(f"  RSI<35:   交易={rsi['trades']} 胜率={rsi['win_rate']:.1%} 均={rsi['avg']:+.2%} PF={rsi['pf']:.2f}")
    else:
        print(f"  RSI<35:   无结果")

# ============================================================
# 弹簧+方向过滤: 仅在EMA200下方时做多（逆势）
# ============================================================
print("\n" + "="*65)
print("弹簧策略 + EMA200方向过滤（仅在EMA200下方做多=逆势）")
print("="*65)

def backtest_spring_filtered(data, squeeze_thresh=0.50, lookback=24, filter_ema=True):
    if len(data) < 50:
        return None
    close_arr = data['close'].values.astype(float)
    squeeze = data['atr'].values < data['atr_ma'].values * squeeze_thresh
    at_lower = close_arr <= data['range_low'].values * 1.01
    above_200 = close_arr > data['ema200'].values

    if filter_ema:
        sig = squeeze & at_lower & ~above_200  # EMA200下方才做多
    else:
        sig = squeeze & at_lower

    sig_arr = sig.astype(int)
    trades = []
    pos = None
    for i in range(30, len(sig_arr) - 5):
        if sig_arr[i] == 1 and pos is None:
            pos = i
            entry = float(close_arr[i])
        elif pos is not None and sig_arr[i] == 0:
            ret = (float(close_arr[i]) - entry) / entry
            trades.append(ret)
            pos = None
    if not trades or len(trades) < 5:
        return None
    wins = [r for r in trades if r > 0]
    losses = [r for r in trades if r < 0]
    return {'trades': len(trades), 'win_rate': len(wins)/len(trades),
            'avg': np.mean(trades), 'pf': abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999,
            'wins': len(wins), 'losses': len(losses)}

for name, data in [("训练集", train), ("验证集", val), ("测试集", test)]:
    r = backtest_spring_filtered(data, 0.50, 24, filter_ema=True)
    if r:
        print(f"  {name}: 交易={r['trades']} 胜率={r['win_rate']:.1%} 均={r['avg']:+.2%} PF={r['pf']:.2f} ({r['wins']}胜/{r['losses']}负)")
    else:
        print(f"  {name}: 无结果")

# ============================================================
# 最终判决
# ============================================================
print("\n" + "="*65)
print("最终判决")
print("="*65)
train_r = backtest_spring_filtered(train, 0.50, 24, True)
val_r = backtest_spring_filtered(val, 0.50, 24, True)
test_r = backtest_spring_filtered(test, 0.50, 24, True)

if train_r and val_r:
    pf_decay = (train_r['pf'] - val_r['pf']) / train_r['pf'] if train_r['pf'] > 0 else 999
    wr_decay = (train_r['win_rate'] - val_r['win_rate']) / train_r['win_rate'] if train_r['win_rate'] > 0 else 999
    falsified = pf_decay > 0.5 or val_r['pf'] < 1.0 or val_r['win_rate'] < 0.40
    print(f"  训练集: PF={train_r['pf']:.2f} 胜率={train_r['win_rate']:.1%}")
    print(f"  验证集: PF={val_r['pf']:.2f} 胜率={val_r['win_rate']:.1%} (衰减={pf_decay:.1%})")
    print(f"  测试集: PF={test_r['pf'] if test_r else 'N/A'} 胜率={test_r['win_rate']:.1%} ({test_r['trades'] if test_r else 0}笔)")
    print(f"  证伪: {'🚫 YES' if falsified else '✅ NO'}")
