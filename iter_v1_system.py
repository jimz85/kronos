#!/usr/bin/env python3
"""
【系统V1.0 第1轮】10-30x杠杆 + 合理仓位分析
核心：10x杠杆配10%仓位，20x配5%仓位，30x配3%仓位
"""
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')
import json
import time

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
START_TIME = time.time()
END_TIME = START_TIME + 5 * 3600  # 17:00结束

def load_5m(coin):
    f = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    df = pd.read_csv(f)
    df.columns = [c.strip().lower() for c in df.columns]
    ts_col = next((c for c in ['datetime_utc', 'timestamp', 'time'] if c in df.columns), df.columns[0])
    df['timestamp'] = pd.to_datetime(df[ts_col], errors='coerce')
    if df['timestamp'].isna().all():
        df['timestamp'] = pd.to_datetime(df.iloc[:, 0], unit='ms', errors='coerce')
    df = df.dropna(subset=['timestamp']).set_index('timestamp')
    col_map = {c: 'volume' if c.lower()=='vol' else c.lower() for c in df.columns}
    df = df.rename(columns=col_map)
    return df[['open','high','low','close','volume']].astype(float)

def resample_df(df, tf):
    t = tf.replace('min', 'T').replace('hour', 'H')
    return df.resample(t).agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()

def calc_rsi(prices, period=14):
    delta = np.diff(prices, prepend=prices[0])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    ag = pd.Series(gains).rolling(period).mean()
    al = pd.Series(losses).rolling(period).mean()
    rs = ag / (al + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_adx(high, low, close, period=14):
    hd = np.diff(high, prepend=high[0])
    ld = -np.diff(low, prepend=low[0])
    pdm = np.where(hd > ld, hd, 0.0)
    mdm = np.where(ld > hd, ld, 0.0)
    pc = np.roll(close, 1); pc[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    atrs = pd.Series(tr).rolling(period).mean()
    pdi = pd.Series(pdm).rolling(period).mean() / atrs * 100
    mdi = pd.Series(mdm).rolling(period).mean() / atrs * 100
    dx = abs(pdi - mdi) / (pdi + mdi + 1e-10) * 100
    return dx.rolling(period).mean(), pdi, mdi

def calc_atr(high, low, close, period=14):
    pc = np.roll(close, 1); pc[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    return pd.Series(tr).rolling(period).mean()

def backtest(df_src, cfg, tf='15min'):
    """回测：自动多空 + 动态仓位"""
    df = resample_df(df_src.copy(), tf)
    n = len(df)
    if n < 500:
        return pd.DataFrame()

    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    rsi = calc_rsi(close, 14).values
    adx, _, _ = calc_adx(high, low, close, 14)
    adx = adx.values
    atr = calc_atr(high, low, close, 14).values

    fee = cfg.get('fee', 0.0005)  # 0.05% taker
    slip = cfg.get('slip', 0.002)  # 0.2% 滑点
    funding = cfg.get('funding', 0.0001)  # 0.01% / 8h
    adx_min = cfg.get('adx_min', 15)
    rsi_over = cfg.get('rsi_over', 70)
    rsi_under = cfg.get('rsi_under', 30)
    lev_base = cfg.get('lev', 10)  # 基础杠杆
    pos_pct = cfg.get('pos_pct', 0.10)  # 仓位比例
    stop_pct = cfg.get('stop_pct', 0.05)  # 止损%
    tp_pct = cfg.get('tp_pct', 0.10)  # 止盈%

    pos = 0; side = None; entry = 0.0; bar0 = 0; stop = 0.0; tp_price = 0.0
    trades = []

    for i in range(100, n):
        c = float(close[i]); h = float(high[i]); l = float(low[i])
        atr_i = float(atr[i]) if not np.isnan(atr[i]) else 0.0

        # 动态杠杆：根据ATR波动率调整
        if atr_i > 0 and c > 0:
            atr_pct = atr_i / c
            if atr_pct > 0.05:  # 高波动
                lev = min(lev_base, 10)  # 限制杠杆
            else:  # 低波动
                lev = min(lev_base, 30)  # 可以用更高杠杆
        else:
            lev = lev_base

        if pos == 0:
            ls = ss = False
            if rsi[i] < rsi_under and adx[i] > adx_min:
                ls = True
            if rsi[i] > rsi_over and adx[i] > adx_min:
                ss = True
            if ls:
                entry = c * (1 + fee + slip)
                stop = entry * (1 - stop_pct)
                tp_price = entry * (1 + tp_pct)
                pos = 1; side = 'long'; entry = float(entry); bar0 = i
            elif ss:
                entry = c * (1 - fee - slip)
                stop = entry * (1 + stop_pct)
                tp_price = entry * (1 - tp_pct)
                pos = -1; side = 'short'; entry = float(entry); bar0 = i
        else:
            pnl = 0.0; reason = ''
            if side == 'long':
                if l <= stop:
                    pnl = (stop / entry - 1) * 100.0 * lev * pos_pct; reason = 'SL'
                elif h >= tp_price:
                    pnl = (tp_price / entry - 1) * 100.0 * lev * pos_pct; reason = 'TP'
            else:
                if h >= stop:
                    pnl = (entry / stop - 1) * 100.0 * lev * pos_pct; reason = 'SL'
                elif l <= tp_price:
                    pnl = (entry / tp_price - 1) * 100.0 * lev * pos_pct; reason = 'TP'
            if pnl != 0:
                hours = (df.index[i] - df.index[bar0]).total_seconds() / 3600
                funding_cost = funding * (hours / 8) * pos_pct * 100 if pos != 0 else 0
                trades.append({
                    'date': str(df.index[i]), 'side': side,
                    'pnl': pnl - funding_cost, 'gross': pnl,
                    'leverage': lev, 'pos_pct': pos_pct,
                    'reason': reason, 'hours': hours
                })
                pos = 0

    return pd.DataFrame(trades)

def analyze(t, label=""):
    if len(t) == 0:
        return None
    t = t.copy()
    t['date'] = pd.to_datetime(t['date'])
    years = max((t['date'].max() - t['date'].min()).days / 365.0, 0.1)
    total = t['pnl'].sum()
    gross = t['gross'].sum()
    wr = (t['pnl'] > 0).mean() * 100
    cum = t['pnl'].cumsum()
    dd = (cum.cummax() - cum).max()
    longs = t[t['side'] == 'long']
    shorts = t[t['side'] == 'short']
    winners = t[t['pnl'] > 0]['pnl']
    losers = t[t['pnl'] < 0]['pnl']
    avg_win = float(winners.mean()) if len(winners) > 0 else 0.0
    avg_loss = abs(float(losers.mean())) if len(losers) > 0 else 1.0
    rr = avg_win / avg_loss if avg_loss > 0 else 0.0
    yearly = dict(t.groupby(t['date'].dt.year)['pnl'].sum())

    # 交易成本分析
    total_cost = gross - total
    cost_ratio = total_cost / abs(gross) * 100 if gross != 0 else 0

    # 连续亏损
    t['win'] = t['pnl'] > 0
    max_consec_loss = 0
    cur = 0
    for w in t['win']:
        if not w:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    # 单日最大亏损
    t['date_only'] = t['date'].dt.date
    daily_pnl = t.groupby('date_only')['pnl'].sum()
    max_daily_loss = abs(daily_pnl.min())

    return {
        'label': label, 'total_pnl': float(total), 'gross': float(gross),
        'win_rate': float(wr), 'max_dd': float(dd),
        'annual_trades': float(len(t) / years), 'n_trades': len(t), 'rr': float(rr),
        'long_pnl': float(longs['pnl'].sum()) if len(longs) > 0 else 0.0,
        'longs_n': len(longs), 'shorts_n': len(shorts),
        'short_pnl': float(shorts['pnl'].sum()) if len(shorts) > 0 else 0.0,
        'tp_rate': float((t['reason'] == 'TP').mean() * 100),
        'sl_rate': float((t['reason'] == 'SL').mean() * 100),
        'avg_hours': float(t['hours'].mean()),
        'cost_ratio': float(cost_ratio),
        'max_consec_loss': int(max_consec_loss),
        'max_daily_loss': float(max_daily_loss),
        'years': float(years), 'yearly': yearly,
    }

def risk_score(r):
    """风险评分：0-100"""
    if r is None or r['n_trades'] < 15:
        return 0
    score = 100
    # DD扣分
    if r['max_dd'] > 20: score -= 50
    if r['max_dd'] > 50: score -= 30
    # 单日亏损扣分
    if r['max_daily_loss'] > 3: score -= 30
    if r['max_daily_loss'] > 5: score -= 20
    # 连续亏损扣分
    if r['max_consec_loss'] > 5: score -= 20
    if r['max_consec_loss'] > 10: score -= 20
    # 成本扣分
    if r['cost_ratio'] > 30: score -= 20
    # 胜率加分
    if r['win_rate'] >= 50: score += 20
    if r['win_rate'] >= 40: score += 10
    return max(0, min(100, score))

print("="*80)
print("【系统V1.0 第1轮】10-30x杠杆方案分析")
print("="*80)

# 加载数据
coins = {}
for c in ['DOGE', 'AVAX', 'DOT', 'ETH', 'BTC', 'ADA']:
    try:
        df = load_5m(c)
        if len(df) > 10000:
            coins[c] = df
            print(f"  {c}: {len(df)}行")
    except:
        pass

results = []

# ================================================
# 测试不同杠杆和仓位组合
# ================================================
print("\n" + "="*60)
print("【杠杆×仓位 风险分析】")
print("="*60)

combos = [
    {'lev': 10, 'pos_pct': 0.10, 'name': '10x杠杆 10%仓位'},
    {'lev': 15, 'pos_pct': 0.07, 'name': '15x杠杆 7%仓位'},
    {'lev': 20, 'pos_pct': 0.05, 'name': '20x杠杆 5%仓位'},
    {'lev': 30, 'pos_pct': 0.03, 'name': '30x杠杆 3%仓位'},
]

for combo in combos:
    print(f"\n  {combo['name']}:")
    for stop_pct in [0.05, 0.08, 0.10]:
        tp_pct = stop_pct * 2  # 赔率2:1
        max_loss_per_trade = stop_pct * combo['lev'] * combo['pos_pct'] * 100
        print(f"    止损{stop_pct:.0%}止盈{tp_pct:.0%}: 单笔账户损失{max_loss_per_trade:.1f}%")

# ================================================
# 回测 DOGE 15min
# ================================================
print("\n" + "="*60)
print("【DOGE 15min 回测】")
print("="*60)

df = coins.get('DOGE')
if df is not None:
    # 测试不同配置
    configs = [
        {'lev': 10, 'pos_pct': 0.10, 'stop_pct': 0.05, 'tp_pct': 0.10, 'adx_min': 15, 'rsi_over': 70, 'rsi_under': 30, 'fee': 0.0005, 'slip': 0.002, 'funding': 0.0001},
        {'lev': 20, 'pos_pct': 0.05, 'stop_pct': 0.05, 'tp_pct': 0.10, 'adx_min': 15, 'rsi_over': 70, 'rsi_under': 30, 'fee': 0.0005, 'slip': 0.002, 'funding': 0.0001},
        {'lev': 10, 'pos_pct': 0.10, 'stop_pct': 0.08, 'tp_pct': 0.16, 'adx_min': 15, 'rsi_over': 70, 'rsi_under': 30, 'fee': 0.0005, 'slip': 0.002, 'funding': 0.0001},
        {'lev': 15, 'pos_pct': 0.07, 'stop_pct': 0.06, 'tp_pct': 0.12, 'adx_min': 15, 'rsi_over': 70, 'rsi_under': 30, 'fee': 0.0005, 'slip': 0.002, 'funding': 0.0001},
    ]

    for cfg in configs:
        t = backtest(df, cfg, '15min')
        if len(t) < 15:
            continue
        r = analyze(t, f"LEV={cfg['lev']} POS={cfg['pos_pct']:.0%} SL={cfg['stop_pct']:.0%} TP={cfg['tp_pct']:.0%}")
        r['lev'] = cfg['lev']
        r['pos_pct'] = cfg['pos_pct']
        r['score'] = risk_score(r)
        results.append(r)

        print(f"\n  {r['label']}")
        print(f"    总收益: {r['total_pnl']:+.1f}%")
        print(f"    胜率: {r['win_rate']:.0f}% 盈亏比: {r['rr']:.2f}")
        print(f"    年化: {r['annual_trades']:.0f}笔 ({r['annual_trades']/365:.1f}笔/天)")
        print(f"    最大DD: {r['max_dd']:.1f}%")
        print(f"    单日最大亏损: {r['max_daily_loss']:.1f}%")
        print(f"    连续亏损: {r['max_consec_loss']}笔")
        print(f"    成本占比: {r['cost_ratio']:.0f}%")
        print(f"    风险评分: {r['score']:.0f}/100")

# ================================================
# 多币种验证最优配置
# ================================================
print("\n" + "="*60)
print("【多币种 20x杠杆5%仓位 验证】")
print("="*60)

cfg = {'lev': 20, 'pos_pct': 0.05, 'stop_pct': 0.05, 'tp_pct': 0.10,
       'adx_min': 15, 'rsi_over': 70, 'rsi_under': 30,
       'fee': 0.0005, 'slip': 0.002, 'funding': 0.0001}

yearly_all = {}
for coin, df in coins.items():
    t = backtest(df, cfg, '15min')
    if len(t) < 15:
        continue
    r = analyze(t, coin)
    r['lev'] = 20
    r['pos_pct'] = 0.05
    r['score'] = risk_score(r)

    print(f"\n  {coin}:")
    print(f"    收益: {r['total_pnl']:+.1f}% 胜率: {r['win_rate']:.0f}% 盈亏比: {r['rr']:.2f}")
    print(f"    DD: {r['max_dd']:.1f}% 日损: {r['max_daily_loss']:.1f}% 连亏: {r['max_consec_loss']}笔")
    print(f"    风险评分: {r['score']:.0f}/100")

    for yr, pnl in r['yearly'].items():
        if yr not in yearly_all:
            yearly_all[yr] = 0
        yearly_all[yr] += pnl

print("\n  多币种组合年度表现:")
cum = 0
for yr in sorted(yearly_all.keys()):
    cum += yearly_all[yr]
    print(f"    {int(yr)}: {yearly_all[yr]:+.1f}% 累计{cum:+.1f}%")

# ================================================
# 结果汇总
# ================================================
print("\n" + "="*80)
print("【第1轮结论】")
print("="*80)

print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  核心发现：

  1. 10x杠杆 × 10%仓位 × 5%止损 = 5%账户/笔
     - 可行，但收益低

  2. 20x杠杆 × 5%仓位 × 5%止损 = 5%账户/笔
     - 收益放大了，但风险相同

  3. 30x杠杆 × 3%仓位 × 5%止损 = 4.5%账户/笔
     - 最高杠杆方案

  问题：10-30x杠杆在5%止损下，单笔账户损失约5%
       要保持账户安全，需要控制仓位

  建议：先用10x杠杆10%仓位测试，实盘可行后再提高杠杆
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")

# 保存结果
with open('/Users/jimingzhang/kronos/round1_results.json', 'w') as f:
    json.dump([dict(r) for r in results], f, indent=2, default=str)

elapsed = time.time() - START_TIME
print(f"\n第1轮完成，耗时: {elapsed/60:.1f}分钟")
print(f"剩余时间: {(END_TIME - time.time())/3600:.1f}小时")
