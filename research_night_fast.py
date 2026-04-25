"""
通宵量化研究 V2 — 快速参数扫描
==============================
阶段1: 粗粒度扫描（快速淘汰差参数）
阶段2: 细粒度围绕最优参数优化
重点: 盈亏比>2.0, Walk-Forward, 2022熊市
"""

import pandas as pd
import numpy as np
from datetime import datetime
import json, os, time, itertools, requests

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
os.makedirs('~/kronos/research_night', exist_ok=True)
OUT_DIR = os.path.expanduser('~/kronos/research_night')

COINS = ['DOGE', 'DOT', 'AVAX', 'ADA', 'BNB', 'BTC', 'ETH']
FEE = 0.002

def calc_rsi(close, period=14):
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.inf)
    return (100 - 100/(1+rs)).values

def calc_adx(high, low, close, period=14):
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    plus_dm = h.diff().clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    tr = pd.concat([h-l, abs(h-c.shift(1)), abs(l-c.shift(1))], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    di = 100 * plus_dm.rolling(period).mean() / atr
    di_neg = 100 * minus_dm.rolling(period).mean() / atr
    dx = 100 * abs(di-di_neg) / (di+di_neg)
    return dx.rolling(period).mean().values, di.values, di_neg.values

def load_data(coin):
    path = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    df = pd.read_csv(path)
    if 'timestamp' in df.columns:
        df['dt'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_localize(None)
    else:
        df['dt'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    df = df.set_index('dt').sort_index()
    vol_col = 'vol' if 'vol' in df.columns else ('volume' if 'volume' in df.columns else None)
    cols = ['open', 'high', 'low', 'close']
    if vol_col: cols.append(vol_col)
    return df[cols].tail(300000)  # 最多30万行

def bt(params, df):
    """回测，返回结果或None"""
    rsi_os = params['rsi_os']
    rsi_ob = params['rsi_ob']
    adx_min = params['adx_min']
    sl = params['sl']
    tp = params['tp']
    lev = params['lev']
    pos = params['pos']
    hold = params['hold']
    
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    n = len(df)
    if n < 300: return None
    
    rsi = calc_rsi(close)
    adx, _, _ = calc_adx(high, low, close)
    
    sig_long = (rsi < rsi_os) & (adx > adx_min)
    sig_short = (rsi > rsi_ob) & (adx > adx_min)
    
    capital = 10000
    pos_state = 0
    entry_price = 0
    entry_idx = 0
    last_exit = -999
    trades = []
    
    for i in range(60, n-1):
        if i - last_exit < 12: continue  # 1h冷却
        
        if pos_state == 0:
            if sig_long[i]:
                pos_state = 1; entry_price = close[i+1]; entry_idx = i+1
            elif sig_short[i]:
                pos_state = -1; entry_price = close[i+1]; entry_idx = i+1
        elif pos_state != 0:
            hold_bars = i - entry_idx
            pnl_pct = (close[i]-entry_price)/entry_price if pos_state==1 else (entry_price-close[i])/entry_price
            exit_price = None; reason = None; ret_pct = 0
            
            if pos_state == 1:
                if pnl_pct <= -sl: exit_price = entry_price*(1-sl); reason='SL'; ret_pct=-sl
                elif pnl_pct >= tp: exit_price = entry_price*(1+tp); reason='TP'; ret_pct=tp
            else:
                if pnl_pct <= -sl: exit_price = entry_price*(1+sl); reason='SL'; ret_pct=-sl
                elif pnl_pct >= tp: exit_price = entry_price*(1-tp); reason='TP'; ret_pct=tp
            
            if hold_bars >= hold and exit_price is None:
                exit_price = close[i]; reason='TO'; ret_pct = pnl_pct
            
            if exit_price is not None:
                net = ret_pct - FEE
                pnl = capital * pos * lev * net
                capital += pnl
                trades.append({'d': 'L' if pos_state==1 else 'S', 'ret': net, 'pnl': pnl, 'reason': reason, 'hold': hold_bars})
                last_exit = i; pos_state = 0
    
    if not trades: return None
    df_t = pd.DataFrame(trades)
    n_w = (df_t['pnl']>0).sum(); n_l = len(df_t)-n_w
    wr = n_w/len(df_t)
    avg_w = df_t[df_t['pnl']>0]['pnl'].mean() if n_w else 0
    avg_l = abs(df_t[df_t['pnl']<=0]['pnl'].mean()) if n_l else 1
    wlr = avg_w/avg_l if avg_l else 0
    total_ret = (capital-10000)/10000
    
    # 最大DD
    equity = [10000]
    for t in trades:
        equity.append(equity[-1]+t['pnl'])
    peak = np.maximum.accumulate(equity)
    dd = np.min(np.array(equity)/peak)
    
    years = (df.index[-1]-df.index[60]).days/365.25
    daily = len(trades)/max((df.index[-1]-df.index[60]).days, 1)
    
    return {
        'ret': total_ret, 'ann': ((1+total_ret)**(1/years)-1) if years>0.01 else 0,
        'dd': 1-dd, 'n': len(trades), 'wr': wr, 'wlr': wlr,
        'daily': daily, 'avg_w': avg_w, 'avg_l': avg_l,
        'start': str(df.index[60])[:10], 'end': str(df.index[-1])[:10],
    }

def wf_test(params, df):
    """Walk-Forward 3 split验证"""
    n = len(df); sz = n//4
    passed = 0; rets = []
    for i in range(3):
        train = df.iloc[max(0,i*sz-sz):i*sz]
        test = df.iloc[i*sz:min((i+1)*sz, n)]
        r1 = bt(params, train); r2 = bt(params, test)
        if r1 and r2 and r1['ret']>0 and r2['ret']>0: passed += 1
        if r2: rets.append(r2['ret'])
    return {'wf_pass': passed/3, 'wf_ret': np.mean(rets) if rets else 0}

def bear2022(params, df):
    """2022熊市验证"""
    try:
        d = df['2022-01-01':'2022-12-31']
        if len(d) < 1000: return None
        r = bt(params, d)
        return r['ret'] if r else None
    except: return None

def phase1_coarse_scan(coins):
    """阶段1: 粗粒度扫描"""
    print("\n📊 Phase 1: 粗粒度扫描")
    
    coarse_grid = {
        'rsi_os': [25, 30, 35],
        'rsi_ob': [65, 70, 75],
        'adx_min': [12, 15, 18],
        'sl': [0.03, 0.05, 0.08],
        'tp': [0.06, 0.10, 0.15, 0.20],
        'lev': [3, 5],
        'pos': [0.10, 0.15],
        'hold': [24, 48, 72],
    }
    
    combos = list(itertools.product(
        coarse_grid['rsi_os'], coarse_grid['rsi_ob'], coarse_grid['adx_min'],
        coarse_grid['sl'], coarse_grid['tp'], coarse_grid['lev'],
        coarse_grid['pos'], coarse_grid['hold']
    ))
    print(f"总组合数: {len(combos)}")
    
    results = []
    t0 = time.time()
    
    for coin in coins:
        df = load_data(coin)
        print(f"\n  {coin}: {len(df)}行", end=" ", flush=True)
        
        for idx, (rsi_os, rsi_ob, adx_min, sl, tp, lev, pos, hold) in enumerate(combos):
            if idx % 2000 == 0:
                print(f".", end="", flush=True)
            
            params = {'rsi_os': rsi_os, 'rsi_ob': rsi_ob, 'adx_min': adx_min,
                     'sl': sl, 'tp': tp, 'lev': lev, 'pos': pos, 'hold': hold}
            
            r = bt(params, df)
            if not r: continue
            
            # 过滤: 正收益 + WLR>1.5 + 日均>0.3
            if r['ret'] > 0 and r['wlr'] > 1.5 and r['daily'] > 0.3:
                r['coin'] = coin
                r['params'] = params
                results.append(r)
        
        print(f" ✓ {len(results)}个合格")
    
    elapsed = time.time() - t0
    print(f"\n⏱️ Phase 1耗时: {elapsed:.0f}s, 合格结果: {len(results)}")
    
    if results:
        df_r = pd.DataFrame(results).sort_values('wlr', ascending=False)
        print("\nTop 10 (按WLR排序):")
        print(f"{'币种':<6} {'收益':>8} {'年化':>8} {'DD':>6} {'WLR':>5} {'胜率':>6} {'日均':>5}")
        for _, row in df_r.head(10).iterrows():
            print(f"{row['coin']:<6} {row['ret']*100:>+7.1f}% {row['ann']*100:>+7.1f}% {row['dd']*100:>5.1f}% {row['wlr']:>5.2f} {row['wr']*100:>5.1f}% {row['daily']:>5.2f}")
        return df_r
    return None

def phase2_fine_tune(best_params_list, coins):
    """阶段2: 细粒度优化"""
    print("\n📊 Phase 2: 细粒度优化")
    
    fine_results = []
    t0 = time.time()
    
    for entry in best_params_list[:5]:  # 最多5组参数
        coin = entry['coin']
        base = entry['params']
        
        df = load_data(coin)
        print(f"\n  {coin} 精细优化 (base WLR={entry['wlr']:.2f})", end=" ", flush=True)
        
        # 围绕基础参数微调
        fine_grid = []
        for drs in [-3, 0, 3]:
            for drb in [-3, 0, 3]:
                for dadx in [-2, 0, 2]:
                    for dsl in [-0.01, 0, 0.01]:
                        for dtp in [-0.02, 0, 0.02, 0.03]:
                            for dlev in [-1, 0, 1]:
                                p = {
                                    'rsi_os': max(20, min(45, base['rsi_os']+drs)),
                                    'rsi_ob': min(80, max(55, base['rsi_ob']+drb)),
                                    'adx_min': max(8, min(25, base['adx_min']+dadx)),
                                    'sl': max(0.01, min(0.15, base['sl']+dsl)),
                                    'tp': max(0.05, min(0.30, base['tp']+dtp)),
                                    'lev': max(1, min(10, base['lev']+dlev)),
                                    'pos': base['pos'],
                                    'hold': base['hold'],
                                }
                                fine_grid.append(p)
        
        # 去重
        seen = set()
        unique_grid = []
        for p in fine_grid:
            key = tuple(sorted(p.items()))
            if key not in seen:
                seen.add(key); unique_grid.append(p)
        
        n_fine = len(unique_grid)
        print(f"({n_fine}组合)", end=" ", flush=True)
        
        for idx, params in enumerate(unique_grid):
            if idx % 500 == 0:
                print(".", end="", flush=True)
            r = bt(params, df)
            if r and r['ret'] > 0 and r['wlr'] > 1.8 and r['daily'] > 0.3:
                r['coin'] = coin
                r['params'] = params
                # Walk-Forward
                wf = wf_test(params, df)
                r['wf_pass'] = wf['wf_pass']
                r['wf_ret'] = wf['wf_ret']
                # 2022熊市
                r['bear2022'] = bear2022(params, df)
                fine_results.append(r)
        
        print(f" ✓ {sum(1 for r in fine_results if r['coin']==coin)}个合格")
    
    elapsed = time.time() - t0
    print(f"\n⏱️ Phase 2耗时: {elapsed:.0f}s")
    
    if fine_results:
        df_f = pd.DataFrame(fine_results).sort_values('wlr', ascending=False)
        print("\n🏆 Phase 2 Top 10:")
        for _, row in df_f.head(10).iterrows():
            wf_str = f"WF{row.get('wf_pass',0):.0%}" if pd.notna(row.get('wf_pass')) else '-'
            bear_str = f"2022{row['bear2022']*100:+.0f}%" if pd.notna(row.get('bear2022')) else '-'
            print(f"  {row['coin']}: WLR={row['wlr']:.2f} 收益={row['ret']*100:+.1f}% DD={row['dd']*100:.1f}% {wf_str} {bear_str}")
        return df_f
    return None

def main():
    t_start = time.time()
    print("=" * 70)
    print("🌙 Kronos通宵量化研究 V2 | 2026-04-18")
    print("目标: WLR>2.0 + Walk-Forward通过 + 2022熊市正收益")
    print("=" * 70)
    
    # Phase 1
    phase1 = phase1_coarse_scan(COINS)
    
    if phase1 is not None and len(phase1) > 0:
        # 保存Phase1
        p1_path = f'{OUT_DIR}/phase1_results.json'
        phase1.to_json(p1_path, orient='records', indent=2)
        print(f"\n✅ Phase1已保存: {p1_path}")
        
        # Top20做Phase2
        top_list = []
        for _, row in phase1.head(20).iterrows():
            top_list.append({
                'coin': row['coin'],
                'params': row['params'],
                'wlr': row['wlr'],
                'ret': row['ret'],
            })
        
        phase2 = phase2_fine_tune(top_list, COINS)
        
        if phase2 is not None and len(phase2) > 0:
            # 最终保存
            best = phase2.iloc[0]
            final = {
                'coin': best['coin'],
                'total_return_pct': round(best['ret']*100, 2),
                'annual_return_pct': round(best['ann']*100, 2),
                'max_dd_pct': round(best['dd']*100, 2),
                'wl_ratio': round(best['wlr'], 2),
                'win_rate_pct': round(best['wr']*100, 2),
                'daily_trades': round(best['daily'], 2),
                'wf_pass_rate': round(best.get('wf_pass', 0), 2),
                'wf_avg_return_pct': round(best.get('wf_ret', 0)*100, 2),
                'bear_2022_return_pct': round(best.get('bear2022', 0)*100, 2) if pd.notna(best.get('bear2022')) else None,
                'params': best['params'],
            }
            
            final_path = f'{OUT_DIR}/FINAL_RESULT.json'
            with open(final_path, 'w') as f:
                json.dump(final, f, indent=2)
            
            print("\n" + "=" * 70)
            print("🏆 最终最优策略")
            print("=" * 70)
            print(f"币种: {final['coin']}")
            print(f"总收益: {final['total_return_pct']:+.2f}%")
            print(f"年化: {final['annual_return_pct']:+.2f}%")
            print(f"最大DD: {final['max_dd_pct']:.2f}%")
            print(f"盈亏比(WLR): {final['wl_ratio']:.2f}")
            print(f"胜率: {final['win_rate_pct']:.2f}%")
            print(f"日均交易: {final['daily_trades']:.2f}笔")
            print(f"Walk-Forward通过率: {final['wf_pass_rate']:.0%}")
            print(f"2022熊市收益: {final['bear_2022_return_pct']}")
            print(f"\n参数: RSI<{final['params']['rsi_os']}/>{final['params']['rsi_ob']} ADX>{final['params']['adx_min']}")
            print(f"止损/止盈: {final['params']['sl']*100:.0f}%/{final['params']['tp']*100:.0f}%")
            print(f"杠杆: {final['params']['lev']}x | 仓位: {final['params']['pos']*100:.0f}% | 持仓: {final['params']['hold']}根")
            print(f"\n✅ 已保存: {final_path}")
    
    total_time = time.time() - t_start
    print(f"\n⏱️ 总耗时: {total_time/60:.1f}分钟")
    print("🎉 研究完成!")

if __name__ == '__main__':
    main()
