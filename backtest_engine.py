"""
backtest_engine.py
=================
全局统一回测引擎 — 所有策略共用

规则：
  - FEE_AND_SLIPPAGE = 0.002（0.2%，不变）
  - 动态出场：1.5xATR止损 / 3xATR触发→保本移动止损 / 24h强制
  - 信号去重：2h冷却（持仓期不重复开仓）
"""

import numpy as np
import pandas as pd
from collections import deque

# ── 全局费率（唯一标准）─────────────────────────────────────────────
FEE_AND_SLIPPAGE = 0.002   # 0.2% 总损耗（maker+taker+滑点）


class UnifiedBacktester:
    """
    统一回测引擎：
      - 逐根K线模拟，完整动态出场
      - 每笔交易扣0.2%手续费+滑点
      - 2h冷却去重
    """

    def __init__(self, df_1h, is_long: bool, params: dict, wlr_tracker=None):
        self.df      = df_1h
        self.is_long = is_long
        self.p       = params
        self.wlr     = wlr_tracker
        self.trades  = []

    def run(self, signal, cooldown_1h: int = 2) -> dict:
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

            # 冷却期
            if i - last_exit_idx < cooldown_1h:
                i += 1
                continue

            # 做空WLR过滤
            if not self.is_long and self.wlr is not None:
                if self.wlr.get_prev_wlr() < self.p.get("wlr_min", 1.2):
                    i += 1
                    continue

            # 入场
            entry_price = open_[i + 1]
            entry_idx   = i + 1
            entry_atr   = atr[i] if not np.isnan(atr[i]) and atr[i] > 0 else 1.0
            stop_loss   = (entry_price - self.p["atr_sl"] * entry_atr if self.is_long
                          else entry_price + self.p["atr_sl"] * entry_atr)
            tp_triggered = False
            j = entry_idx

            # P0 Fix: 用exited标志替换while-else，避免循环正常结束后误执行else分支
            exited = False
            while j < min(entry_idx + self.p["max_hold_1h"], n):
                curr_high = high[j]
                curr_low  = low[j]
                curr_atr  = atr[j] if not np.isnan(atr[j]) and atr[j] > 0 else entry_atr
                hold_bars = j - entry_idx

                # 止损
                if self.is_long:
                    if curr_low <= stop_loss:
                        ret = (stop_loss / entry_price - 1 - FEE_AND_SLIPPAGE) * self.p["max_pos"]
                        self._record(ret, hold_bars)
                        last_exit_idx = j
                        if self.wlr:
                            self.wlr.update_last_result(ret)
                        i = j + 1
                        exited = True
                        break
                else:
                    if curr_high >= stop_loss:
                        ret = (entry_price / stop_loss - 1 - FEE_AND_SLIPPAGE) * self.p["max_pos"]
                        self._record(ret, hold_bars)
                        last_exit_idx = j
                        if self.wlr:
                            self.wlr.update_last_result(ret)
                        i = j + 1
                        exited = True
                        break

                # 移动止盈触发
                if not tp_triggered:
                    profit_pct = ((curr_high - entry_price) / entry_price if self.is_long
                                  else (entry_price - curr_low) / entry_price)
                    if profit_pct >= self.p["atr_tp"]:
                        stop_loss   = entry_price
                        tp_triggered = True

                # 移动止损触发
                if tp_triggered:
                    if self.is_long and curr_low <= stop_loss:
                        ret = (entry_price / entry_price - 1 - FEE_AND_SLIPPAGE) * self.p["max_pos"]
                        self._record(ret, hold_bars)
                        last_exit_idx = j
                        if self.wlr:
                            self.wlr.update_last_result(ret)
                        i = j + 1
                        exited = True
                        break
                    elif not self.is_long and curr_high >= stop_loss:
                        ret = (entry_price / entry_price - 1 - FEE_AND_SLIPPAGE) * self.p["max_pos"]
                        self._record(ret, hold_bars)
                        last_exit_idx = j
                        if self.wlr:
                            self.wlr.update_last_result(ret)
                        i = j + 1
                        exited = True
                        break

                # 24h强制平仓
                if hold_bars >= self.p["max_hold_1h"] - 1:
                    exit_price = close[j]
                    ret = ((exit_price / entry_price - 1 - FEE_AND_SLIPPAGE) * self.p["max_pos"] if self.is_long
                           else (entry_price / exit_price - 1 - FEE_AND_SLIPPAGE) * self.p["max_pos"])
                    self._record(ret, hold_bars)
                    last_exit_idx = j
                    if self.wlr:
                        self.wlr.update_last_result(ret)
                    i = j + 1
                    exited = True
                    break

                j += 1
            
            # P0 Fix: 只有真正触发了退出（通过break跳出）才更新last_exit_idx，
            # while-else只在数据完全耗尽时执行（正常结束循环），不再用close[-1]作为错误的exit_price
            if not exited:
                # 数据耗尽，未触发任何退出条件，按24h强制平仓处理
                exit_price = close[j] if j < n else close[-1]
                ret = ((exit_price / entry_price - 1 - FEE_AND_SLIPPAGE) * self.p["max_pos"] if self.is_long
                       else (entry_price / exit_price - 1 - FEE_AND_SLIPPAGE) * self.p["max_pos"])
                self._record(ret, j - entry_idx)
                last_exit_idx = j
                if self.wlr:
                    self.wlr.update_last_result(ret)
                i = j + 1

        return self._summary()

    def _record(self, ret, hold_bars):
        self.trades.append({"return": ret, "hold_bars": hold_bars})

    def _summary(self) -> dict:
        rets = np.array([t["return"] for t in self.trades], dtype=float)
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


class WLRTracker:
    """追踪最近已结算交易的盈亏比"""
    def __init__(self, lookback=50):
        self.lookback = lookback
        self.results  = deque(maxlen=lookback)
        self.last_wlr = 99.99

    def add_result(self, ret):
        self.results.append(ret)

    def update_last_result(self, ret):
        if not isinstance(ret, (int, float)):
            return  # 忽略无效类型
        if len(self.results) > 0:
            self.results[-1] = float(ret)
        else:
            self.results.append(float(ret))
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
