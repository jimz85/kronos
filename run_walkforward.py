#!/usr/bin/env python3
"""
Walk-Forward Analysis - 使用本地CSV数据
"""
import numpy as np
import pandas as pd
import json
import warnings
warnings.filterwarnings('ignore')

def calc_rsi(prices, period=14):
    d = np.diff(prices, prepend=prices[0])
    g = np.where(d > 0, d, 0)
    l = np.where(d < 0, -d, 0)
    ag = pd.Series(g).rolling(period).mean()
    al = pd.Series(l).rolling(period).mean()
    return 100 - (100 / (1 + ag / (al + 1e-10)))

def calc_ema(prices, period):
    return pd.Series(prices).ewm(span=period, adjust=False).mean()

def load_csv(path):
    df = pd.read_csv(path, skiprows=3, names=['datetime','close','high','low','open','volume'])
    df['timestamp'] = pd.to_datetime(df['datetime'])
    df = df.set_index('timestamp').sort_index()
    df = df[['open','high','low','close','volume']].astype(float)
    return df

COINS = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "DOGE-USD"]

DATA_FILES = {
    "BTC-USD": "/tmp/btc_1h_processed.csv",
    "ETH-USD": "/tmp/eth_1h.csv",
    "SOL-USD": "/tmp/sol_1h.csv",
    "BNB-USD": "/tmp/bnb_1h.csv",
    "DOGE-USD": "/tmp/doge_1h.csv",
}

for coin, path in DATA_FILES.items():
    if coin == "BTC-USD":
        continue
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{coin.replace('-','')}?interval=1h&period1=1713139200&period2=1744675200"
    print(f"Need to download {coin} data...")

print("Using BTC-USD 1H data from /tmp/btc_1h_processed.csv")

# Load data
df = load_csv("/tmp/btc_1h_processed.csv")
print(f"Loaded {len(df)} bars: {df.index.min()} → {df.index.max()}")

# Split
train = df[df.index < '2025-01-01']
val   = df[(df.index >= '2025-01-01') & (df.index < '2025-04-01')]
test  = df[df.index >= '2025-04-01']
print(f"Train: {len(train)} bars | Val: {len(val)} bars | Test: {len(test)} bars")

def backtest(data, rsi_buy, rsi_sell, stop_pct, target_pct, hold_max, use_ema20_filter=False):
    """1H回测"""
    if len(data) < 100:
        return None

    close = data['close'].values.astype(float)
    high  = data['high'].values.astype(float)
    low   = data['low'].values.astype(float)
    rsi   = calc_rsi(close, 14).values

    # EMA20 filter (4H equivalent: 4 bars)
    ema20_4h = calc_ema(close[::4], 20) if len(close) > 4 else None
    if ema20_4h is not None:
        ema20_4h_expanded = np.repeat(ema20_4h.values, 4)[:len(close)]

    # 未创新低（前4根1H K线的最低价）
    rolling_low = pd.Series(low).rolling(4).min().shift(1).values

    trades = []
    pos = None

    for i in range(20, len(close) - hold_max - 1):
        price = float(close[i])
        rsi_val = float(rsi[i])
        prev_low4 = float(rolling_low[i])

        if pos is None:
            # 入场条件
            ema_ok = (not use_ema20_filter) or (i >= 4 and float(ema20_4h_expanded[i]) < price)
            rsi_ok = rsi_val < rsi_buy
            no_new_low = price >= prev_low4

            if ema_ok and rsi_ok and no_new_low:
                pos = i
        else:
            curr_price = float(close[i])
            hold = i - pos
            ret = (curr_price - float(close[pos])) / float(close[pos])

            # 出场
            if curr_price <= float(close[pos]) * (1 - stop_pct):
                trades.append(ret)
                pos = None
            elif curr_price >= float(close[pos]) * (1 + target_pct):
                trades.append(ret)
                pos = None
            elif rsi_val > rsi_sell:
                trades.append(ret)
                pos = None
            elif hold >= hold_max:
                trades.append(ret)
                pos = None

    if not trades:
        return {'trades': 0, 'win_rate': 0, 'pf': 0, 'avg': 0, 'total': 0}

    wins   = [r for r in trades if r > 0]
    losses = [r for r in trades if r < 0]
    return {
        'trades': len(trades),
        'win_rate': len(wins)/len(trades),
        'pf': abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 999,
        'avg': np.mean(trades),
        'total': sum(trades),
    }

# ============================================================
# 阶段1: 训练集暴力搜索
# ============================================================
print("\n" + "="*60)
print("🔍 阶段1: 训练集 (2024-04至2024-12) 参数搜索")
print("="*60)

best = None
all_results = []

for rsi_buy in [25, 30, 35, 40, 45]:
    for rsi_sell in [50, 55, 60, 65, 70, 75]:
        for stop_pct in [0.015, 0.02, 0.025, 0.03, 0.04]:
            for target_pct in [0.01, 0.02, 0.03, 0.04, 0.05]:
                for hold_max in [4, 8, 12, 16, 24]:
                    for ema20 in [True, False]:
                        r = backtest(train, rsi_buy, rsi_sell, stop_pct, target_pct, hold_max, ema20)
                        if not r or r['trades'] < 20:
                            continue
                        score = r['pf'] * r['win_rate']
                        all_results.append({
                            'rsi_buy': rsi_buy, 'rsi_sell': rsi_sell,
                            'stop': stop_pct, 'target': target_pct,
                            'hold': hold_max, 'ema20': ema20,
                            **r, 'score': score
                        })

if not all_results:
    print("❌ 训练集无有效参数")
    exit(1)

all_results.sort(key=lambda x: x['score'], reverse=True)
best = all_results[0]

print(f"\n🏆 最优参数 (top5):")
for i, r in enumerate(all_results[:5]):
    print(f"  #{i+1}: RSI<{r['rsi_buy']}/{r['rsi_sell']} 止{r['stop']:.1%} 目{r['target']:.1%} 持{r['hold']}h EMA20={r['ema20']}")
    print(f"       交易={r['trades']} 胜率={r['win_rate']:.1%} PF={r['pf']:.2f} 均收益={r['avg']:.2%}")

# ============================================================
# 阶段2: 验证集盲测
# ============================================================
print("\n" + "="*60)
print("🎯 阶段2: 验证集 (2025Q1) 盲测")
print("="*60)

val_r = backtest(val, best['rsi_buy'], best['rsi_sell'],
                 best['stop'], best['target'], best['hold'], best['ema20'])

if not val_r or val_r['trades'] < 5:
    print("❌ 验证集交易不足")
    exit(1)

wr_decay = (best['win_rate'] - val_r['win_rate']) / best['win_rate'] if best['win_rate'] > 0 else 1.0
avg_decay = (best['avg'] - val_r['avg']) / best['avg'] if best['avg'] > 0 else 1.0
falsified = wr_decay > 0.5 or val_r['win_rate'] < 0.40

print(f"\n训练集: 胜率={best['win_rate']:.1%} PF={best['pf']:.2f} 均收益={best['avg']:.2%}")
print(f"验证集: 胜率={val_r['win_rate']:.1%} PF={val_r['pf']:.2f} 均收益={val_r['avg']:.2%}")
print(f"胜率衰减: {wr_decay:.1%}")
print(f"平均收益衰减: {avg_decay:.1%}")
print(f"证伪: {'🚫 YES' if falsified else '✅ NO'}")

# ============================================================
# 阶段3: 测试集
# ============================================================
print("\n" + "="*60)
print("⚖️ 阶段3: 测试集 (2025Q2至2026Q1)")
print("="*60)

test_r = backtest(test, best['rsi_buy'], best['rsi_sell'],
                  best['stop'], best['target'], best['hold'], best['ema20'])

if not test_r:
    print("❌ 测试集无结果")
else:
    print(f"测试集: 交易={test_r['trades']} 胜率={test_r['win_rate']:.1%} PF={test_r['pf']:.2f} 均收益={test_r['avg']:.2%} 总收益={test_r['total']:.1%}")

# ============================================================
# 平滑性测试
# ============================================================
print("\n" + "="*60)
print("📈 平滑性测试: RSI±5微调")
print("="*60)

rsi_base = best['rsi_buy']
smooth_results = []
for delta in [-5, -3, 0, 3, 5]:
    rsi_test = max(15, rsi_base + delta)
    r = backtest(train, rsi_test, best['rsi_sell'], best['stop'], best['target'], best['hold'], best['ema20'])
    if r:
        smooth_results.append({'delta': delta, 'rsi': rsi_test, **r})
        print(f"  RSI {rsi_test} (Δ{delta:+d}): 胜率={r['win_rate']:.1%} PF={r['pf']:.2f}")

pf_base = best['pf']
pf_min = min(r['pf'] for r in smooth_results)
stability = pf_min / pf_base if pf_base > 0 else 0
print(f"\nPF稳定性: {stability:.1%} (最小={pf_min:.2f} / 基准={pf_base:.2f})")
print(f"结论: {'✅ 平滑' if stability > 0.7 else '❌ 尖峰 - 过拟合风险'}")

# ============================================================
# 保存
# ============================================================
summary = {
    'coin': 'BTC-USD',
    'train_period': f"{train.index.min().date()} → {train.index.max().date()}",
    'val_period': f"{val.index.min().date()} → {val.index.max().date()}",
    'test_period': f"{test.index.min().date()} → {test.index.max().date()}",
    'best_params': {k: v for k, v in best.items() if k not in ['trades','win_rate','pf','avg','total','score']},
    'train_result': {k: v for k, v in best.items() if k in ['trades','win_rate','pf','avg','total']},
    'val_result': val_r,
    'val_wr_decay': wr_decay,
    'val_falsified': falsified,
    'test_result': test_r,
    'smooth_stability': stability,
    'is_smooth': stability > 0.7,
}

out = "/Users/jimingzhang/kronos/walk_forward_BTC_USD.json"
with open(out, 'w') as f:
    json.dump(summary, f, indent=2, default=str)
print(f"\n💾 结果: {out}")

print("\n" + "="*60)
print("最终结论")
print("="*60)
if falsified:
    print("🚫 策略证伪，训练参数在验证集上表现差太多")
elif not test_r:
    print("⚠️ 测试集无有效结果")
elif test_r['win_rate'] < 0.40:
    print(f"⚠️ 测试集胜率仅 {test_r['win_rate']:.1%}，不推荐实盘")
else:
    print(f"✅ 策略通过检验")
    print(f"   训练胜率: {best['win_rate']:.1%}")
    print(f"   验证胜率: {val_r['win_rate']:.1%} (衰减 {wr_decay:.1%})")
    print(f"   测试胜率: {test_r['win_rate']:.1%}")
    print(f"   平滑性: {'✅' if stability > 0.7 else '❌'}")
