"""SOPR 系列假设因子

SOPR (Spent Output Profit Ratio) = 已花费输出的实现价值 / 成本
- SOPR > 1：整体盈利已实现（持有者赚钱）
- SOPR < 1：整体亏损已实现（持有者亏钱）
- SOPR 极高值 > 1.1：大量利润实现 → 潜在顶部
- SOPR 极低值 < 0.9：大量亏损实现 → 潜在底部
"""
import numpy as np
import pandas as pd
from . import register, HypothesisOutput


@register('hyp_sopr_extreme_low', 'onchain_signal', 'long', 0.7, impl='rule')
def sopr_extreme_low(f):
    """SOPR < 0.9：大量亏损实现，市场超卖 → 买入信号"""
    sopr = f['sopr']
    trig = sopr < 0.9
    score = np.where(sopr < 0.8, 0.8, np.where(sopr < 0.9, 0.6, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.7,
                            notes='SOPR < 0.9 → 大量亏损实现，超卖')


@register('hyp_sopr_extreme_high', 'onchain_signal', 'short', 0.7, impl='rule')
def sopr_extreme_high(f):
    """SOPR > 1.1：大量利润实现，市场过热 → 卖出信号"""
    sopr = f['sopr']
    trig = sopr > 1.1
    score = np.where(sopr > 1.2, -0.8, np.where(sopr > 1.1, -0.6, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='short', confidence=0.7,
                            notes='SOPR > 1.1 → 大量利润实现，过热')


@register('hyp_sopr_rising_from_low', 'onchain_signal', 'long', 0.6, impl='rule')
def sopr_rising_from_low(f):
    """SOPR 从极端低位反弹：SOPR从<0.9升至>1.0，表明卖压衰竭"""
    sopr = f['sopr']
    sopr_prev = f.get('sopr_prev', sopr.shift(1))
    trig = (sopr_prev < 0.9) & (sopr > 1.0)
    score = np.where(trig, 0.65, 0.0)
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.6,
                            notes='SOPR从<0.9反弹至>1.0 → 卖压衰竭')


@register('hyp_sopr_falling_from_high', 'onchain_signal', 'short', 0.6, impl='rule')
def sopr_falling_from_high(f):
    """SOPR 从极端高位回落：SOPR从>1.2跌至<1.0，表明利润收割"""
    sopr = f['sopr']
    sopr_prev = f.get('sopr_prev', sopr.shift(1))
    trig = (sopr_prev > 1.2) & (sopr < 1.0)
    score = np.where(trig, -0.65, 0.0)
    return HypothesisOutput(triggered=trig, score=score, bias='short', confidence=0.6,
                            notes='SOPR从>1.2回落至<1.0 → 利润收割')
