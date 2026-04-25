#!/usr/bin/env python3
"""
backtest_rsi_adx_pattern.py  v2
=============================
多空非对称回测框架。

做多（牛市参数）：
  - RSI_15m < 45
  - 趋势向上（收盘>MA20 且 MA20向上）
  - +DI > -DI
  - ADX > 20
  - 持仓：2h（24根5min K线）
  - 最大仓位：0.8

做空（牛市参数，更严格3倍）：
  - RSI_15m > 55
  - 趋势向下（收盘<MA20 且 MA20向下）
  - -DI > +DI + 5（多头力量差值>5）
  - ADX > 30
  - 持仓：1h（12根5min K线）
  - 最大仓位：0.2

多空完全分开统计，输出两个独立模式库。
"""

import json
import time
import numpy as np
import pandas as pd
import talib

# ── 配置 ────────────────────────────────────────────────────────────────
DATA_DIR   = "/Users/jimingzhang/Desktop/crypto_data_Pre5m"
SYMBOL     = "BTC"
INTERVAL   = "5m"
MAX_BARS   = 200_000   # 拉长到约2年，覆盖牛熊

# 做多参数（牛市）
L_RSI_ENTRY   = 45
L_ADX_THRESH  = 20
L_HOLD_BARS   = 24   # 2h
L_MAX_POS     = 0.8

# 做空参数（牛市，更严格）
S_RSI_ENTRY   = 55
S_ADX_THRESH  = 30
S_HOLD_BARS   = 12   # 1h
S_MAX_POS     = 0.2
S_DI_MARGIN   = 5    # -DI > +DI + 5

# 共用
RSI_PERIOD    = 14
FEE           = 0.002

# 分桶边界
RSI_LONG_BUCKETS  = [(0, 30), (30, 35), (35, 40), (40, 45)]
RSI_SHORT_BUCKETS = [(55, 60), (60, 65), (65, 70), (70, 100)]
ADX_BUCKETS       = [(0, 20), (20, 25), (25, 30), (30, 100)]

# ── 数据加载 ───────────────────────────────────────────────────────────
def load_data(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    fname = f"{symbol}_USDT_{interval}_from_20180101.csv"
    path  = f"{DATA_DIR}/{fname}"
    df    = pd.read_csv(path)
    dt_col = next((c for c in df.columns if "datetime" in c.lower()), df.columns[0])
    df["ts"] = pd.to_datetime(df[dt_col], errors="coerce").dt.tz_localize(None)
    df = df.dropna(subset=["ts"])
    result = pd.DataFrame()
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            result[col] = df[col].values
    result["volume"] = 0.0
    result["ts"] = df["ts"].values
    result = result.set_index("ts").sort_index()
    return result.tail(limit)

# ── 指标计算 ───────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]

    # 5min RSI（原始）
    rsi_5m = talib.RSI(close, timeperiod=RSI_PERIOD)

    # 15min RSI（resample 3根5min）
    df_15m = df.resample("15min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    rsi_15m_raw = talib.RSI(df_15m["close"], timeperiod=RSI_PERIOD)
    rsi_15m = rsi_15m_raw.reindex(df.index, method="ffill")

    # 1h 指标（talib，向量计算）
    df_1h = df.resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()

    adx_1h   = talib.ADX(df_1h["high"], df_1h["low"], df_1h["close"], timeperiod=14)
    plus_di  = talib.PLUS_DI(df_1h["high"], df_1h["low"], df_1h["close"], timeperiod=14)
    minus_di = talib.MINUS_DI(df_1h["high"], df_1h["low"], df_1h["close"], timeperiod=14)
    ma20_1h  = talib.MA(df_1h["close"], timeperiod=20, matype=0)

    # ffill 到 5min 粒度，滞后 12 根（避免未来函数）
    lag = 12
    df["adx_1h"]    = adx_1h.reindex(df.index, method="ffill").shift(lag)
    df["plus_di"]   = plus_di.reindex(df.index, method="ffill").shift(lag)
    df["minus_di"]  = minus_di.reindex(df.index, method="ffill").shift(lag)
    df["ma20_1h"]   = ma20_1h.reindex(df.index, method="ffill").shift(lag)
    df["ma20_prev"] = df["ma20_1h"].shift(1)

    # 趋势方向（名字含义：_up=做多条件,_down=做空条件）
    df["trend_up"]   = (df["close"] > df["ma20_1h"]) & (df["ma20_1h"] > df["ma20_prev"])   # 均线多头排列=长期涨
    df["trend_down"] = (df["close"] < df["ma20_1h"]) & (df["ma20_1h"] < df["ma20_prev"])   # 均线空头排列=长期跌

    # 多空方向过滤
    df["di_bullish"] = df["plus_di"] > df["minus_di"]
    df["di_bearish"] = df["minus_di"] > (df["plus_di"] + S_DI_MARGIN)

    df["rsi_5m"]  = rsi_5m
    df["rsi_15m"] = rsi_15m

    return df

# ── 信号生成 ───────────────────────────────────────────────────────────
def make_long_signal(df: pd.DataFrame) -> pd.Series:
    return (
        (df["rsi_15m"] < L_RSI_ENTRY) &
        df["trend_up"] &
        df["di_bullish"] &
        (df["adx_1h"] > L_ADX_THRESH)
    )

def make_short_signal(df: pd.DataFrame) -> pd.Series:
    return (
        (df["rsi_15m"] > S_RSI_ENTRY) &
        df["trend_down"] &
        df["di_bearish"] &
        (df["adx_1h"] > S_ADX_THRESH)
    )

# ── 分桶统计（numpy positional，避免index对齐问题）──────────────────────
def bucket_stats_npy(returns_arr, rsi_arr, adx_arr, rsi_buckets) -> list:
    records = []
    mask_valid = ~(np.isnan(returns_arr) | np.isnan(rsi_arr) | np.isnan(adx_arr))
    r_arr   = returns_arr[mask_valid]
    rsi_arr = rsi_arr[mask_valid]
    adx_arr = adx_arr[mask_valid]

    for (rsi_lo, rsi_hi) in rsi_buckets:
        for (adx_lo, adx_hi) in ADX_BUCKETS:
            m = (rsi_arr >= rsi_lo) & (rsi_arr < rsi_hi) & \
                (adx_arr >= adx_lo) & (adx_arr < adx_hi)
            sub = r_arr[m]
            if len(sub) < 5:
                continue
            wins  = (sub > 0).sum()
            avg_w = sub[sub > 0].mean() if wins > 0 else 0.0
            avg_l = abs(sub[sub < 0].mean()) if (sub < 0).sum() > 0 else 1e-9
            tot_w = sub[sub > 0].sum()
            tot_l = abs(sub[sub < 0].sum())
            records.append({
                "rsi_range":       f"{rsi_lo}-{rsi_hi}",
                "adx_range":       f"{adx_lo}-{adx_hi}",
                "count":           int(len(sub)),
                "win_rate":        round(float(wins / len(sub)), 4),
                "avg_return":      round(float(sub.mean()), 6),
                "profit_factor":   round(float(tot_w / tot_l) if tot_l > 1e-9 else 99.99, 2),
                "win_loss_ratio":  round(float(avg_w / avg_l), 3),
            })
    return records

# ── 单组回测 ───────────────────────────────────────────────────────────
def backtest_group(df: pd.DataFrame, signal: pd.Series, hold_bars: int,
                   max_pos: float, group_name: str,
                   rsi_buckets) -> dict:
    # 未来收益率
    future_close = df["close"].shift(-hold_bars)
    raw_ret = (future_close / df["close"] - 1 - FEE) * max_pos  # 仓位权重

    sig_mask = signal.fillna(False)
    strat_ret = raw_ret[sig_mask]

    n    = len(strat_ret)
    if n == 0:
        print(f"  ⚠️  {group_name}: 0 signals")
        return None

    wins   = (strat_ret > 0).sum()
    losses = (strat_ret < 0).sum()
    wr     = float(wins / n)
    avg_r  = float(strat_ret.mean())
    tot_w  = float(strat_ret[strat_ret > 0].sum())
    tot_l  = abs(float(strat_ret[strat_ret < 0].sum()))
    pf     = tot_w / tot_l if tot_l > 1e-9 else 99.99
    avg_w  = float(strat_ret[strat_ret > 0].mean()) if wins > 0 else 0.0
    avg_l  = abs(float(strat_ret[strat_ret < 0].mean())) if losses > 0 else 1e-9
    wlr    = avg_w / avg_l

    print(f"\n{'='*60}")
    print(f"  {group_name}")
    print(f"{'='*60}")
    print(f"  信号数量        : {n:>8}")
    print(f"  胜率            : {wr:.2%}")
    print(f"  平均收益/次     : {avg_r:.4%}")
    print(f"  盈亏比          : {wlr:.3f}")
    print(f"  盈利因子        : {pf:.2f}")
    print(f"  总收益          : {float(strat_ret.sum()):.4%}")

    # 模式库（numpy positional）
    pat = bucket_stats_npy(
        raw_ret[sig_mask].values,
        df["rsi_15m"][sig_mask].values,
        df["adx_1h"][sig_mask].values,
        rsi_buckets
    )

    return {
        "signal_count": int(n),
        "win_rate":     round(wr, 4),
        "avg_return":   round(avg_r, 6),
        "profit_factor": round(pf, 2),
        "win_loss_ratio": round(wlr, 3),
        "patterns":     pat,
    }

# ── 主程序 ─────────────────────────────────────────────────────────────
def main():
    t0 = time.time()

    print("📥 加载数据...")
    df = load_data(SYMBOL, INTERVAL, MAX_BARS)
    print(f"  K线: {len(df):,} | {df.index[0]} → {df.index[-1]}")

    print("⚙️  计算指标（talib 向量化）...")
    df = compute_indicators(df)

    warmup = RSI_PERIOD + 20 + 12 + 1
    df = df.iloc[warmup:].copy()
    print(f"  有效数据: {len(df):,} 根5分钟K线")

    # ── 做多信号 ────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  📈 做多回测")
    print("="*60)
    long_sig  = make_long_signal(df)
    long_res  = backtest_group(
        df, long_sig, L_HOLD_BARS, L_MAX_POS,
        f"L（RSI<{L_RSI_ENTRY}, ADX>{L_ADX_THRESH}, 持有{L_HOLD_BARS*5}min）",
        RSI_LONG_BUCKETS
    )

    # ── 做空信号 ───────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  📉 做空回测")
    print("="*60)
    short_sig = make_short_signal(df)
    short_res = backtest_group(
        df, short_sig, S_HOLD_BARS, S_MAX_POS,
        f"S（RSI>{S_RSI_ENTRY}, ADX>{S_ADX_THRESH}, 持有{S_HOLD_BARS*5}min）",
        RSI_SHORT_BUCKETS
    )

    # ── 合并统计 ───────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  📊 多空合并总览")
    print("="*60)

    long_n   = long_res["signal_count"]  if long_res else 0
    short_n  = short_res["signal_count"] if short_res else 0
    total_n  = long_n + short_n

    if long_res and short_res:
        merged_wr = (
            long_res["win_rate"] * long_n +
            short_res["win_rate"] * short_n
        ) / total_n if total_n > 0 else 0
        merged_avg = (
            long_res["avg_return"] * long_n * L_MAX_POS +
            short_res["avg_return"] * short_n * S_MAX_POS
        ) / total_n if total_n > 0 else 0
    elif long_res:
        merged_wr  = long_res["win_rate"]
        merged_avg = long_res["avg_return"] * L_MAX_POS
    elif short_res:
        merged_wr  = short_res["win_rate"]
        merged_avg = short_res["avg_return"] * S_MAX_POS
    else:
        merged_wr = merged_avg = 0

    print(f"  做多信号    : {long_n:>6} 个  |  做空信号: {short_n:>6} 个")
    print(f"  多空合并胜率: {merged_wr:.2%}")
    print(f"  多空合并均收益: {merged_avg:.4%}")

    # ── 保存模式库 ─────────────────────────────────────────────────
    lib = {}
    if long_res:
        lib["long"] = {
            "params": {
                "rsi_entry":  L_RSI_ENTRY,
                "adx_thresh": L_ADX_THRESH,
                "hold_bars":  L_HOLD_BARS,
                "max_pos":    L_MAX_POS,
            },
            "summary": {k: v for k, v in long_res.items() if k != "patterns"},
            "patterns": long_res["patterns"],
        }
    if short_res:
        lib["short"] = {
            "params": {
                "rsi_entry":   S_RSI_ENTRY,
                "adx_thresh":  S_ADX_THRESH,
                "hold_bars":   S_HOLD_BARS,
                "max_pos":     S_MAX_POS,
                "di_margin":   S_DI_MARGIN,
            },
            "summary": {k: v for k, v in short_res.items() if k != "patterns"},
            "patterns": short_res["patterns"],
        }

    out_path = "/Users/jimingzhang/kronos/pattern_library_adx.json"
    with open(out_path, "w") as f:
        json.dump(lib, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 模式库 → {out_path}")
    print(f"\n⏱️  耗时: {time.time()-t0:.1f}秒")

    return lib

if __name__ == "__main__":
    main()
