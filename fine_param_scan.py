#!/usr/bin/env python3
"""
精细参数扫描 - 聚焦最优区间
==================================
"""
import os, json, time, traceback
import numpy as np
import pandas as pd
import talib
from backtest_engine import FEE_AND_SLIPPAGE

DATA_DIR = os.path.expanduser("~/Desktop/crypto_data_Pre5m")
OUT_DIR = os.path.expanduser("~/kronos/market_sense_results/param_explorer")
os.makedirs(OUT_DIR, exist_ok=True)

COINS = ["AVAX", "DOGE", "ALGO"]
LIMIT = 200_000

def load_coin_data(coin):
    for f in os.listdir(DATA_DIR):
        f_uc = f.upper()
        if coin.upper() in f_uc and ('USDT' in f_uc or '5M' in f_uc):
            df = pd.read_csv(f"{DATA_DIR}/{f}", nrows=LIMIT)
            cols_lower = {c.lower().strip(): c for c in df.columns}
            ts_col = next((cols_lower[n] for n in ['datetime_utc','datetime','timestamp','date'] if n in cols_lower), df.columns[0])
            df["ts"] = pd.to_datetime(df[ts_col], errors="coerce").dt.tz_localize(None)
            df = df.dropna(subset=["ts"])
            result = pd.DataFrame(index=df.index)
            result["ts"] = df["ts"]
            for cn, std in [('open','open'),('high','high'),('low','low'),('close','close')]:
                if cn in cols_lower: result[std] = df[cols_lower[cn]].values
                elif std in cols_lower: result[std] = df[cols_lower[std]].values
            result["volume"] = 0.0
            return result.set_index("ts").sort_index()
    return None

def compute_indicators(df_5m):
    c = df_5m["close"].values
    h = df_5m["high"].values
    l = df_5m["low"].values
    df_5m["rsi_5m"] = talib.RSI(c, 14)
    res = df_5m.resample("1h").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    res["rsi"] = talib.RSI(res["close"].values, 14)
    res["adx"] = talib.ADX(res["high"].values, res["low"].values, res["close"].values, 14)
    res["atr"] = talib.ATR(res["high"].values, res["low"].values, res["close"].values, 14)
    res["ma20"] = talib.MA(res["close"].values, 20, 0)
    res["ma50"] = talib.MA(res["close"].values, 50, 0)
    res["ma200"] = talib.MA(res["close"].values, 200, 0)
    for col in ["rsi","adx","atr","ma20","ma50","ma200"]:
        df_5m[f"{col}_1h"] = res[col].reindex(df_5m.index, method="ffill").shift(12)
    res15 = df_5m.resample("15min").agg({"close":"last"}).dropna()
    rsi15 = pd.Series(talib.RSI(res15["close"].values, 14), index=res15.index)
    df_5m["rsi_15m"] = rsi15.reindex(df_5m.index, method="ffill").shift(3)
    return df_5m

def full_backtest(df_1h, sig, is_long, stop_atr, profit_atr, hold_hours, max_pos):
    """完整回测（带止盈止损）"""
    sig_v = sig.values
    close = df_1h["close"].values
    high = df_1h["high"].values
    low = df_1h["low"].values
    open_ = df_1h["open"].values
    atr = df_1h["atr_1h"].values
    n = len(sig_v)
    
    rets = []
    last_exit = -999
    i = 0
    
    while i < n - 1:
        if not sig_v[i] or i - last_exit < 2:
            i += 1
            continue
        
        entry_price = open_[i + 1]
        entry_atr = atr[i] if not np.isnan(atr[i]) and atr[i] > 0 else entry_price * 0.01
        stop_price = entry_price - stop_atr * entry_atr if is_long else entry_price + stop_atr * entry_atr
        tp_price = entry_price + profit_atr * entry_atr if is_long else entry_price - profit_atr * entry_atr
        
        exit_idx = i + 1 + hold_hours
        ret = None
        
        for j in range(i + 1, min(i + 1 + hold_hours, n)):
            curr_high = high[j]
            curr_low = low[j]
            
            if is_long:
                if curr_low <= stop_price:
                    exit_idx = j
                    ret = (stop_price / entry_price - 1 - FEE_AND_SLIPPAGE) * max_pos
                    break
                elif curr_high >= tp_price:
                    # 止盈后持有到结束
                    exit_idx = min(i + 1 + hold_hours, n - 1)
                    ret = (tp_price / entry_price - 1 - FEE_AND_SLIPPAGE) * max_pos
                    break
            else:
                if curr_high >= stop_price:
                    exit_idx = j
                    ret = (entry_price / stop_price - 1 - FEE_AND_SLIPPAGE) * max_pos
                    break
                elif curr_low <= tp_price:
                    exit_idx = min(i + 1 + hold_hours, n - 1)
                    ret = (entry_price / tp_price - 1 - FEE_AND_SLIPPAGE) * max_pos
                    break
        
        if ret is None:
            exit_price = close[min(exit_idx, n - 1)]
            ret = (exit_price / entry_price - 1 - FEE_AND_SLIPPAGE) * max_pos if is_long else (entry_price / exit_price - 1 - FEE_AND_SLIPPAGE) * max_pos
        
        rets.append(ret)
        last_exit = exit_idx
        i = exit_idx + 1
    
    if not rets:
        return {"count": 0, "wr": 0, "wlr": 0, "ret": 0, "avg": 0}
    
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    wr = len(wins) / len(rets)
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 0.001
    wlr = avg_win / avg_loss
    return {
        "count": len(rets), "wr": round(wr,4), "wlr": round(wlr,3),
        "ret": round(sum(rets),4), "avg": round(np.mean(rets),6)
    }

def scan():
    # 细扫参数（聚焦RSI=45, ADX=20, hold=12-24h附近）
    L_RSI = [40, 43, 45, 47, 50]
    S_RSI = [53, 55, 57, 60]
    ADX_L = [18, 20, 22]
    ADX_S = [20, 25, 30]
    HOLD = [8, 12, 16, 20, 24]
    SL_ATR = [1.0, 1.5, 2.0]
    TP_ATR = [2.0, 2.5, 3.0]
    
    all_results = []
    
    for coin in COINS:
        ckpt_file = f"{OUT_DIR}/{coin}_fine_scan.json"
        if os.path.exists(ckpt_file):
            print(f"  ⏭️  {coin} 已细扫，跳过")
            with open(ckpt_file) as f:
                all_results.extend(json.load(f))
            continue
        
        print(f"\n  🔍 {coin} 精细扫描...")
        df = load_coin_data(coin)
        if df is None:
            print(f"  ❌ 数据加载失败")
            continue
        
        df = compute_indicators(df).iloc[230:].copy()
        df_1h = df.resample("1h").agg({"open":"first","high":"max","low":"min","close":"last"}).dropna()
        atr_1h = df["atr_1h"].resample("1h").last()
        df_1h["atr_1h"] = atr_1h.reindex(df_1h.index, method="ffill")
        
        major_up = (df["ma20_1h"] > df["ma50_1h"]) & (df["ma50_1h"] > df["ma200_1h"])
        major_down = (df["ma20_1h"] < df["ma50_1h"]) & (df["ma50_1h"] < df["ma200_1h"])
        rsi_15m = df["rsi_15m"]
        adx_1h = df["adx_1h"]
        
        results = []
        t0 = time.time()
        combo = 0
        total_combos = len(L_RSI)*len(S_RSI)*len(ADX_L)*len(ADX_S)*len(HOLD)*len(SL_ATR)*len(TP_ATR)
        
        for lr in L_RSI:
            for sr in S_RSI:
                for al in ADX_L:
                    for as_ in ADX_S:
                        for hold in HOLD:
                            for sl in SL_ATR:
                                for tp in TP_ATR:
                                    combo += 1
                                    long_sig = (major_up & (rsi_15m < lr) & (adx_1h > al)).resample("1h").last().fillna(False)
                                    short_sig = (major_down & (rsi_15m > sr) & (adx_1h > as_)).resample("1h").last().fillna(False)
                                    
                                    l = full_backtest(df_1h, long_sig, True, sl, tp, hold, 0.8)
                                    s = full_backtest(df_1h, short_sig, False, sl, tp, hold, 0.2)
                                    
                                    results.append({
                                        "coin": coin, "l_rsi": lr, "s_rsi": sr,
                                        "l_adx": al, "s_adx": as_,
                                        "hold": hold, "sl_atr": sl, "tp_atr": tp,
                                        "long": l, "short": s,
                                        "total": l["ret"] + s["ret"]
                                    })
        
        elapsed = time.time() - t0
        
        with open(ckpt_file, "w") as f:
            json.dump(results, f)
        all_results.extend(results)
        
        print(f"  ✅ {coin}: {len(results)}组合 [{elapsed:.1f}s]")
        
        # Top5做多
        top_l = sorted(results, key=lambda x: x["long"]["ret"], reverse=True)[:5]
        print(f"  🏆 做多Top5:")
        for r in top_l:
            l = r["long"]
            print(f"     RSI={r['l_rsi']} ADX={r['l_adx']} h={r['hold']}h SL={r['sl_atr']} TP={r['tp_atr']} → {l['count']}信号 {l['wr']*100:.1f}%WR WLR{l['wlr']:.2f} 收益{l['ret']*100:.1f}%")
        
        # Top5做空
        top_s = sorted(results, key=lambda x: x["short"]["ret"], reverse=True)[:5]
        print(f"  🏆 做空Top5:")
        for r in top_s:
            s = r["short"]
            print(f"     RSI={r['s_rsi']} ADX={r['s_adx']} h={r['hold']}h SL={r['sl_atr']} TP={r['tp_atr']} → {s['count']}信号 {s['wr']*100:.1f}%WR WLR{s['wlr']:.2f} 收益{s['ret']*100:.1f}%")
        
        # Top5综合
        top_t = sorted(results, key=lambda x: x["total"], reverse=True)[:5]
        print(f"  🏆 综合Top5:")
        for r in top_t:
            l = r["long"]; s = r["short"]
            print(f"     L:RSI={r['l_rsi']}A{r['l_adx']}h{r['hold']} S:RSI={r['s_rsi']}A{r['s_adx']} → 合计{r['total']*100:.1f}%")
    
    # 全局Top
    print(f"\n{'='*70}")
    print("  全局综合Top10:")
    top_all = sorted(all_results, key=lambda x: x["total"], reverse=True)[:10]
    for i, r in enumerate(top_all):
        l = r["long"]; s = r["short"]
        print(f"  #{i+1} {r['coin']} L:RSI={r['l_rsi']}A{r['l_adx']}h{r['hold']} SL={r['sl_atr']} TP={r['tp_atr']} | 合计:{r['total']*100:.1f}%")
    
    out_file = f"{OUT_DIR}/fine_scan_results.json"
    with open(out_file, "w") as f:
        json.dump(all_results, f, default=str)
    
    print(f"\n✅ 细扫完成！{len(all_results)}组合")
    return all_results

if __name__ == "__main__":
    print(f"🚀 精细参数扫描: {COINS}")
    results = scan()
