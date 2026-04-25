#!/usr/bin/env python3
"""
run_comparison.py
================
三个策略统一回测对比 — 同一引擎、同一费率

输出：
  v1.0_stable（参数10/ADX20/RSI68）
  v1.1（参数8/ADX18/RSI70）
  熊市对称（做空版，RSI>32/ADX>25）
"""

import sys, time, json
sys.path.insert(0, "/Users/jimingzhang/kronos")

import numpy as np
import pandas as pd
import talib
from backtest_engine import UnifiedBacktester, WLRTracker

DATA_DIR  = "/Users/jimingzhang/Desktop/crypto_data_Pre5m"
RSI_PERIOD = 14
ATR_PERIOD = 14

def load_data(symbol, interval, limit=200_000):
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


def load_year_data(symbol, interval, year, warmup_days=90):
    """加载年份+预热期"""
    fname = f"{symbol}_USDT_{interval}_from_20180101.csv"
    path  = f"{DATA_DIR}/{fname}"
    df    = pd.read_csv(path)
    dt_col = next((c for c in df.columns if "datetime" in c.lower()), df.columns[0])
    df["ts"] = pd.to_datetime(df[dt_col], errors="coerce").dt.tz_localize(None)
    df = df.dropna(subset=["ts"])
    from datetime import datetime, timedelta
    start = f"{year}-01-01"
    end   = f"{year+1}-01-01"
    warmup_start = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    df = df[(df["ts"] >= warmup_start) & (df["ts"] < end)]
    result = pd.DataFrame()
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            result[col] = df[col].values
    result["volume"] = 0.0
    result["ts"] = df["ts"].values
    result = result.set_index("ts").sort_index()
    return result


def compute_indicators(df):
    close = df["close"]
    df_15m = df.resample("15min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    rsi_15m_raw = talib.RSI(df_15m["close"], timeperiod=RSI_PERIOD)
    rsi_15m = rsi_15m_raw.reindex(df.index, method="ffill")
    df_1h = df.resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    adx_1h   = talib.ADX(df_1h["high"], df_1h["low"], df_1h["close"], timeperiod=14)
    plus_di  = talib.PLUS_DI(df_1h["high"], df_1h["low"], df_1h["close"], timeperiod=14)
    minus_di = talib.MINUS_DI(df_1h["high"], df_1h["low"], df_1h["close"], timeperiod=14)
    atr_1h   = talib.ATR(df_1h["high"], df_1h["low"], df_1h["close"], timeperiod=ATR_PERIOD)
    ma20_1h  = talib.MA(df_1h["close"], timeperiod=20, matype=0)
    ma50_1h  = talib.MA(df_1h["close"], timeperiod=50, matype=0)
    ma200_1h = talib.MA(df_1h["close"], timeperiod=200, matype=0)
    lag = 12
    df["adx_1h"]    = adx_1h.reindex(df.index, method="ffill").shift(lag)
    df["plus_di"]   = plus_di.reindex(df.index, method="ffill").shift(lag)
    df["minus_di"]  = minus_di.reindex(df.index, method="ffill").shift(lag)
    df["ma20_1h"]   = ma20_1h.reindex(df.index, method="ffill").shift(lag)
    df["ma50_1h"]   = ma50_1h.reindex(df.index, method="ffill").shift(lag)
    df["ma200_1h"]  = ma200_1h.reindex(df.index, method="ffill").shift(lag)
    df["atr_1h"]    = atr_1h.reindex(df.index, method="ffill").shift(lag)
    df["major_up"]   = (df["ma20_1h"] > df["ma50_1h"]) & (df["ma50_1h"] > df["ma200_1h"])
    df["major_down"] = (df["ma20_1h"] < df["ma50_1h"]) & (df["ma50_1h"] < df["ma200_1h"])
    df["rsi_15m"] = rsi_15m
    return df


def prep_1h(df, rolling_window=10):
    df_1h = df.resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "adx_1h": "last", "plus_di": "last", "minus_di": "last",
        "ma20_1h": "last", "ma50_1h": "last", "ma200_1h": "last",
        "rsi_15m": "last", "atr_1h": "last",
        "major_up": "last", "major_down": "last",
    }).dropna()
    for col in ["major_up", "major_down"]:
        df_1h[col] = df_1h[col].astype(bool)
    df_1h["rolling_max"] = df_1h["close"].rolling(window=rolling_window).max().shift(1)
    df_1h["rolling_min"] = df_1h["close"].rolling(window=rolling_window).min().shift(1)
    df_1h["adx_turn"] = (
        (df_1h["adx_1h"].shift(1) < df_1h["adx_1h"].shift(2)) &
        (df_1h["adx_1h"] > df_1h["adx_1h"].shift(1))
    )
    return df_1h


def run_strategy(df_1h, strategy_name, long_params, short_params,
                long_sig_fn, short_sig_fn, wlr_tracker=None):
    """统一运行入口"""
    print(f"\n{'='*60}")
    print(f"  {strategy_name}")
    print(f"{'='*60}")

    long_res  = None
    short_res = None

    if long_sig_fn is not None:
        sig = long_sig_fn(df_1h)
        if sig.sum() > 0:
            tester = UnifiedBacktester(df_1h, is_long=True, params=long_params, wlr_tracker=None)
            long_res = tester.run(sig, cooldown_1h=2)
            print(f"\n  做多 | 信号:{sig.sum():>4} | 胜率:{long_res['win_rate']:.2%} | "
                  f"盈亏比:{long_res['win_loss_ratio']:.3f} | 总收益:{long_res['total_return']:.4%}")

    if short_sig_fn is not None:
        sig_b = short_sig_fn(df_1h)
        if sig_b.sum() > 0:
            wlr_t = wlr_tracker if wlr_tracker is not None else WLRTracker()
            tester = UnifiedBacktester(df_1h, is_long=False, params=short_params, wlr_tracker=wlr_t)
            short_res = tester.run(sig_b, cooldown_1h=2)
            print(f"  做空 | 信号:{sig_b.sum():>4} | 胜率:{short_res['win_rate']:.2%} | "
                  f"盈亏比:{short_res['win_loss_ratio']:.3f} | 总收益:{short_res['total_return']:.4%}")

    ln = long_res["signal_count"]  if long_res  else 0
    sn = short_res["signal_count"] if short_res else 0
    tn = ln + sn
    if tn == 0:
        return {"name": strategy_name, "signal_count": 0, "win_rate": 0,
                "wlr": 0, "total_return": 0}

    m_wr  = ((long_res["win_rate"]*ln + short_res["win_rate"]*sn) / tn) if (ln>0 and sn>0) \
             else (long_res["win_rate"] if ln>0 else short_res["win_rate"])
    m_tot = (long_res["total_return"] + short_res["total_return"]) if (ln>0 and sn>0) \
             else (long_res["total_return"] if ln>0 else short_res["total_return"])

    print(f"  合并 | 总信号:{tn:>4} | 胜率:{m_wr:.2%} | 总收益:{m_tot:.4%}")
    return {
        "name": strategy_name,
        "signal_count": tn,
        "win_rate": round(m_wr, 4),
        "wlr": round((long_res["win_loss_ratio"]*ln + short_res["win_loss_ratio"]*sn)/tn, 3) if (ln>0 and sn>0) \
                else (long_res["win_loss_ratio"] if ln>0 else short_res["win_loss_ratio"]),
        "total_return": round(m_tot, 6),
        "long":  long_res,
        "short": short_res,
    }


def main():
    t0 = time.time()

    # ══════════════════════════════════════════════
    # v1.0_stable 参数（10/ADX20/RSI68）
    # ══════════════════════════════════════════════
    print("\n" + "█"*60)
    print("  v1.0_stable（10窗 / ADX>20 / RSI<68）")
    print("█"*60)

    df = load_data("BTC", "5m", 200_000)
    df = compute_indicators(df)
    warmup = RSI_PERIOD + 200 + 12 + 1
    df = df.iloc[warmup:].copy()
    df_1h = prep_1h(df, rolling_window=10)

    v1_long_params  = {"atr_sl": 1.5, "atr_tp": 3.0, "max_pos": 0.80, "max_hold_1h": 24}
    v1_short_params = {"atr_sl": 1.5, "atr_tp": 3.0, "max_pos": 0.20, "max_hold_1h": 24,
                       "wlr_min": 1.2}

    def v1_long_sig(df_1h):
        return (
            df_1h["major_up"] &
            ((df_1h["close"] > df_1h["rolling_max"]) | df_1h["adx_turn"]) &
            (df_1h["plus_di"] > df_1h["minus_di"]) &
            (df_1h["adx_1h"] > 20) &
            (df_1h["rsi_15m"] < 68)
        )

    def v1_short_sig(df_1h):
        return (
            df_1h["major_down"] &
            ((df_1h["close"] < df_1h["rolling_min"]) | df_1h["adx_turn"]) &
            (df_1h["minus_di"] > df_1h["plus_di"]) &
            (df_1h["adx_1h"] > 25) &
            (df_1h["rsi_15m"] > 72)
        )

    v1_res = run_strategy(df_1h, "v1.0_stable（10/ADX20/RSI68）",
                          v1_long_params, v1_short_params,
                          v1_long_sig, v1_short_sig)

    # ══════════════════════════════════════════════
    # v1.1 参数（8/ADX18/RSI70）
    # ══════════════════════════════════════════════
    print("\n" + "█"*60)
    print("  v1.1（8窗 / ADX>18 / RSI<70）")
    print("█"*60)

    df2 = load_data("BTC", "5m", 200_000)
    df2 = compute_indicators(df2)
    df2 = df2.iloc[warmup:].copy()
    df_1h_2 = prep_1h(df2, rolling_window=8)

    def v11_long_sig(df_1h):
        return (
            df_1h["major_up"] &
            ((df_1h["close"] > df_1h["rolling_max"]) | df_1h["adx_turn"]) &
            (df_1h["plus_di"] > df_1h["minus_di"]) &
            (df_1h["adx_1h"] > 18) &
            (df_1h["rsi_15m"] < 70)
        )

    def v11_short_sig(df_1h):
        return (
            df_1h["major_down"] &
            ((df_1h["close"] < df_1h["rolling_min"]) | df_1h["adx_turn"]) &
            (df_1h["minus_di"] > df_1h["plus_di"]) &
            (df_1h["adx_1h"] > 25) &
            (df_1h["rsi_15m"] > 72)
        )

    v11_res = run_strategy(df_1h_2, "v1.1（8/ADX18/RSI70）",
                           v1_long_params, v1_short_params,
                           v11_long_sig, v11_short_sig)

    # ══════════════════════════════════════════════
    # 熊市对称策略（2022年数据，做空为主）
    # ══════════════════════════════════════════════
    print("\n" + "█"*60)
    print("  熊市对称策略（2022年，RSI>32 / ADX>18，做空版）")
    print("█"*60)

    df_bear = load_year_data("BTC", "5m", 2022, warmup_days=90)
    df_bear = compute_indicators(df_bear)
    df_bear = df_bear.iloc[warmup:].copy()
    df_1h_b = prep_1h(df_bear, rolling_window=8)

    bear_long_params  = {"atr_sl": 1.5, "atr_tp": 3.0, "max_pos": 0.80, "max_hold_1h": 24}
    bear_short_params = {"atr_sl": 1.5, "atr_tp": 3.0, "max_pos": 0.20, "max_hold_1h": 24,
                         "wlr_min": 1.2}

    def bear_long_sig(df_1h):
        # 熊市做多：只做反弹，严格RSI<30
        return (
            (df_1h["rsi_15m"] < 30) &
            ((df_1h["close"] > df_1h["rolling_max"]) | df_1h["adx_turn"]) &
            (df_1h["adx_1h"] > 25) &
            (df_1h["plus_di"] > df_1h["minus_di"])
        )

    def bear_short_sig(df_1h):
        # 熊市对称做空：ma20<ma50<ma200确认熊市，RSI>32
        return (
            (~df_1h["major_up"]) &   # 不是牛市（允许做空）
            ((df_1h["close"] < df_1h["rolling_min"]) | df_1h["adx_turn"]) &
            (df_1h["minus_di"] > df_1h["plus_di"]) &
            (df_1h["adx_1h"] > 18) &
            (df_1h["rsi_15m"] > 32)
        )

    bear_res = run_strategy(df_1h_b, "熊市对称（2022，RSI>32/ADX>18）",
                           bear_long_params, bear_short_params,
                           bear_long_sig, bear_short_sig)

    # ══════════════════════════════════════════════
    # 最终对比表
    # ══════════════════════════════════════════════
    print("\n" + "█"*60)
    print("  📊 统一引擎对比（三策略，扣费0.2%）")
    print("█"*60)
    print(f"\n{'策略':<30} {'信号数':>8} {'胜率':>8} {'盈亏比':>8} {'总收益':>10}")
    print("-"*66)

    results = [v1_res, v11_res, bear_res]
    for r in results:
        print(f"{r['name']:<30} {r['signal_count']:>8} "
              f"{r['win_rate']:>7.2%} {r['wlr']:>8.3f} {r['total_return']:>+9.4%}")

    # 保存
    out = {r["name"]: r for r in results}
    with open("/Users/jimingzhang/kronos/comparison_results.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 结果 → comparison_results.json")
    print(f"⏱️  耗时: {time.time()-t0:.1f}秒")
    return out


if __name__ == "__main__":
    main()
