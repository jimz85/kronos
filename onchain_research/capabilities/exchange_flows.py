"""交易所资金流向系列假设因子

交易所余额变化 → 资金流入/流出
- 余额上升：持币者把钱放交易所 → 潜在抛压
- 余额下降：持币者提币 → 潜在上涨动力
"""
import numpy as np
import pandas as pd
from . import register, HypothesisOutput


@register('hyp_exchange_outflow_accel', 'onchain_signal', 'long', 0.65, impl='rule')
def exchange_outflow_accel(f):
    """交易所余额 7日变化 < -5%：资金加速流出交易所 → 潜在上涨动力"""
    chg = f['exchange_balance_chg_7d']  # 负值 = 流出
    trig = chg < -0.05
    score = np.where(chg < -0.10, 0.75, np.where(chg < -0.05, 0.55, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.65,
                            notes='交易所余额7日降>-5% → 资金流出')


@register('hyp_exchange_inflow_warning', 'onchain_signal', 'short', 0.65, impl='rule')
def exchange_inflow_warning(f):
    """交易所余额 7日变化 > +5%：资金加速流入交易所 → 潜在抛压"""
    chg = f['exchange_balance_chg_7d']  # 正值 = 流入
    trig = chg > 0.05
    score = np.where(chg > 0.10, -0.75, np.where(chg > 0.05, -0.55, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='short', confidence=0.65,
                            notes='交易所余额7日增>+5% → 资金流入警告')


@register('hyp_exchange_outflow_sustained', 'onchain_signal', 'long', 0.6, impl='rule')
def exchange_outflow_sustained(f):
    """交易所余额 30日持续下降：长期持币者锁仓 → 供血紧缩"""
    chg = f['exchange_balance_chg_30d']
    trig = chg < -0.15
    score = np.where(chg < -0.25, 0.7, np.where(chg < -0.15, 0.5, 0.0))
    return HypothesisOutput(triggered=trig, score=score, bias='long', confidence=0.6,
                            notes='交易所余额30日降>-15% → 持续流出')
