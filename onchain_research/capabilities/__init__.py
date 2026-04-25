"""链上因子能力注册中心

复刻 crypto-kol-quant 的 CAP_REGISTRY 模式。

每个假设/因子是一个函数：
    def my_hypothesis(feat: pd.Series | pd.DataFrame) -> HypothesisOutput

Return signature:
    {
      'triggered': bool or Series[bool],
      'score': float or Series[float],  # -1..+1, sign = direction, magnitude = strength
      'bias': 'long'|'short'|'neutral'|None,
      'confidence': 0..1,
      'notes': str,
    }
"""
from dataclasses import dataclass
from typing import Callable, Any
import pandas as pd
import numpy as np

CAP_REGISTRY: dict[str, dict] = {}

@dataclass
class HypothesisOutput:
    triggered: Any      # bool or Series[bool]
    score: Any          # float or Series, signed -1..+1
    bias: Any = None    # 'long'|'short'|'neutral'|None
    confidence: Any = 0.5
    notes: str = ''

def register(hyp_id: str, hyp_type: str, bias_default: str = 'neutral',
             confidence_base: float = 0.5, impl: str = 'rule', na_reason: str = ''):
    """装饰器：注册一个链上假设因子"""
    def wrap(fn):
        CAP_REGISTRY[hyp_id] = {
            'id': hyp_id,
            'type': hyp_type,
            'bias_default': bias_default,
            'confidence_base': confidence_base,
            'impl': impl,
            'na_reason': na_reason,
            'fn': fn,
            'name': fn.__name__,
        }
        return fn
    return wrap

def evaluate_all(feat):
    """运行所有注册的假设因子，返回分数面板和触发面板。"""
    scores = {}
    triggered = {}
    for cid, meta in CAP_REGISTRY.items():
        try:
            out = meta['fn'](feat)
            if isinstance(out, HypothesisOutput):
                scores[cid] = out.score
                triggered[cid] = out.triggered
            elif isinstance(out, dict):
                scores[cid] = out.get('score', 0)
                triggered[cid] = out.get('triggered', False)
            else:
                scores[cid] = out
                triggered[cid] = out != 0 if hasattr(out, '__iter__') else bool(out)
        except Exception as e:
            scores[cid] = np.nan if isinstance(feat, pd.DataFrame) else 0
            triggered[cid] = False
    return scores, triggered


# 导入子模块，触发因子注册
from . import mvrv
from . import sopr
from . import exchange_flows
from . import etf_flows
