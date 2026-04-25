#!/usr/bin/env python3
"""
strategy_bear_2022.py
=====================
熊市均值回归策略验证 — 用2022年全年数据

核心逻辑：RSI超跌 → 趋势反弹
参数（熊市专用）：
  - RSI < 30（深度超卖才进场）
  - ADX > 30（需要强趋势才能反弹）
  - 大趋势向下（熊市背景）
  - 持仓：6h动态出场
  - 做空：RSI>70 + ADX>25 + 大趋势向下（顺势做空）
"""

import json, time
import numpy as np
import pandas as pd
import talib
from collections import deque

DATA_DIR  = "/Users/jimingzhang/Desktop/crypto_data_Pre5m"
SYMBOL    = "BTC"
INTERVAL  = "5m"
RSI_PERIOD  = 14
ATR_PERIOD  = 14
WL_LOOKBACK = 50

# ── 熊市做多参数 ────────────────────────────────────────────────────
L_MAX_POS    = 0.80
L_ACTUAL_LEV = 0.80
L_RSI_FILTER = 30      # 深度超卖：RSI<30（熊市）
L_ADX_THRESH = 30      # 需要强趋势
L_ATR_MULT_SL = 1.5
L_ATR_MULT_TP = 3.0
MAX_HOLD_1H   = 24
ROLLING_WINDOW = 8

# ── 熊市做空参数 ────────────────────────────────────────────────────
S_MAX_POS    = 0.20
S_ACTUAL_LEV = 0.20
S_RSI_FILTER = 70      # RSI>70超买才做空
S_ADX_THRESH = 25      # 比做多宽松
S_ATR_MULT_SL = 1.5
S_ATR_MULT_TP = 3.0
S_WLR_MIN     = 1.2
COOLDOWN_1H   = 2


def load_year_data(symbol, interval, year, warmup_days=90):
    """只加载指定年份+预热期的数据"""
    fname = f"{symbol}_USDT_{interval}_from_20180101.csv"
    path  = f"{DATA_DIR}/{fname}"
    df    = pd.read_csv(path)
    dt_col = next((c for c in df.columns if "datetime" in c.lower()), df.columns[0])
    df["ts"] = pd.to_datetime(df[dt_col], errors="coerce").dt.tz_localize(None)
    df = df.dropna(subset=["ts"])
    # 目标年份
    start = f"{year}-01-01"
    end   = f"{year+1}-01-01"
    # 预热期往前延伸warmup_days天
    from datetime import datetime, timedelta
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
    rsi_5m = talib.RSI(close, timeperiod=RSI_PERIOD)

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
    df["plus_di"]    = plus_di.reindex(df.index, method="ffill").shift(lag)
    df["minus_di"]   = minus_di.reindex(df.index, method="ffill").shift(lag)
    df["ma20_1h"]   = ma20_1h.reindex(df.index, method="ffill").shift(lag)
    df["ma50_1h"]   = ma50_1h.reindex(df.index, method="ffill").shift(lag)
    df["ma200_1h"]  = ma200_1h.reindex(df.index, method="ffill").shift(lag)
    df["atr_1h"]    = atr_1h.reindex(df.index, method="ffill").shift(lag)

    df["major_up"]   = (df["ma20_1h"] > df["ma50_1h"]) & (df["ma50_1h"] > df["ma200_1h"])
    df["major_down"] = (df["ma20_1h"] < df["ma50_1h"]) & (df["ma50_1h"] < df["ma200_1h"])
    df["rsi_5m"]  = rsi_5m
    df["rsi_15m"] = rsi_15m
    return df


class WLRTracker:
    def __init__(self, lookback=WL_LOOKBACK):
        self.lookback = lookback
        self.results  = deque(maxlen=lookback)
        self.last_wlr = 99.99

    def add_result(self, ret):
        self.results.append(ret)

    def update_last_result(self, ret):
        if len(self.results) > 0:
            self.results[-1] = ret
        else:
            self.results.append(ret)
        self._recalc()

    def get_prev_wlr(self):
        return self.last_wlr

    def _recalc(self):
        wins   = [r for r in self.results if r > 0]
        losses = [abs(r) for r in self.results if r < 0]
        if len(wins) < 3 or len(losses) < 3:
            self.last_wlr = 99.99
            return
        avg_w = np.mean(wins)
        avg_l = np.mean(losses)
        self.last_wlr = float(avg_w / avg_l) if avg_l > 1e-9 else 99.99


class DynamicBacktester:
    def __init__(self, df_1h, is_long, params, wlr_tracker=None):
        self.df   = df_1h
        self.is_long = is_long
        self.p    = params
        self.wlr  = wlr_tracker
        self.trades = []
        self.all_rets = []

    def run(self, signal) -> dict:
        sig   = signal.astype(bool).values
        close = self.df["close"].values
        high  = self.df["high"].values
        low   = self.df["low"].values
        open_ = self.df["open"].values
        atr   = self.df["atr_1h"].values
        n     = len(sig)
        last_exit_idx = -999
        i = 0

        while i < n - 1:
            if not sig[i]:
                i += 1
                continue
            if i - last_exit_idx < COOLDOWN_1H:
                i += 1
                continue
            if not self.is_long and self.wlr is not None:
                if self.wlr.get_prev_wlr() < S_WLR_MIN:
                    i += 1
                    continue

            entry_price = open_[i + 1]
            entry_idx   = i + 1
            entry_atr   = atr[i] if not np.isnan(atr[i]) and atr[i] > 0 else 1.0
            stop_loss   = entry_price - self.p["atr_sl"] * entry_atr if self.is_long \
                         else entry_price + self.p["atr_sl"] * entry_atr
            tp_triggered = False
            j = entry_idx

            while j < min(entry_idx + MAX_HOLD_1H, n):
                curr_high = high[j]
                curr_low  = low[j]
                curr_atr  = atr[j] if not np.isnan(atr[j]) and atr[j] > 0 else entry_atr
                hold_bars = j - entry_idx

                # 止损
                if self.is_long:
                    if curr_low <= stop_loss:
                        ret = (stop_loss / entry_price - 1) * self.p["max_pos"]
                        self._record(ret, hold_bars); last_exit_idx = j
                        if self.wlr: self.wlr.update_last_result(ret)
                        i = j + 1; break
                else:
                    if curr_high >= stop_loss:
                        ret = (entry_price / stop_loss - 1) * self.p["max_pos"]
                        self._record(ret, hold_bars); last_exit_idx = j
                        if self.wlr: self.wlr.update_last_result(ret)
                        i = j + 1; break

                # 移动止盈触发
                if not tp_triggered:
                    profit_pct = (curr_high - entry_price) / entry_price if self.is_long \
                                 else (entry_price - curr_low) / entry_price
                    if profit_pct >= self.p["atr_tp"]:
                        stop_loss = entry_price
                        tp_triggered = True

                # 移动止损被触发
                if tp_triggered:
                    if self.is_long and curr_low <= stop_loss:
                        ret = 0.0 * self.p["max_pos"]
                        self._record(ret, hold_bars); last_exit_idx = j
                        if self.wlr: self.wlr.update_last_result(ret)
                        i = j + 1; break
                    elif not self.is_long and curr_high >= stop_loss:
                        ret = 0.0 * self.p["max_pos"]
                        self._record(ret, hold_bars); last_exit_idx = j
                        if self.wlr: self.wlr.update_last_result(ret)
                        i = j + 1; break

                # 24h强制平仓
                if hold_bars >= MAX_HOLD_1H - 1:
                    exit_price = close[j]
                    ret = (exit_price / entry_price - 1) * self.p["max_pos"] if self.is_long \
                          else (entry_price / exit_price - 1) * self.p["max_pos"]
                    self._record(ret, hold_bars); last_exit_idx = j
                    if self.wlr: self.wlr.update_last_result(ret)
                    i = j + 1; break
                j += 1
            else:
                if j >= n: break
                exit_price = close[-1]
                ret = (exit_price / entry_price - 1) * self.p["max_pos"] if self.is_long \
                      else (entry_price / exit_price - 1) * self.p["max_pos"]
                self._record(ret, j - entry_idx)
                last_exit_idx = j
                if self.wlr: self.wlr.update_last_result(ret)
                i = j + 1

        return self._summary()

    def _record(self, ret, hold_bars):
        self.all_rets.append(ret)
        self.trades.append({"return": ret, "hold_bars": hold_bars})

    def _summary(self) -> dict:
        rets = np.array(self.all_rets, dtype=float)
        if len(rets) == 0:
            return {"signal_count": 0, "win_rate": 0, "avg_return": 0,
                    "profit_factor": 0, "win_loss_ratio": 0,
                    "max_drawdown": 0, "total_return": 0, "trades": []}
        wins   = rets[rets > 0]
        losses = rets[rets < 0]
        wr     = len(wins) / len(rets)
        avg_r  = float(rets.mean())
        tot_w  = float(wins.sum()) if len(wins) > 0 else 0.0
        tot_l  = abs(float(losses.sum())) if len(losses) > 0 else 1e-9
        pf     = tot_w / tot_l if tot_l > 1e-9 else 99.99
        avg_w  = float(wins.mean()) if len(wins) > 0 else 0.0
        avg_l  = abs(float(losses.mean())) if len(losses) > 0 else 1e-9
        wlr    = avg_w / avg_l if avg_l > 1e-9 else 99.99
        max_dd = float(rets.min())
        return {
            "signal_count":  len(rets),
            "win_rate":      round(wr, 4),
            "avg_return":    round(avg_r, 6),
            "profit_factor": round(pf, 2),
            "win_loss_ratio": round(wlr, 3),
            "max_drawdown":  round(max_dd, 6),
            "total_return":  round(float(rets.sum()), 6),
            "trades":        self.trades,
        }


def main():
    t0 = time.time()
    print("📥 加载2022年数据...")
    df = load_year_data(SYMBOL, INTERVAL, 2022)
    print(f"  2022年K线: {len(df):,} | {df.index[0]} → {df.index[-1]}")

    print("⚙️  计算指标...")
    df = compute_indicators(df)
    warmup = RSI_PERIOD + 200 + 12 + 1
    df = df.iloc[warmup:].copy()
    print(f"  有效数据: {len(df):,} 根 | {df.index[0]} → {df.index[-1]}")

    df_1h = df.resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "adx_1h": "last", "plus_di": "last", "minus_di": "last",
        "ma20_1h": "last", "ma50_1h": "last", "ma200_1h": "last",
        "rsi_15m": "last", "atr_1h": "last",
        "major_up": "last", "major_down": "last",
    }).dropna()
    for col in ["major_up", "major_down"]:
        df_1h[col] = df_1h[col].astype(bool)

    # debug：指标分布
    print(f"  major_up占比: {df_1h['major_up'].mean():.2%} | major_down: {df_1h['major_down'].mean():.2%}")
    print(f"  rsi_15m<30占比: {(df_1h['rsi_15m']<30).mean():.2%} | rsi>70: {(df_1h['rsi_15m']>70).mean():.2%}")
    print(f"  ADX均值: {df_1h['adx_1h'].mean():.1f} | ADX>30占比: {(df_1h['adx_1h']>30).mean():.2%}")

    df_1h["rolling_max"] = df_1h["close"].rolling(window=ROLLING_WINDOW).max().shift(1)
    df_1h["rolling_min"] = df_1h["close"].rolling(window=ROLLING_WINDOW).min().shift(1)

    # 熊市ADX拐点：ADX从低位回升即可（不需要<20门槛）
    df_1h["adx_turn_long"] = (
        (df_1h["adx_1h"].shift(1) < df_1h["adx_1h"].shift(2)) &  # ADX前一根在下降
        (df_1h["adx_1h"] > df_1h["adx_1h"].shift(1)) &            # 本根ADX在上升
        (df_1h["adx_1h"] > L_ADX_THRESH)                          # ADX>30
    )
    df_1h["adx_turn_short"] = (
        (df_1h["adx_1h"].shift(1) > df_1h["adx_1h"].shift(2)) &
        (df_1h["adx_1h"] < df_1h["adx_1h"].shift(1)) &
        (df_1h["adx_1h"] > S_ADX_THRESH)
    )

    # ── 熊市做多：均值回归 ──────────────────────────────────────
    # 熊市里不做大趋势要求，只要求：
    # RSI深度超卖 + ADX强势反弹启动 + RSI<30
    long_sig = (
        (df_1h["rsi_15m"] < L_RSI_FILTER) &   # 深度超卖：RSI<30
        ((df_1h["close"] > df_1h["rolling_max"]) | df_1h["adx_turn_long"]) &  # 反弹启动
        (df_1h["adx_1h"] > L_ADX_THRESH) &    # 强趋势反弹
        (df_1h["plus_di"] > df_1h["minus_di"])  # 多头力量占优
    )

    # ── 熊市做空：顺势做空（主战场）────────────────────────────
    # 熊市里RSI>70的反弹机会更多，用RSI>55（次级超买）做入场
    short_sig_base = (
        (df_1h["rsi_15m"] > 55) &             # 次级超买
        ((df_1h["close"] < df_1h["rolling_min"]) | df_1h["adx_turn_short"]) &  # 跌破支撑或ADX向下拐
        (df_1h["adx_1h"] > S_ADX_THRESH) &     # ADX>25确认下跌趋势
        (df_1h["minus_di"] > df_1h["plus_di"])  # 空头力量占优
    )

    print(f"\n  2022年信号 | 做多: {long_sig.sum()} | 做空(过滤前): {short_sig_base.sum()}")

    # ── 回测 ────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  📈 熊市做多（RSI<30 + ADX>30 + 动态出场）")
    print("="*60)
    long_tester = DynamicBacktester(df_1h, is_long=True, params={
        "atr_sl": L_ATR_MULT_SL, "atr_tp": L_ATR_MULT_TP, "max_pos": L_MAX_POS,
    })
    long_res = long_tester.run(long_sig)

    if long_res["signal_count"] > 0:
        print(f"\n  做多结果（RSI<{L_RSI_FILTER}, ADX>{L_ADX_THRESH}, 名义{int(L_MAX_POS*100)}%）")
        print(f"  信号数量: {long_res['signal_count']:>6} | 胜率: {long_res['win_rate']:.2%}")
        print(f"  平均收益: {long_res['avg_return']:.4%} | 盈亏比: {long_res['win_loss_ratio']:.3f}")
        print(f"  盈利因子: {long_res['profit_factor']:.2f} | 总收益: {long_res['total_return']:.4%}")
        print(f"  最大单亏: {long_res['max_drawdown']:.4%}")
    else:
        print("  ⚠️  0个做多信号")

    print("\n" + "="*60)
    print("  📉 熊市做空（RSI>70 + ADX>25 + WLR过滤）")
    print("="*60)
    wlr_t = WLRTracker()
    short_tester = DynamicBacktester(df_1h, is_long=False, params={
        "atr_sl": S_ATR_MULT_SL, "atr_tp": S_ATR_MULT_TP, "max_pos": S_MAX_POS,
    }, wlr_tracker=wlr_t)
    short_res = short_tester.run(short_sig_base)

    if short_res["signal_count"] > 0:
        print(f"\n  做空结果（RSI>{S_RSI_FILTER}, ADX>{S_ADX_THRESH}, 名义{int(S_MAX_POS*100)}%）")
        print(f"  信号数量: {short_res['signal_count']:>6} | 胜率: {short_res['win_rate']:.2%}")
        print(f"  平均收益: {short_res['avg_return']:.4%} | 盈亏比: {short_res['win_loss_ratio']:.3f}")
        print(f"  盈利因子: {short_res['profit_factor']:.2f} | 总收益: {short_res['total_return']:.4%}")
        print(f"  最大单亏: {short_res['max_drawdown']:.4%}")
    else:
        print("  ⚠️  0个做空信号（可能WLR过滤全挡）")

    # ── 合并 ───────────────────────────────────────────────
    ln = long_res["signal_count"]
    sn = short_res["signal_count"]
    tn = ln + sn
    years_span = len(df_1h) / 24 / 365

    print("\n" + "="*60)
    print("  📊 2022熊市总览")
    print("="*60)
    if ln > 0 and sn > 0:
        m_wr  = (long_res["win_rate"]*ln + short_res["win_rate"]*sn) / tn
        m_avg = (long_res["avg_return"]*ln + short_res["avg_return"]*sn) / tn
        m_tot = long_res["total_return"] + short_res["total_return"]
    elif ln > 0:
        m_wr, m_avg, m_tot = long_res["win_rate"], long_res["avg_return"], long_res["total_return"]
    else:
        m_wr = m_avg = m_tot = 0

    print(f"  做多: {ln} | 做空: {sn} | 总计: {tn}")
    print(f"  合并胜率: {m_wr:.2%} | 合并均收益: {m_avg:.4%} | 合并总收益: {m_tot:.4%}")
    print(f"  数据范围: {years_span:.1f}年")

    # 保存
    out = {
        "version": "bear_2022",
        "year": 2022,
        "long":  {"params": {"rsi_filter": L_RSI_FILTER, "adx_thresh": L_ADX_THRESH,
                             "max_pos": L_MAX_POS, "actual_lev": L_ACTUAL_LEV},
                  "summary": long_res},
        "short": {"params": {"rsi_filter": S_RSI_FILTER, "adx_thresh": S_ADX_THRESH,
                             "max_pos": S_MAX_POS, "actual_lev": S_ACTUAL_LEV, "wlr_min": S_WLR_MIN},
                  "summary": short_res},
    }
    with open("/Users/jimingzhang/kronos/pattern_library_bear_2022.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 熊市模式库 → pattern_library_bear_2022.json")
    print(f"⏱️  耗时: {time.time()-t0:.1f}秒")
    return out

if __name__ == "__main__":
    main()
