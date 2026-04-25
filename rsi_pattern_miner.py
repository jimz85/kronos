#!/usr/bin/env python3
"""
RSI Pattern Miner v2 - 系统性挖掘历史盈利规律
修复: atr_ratio 是 ATR/ATR_MA，不是原始 ATR_PCT
"""
import pandas as pd
import numpy as np
import json, os, sys
from itertools import product
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OUT_DIR = Path.home() / '.hermes' / 'cron' / 'output'
OUT_DIR.mkdir(parents=True, exist_ok=True)

COINS = ['BTC', 'ETH', 'BNB', 'DOGE', 'ADA', 'AVAX']

# 参数网格
RSI_L = [20, 25, 28, 30, 32, 35, 40]  # 做多RSI阈值
RSI_S = [60, 65, 70, 75, 80]           # 做空RSI阈值
LEV = [1, 2, 3]
SL_PCT = [0.5, 1.0, 1.5, 2.0]          # 止损%
TP_PCT = [1.5, 2.0, 3.0]               # 止盈%
COOLDOWN = [3, 6]                        # 冷却K线数

START = '2022-01-01'
END = '2026-04-15'

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
    tr = pd.concat([tr1,tr2,tr3], axis=1).max(axis=1)
    return tr.rolling(n).mean()

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
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    return dx.rolling(n).mean()

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
    
    dt_col = next((c for c in cols if c in ('datetime_utc', 'datetime_utc.1', 'datetime', 'date', 'timestamp')), cols[0])
    vol_col = 'volume' if 'volume' in df.columns else ('vol' if 'vol' in df.columns else None)
    
    result = pd.DataFrame()
    for col in ['open', 'high', 'low', 'close']:
        if col in df.columns: result[col] = df[col]
    result['volume'] = df[vol_col] if vol_col and vol_col in df.columns else 0
    result['ts'] = pd.to_datetime(df[dt_col]).dt.tz_localize(None)
    result = result.set_index('ts').sort_index()
    return result[result['close'] > 0]

def resample(df, rule):
    return df[['open','high','low','close','volume']].resample(rule).agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum'
    }).dropna()

def simulate(df, rsi_l, rsi_s, lev, sl_pct, tp_pct, cooldown):
    """单参数组回测"""
    df = df.copy()
    df['rsi'] = calc_rsi(df['close'], 14)
    df['atr'] = calc_atr(df['high'], df['low'], df['close'], 14)
    df['atr_pct'] = df['atr'] / df['close']
    df['atr_ratio'] = df['atr_pct'] / df['atr_pct'].rolling(20).mean()  # ATR相对历史
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['adx'] = calc_adx(df)
    df['vol_ma'] = df['volume'].rolling(20).mean()
    df['vol_ratio'] = df['volume'] / (df['vol_ma'] + 1e-10)
    
    df['trend_up'] = df['ema20'] > df['ema50']
    df['trend_down'] = df['ema20'] < df['ema50']
    
    df = df.dropna()
    if len(df) < 100: return None
    
    position = 0
    entry_price = 0
    entry_bar = -9999
    equity = 1.0
    trades = []
    
    records = df.iloc[50:].iterrows()  # 预热50根K线
    bars = list(records)
    
    for idx, row in bars:
        bar_idx = df.index.get_loc(idx)
        rsi_val = row['rsi']
        adx_val = row['adx']
        atr_ratio = row['atr_ratio']
        vol_ratio = row['vol_ratio']
        price = row['close']
        is_up = row['trend_up']
        is_down = row['trend_down']
        
        bars_since = bar_idx - entry_bar
        
        # 平仓
        if position != 0 and bars_since > cooldown:
            if position == 1:
                pnl_pct = (price - entry_price) / entry_price * lev
            else:
                pnl_pct = (entry_price - price) / entry_price * lev
            
            if pnl_pct <= -sl_pct / 100:
                equity *= (1 - sl_pct / 100)
                trades.append({'type':'loss', 'pnl':-sl_pct/100, 'rsi':rsi_val, 'adx':adx_val, 'atr_ratio':atr_ratio, 'vol_ratio':vol_ratio})
                position = 0
            elif pnl_pct >= tp_pct / 100:
                equity *= (1 + tp_pct / 100)
                trades.append({'type':'win', 'pnl':tp_pct/100, 'rsi':rsi_val, 'adx':adx_val, 'atr_ratio':atr_ratio, 'vol_ratio':vol_ratio})
                position = 0
        
        # 开多
        if position == 0 and is_up and rsi_val < rsi_l:
            # RSI反弹: 当前RSI >= 前一根RSI
            if bar_idx > 0:
                rsi_prev = df['rsi'].iloc[bar_idx - 1]
                if rsi_val >= rsi_prev:  # RSI正在反弹
                    position = 1
                    entry_price = price
                    entry_bar = bar_idx
        
        # 开空
        if position == 0 and is_down and rsi_val > rsi_s:
            if bar_idx > 0:
                rsi_prev = df['rsi'].iloc[bar_idx - 1]
                if rsi_val <= rsi_prev:  # RSI正在回落
                    position = -1
                    entry_price = price
                    entry_bar = bar_idx
    
    return {
        'equity': equity,
        'n_trades': len(trades),
        'wins': sum(1 for t in trades if t['type']=='win'),
        'losses': sum(1 for t in trades if t['type']=='loss'),
        'avg_rsi': np.mean([t['rsi'] for t in trades]) if trades else np.nan,
        'avg_adx': np.mean([t['adx'] for t in trades]) if trades else np.nan,
        'avg_vol_ratio': np.mean([t['vol_ratio'] for t in trades]) if trades else np.nan,
    }

def main():
    print("RSI Pattern Miner v2")
    print(f"时间: {START} ~ {END}")
    print(f"组合数: {len(RSI_L)*len(RSI_S)*len(LEV)*len(SL_PCT)*len(TP_PCT)*len(COOLDOWN)} per coin")
    
    all_results = []
    
    for coin in COINS:
        print(f"\n处理 {coin}...", end=' ', flush=True)
        df = load_data(coin)
        if df is None:
            print("无数据"); continue
        
        df = df[START:END]
        ohlc = resample(df, '15min')
        print(f"({len(ohlc)}根K线)", end=' ')
        
        n_done = 0
        for rsi_l, rsi_s, lev, sl, tp, cd in product(RSI_L, RSI_S, LEV, SL_PCT, TP_PCT, COOLDOWN):
            if rsi_s <= rsi_l: continue
            
            result = simulate(ohlc, rsi_l, rsi_s, lev, sl, tp, cd)
            if result is None or result['n_trades'] < 20: continue
            
            wr = result['wins'] / result['n_trades']
            net = result['equity'] - 1
            loss_rate = result['losses'] / result['n_trades']
            
            # 年化估算
            ann_factor = (365 * 24 * 4) / max(result['n_trades'], 1)  # 15min bars per year
            ann_return = (result['equity'] ** ann_factor - 1) if result['equity'] > 0 else -1
            
            all_results.append({
                'coin': coin,
                'rsi_l': rsi_l, 'rsi_s': rsi_s,
                'lev': lev, 'sl': sl, 'tp': tp, 'cooldown': cd,
                **result,
                'win_rate': wr,
                'net_return': net,
                'ann_return': ann_return,
                'loss_rate': loss_rate,
                'rr': tp / sl,
            })
            n_done += 1
        
        print(f"→ {n_done}个有效组合")
    
    all_results.sort(key=lambda x: x['equity'], reverse=True)
    
    # ===== Top 20 =====
    print(f"\n\n{'='*80}")
    print("TOP 20 最赚钱组合")
    print(f"{'='*80}")
    
    for i, r in enumerate(all_results[:20]):
        ann = r['ann_return'] * 100
        print(f"\n#{i+1} {r['coin']} | equity={r['equity']:.4f} | 年化={ann:+.1f}%")
        print(f"   LONG: RSI<{r['rsi_l']} | SHORT: RSI>{r['rsi_s']} | 杠杆={r['lev']}x")
        print(f"   SL={r['sl']}% TP={r['tp']}% | 赔率={r['rr']}:1 | 冷却={r['cooldown']}根")
        print(f"   胜率={r['win_rate']:.1%} | {r['n_trades']}笔交易 | ADX均值={r['avg_adx']:.1f} | VolRatio={r['avg_vol_ratio']:.2f}x")
    
    # ===== 按币种汇总 =====
    print(f"\n\n{'='*80}")
    print("各币种最佳参数")
    for coin in COINS:
        coin_results = [r for r in all_results if r['coin'] == coin]
        if not coin_results: continue
        best = max(coin_results, key=lambda x: x['equity'])
        ann = best['ann_return'] * 100
        print(f"\n{coin}: RSI<{best['rsi_l']} / RSI>{best['rsi_s']} | SL={best['sl']}% TP={best['tp']}% | {best['lev']}x")
        print(f"  equity={best['equity']:.4f} | 年化={ann:+.1f}% | 胜率={best['win_rate']:.1%} | {best['n_trades']}笔")
        
        # 统计不同RSI_L的表现
        rsi_grp = {}
        for r in coin_results:
            k = r['rsi_l']
            if k not in rsi_grp: rsi_grp[k] = []
            rsi_grp[k].append(r['equity'])
        for k in sorted(rsi_grp):
            vals = rsi_grp[k]
            print(f"  RSI<{k}: avg_equity={np.mean(vals):.4f} ({len(vals)}组)")
    
    # ===== 不同止损止盈分析 =====
    print(f"\n\n{'='*80}")
    print("止损止盈赔率分析")
    for coin in COINS:
        coin_results = [r for r in all_results if r['coin'] == coin]
        if not coin_results: continue
        print(f"\n{coin}:")
        for tp in sorted(set(r['tp'] for r in coin_results)):
            for sl in sorted(set(r['sl'] for r in coin_results)):
                subset = [r for r in coin_results if r['tp']==tp and r['sl']==sl]
                if subset:
                    avg_eq = np.mean([r['equity'] for r in subset])
                    avg_wr = np.mean([r['win_rate'] for r in subset])
                    print(f"  TP={tp}% SL={sl}%: equity={avg_eq:.4f} WR={avg_wr:.1%}")
    
    out_file = OUT_DIR / 'rsi_pattern_results.json'
    with open(out_file, 'w') as f:
        json.dump(all_results, f, indent=2)
    print(f"\n\n完整结果: {out_file}")

if __name__ == '__main__':
    main()
