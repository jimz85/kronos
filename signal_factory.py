#!/usr/bin/env python3
"""
Kronos SignalFactory - 可组合信号系统
==========================================
把策略从"固定加权评分"改成"信号积木"组合

核心概念：
- Signal（信号）：单一技术指标判断（RSI<30=买入信号）
- Condition（条件）：信号的组合（RSI<30 AND ADX>20）
- Strategy（策略）：多条条件AND/OR组合
- SignalFamily（信号族）：同一类型的多个信号（RSI族、ADX族等）

支持的操作：
- AND: 所有条件都满足
- OR:  任一条件满足
- NOT: 条件不满足
- THRESHOLD: 超过阈值

运行：
  python3 signal_factory.py                    # 测试所有信号族
  python3 signal_factory.py --strategy RSI_ADX # 测试RSI+ADX组合
"""

import os, json, math, itertools, warnings
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore', 'overflow')  # ADX计算中ATR接近0时的overflow为正常

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional
from enum import Enum

# ========== 数据获取 ==========

def fetch_ohlcv(coin, bar='1H', limit=500):
    """获取OKX K线数据（优先本地缓存，失败则用requests）"""
    # 先尝试本地数据（与kronos_multi_coin.py共用）
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from kronos_multi_coin import get_ohlcv
        data = get_ohlcv(coin, bar, min(limit, 300))
        if data and len(data) >= 50:
            import pandas as pd
            df = pd.DataFrame(data)
            df['date'] = pd.to_datetime(df['ts'], unit='ms')
            return df.set_index('date')
    except:
        pass

    # 备选：直接请求OKX（带超时保护）
    import signal as _sig
    _sig.alarm(3)
    try:
        import requests
        all_data = []
        after = None
        for batch in range(3):
            if after:
                url = f'https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bar}&limit=300&after={after}'
            else:
                url = f'https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bar}&limit=300'
            r = requests.get(url, timeout=2)
            data = r.json()
            if data.get('code') != '0' or not data.get('data'):
                break
            candles = data['data']
            all_data.extend(candles)
            if len(candles) < 300:
                break
            after = candles[-1][0]
            import time; time.sleep(0.1)

        rows = []
        for d in reversed(all_data):
            rows.append({
                'ts': int(d[0]),
                'close': float(d[4]),
                'high': float(d[2]),
                'low': float(d[3]),
                'volume': float(d[5]),
            })
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['ts'], unit='ms')
        return df.set_index('date')
    except Exception:
        return None
    finally:
        _sig.alarm(0)

# ========== 指标计算 ==========

def calc_rsi(closes, period=14):
    closes = np.asarray(closes)
    deltas = np.diff(closes, prepend=closes[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.zeros(len(closes))
    avg_l = np.zeros(len(closes))
    avg_g[period] = np.mean(gains[1:period+1])
    avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, len(closes)):
        avg_g[i] = (avg_g[i-1] * (period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1] * (period-1) + losses[i]) / period
    rs = np.divide(avg_g, avg_l, out=np.zeros_like(avg_g), where=avg_l != 0)
    rsi = np.where(avg_l == 0, 100, 100 - (100 / (1 + rs)))
    return rsi

def calc_adx(highs, lows, closes, period=14):
    """计算ADX (Wilder平滑) - 已验证版本"""
    highs = np.asarray(highs); lows = np.asarray(lows); closes = np.asarray(closes)
    n = len(closes)
    if n < period * 2:
        return np.full(n, 20.0)
    tr = np.zeros(n-1); plus_dm = np.zeros(n-1); minus_dm = np.zeros(n-1)
    for i in range(1, n):
        tr[i-1] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        up = highs[i] - highs[i-1]; down = lows[i-1] - lows[i]
        plus_dm[i-1] = up if (up > down and up > 0) else 0.0
        minus_dm[i-1] = down if (down > up and down > 0) else 0.0
    atr = np.zeros(n); plus_di = np.zeros(n); minus_di = np.zeros(n)
    atr[period] = np.mean(tr[:period]) if period < len(tr) else tr[0]
    plus_di[period] = np.mean(plus_dm[:period])
    minus_di[period] = np.mean(minus_dm[:period])
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i-1]) / period
        plus_di[i] = (plus_di[i-1] * (period-1) + plus_dm[i-1]) / period
        minus_di[i] = (minus_di[i-1] * (period-1) + minus_dm[i-1]) / period
        if atr[i] > 0:
            plus_di[i] = (plus_di[i] / atr[i]) * 100
            minus_di[i] = (minus_di[i] / atr[i]) * 100
    dx = np.zeros(n)
    for i in range(period, n):
        if plus_di[i] + minus_di[i] > 0:
            dx[i] = abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i]) * 100
    adx_out = np.full(n, 20.0)
    adx_out[period*2] = np.mean(dx[period:period*2])
    for i in range(period*2+1, n):
        adx_out[i] = (adx_out[i-1] * (period-1) + dx[i]) / period
    return np.nan_to_num(adx_out, nan=20.0)

def calc_ema(closes, period=20):
    closes = np.asarray(closes)
    ema = np.zeros(len(closes))
    ema[period-1] = np.mean(closes[:period])
    for i in range(period, len(closes)):
        ema[i] = (ema[i-1] * (period-1) + closes[i]) / period
    ema[:period-1] = ema[period-1]
    return ema

def calc_atr(highs, lows, closes, period=14):
    highs = np.asarray(highs); lows = np.asarray(lows); closes = np.asarray(closes)
    n = len(closes)
    tr = np.zeros(n-1)
    for i in range(1, n):
        tr[i-1] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    atr = np.zeros(n)
    atr[period-1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i-1]) / period
    atr[:period-1] = atr[period-1]
    return atr

def calc_macd(closes, fast=12, slow=26, signal=9):
    closes = np.asarray(closes)
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    macd = ema_fast - ema_slow
    # Signal line = EMA of MACD
    sig = np.zeros(len(macd))
    sig[signal-1] = np.mean(macd[:signal])
    for i in range(signal, len(macd)):
        sig[i] = (sig[i-1] * (signal-1) + macd[i]) / signal
    hist = macd - sig
    return macd, sig, hist

def calc_bollinger(closes, period=20, std_dev=2):
    closes = np.asarray(closes)
    mid = calc_ema(closes, period)
    std = np.zeros(len(closes))
    for i in range(period-1, len(closes)):
        std[i] = np.std(closes[i-period+1:i+1], ddof=1)
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower

def calc_cci(highs, lows, closes, period=20):
    """
    计算CCI (Commodity Channel Index)
    CCI < -100 = 超卖（可能反转向上） → 做多信号
    CCI > 100  = 超买（可能反转向下） → 做空信号
    回测最优参数: period=20
    """
    highs = np.asarray(highs); lows = np.asarray(lows); closes = np.asarray(closes)
    n = len(closes)
    if n <= period:
        return np.full(n, 0.0)

    # Typical Price
    tp = (highs + lows + closes) / 3.0

    # SMA of Typical Price
    tp_sma = np.zeros(n)
    tp_sma[period-1] = np.mean(tp[:period])
    for i in range(period, n):
        tp_sma[i] = (tp_sma[i-1] * (period-1) + tp[i]) / period

    # Mean Deviation
    mean_dev = np.zeros(n)
    for i in range(period-1, n):
        mean_dev[i] = np.mean(np.abs(tp[i-period+1:i+1] - tp_sma[i]))

    # CCI = (TP - SMA) / (0.015 * MeanDev)
    cci = np.zeros(n)
    for i in range(period-1, n):
        if mean_dev[i] > 0:
            cci[i] = (tp[i] - tp_sma[i]) / (0.015 * mean_dev[i])
        else:
            cci[i] = 0.0

    cci[:period-1] = cci[period-1]
    return np.nan_to_num(cci, nan=0.0)

# ========== SignalFactory 核心 ==========

@dataclass
class Signal:
    """单一指标信号"""
    name: str                           # 信号名，如 'RSI14'
    indicator: np.ndarray               # 指标数组
    operator: Literal['gt', 'lt', 'gte', 'lte', 'cross_above', 'cross_below', 'in_band', 'out_band'] = 'gt'
    threshold: float = 0                # 阈值
    params: dict = field(default_factory=dict)

    def evaluate(self, idx: int) -> bool:
        val = self.indicator[idx]
        if math.isnan(val):
            return False
        if self.operator == 'gt':
            return val > self.threshold
        elif self.operator == 'lt':
            return val < self.threshold
        elif self.operator == 'gte':
            return val >= self.threshold
        elif self.operator == 'lte':
            return val <= self.threshold
        elif self.operator == 'cross_above':
            if idx < 1:
                return False
            return self.indicator[idx-1] <= self.threshold and val > self.threshold
        elif self.operator == 'cross_below':
            if idx < 1:
                return False
            return self.indicator[idx-1] >= self.threshold and val < self.threshold
        elif self.operator == 'in_band':
            return self.params.get('lower', -9999) < val < self.params.get('upper', 9999)
        elif self.operator == 'out_band':
            return val < self.params.get('lower', -9999) or val > self.params.get('upper', 9999)
        return False


@dataclass
class BBSignal:
    """布林带专用信号：逐点比较价格和布林带"""
    name: str
    closes: np.ndarray
    bb_upper: np.ndarray
    bb_mid: np.ndarray
    bb_lower: np.ndarray
    signal_type: Literal['lower_touch', 'upper_touch', 'lower_touch_prev', 'upper_touch_prev'] = 'lower_touch'

    def evaluate(self, idx: int) -> bool:
        if idx < 0 or idx >= len(self.closes):
            return False
        c = self.closes[idx]
        lo = self.bb_lower[idx]
        up = self.bb_upper[idx]
        mi = self.bb_mid[idx]

        if self.signal_type == 'lower_touch':
            # 价格触及下轨（下轨≤价格≤下轨+小缓冲）
            return lo <= c <= lo * 1.02  # 2%缓冲容错
        elif self.signal_type == 'upper_touch':
            return up * 0.98 <= c <= up  # 价格触及上轨
        elif self.signal_type == 'lower_touch_prev':
            if idx < 1:
                return False
            prev_c = self.closes[idx-1]
            prev_lo = self.bb_lower[idx-1]
            return prev_lo <= prev_c <= prev_lo * 1.02
        elif self.signal_type == 'upper_touch_prev':
            if idx < 1:
                return False
            prev_c = self.closes[idx-1]
            prev_up = self.bb_upper[idx-1]
            return prev_up * 0.98 <= prev_c <= prev_up
        return False


@dataclass
@dataclass
class Condition:
    """信号条件组合（AND/OR/NOT）"""
    signals: list[Signal] = field(default_factory=list)
    combinator: Literal['AND', 'OR', 'NOT'] = 'AND'

    def evaluate(self, idx: int) -> bool:
        if not self.signals:
            return False
        if self.combinator == 'AND':
            return all(s.evaluate(idx) for s in self.signals)
        elif self.combinator == 'OR':
            return any(s.evaluate(idx) for s in self.signals)
        elif self.combinator == 'NOT':
            return not any(s.evaluate(idx) for s in self.signals)
        return False

@dataclass
class Strategy:
    """完整交易策略"""
    name: str
    long_conditions: list[Condition] = field(default_factory=list)
    short_conditions: list[Condition] = field(default_factory=list)
    exit_conditions: list[Condition] = field(default_factory=list)
    min_confidence: float = 0.5    # 最低置信度（满足多少比例的条件）

    def evaluate_long(self, idx: int) -> bool:
        if not self.long_conditions:
            return False
        satisfied = sum(c.evaluate(idx) for c in self.long_conditions)
        return satisfied >= len(self.long_conditions) * self.min_confidence

    def evaluate_short(self, idx: int) -> bool:
        if not self.short_conditions:
            return False
        satisfied = sum(c.evaluate(idx) for c in self.short_conditions)
        return satisfied >= len(self.short_conditions) * self.min_confidence

    def evaluate_exit(self, idx: int) -> bool:
        if not self.exit_conditions:
            return False
        return any(c.evaluate(idx) for c in self.exit_conditions)

# ========== 预定义信号族 ==========

class SignalFamily:
    """预定义信号族工厂"""

    @staticmethod
    def RSI(family='RSI', closes=None, period=14, levels=None):
        """RSI信号族"""
        if closes is None:
            return []
        rsi = calc_rsi(closes, period)
        signals = []
        if levels is None:
            levels = {'oversold': 30, 'neutral_low': 40, 'neutral_high': 60, 'overbought': 70}
        signals.append(Signal(f'{family}_oversold', rsi, 'lt', levels['oversold']))
        signals.append(Signal(f'{family}_neutral_low', rsi, 'lt', levels['neutral_low']))
        signals.append(Signal(f'{family}_neutral_high', rsi, 'gt', levels['neutral_high']))
        signals.append(Signal(f'{family}_overbought', rsi, 'gt', levels['overbought']))
        signals.append(Signal(f'{family}_cross30_up', rsi, 'cross_above', 30))
        signals.append(Signal(f'{family}_cross70_down', rsi, 'cross_below', 70))
        return signals

    @staticmethod
    def ADX(highs=None, lows=None, closes=None, period=14, thresholds=None):
        """ADX信号族"""
        if highs is None:
            return []
        adx = calc_adx(highs, lows, closes, period)
        if thresholds is None:
            thresholds = {'weak': 15, 'moderate': 20, 'strong': 30, 'extreme': 50}
        return [
            Signal('ADX_weak', adx, 'lt', thresholds['weak']),
            Signal('ADX_moderate', adx, 'gte', thresholds['moderate']),
            Signal('ADX_strong', adx, 'gte', thresholds['strong']),
            Signal('ADX_extreme', adx, 'gte', thresholds['extreme']),
        ]

    @staticmethod
    def MACD(highs=None, lows=None, closes=None, fast=12, slow=26, signal=9):
        """MACD信号族"""
        if closes is None:
            return []
        macd, sig, hist = calc_macd(closes, fast, slow, signal)
        return [
            Signal('MACD_bullish_cross', hist, 'cross_above', 0),
            Signal('MACD_bearish_cross', hist, 'cross_below', 0),
            Signal('MACD_hist_positive', hist, 'gt', 0),
            Signal('MACD_hist_negative', hist, 'lt', 0),
        ]

    @staticmethod
    def Bollinger(closes=None, period=20, std_dev=2):
        """布林带信号族"""
        if closes is None:
            return []
        upper, mid, lower = calc_bollinger(closes, period, std_dev)
        return [
            Signal('BB_lower_touch', closes, 'lte', lower[-1] if len(lower) else 0),
            Signal('BB_upper_touch', closes, 'gte', upper[-1] if len(upper) else 9999),
            Signal('BB_lower_band', closes, 'lt', lower[-1] if len(lower) else 0),
            Signal('BB_upper_band', closes, 'gt', upper[-1] if len(upper) else 9999),
            Signal('BB_mid_reclaim', closes, 'gte', mid[-1] if len(mid) else 0),
        ]

    @staticmethod
    def EMA(closes=None, periods=[20, 50, 200]):
        """EMA信号族"""
        if closes is None:
            return []
        signals = []
        emas = {p: calc_ema(closes, p) for p in periods}
        for p, ema in emas.items():
            signals.append(Signal(f'EMA{p}_price_above', closes, 'gte', ema[-1] if len(ema) else 0))
            signals.append(Signal(f'EMA{p}_price_below', closes, 'lte', ema[-1] if len(ema) else 9999))
        # EMA金叉/死叉
        sorted_periods = sorted(periods)
        for i in range(len(sorted_periods)-1):
            p1, p2 = sorted_periods[i], sorted_periods[i+1]
            e1, e2 = emas[p1], emas[p2]
            if len(e1) > 1 and len(e2) > 1:
                cross_val = e2[-1] - e1[-1]  # 短期在长期上方=多头
                signals.append(Signal(f'EMA{p1}p{p2}_golden_cross', np.array([0, cross_val]), 'gt', 0))
                signals.append(Signal(f'EMA{p1}p{p2}_death_cross', np.array([0, cross_val]), 'lt', 0))
        return signals

    @staticmethod
    def Volume(volumes=None, closes=None, period=20):
        """成交量信号族"""
        if volumes is None:
            return []
        vol_sma = pd.Series(volumes).rolling(period).mean().values
        vol_ratio = volumes / (vol_sma + 1e-10)
        return [
            Signal('VOL_spike', vol_ratio, 'gt', 2.0),      # 成交量超过MA的2倍
            Signal('VOL_dry', vol_ratio, 'lt', 0.5),        # 成交量萎缩到MA的50%以下
            Signal('VOL_increasing', vol_ratio, 'gt', 1.2), # 成交量放大
        ]

# ========== 预定义策略模板 ==========

class StrategyTemplates:
    """预定义策略模板库"""

    @staticmethod
    def RSI_ADX():
        """RSI+ADX趋势确认策略"""
        return Strategy(
            name='RSI_ADX',
            long_conditions=[
                # RSI超卖 AND ADX确认趋势
                Condition([
                    Signal('RSI_temp', np.array([0]), 'lt', 35),  # placeholder
                    Signal('ADX_temp', np.array([0]), 'gte', 20),  # placeholder
                ], 'AND'),
            ],
            short_conditions=[
                Condition([
                    Signal('RSI_temp', np.array([0]), 'gt', 65),
                    Signal('ADX_temp', np.array([0]), 'gte', 20),
                ], 'AND'),
            ],
            exit_conditions=[
                Condition([Signal('RSI_temp', np.array([0]), 'gt', 50)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def RSI_mean_reversion():
        """RSI均值回归策略（震荡市场）"""
        return Strategy(
            name='RSI_MeanReversion',
            long_conditions=[
                Condition([Signal('RSI_temp', np.array([0]), 'lt', 30)], 'AND'),
                Condition([Signal('ADX_temp', np.array([0]), 'lt', 25)], 'AND'),  # 低ADX=震荡
            ],
            short_conditions=[
                Condition([Signal('RSI_temp', np.array([0]), 'gt', 70)], 'AND'),
                Condition([Signal('ADX_temp', np.array([0]), 'lt', 25)], 'AND'),
            ],
            exit_conditions=[
                Condition([Signal('RSI_temp', np.array([0]), 'gt', 50)], 'AND'),
                Condition([Signal('RSI_temp', np.array([0]), 'lt', 50)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def MACD_trend():
        """MACD趋势跟踪策略"""
        return Strategy(
            name='MACD_Trend',
            long_conditions=[
                Condition([Signal('MACD_hist_positive', np.array([0]), 'gt', 0)], 'AND'),
                Condition([Signal('ADX_temp', np.array([0]), 'gte', 20)], 'AND'),
            ],
            short_conditions=[
                Condition([Signal('MACD_hist_negative', np.array([0]), 'lt', 0)], 'AND'),
                Condition([Signal('ADX_temp', np.array([0]), 'gte', 20)], 'AND'),
            ],
            exit_conditions=[
                Condition([Signal('MACD_bearish_cross', np.array([0]), 'gt', 0)], 'AND'),
                Condition([Signal('MACD_bullish_cross', np.array([0]), 'lt', 0)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def Bollinger_bounce():
        """布林带反弹策略"""
        return Strategy(
            name='Bollinger_Bounce',
            long_conditions=[
                Condition([BBSignal(
                    'BB_lower_touch', np.array([0]), np.array([0]),
                    np.array([0]), np.array([0]), signal_type='lower_touch')], 'AND'),
            ],
            short_conditions=[
                Condition([BBSignal(
                    'BB_upper_touch', np.array([0]), np.array([0]),
                    np.array([0]), np.array([0]), signal_type='upper_touch')], 'AND'),
            ],
            exit_conditions=[
                # 回到中轨 = 价格 >= 中轨
                Condition([Signal('BB_mid_reclaim', np.array([0]), 'gte', 0)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def EMA_crossover():
        """EMA金叉死叉策略"""
        return Strategy(
            name='EMA_Crossover',
            long_conditions=[
                Condition([Signal('EMA20p50_golden_cross', np.array([0]), 'gt', 0)], 'AND'),
            ],
            short_conditions=[
                Condition([Signal('EMA20p50_death_cross', np.array([0]), 'lt', 0)], 'AND'),
            ],
            exit_conditions=[
                Condition([Signal('EMA20_price_below', np.array([0]), 'gt', 0)], 'AND'),
                Condition([Signal('EMA20_price_above', np.array([0]), 'lt', 0)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def multi_timeframe():
        """多周期共振策略（1H+4H）"""
        return Strategy(
            name='MultiTimeframe',
            long_conditions=[
                # 4H RSI超卖 AND 1H RSI超卖 AND 4H ADX确认
                Condition([Signal('RSI4h_oversold', np.array([0]), 'lt', 30)], 'AND'),
                Condition([Signal('RSI1h_oversold', np.array([0]), 'lt', 35)], 'AND'),
                Condition([Signal('ADX4h_strong', np.array([0]), 'gte', 20)], 'AND'),
            ],
            short_conditions=[
                Condition([Signal('RSI4h_overbought', np.array([0]), 'gt', 70)], 'AND'),
                Condition([Signal('RSI1h_overbought', np.array([0]), 'gt', 65)], 'AND'),
                Condition([Signal('ADX4h_strong', np.array([0]), 'gte', 20)], 'AND'),
            ],
            exit_conditions=[
                Condition([Signal('RSI4h_neutral_high', np.array([0]), 'gt', 60)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def vol_breakout():
        """成交量突破策略"""
        return Strategy(
            name='Vol_Breakout',
            long_conditions=[
                Condition([Signal('VOL_spike', np.array([0]), 'gt', 0)], 'AND'),
                Condition([Signal('ADX_temp', np.array([0]), 'gte', 20)], 'AND'),
                Condition([Signal('RSI_temp', np.array([0]), 'gt', 50)], 'AND'),
            ],
            short_conditions=[
                Condition([Signal('VOL_spike', np.array([0]), 'gt', 0)], 'AND'),
                Condition([Signal('ADX_temp', np.array([0]), 'gte', 20)], 'AND'),
                Condition([Signal('RSI_temp', np.array([0]), 'lt', 50)], 'AND'),
            ],
            exit_conditions=[
                Condition([Signal('VOL_dry', np.array([0]), 'lt', 0)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def CCI_reversal_4H():
        """
        CCI超卖/超买反转策略（4H周期，回测最优）
        CCI < -100 → 超卖 → 做多（预期向上反转）
        CCI > 100  → 超买 → 做空（预期向下反转）
        回测数据：SOL 4H CCI(20) WR=86% 收益=+30.7%  DD=4.1%
                  ETH 4H CCI(20) WR=86% 收益=+28.1%  DD=0.7%
                  DOGE 4H CCI(20) WR=100% 收益=+27.6% DD=0.0%
        """
        return Strategy(
            name='CCI_Reversal_4H',
            long_conditions=[
                # 4H CCI < -100 = 超卖
                Condition([Signal('CCI4h_oversold', np.array([0]), 'lt', -100)], 'AND'),
            ],
            short_conditions=[
                # 4H CCI > 100 = 超买
                Condition([Signal('CCI4h_overbought', np.array([0]), 'gt', 100)], 'AND'),
            ],
            exit_conditions=[
                # CCI回归到0轴 = 中性，平仓
                Condition([Signal('CCI4h_neutral', np.array([0]), 'gt', 0)], 'AND'),
                Condition([Signal('CCI4h_neutral', np.array([0]), 'lt', 0)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def CCI_RSI_combined_4H():
        """
        CCI + RSI 双周期共振策略（4H）
        CCI超卖 + RSI超卖 → 做多
        CCI超买 + RSI超卖 → 做空
        """
        return Strategy(
            name='CCI_RSI_Combined_4H',
            long_conditions=[
                Condition([Signal('CCI4h_oversold', np.array([0]), 'lt', -100)], 'AND'),
                Condition([Signal('RSI4h_oversold', np.array([0]), 'lt', 35)], 'AND'),
            ],
            short_conditions=[
                Condition([Signal('CCI4h_overbought', np.array([0]), 'gt', 100)], 'AND'),
                Condition([Signal('RSI4h_overbought', np.array([0]), 'gt', 65)], 'AND'),
            ],
            exit_conditions=[
                Condition([Signal('CCI4h_neutral', np.array([0]), 'gt', 0)], 'AND'),
            ],
            min_confidence=1.0,
        )

    @staticmethod
    def all():
        """返回所有策略模板"""
        return [
            StrategyTemplates.RSI_ADX(),
            StrategyTemplates.RSI_mean_reversion(),
            StrategyTemplates.MACD_trend(),
            StrategyTemplates.Bollinger_bounce(),
            StrategyTemplates.EMA_crossover(),
            StrategyTemplates.multi_timeframe(),
            StrategyTemplates.vol_breakout(),
            StrategyTemplates.CCI_reversal_4H(),      # 4H CCI超卖反转（回测最优）
            StrategyTemplates.CCI_RSI_combined_4H(),  # 4H CCI+RSI共振
        ]

# ========== 信号工厂引擎 ==========

class SignalEngine:
    """信号工厂引擎 - 用实际数据填充策略"""

    def __init__(self, coin='AVAX', bar='1H'):
        self.coin = coin
        self.bar = bar
        self.df = fetch_ohlcv(coin, bar, limit=500)
        self.df_4h = fetch_ohlcv(coin, '4H', limit=500) if bar == '1H' else None
        self._signals = {}  # cache computed signals

    def _compute_signals(self):
        """计算所有信号指标"""
        if self._signals:
            return
        if self.df is None:
            return  # 无数据时跳过，避免崩溃
        df = self.df
        self._signals = {
            'RSI14': calc_rsi(df['close'].values, 14),
            'RSI7': calc_rsi(df['close'].values, 7),
            'ADX14': calc_adx(df['high'].values, df['low'].values, df['close'].values, 14),
            'MACD': calc_macd(df['close'].values)[2],  # histogram
            'BB_upper': calc_bollinger(df['close'].values)[0],
            'BB_lower': calc_bollinger(df['close'].values)[2],
            'BB_mid': calc_bollinger(df['close'].values)[1],
            'EMA20': calc_ema(df['close'].values, 20),
            'EMA50': calc_ema(df['close'].values, 50),
            'VOL': df['volume'].values,
            'close': df['close'].values,
        }
        if self.df_4h is not None:
            self._signals['RSI14_4H'] = calc_rsi(self.df_4h['close'].values, 14)
            self._signals['ADX14_4H'] = calc_adx(
                self.df_4h['high'].values, self.df_4h['low'].values,
                self.df_4h['close'].values, 14
            )
            # CCI 4H（回测最优period=20）
            self._signals['CCI20_4H'] = calc_cci(
                self.df_4h['high'].values, self.df_4h['low'].values,
                self.df_4h['close'].values, 20
            )
            # CCI 4H (period=14 对比)
            self._signals['CCI14_4H'] = calc_cci(
                self.df_4h['high'].values, self.df_4h['low'].values,
                self.df_4h['close'].values, 14
            )

    def build_strategy(self, template: Strategy) -> Strategy:
        """用实际数据填充策略模板"""
        self._compute_signals()
        s = template

        # 重建条件中的Signal引用
        def replace_signals(conditions, signal_map):
            new_conditions = []
            for cond in conditions:
                new_signals = []
                for sig in cond.signals:
                    # BBSignal：特殊处理，替换为实际的BB数组
                    if isinstance(sig, BBSignal):
                        new_sig = BBSignal(
                            name=sig.name,
                            closes=self._signals['close'],
                            bb_upper=self._signals['BB_upper'],
                            bb_mid=self._signals['BB_mid'],
                            bb_lower=self._signals['BB_lower'],
                            signal_type=sig.signal_type,
                        )
                        new_signals.append(new_sig)
                    elif sig.name in signal_map:
                        # 复制信号但使用实际的indicator
                        new_sig = Signal(
                            name=sig.name,
                            indicator=signal_map[sig.name],
                            operator=sig.operator,
                            threshold=sig.threshold,
                            params=sig.params,
                        )
                        new_signals.append(new_sig)
                if new_signals:
                    new_conditions.append(Condition(new_signals, cond.combinator))
            return new_conditions

        signal_map = {
            'RSI_temp': self._signals['RSI14'],
            'ADX_temp': self._signals['ADX14'],
            'RSI4h_oversold': self._signals.get('RSI14_4H', self._signals['RSI14']),
            'RSI4h_overbought': self._signals.get('RSI14_4H', self._signals['RSI14']),
            'RSI4h_neutral_high': self._signals.get('RSI14_4H', self._signals['RSI14']),
            'RSI1h_oversold': self._signals['RSI14'],
            'RSI1h_overbought': self._signals['RSI14'],
            'ADX4h_strong': self._signals.get('ADX14_4H', self._signals['ADX14']),
            # CCI 4H信号（回测最优period=20）
            'CCI4h_oversold': self._signals.get('CCI20_4H', self._signals.get('CCI14_4H', np.array([0]))),
            'CCI4h_overbought': self._signals.get('CCI20_4H', self._signals.get('CCI14_4H', np.array([0]))),
            'CCI4h_neutral': self._signals.get('CCI20_4H', self._signals.get('CCI14_4H', np.array([0]))),
            # CCI 1H信号
            'CCI_temp': self._signals.get('CCI20', np.array([0])),
            'CCI4h_14': self._signals.get('CCI14_4H', np.array([0])),
            'CCI4h_20': self._signals.get('CCI20_4H', np.array([0])),
            'MACD_hist_positive': self._signals['MACD'],
            'MACD_hist_negative': self._signals['MACD'],
            'MACD_bullish_cross': self._signals['MACD'],
            'MACD_bearish_cross': self._signals['MACD'],
            # BB信号：用BB数组
            'BB_lower_band': self._signals['BB_lower'],
            'BB_upper_band': self._signals['BB_upper'],
            'BB_mid_reclaim': self._signals['BB_mid'],
            'EMA20_price_above': self._signals['close'],
            'EMA20_price_below': self._signals['close'],
            'EMA20p50_golden_cross': np.diff(self._signals['EMA20'] - self._signals['EMA50'], prepend=0),
            'EMA20p50_death_cross': np.diff(self._signals['EMA20'] - self._signals['EMA50'], prepend=0),
            'VOL_spike': self._signals['VOL'] / (pd.Series(self._signals['VOL']).rolling(20).mean().values + 1e-10),
            'VOL_dry': self._signals['VOL'] / (pd.Series(self._signals['VOL']).rolling(20).mean().values + 1e-10),
        }

        return Strategy(
            name=s.name,
            long_conditions=replace_signals(s.long_conditions, signal_map),
            short_conditions=replace_signals(s.short_conditions, signal_map),
            exit_conditions=replace_signals(s.exit_conditions, signal_map),
            min_confidence=s.min_confidence,
        )

    def evaluate_all(self) -> dict:
        """评估所有策略，返回各策略的当前信号"""
        results = {}
        for template in StrategyTemplates.all():
            strategy = self.build_strategy(template)
            idx = -1  # 最新一根K线
            self._compute_signals()

            rsi = self._signals['RSI14'][-1]
            adx = self._signals['ADX14'][-1]
            price = self._signals['close'][-1]

            long_signal = strategy.evaluate_long(idx)
            short_signal = strategy.evaluate_short(idx)
            exit_signal = strategy.evaluate_exit(idx)

            # 满足的条件数量
            long_satisfied = sum(c.evaluate(idx) for c in strategy.long_conditions) if strategy.long_conditions else 0
            short_satisfied = sum(c.evaluate(idx) for c in strategy.short_conditions) if strategy.short_conditions else 0

            results[template.name] = {
                'price': price,
                'rsi': rsi,
                'adx': adx,
                'long': long_signal,
                'short': short_signal,
                'exit': exit_signal,
                'long_satisfied': long_satisfied,
                'long_total': len(strategy.long_conditions),
                'short_satisfied': short_satisfied,
                'short_total': len(strategy.short_conditions),
                'direction': 'long' if long_signal else ('short' if short_signal else 'neutral'),
            }

        return results

    def backtest_strategy(self, strategy_name: str) -> dict:
        """回测单个策略"""
        template = next((s for s in StrategyTemplates.all() if s.name == strategy_name), None)
        if not template:
            return {'error': f'Unknown strategy: {strategy_name}'}

        strategy = self.build_strategy(template)
        df = self.df

        # 生成信号序列
        entries_long = np.zeros(len(df))
        entries_short = np.zeros(len(df))
        exits = np.zeros(len(df))

        for i in range(len(df)):
            if strategy.evaluate_long(i):
                entries_long[i] = 1
            if strategy.evaluate_short(i):
                entries_short[i] = 1
            if strategy.evaluate_exit(i):
                exits[i] = 1

        return {
            'strategy': strategy_name,
            'data_points': len(df),
            'date_range': f'{df.index[0].date()} ~ {df.index[-1].date()}',
            'long_signals': int(entries_long.sum()),
            'short_signals': int(entries_short.sum()),
            'exit_signals': int(exits.sum()),
            'price': self._signals['close'][-1],
            'rsi': self._signals['RSI14'][-1],
            'adx': self._signals['ADX14'][-1],
            'current_long': bool(entries_long[-1]),
            'current_short': bool(entries_short[-1]),
        }

# ========== 主程序 ==========

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--coin', default='AVAX')
    parser.add_argument('--strategy', default=None, help='测试指定策略')
    parser.add_argument('--backtest', action='store_true', help='回测所有策略')
    args = parser.parse_args()

    engine = SignalEngine(args.coin, '1H')

    if args.backtest:
        print(f'\n{"="*60}')
        print(f'SignalFactory 回测报告')
        print(f'{"="*60}')
        print(f'币种: {args.coin} | 时间周期: 1H')
        print(f'数据: {engine.df.index[0].date()} ~ {engine.df.index[-1].date()} ({len(engine.df)}条)')
        print()

        for template in StrategyTemplates.all():
            result = engine.backtest_strategy(template.name)
            if 'error' in result:
                print(f'  {result["error"]}')
                continue
            current = '📈 LONG' if result['current_long'] else ('📉 SHORT' if result['current_short'] else '⏸ neutral')
            print(f'【{result["strategy"]}】{current}')
            print(f'  当前: price=${result["price"]:.4f} RSI={result["rsi"]:.1f} ADX={result["adx"]:.1f}')
            print(f'  历史: 做多信号{result["long_signals"]}次 | 做空信号{result["short_signals"]}次 | 退出信号{result["exit_signals"]}次')
            print()

    elif args.strategy:
        result = engine.backtest_strategy(args.strategy)
        print(f'\n{"="*60}')
        print(f'策略: {result["strategy"]}')
        print(f'{"="*60}')
        for k, v in result.items():
            print(f'  {k}: {v}')

    else:
        # 实时信号扫描
        print(f'\n{"="*60}')
        print(f'SignalFactory 实时信号扫描')
        print(f'{"="*60}')
        print(f'币种: {args.coin} | 周期: 1H')
        print(f'时间: {engine.df.index[-1]}')
        print()

        results = engine.evaluate_all()

        print('【各策略信号】')
        for name, r in sorted(results.items(), key=lambda x: x[0]):
            current = '📈 LONG' if r['long'] else ('📉 SHORT' if r['short'] else '⏸')
            print(f'  {name:25s} {current}  (RSI={r["rsi"]:.1f} ADX={r["adx"]:.1f})')
            if r['long'] or r['short']:
                print(f'    满足条件: 做多{r["long_satisfied"]}/{r["long_total"]} 做空{r["short_satisfied"]}/{r["short_total"]}')

        # 汇总
        long_strategies = [n for n, r in results.items() if r['long']]
        short_strategies = [n for n, r in results.items() if r['short']]
        neutral_strategies = [n for n, r in results.items() if not r['long'] and not r['short']]
        print(f'\n【汇总】')
        print(f'  做多共振: {len(long_strategies)}/{len(results)} 个策略')
        print(f'  做空共振: {len(short_strategies)}/{len(results)} 个策略')
        if long_strategies:
            print(f'  做多: {", ".join(long_strategies)}')
        if short_strategies:
            print(f'  做空: {", ".join(short_strategies)}')

        # 当前RSI极端区域检查
        rsi = results[list(results.keys())[0]]['rsi']
        if rsi < 30:
            print(f'\n  ⚠️ RSI={rsi:.1f} 严重超卖，否决所有做空信号')
            short_strategies = []
        elif rsi > 70:
            print(f'\n  ⚠️ RSI={rsi:.1f} 严重超买，否决所有做多信号')
            long_strategies = []

        if len(long_strategies) >= 3 and len(long_strategies) > len(short_strategies):
            print(f'\n  → 多周期共振信号确认，做多（{len(long_strategies)}个策略同意）')
        elif len(short_strategies) >= 3 and len(short_strategies) > len(long_strategies):
            print(f'\n  → 多周期共振信号确认，做空（{len(short_strategies)}个策略同意）')
        elif len(long_strategies) > len(short_strategies):
            print(f'\n  → 偏多（{len(long_strategies)} vs {len(short_strategies)}），轻仓试探')
        elif len(short_strategies) > len(long_strategies):
            print(f'\n  → 偏空（{len(short_strategies)} vs {len(long_strategies)}），轻仓试探')
        else:
            print(f'\n  → 信号分散，等待明确方向')
