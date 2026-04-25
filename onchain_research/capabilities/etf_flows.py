"""ETF 资金流系列假设因子

数据来源：
- Farside Investors (https://farsideworkers.com/)
- The Block
- SoSoValue

因子字段（由 feature_engine 从 ETF flow CSV 构建）：
- etf_flow_1d: 当日总净流入（美元）
- etf_flow_3d_sum: 过去3日累计净流入
- etf_flow_5d_ma: 5日移动平均净流入
- etf_flow_zscore_20d: 20日Z-Score
- etf_flow_consecutive_days: 连续净流入天数
- etf_flow_domination: IBIT占总净流入比例
"""
import numpy as np
import pandas as pd
from . import register, HypothesisOutput


@register('hyp_etf_flow_1d_positive', 'etf_flow', 'long', 0.6, impl='rule')
def etf_flow_1d_positive(f):
    """当日ETF总净流入 > 0：机构买入 → 短期看多"""
    flow = f['etf_flow_1d']
    trig = flow > 0
    score = np.where(flow > 1e8, 0.7, np.where(flow > 0, 0.4, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.6,
                          notes='ETF当日净流入 > 0')


@register('hyp_etf_flow_1d_negative', 'etf_flow', 'short', 0.6, impl='rule')
def etf_flow_1d_negative(f):
    """当日ETF总净流入 < 0：机构赎回 → 短期看空"""
    flow = f['etf_flow_1d']
    trig = flow < 0
    score = np.where(flow < -1e8, -0.7, np.where(flow < 0, -0.4, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='short', confidence=0.6,
                          notes='ETF当日净流入 < 0')


@register('hyp_etf_flow_3d_accumulative', 'etf_flow', 'long', 0.65, impl='rule')
def etf_flow_3d_accumulative(f):
    """3日累计净流入 > 0：机构持续买入 → 中期看多"""
    flow = f['etf_flow_3d_sum']
    trig = flow > 0
    score = np.where(flow > 3e8, 0.75, np.where(flow > 0, 0.5, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.65,
                          notes='3日累计净流入 > 0')


@register('hyp_etf_flow_5d_ma_positive', 'etf_flow', 'long', 0.55, impl='rule')
def etf_flow_5d_ma_positive(f):
    """5日MA净流入 > 0：持续买入趋势"""
    flow = f['etf_flow_5d_ma']
    trig = flow > 0
    score = np.where(flow > 5e7, 0.6, np.where(flow > 0, 0.35, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.55,
                          notes='5日MA净流入 > 0')


@register('hyp_etf_flow_zscore_extreme', 'etf_flow', 'neutral', 0.6, impl='rule')
def etf_flow_zscore_extreme(f):
    """Z-Score > 2 或 < -2：异常流入/流出，极端信号"""
    z = f['etf_flow_zscore_20d']
    bull = z > 2
    bear = z < -2
    trig = bull | bear
    score = np.where(bull, 0.6, np.where(bear, -0.6, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='neutral', confidence=0.6,
                          notes='Z-Score > 2 (bull) or < -2 (bear)')


@register('hyp_etf_flow_consecutive_3d', 'etf_flow', 'long', 0.65, impl='rule')
def etf_flow_consecutive_3d(f):
    """连续3日净流入：机构持续买入趋势"""
    days = f['etf_flow_consecutive_days']
    trig = days >= 3
    score = np.where(days >= 5, 0.75, np.where(days >= 3, 0.55, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.65,
                          notes='连续3日+净流入')


@register('hyp_etf_flow_large_outflow', 'etf_flow', 'short', 0.7, impl='rule')
def etf_flow_large_outflow(f):
    """单日净流出 > 5亿美元：机构大规模赎回 → 严重利空"""
    flow = f['etf_flow_1d']
    trig = flow < -5e8
    score = np.where(flow < -1e9, -0.9, np.where(flow < -5e8, -0.7, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='short', confidence=0.7,
                          notes='单日净流出 > 5亿美元')
