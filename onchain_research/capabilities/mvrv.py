"""MVRV 系列假设因子

来源：链上数据 / glassnode / 自建UTXO计算
"""
import numpy as np
import pandas as pd
from . import register, HypothesisOutput

# MVRV Z-Score 阈值（经验值）
Z_EXTREME_HIGH = 3.5   # 极度高估 → 禁止开仓
Z_HIGH         = 1.5   # 正常偏高 → 减少开仓
Z_LOW          = 0.0   # 低估 → 积极买入
Z_VERY_LOW     = -1.0  # 极度低估 → 强烈买入信号


@register('hyp_mvrv_zscore_undervalued', 'onchain_signal', 'long', 0.7, impl='rule')
def mvrv_z_undervalued(f):
    """MVRV Z-Score < 0：市场低估，历史对应底部区域 → 做多"""
    z = f['mvrv_zscore']
    trig = z < 0
    score = np.where(z < -1.0, 0.8, np.where(z < 0, 0.6, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.7,
                            notes='MVRV Z-Score < 0 → 低估区域')


@register('hyp_mvrv_zscore_allow_open', 'onchain_signal', 'neutral', 0.6, impl='rule')
def mvrv_z_allow_open(f):
    """MVRV Z-Score < 1.5：允许开仓（作为滤波器叠加到其他策略）"""
    z = f['mvrv_zscore']
    trig = (z < 1.5) & (z > -1.0)
    score = np.where(z < 0, 0.5, np.where(z < 1.5, 0.3, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='neutral', confidence=0.6,
                            notes='MVRV Z-Score < 1.5 → 允许开仓')


@register('hyp_mvrv_zscore_extreme_high', 'onchain_signal', 'short', 0.8, impl='rule')
def mvrv_z_extreme_high(f):
    """MVRV Z-Score > 3.5：极度高估 → 禁止开仓/做空"""
    z = f['mvrv_zscore']
    trig = z > 3.5
    score = np.where(z > 3.5, -0.9, np.where(z > 3.0, -0.6, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='short', confidence=0.8,
                            notes='MVRV Z-Score > 3.5 → 极度高估')


@register('hyp_mvrv_zscore_mean_reversion', 'onchain_signal', 'neutral', 0.65, impl='rule')
def mvrv_z_mean_reversion(f):
    """MVRV Z-Score 从极值回归：当 Z-Score 从 >3 回落至 <2.5 时卖出"""
    z = f['mvrv_zscore']
    z_prev = f.get('mvrv_zscore_prev', z.shift(1))
    trig = (z_prev > 3.0) & (z < 2.5)
    score = np.where(trig, -0.7, 0.0)
    return HypothesisOutput(triggered=trig, score=score, bias='short', confidence=0.65,
                            notes='Z-Score 从>3回落至<2.5 → 高估派发')


@register('hyp_mvrv_ratio_above_3', 'onchain_signal', 'short', 0.75, impl='rule')
def mvrv_ratio_above_3(f):
    """MVRV比率 > 3：市场极度泡沫化"""
    r = f['mvrv_ratio']
    trig = r > 3.0
    score = np.where(r > 3.5, -0.85, np.where(r > 3.0, -0.7, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='short', confidence=0.75,
                            notes='MVRV比率 > 3 → 泡沫化')


@register('hyp_mvrv_zscore_hist_percentile', 'onchain_signal', 'neutral', 0.6, impl='rule')
def mvrv_z_hist_percentile(f):
    """MVRV Z-Score 历史分位数 < 10%：历史低位区间"""
    z = f['mvrv_zscore']
    pct = f.get('mvrv_zscore_pctile', pd.Series(np.nan, index=z.index))
    trig = pct < 0.1
    score = np.where(pct < 0.05, 0.7, np.where(pct < 0.1, 0.5, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.6,
                            notes='Z-Score历史分位数 < 10% → 相对底部')
