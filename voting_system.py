"""
Kronos 多因子投票系统 v1.0
============================
核心设计：
  - 每个因子独立投票（-2到+2分）
  - IC动态权重：近期IC越好权重越高
  - 一票否决机制：黑天鹅条件触发直接禁止交易
  - 因子去冗余：RSI和布林带共线性处理
  - Gemma作为异质因子参与投票（不重复计算技术指标）

使用方式：
  vs = VotingSystem(coin, direction='long' or 'short')
  decision = vs.vote()
  # decision = {
  #   'action': 'open'/'wait'/'veto',
  #   'direction': 'long'/'short'/'neutral',
  #   'vote_score': -2.0 ~ +2.0,   # 加权总分
  #   'vote_pct': 0~100,           # 置信度百分比
  #   'confidence': 'low'/'medium'/'high',
  #   'weight_detail': {...},
  #   'veto_triggered': None or str,
  #   'factor_votes': [...],
  #   'reason': str,
  # }
"""

import os, sys, json, time, math, requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from kronos_utils import atomic_write_json  # 原子写入（防断电损坏）
import numpy as np

# ========== 上下文Schema导入（与gemma4_hourly_review.py保持一致）==========
sys.path.insert(0, str(Path(__file__).parent))
try:
    from context_schema import CTX_FILE
    CTX_PATH = str(Path(__file__).parent / CTX_FILE)
except ImportError:
    # 降级：context_schema.py 不存在时使用内联版本
    CTX_FILE = "factor_context.json"
    CTX_PATH = "factor_context.json"

# ========== IC追踪器 ==========

class ICTracker:
    """
    滚动IC追踪器 - 每月更新一次因子权重
    使用Spearman秩相关系数，对异常值更鲁棒
    权重更新公式: W_new = 0.7*W_old + 0.3*IC_last_month
    IC为负的因子权重置0，不参与投票
    单因子最大权重30%
    """

    CACHE_FILE = os.path.expanduser('~/.hermes/kronos_ic_weights.json')
    WINDOW_DAYS = 90  # 3个月滚动窗口

    # 因子池（每个因子的历史IC数据）
    # 结构: {factor_name: [ {'month': '2026-03', 'ic': 0.08}, ... ]}
    _ic_history: Dict[str, List[Dict]] = {}
    _weights: Dict[str, float] = {}
    _last_update: Optional[str] = None

    def __init__(self):
        self._load_cache()

    def _load_cache(self):
        """从磁盘加载IC历史和权重"""
        if os.path.exists(self.CACHE_FILE):
            try:
                with open(self.CACHE_FILE) as f:
                    data = json.load(f)
                ICTracker._ic_history = data.get('ic_history', {})
                ICTracker._weights = data.get('weights', {})
                ICTracker._last_update = data.get('last_update')
            except:
                pass

    def _save_cache(self):
        """持久化到磁盘"""
        os.makedirs(os.path.dirname(self.CACHE_FILE), exist_ok=True)
        data = {
            'ic_history': ICTracker._ic_history,
            'weights': ICTracker._weights,
            'last_update': ICTracker._last_update,
        }
        with open(self.CACHE_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def spearman_ic(signal_values: np.ndarray, future_returns: np.ndarray) -> float:
        """
        计算Spearman秩相关系数（IC值）
        signal_values: 因子信号值序列（如RSI）
        future_returns: 未来收益率序列
        """
        if len(signal_values) < 30 or len(future_returns) < 30:
            return 0.0
        if len(signal_values) != len(future_returns):
            min_len = min(len(signal_values), len(future_returns))
            signal_values = signal_values[-min_len:]
            future_returns = future_returns[-min_len:]

        # 去除NaN
        mask = ~(np.isnan(signal_values) | np.isnan(future_returns))
        sv = signal_values[mask]
        fr = future_returns[mask]
        if len(sv) < 30:
            return 0.0

        # Spearman: 用rank替代原始值
        def rank(x):
            order = np.argsort(np.argsort(x))
            return order / (len(x) - 1) * 2 - 1  # 归一化到[-1, 1]

        rank_sig = rank(sv)
        rank_ret = rank(fr)
        corr = np.corrcoef(rank_sig, rank_ret)[0, 1]
        return float(corr) if not np.isnan(corr) else 0.0

    def record_ic(self, factor_name: str, ic_value: float):
        """
        记录某个因子本月的IC
        每月调用一次即可
        """
        month_key = datetime.now().strftime('%Y-%m')

        if factor_name not in ICTracker._ic_history:
            ICTracker._ic_history[factor_name] = []

        # 更新或追加本月IC
        existing = [i for i in ICTracker._ic_history[factor_name] if i['month'] == month_key]
        if existing:
            existing[0]['ic'] = ic_value
        else:
            ICTracker._ic_history[factor_name].append({'month': month_key, 'ic': ic_value})

        # 只保留最近WINDOW_DAYS天的历史
        cutoff = (datetime.now() - timedelta(days=self.WINDOW_DAYS)).strftime('%Y-%m')
        ICTracker._ic_history[factor_name] = [
            i for i in ICTracker._ic_history[factor_name] if i['month'] >= cutoff
        ]

    def compute_weights(self) -> Dict[str, float]:
        """
        根据IC历史计算动态权重
        W_new = 0.7*W_old + 0.3*IC_last_month（指数衰减加权）
        然后归一化：权重 = max(0, IC) / 所有正IC之和
        - BTC因子最大权重15%（其IC是beta相关性，非方向预测）
        - 其他因子最大权重20%
        - IC为负的因子权重置0
        """
        weights = {}

        # 第一步：计算每个因子的指数衰减加权IC
        for factor, history in ICTracker._ic_history.items():
            if not history:
                weights[factor] = 0.0
                continue

            # 取最近几个月的IC，用指数衰减计算加权平均
            recent_ics = [h['ic'] for h in history[-6:]]  # 最多6个月
            old_weight = ICTracker._weights.get(factor, 0.1)
            latest_ic = recent_ics[-1]

            # 指数衰减加权平均（更重视近期）
            decay_weights = [0.7 ** i for i in range(len(recent_ics))]
            decay_weights = [w / sum(decay_weights) for w in decay_weights]
            weighted_ic = sum(ic * w for ic, w in zip(recent_ics, decay_weights))

            # 新权重 = 0.7*旧权重 + 0.3*最新IC（平滑）
            new_ic_weight = 0.7 * old_weight + 0.3 * max(0, latest_ic)
            weights[factor] = new_ic_weight

        # 第二步：IC为负的因子权重置0
        for factor, history in ICTracker._ic_history.items():
            if history and history[-1]['ic'] < 0:
                weights[factor] = 0.0

        # 第三步：归一化
        positive_weights = {k: v for k, v in weights.items() if v > 0}
        total = sum(positive_weights.values())
        if total > 0:
            for k in weights:
                weights[k] /= total

        # 第四步：应用权重上限（B T C 15%，其他 20%）
        # 正确逻辑：先cap所有因子，再把释放的空间按IC比例分配给未达cap的因子，
        #           然后只对"未达cap且可吸收释放量"的因子做局部归一化
        btc_cap = 0.15
        other_cap = 0.20

        # 记录每个因子cap前的值
        pre_cap = dict(weights)

        # 应用所有cap
        for k in weights:
            cap = btc_cap if k == 'BTC' else other_cap
            weights[k] = min(weights[k], cap)

        # 计算释放量
        excess = sum(pre_cap[k] - weights[k] for k in weights)

        # 找出可以吸收释放量的因子：cap前值 < cap值（说明它没被cap）
        # 注意：用cap前的值判断，这样能吸收的量 = (cap - 实际weight)
        absorbable = {k: (other_cap if k != 'BTC' else btc_cap) - pre_cap[k]
                      for k in weights if pre_cap[k] < (btc_cap if k == 'BTC' else other_cap)}
        absorbable_total = sum(absorbable.values())

        # 按可吸收量比例分配释放量
        if absorbable_total > 0 and excess > 0:
            for k in absorbable:
                boost = excess * (absorbable[k] / absorbable_total)
                # 不能超过cap
                weights[k] = min(weights[k] + boost,
                                btc_cap if k == 'BTC' else other_cap)

        ICTracker._weights = weights
        ICTracker._last_update = datetime.now().strftime('%Y-%m-%d %H:%M')
        self._save_cache()
        return weights

    def get_weight(self, factor_name: str) -> float:
        """获取某因子的当前权重"""
        return ICTracker._weights.get(factor_name, 0.0)

    def get_all_weights(self) -> Dict[str, float]:
        return dict(ICTracker._weights)

    def apply_minimax_adjustment(self):
        """读取MiniMax战略审查给出的IC权重调整，应用到当前权重。

        名称映射：MiniMax输出小写(rsi/adx/vol_ratio/sentiment)，
        IC系统用PascalCase(RSI/ADX/Vol)，自动对齐。
        新因子：sentiment/fundFlow加入IC系统（初始化1%权重）。
        ADX IC为负时跳过提高（反向指标不应复活）。
        """
        import json as _json
        adj_path = Path.home() / '.hermes/kronos_ic_weights_adjustment.json'
        if not adj_path.exists():
            return

        try:
            adj_data = _json.loads(adj_path.read_text())
            adj = adj_data.get('adjustment', {})
            if not adj:
                return

            # 读取当前权重
            ic_file = Path(ICTracker.CACHE_FILE)
            if ic_file.exists():
                ic_data = _json.loads(ic_file.read_text())
            else:
                ic_data = {'weights': dict(ICTracker._weights)}

            weights = ic_data.get('weights', {})

            # MiniMax因子名 → IC系统因子名 映射
            NAME_MAP = {
                'rsi': 'RSI', 'rsi_1h': 'RSI', 'rsi_4h': 'RSI',
                'adx': 'ADX',
                'vol': 'Vol', 'vol_ratio': 'Vol',
                'bollinger': 'Bollinger', 'bb': 'Bollinger',
                'macd': 'MACD',
                'btc': 'BTC',
                'gemma': 'Gemma',
                'sentiment': 'Sentiment',
                'flow': 'FundFlow',
            }

            def _normalize(name: str) -> str:
                n = name.lower().strip()
                return NAME_MAP.get(n, name)

            # 统一权重key名（小写 → PascalCase）
            keys_to_add = {}
            keys_to_remove = []
            for f in list(weights.keys()):
                norm = _normalize(f)
                if norm != f:
                    keys_to_add[norm] = weights[f]
                    keys_to_remove.append(f)
            for k in keys_to_remove:
                del weights[k]
            weights.update(keys_to_add)

            # 读取factor_context获取IC值（判断因子质量）
            # 使用CTX_PATH（与gemma4_hourly_review.py一致）
            factor_ic = {}
            ctx_path = Path(CTX_PATH)
            if ctx_path.exists():
                try:
                    ctx = _json.loads(ctx_path.read_text())
                    for k, v in ctx.get('factor_status', {}).items():
                        factor_ic[k.upper()] = v.get('ic', 0)
                        # vol_ratio → VOL
                        if 'ratio' in k.lower():
                            factor_ic[k.upper().replace('RATIO', '')] = v.get('ic', 0)
                except Exception:
                    pass

            raise_factors = adj.get('raise', [])
            lower_factors = adj.get('lower', [])
            boost = adj.get('boost', 0.1)
            reduce = adj.get('reduce', 0.1)

            applied_raises = []
            applied_lowers = []
            new_factors = []
            skipped = []

            # 提高
            for f in raise_factors:
                norm = _normalize(f)
                # 找实际key（尝试多种形式）
                key = None
                for k in [norm, f.lower(), f]:
                    if k in weights:
                        key = k
                        break

                if key:
                    # ADX权重0且IC为负 → 反向指标，不复活
                    if key == 'ADX' and weights.get(key, 0) == 0:
                        ic_val = factor_ic.get('ADX', 0)
                        if ic_val < 0:
                            skipped.append(f'{f}(ADX) IC={ic_val:.3f}<0 反向指标')
                            continue
                    old = weights[key]
                    weights[key] = min(0.5, weights[key] * (1 + boost))
                    applied_raises.append(f'{f}→{key}:{old*100:.1f}%→{weights[key]*100:.1f}%')
                elif norm in ('Sentiment', 'FundFlow'):
                    weights[norm] = min(0.05, 0.01 * (1 + boost))
                    new_factors.append(f'{norm}:{weights[norm]*100:.2f}%')
                else:
                    skipped.append(f'{f}(→{norm}不存在)')

            # 降低
            for f in lower_factors:
                norm = _normalize(f)
                key = None
                for k in [norm, f.lower(), f]:
                    if k in weights:
                        key = k
                        break

                if key:
                    old = weights[key]
                    weights[key] = max(0.01, weights[key] * (1 - reduce))
                    applied_lowers.append(f'{f}→{key}:{old*100:.1f}%→{weights[key]*100:.1f}%')
                elif norm in ('Sentiment', 'FundFlow'):
                    weights[norm] = 0.01

            # 归一化
            total = sum(weights.values())
            if total > 0:
                for k in weights:
                    weights[k] /= total

            # P1-3 Fix: BTC和Gemma上限，防止单一因子主导投票
            # BTC上限15%（其IC是beta相关性，非方向预测），Gemma上限20%
            btc_cap = 0.15
            gemma_cap = 0.20
            btc_old = weights.get('BTC', 0)
            gemma_old = weights.get('Gemma', 0)
            weights['BTC'] = min(btc_old, btc_cap)
            weights['Gemma'] = min(gemma_old, gemma_cap)
            # 超额部分重新分配给技术因子
            btc_excess = btc_old - weights['BTC']
            gemma_excess = gemma_old - weights['Gemma']
            total_excess = btc_excess + gemma_excess
            tech_factors = [k for k in weights if k not in ('BTC', 'Gemma')]
            if tech_factors and total_excess > 0:
                each_add = total_excess / len(tech_factors)
                for k in tech_factors:
                    weights[k] += each_add
                # 再次归一化
                total = sum(weights.values())
                if total > 0:
                    for k in weights:
                        weights[k] /= total

            ICTracker._weights = weights

            # 保存
            ic_data['weights'] = weights
            ic_data['last_update'] = datetime.now().isoformat()
            ic_data['adjusted_by'] = 'MiniMax战略审查'
            atomic_write_json(ic_file, ic_data, indent=2)

            print(f"[MiniMax权重调整] raise:{applied_raises} new:{new_factors}")
            if applied_lowers:
                print(f"  lower:{applied_lowers}")
            if skipped:
                print(f"  跳过:{skipped}")
            print(f"  结果:{dict(sorted(weights.items(), key=lambda x:-x[1])[:6])}")

        except Exception as e:
            print(f"[ICTracker] MiniMax权重调整失败: {e}")



# ========== 因子基类 ==========

class FactorVoter:
    """因子投票基类"""

    name: str = "base"
    max_vote: float = 2.0  # 最大投票分（绝对值）

    def __init__(self, coin: str, market_data: dict, direction: str):
        self.coin = coin
        self.md = market_data
        self.direction = direction  # 'long' or 'short'

    def vote(self) -> Dict:
        """
        返回: {
            'factor': str,
            'raw_vote': float,      # -max_vote ~ +max_vote
            'vote': float,           # 归一化投票（-2 ~ +2）
            'reason': str,
            'ic': float,            # 该因子当前IC
            'weight': float,        # 该因子当前权重
        }
        """
        raise NotImplementedError


# ========== 因子实现 ==========

class RSVoter(FactorVoter):
    """
    RSI因子投票
    做多：RSI<30=+2, <35=+1.5, <40=+1, 40-60=0, >70=-2
    做空：RSI>70=+2, >65=+1.5, >60=+1, 40-60=0, <30=-2
    IC基准：0.08
    """
    name = "RSI"

    def vote(self) -> Dict:
        rsi = self.md.get('rsi_1h', 50)
        if rsi is None:
            rsi = 50

        ic_tracker = ICTracker()
        ic = ic_tracker.get_weight('RSI') or 0.08  # 无IC数据时用默认值

        if self.direction == 'long':
            if rsi < 25: raw = 2.0
            elif rsi < 30: raw = 1.8
            elif rsi < 35: raw = 1.5
            elif rsi < 40: raw = 1.0
            elif rsi < 55: raw = 0.0
            elif rsi < 65: raw = -0.5
            elif rsi < 70: raw = -1.0
            else: raw = -2.0
            reason = f'RSI={rsi}'
        else:  # short
            if rsi > 75: raw = 2.0
            elif rsi > 70: raw = 1.8
            elif rsi > 65: raw = 1.5
            elif rsi > 60: raw = 1.0
            elif rsi > 45: raw = 0.0
            elif rsi > 35: raw = -0.5
            else: raw = -2.0
            reason = f'RSI={rsi}'

        return {
            'factor': 'RSI',
            'raw_vote': raw,
            'vote': raw,  # 已经是-2~+2
            'reason': reason,
            'ic': ic,
            'weight': ic_tracker.get_weight('RSI'),
        }


class ADXVoter(FactorVoter):
    """
    ADX因子投票
    ADX>25=有趋势=+1.5, >30=强趋势=+2, <20=无趋势=-1
    IC基准：0.05
    """
    name = "ADX"

    def vote(self) -> Dict:
        adx = self.md.get('adx_1h', 20)
        if adx is None:
            adx = 20

        ic_tracker = ICTracker()
        ic = ic_tracker.get_weight('ADX') or 0.05

        if adx > 35: raw = 2.0
        elif adx > 30: raw = 1.8
        elif adx > 25: raw = 1.5
        elif adx > 20: raw = 0.8
        elif adx > 15: raw = 0.0
        else: raw = -1.0

        return {
            'factor': 'ADX',
            'raw_vote': raw,
            'vote': raw,
            'reason': f'ADX={adx}',
            'ic': ic,
            'weight': ic_tracker.get_weight('ADX'),
        }


class BollingerVoter(FactorVoter):
    """
    布林带因子投票（与RSI去冗余后独立）
    价格触及下轨=做多信号，价格触及上轨=做空信号
    使用ATR标准化布林带宽度
    IC基准：0.06
    """
    name = "Bollinger"

    def vote(self) -> Dict:
        price = self.md.get('price', 0)
        if not price:
            return {'factor': 'Bollinger', 'raw_vote': 0, 'vote': 0,
                    'reason': '无价格数据', 'ic': 0, 'weight': 0}

        # 从market_data获取布林带数据（如果可用）
        bb_lower = self.md.get('bb_lower')
        bb_upper = self.md.get('bb_upper')
        bb_mid = self.md.get('bb_mid')

        ic_tracker = ICTracker()
        ic = ic_tracker.get_weight('Bollinger') or 0.06

        # 如果没有预计算的布林带，用简化计算
        if bb_lower is None or bb_upper is None:
            atr = self.md.get('atr', price * 0.02)
            bb_mid = price  # 简化：用现价当布林中轨
            bb_lower = price - 2 * atr
            bb_upper = price + 2 * atr

        band_width = (bb_upper - bb_lower) / (bb_mid + 1e-10)

        if self.direction == 'long':
            # 价格接近下轨（5%以内）= 做多信号
            dist_to_lower = (price - bb_lower) / (bb_mid - bb_lower + 1e-10)
            if dist_to_lower < 0.05: raw = 2.0  # 触及下轨
            elif dist_to_lower < 0.15: raw = 1.5
            elif dist_to_lower < 0.30: raw = 1.0
            elif dist_to_lower > 0.80: raw = -1.0  # 接近上轨，不宜做多
            else: raw = 0.0
        else:  # short
            dist_to_upper = (bb_upper - price) / (bb_upper - bb_mid + 1e-10)
            if dist_to_upper < 0.05: raw = 2.0  # 触及上轨
            elif dist_to_upper < 0.15: raw = 1.5
            elif dist_to_upper < 0.30: raw = 1.0
            elif dist_to_upper > 0.80: raw = -1.0
            else: raw = 0.0

        return {
            'factor': 'Bollinger',
            'raw_vote': raw,
            'vote': raw,
            'reason': f'BB宽度={band_width:.2%}',
            'ic': ic,
            'weight': ic_tracker.get_weight('Bollinger'),
        }


class VolVoter(FactorVoter):
    """
    成交量因子投票
    放量突破=确认趋势，放量跌破=确认趋势
    IC基准：0.07（DOGE/ADA表现好）
    """
    name = "Vol"

    def vote(self) -> Dict:
        vol_ratio = self.md.get('vol_ratio', 1.0)
        if vol_ratio is None:
            vol_ratio = 1.0

        ic_tracker = ICTracker()
        ic = ic_tracker.get_weight('Vol') or 0.07

        if vol_ratio >= 2.5: raw = 2.0
        elif vol_ratio >= 2.0: raw = 1.8
        elif vol_ratio >= 1.5: raw = 1.2
        elif vol_ratio >= 1.2: raw = 0.8
        elif vol_ratio >= 0.8: raw = 0.0
        else: raw = -0.5  # 缩量，不利

        return {
            'factor': 'Vol',
            'raw_vote': raw,
            'vote': raw,
            'reason': f'Vol比率={vol_ratio}',
            'ic': ic,
            'weight': ic_tracker.get_weight('Vol'),
        }


class BTCVoter(FactorVoter):
    """
    BTC方向因子投票（市场环境过滤器）
    不直接参与多空投票，而是调整其他因子的有效性
    这里做环境过滤器：BTC方向与持仓方向冲突时扣分
    IC基准：0.04
    """
    name = "BTC"

    def vote(self) -> Dict:
        btc_dir = self.md.get('btc_direction', 'neutral')
        btc_regime = self.md.get('btc_regime', 'neutral')
        ic_tracker = ICTracker()
        ic = ic_tracker.get_weight('BTC') or 0.04

        if self.direction == 'long':
            if btc_regime == 'bear' or btc_dir == 'overbought':
                # 市场过热/熊市 → 降低做多权重
                raw = -1.5
            elif btc_regime == 'bull' or btc_dir == 'oversold':
                raw = 1.0  # 配合BTC做多
            elif btc_dir == 'neutral':
                raw = 0.3
            else:
                raw = 0.0
        else:  # short
            if btc_regime == 'bull' or btc_dir == 'oversold':
                raw = -1.5  # 不逆BTC牛市做空
            elif btc_regime == 'bear' or btc_dir == 'overbought':
                raw = 1.0  # 配合BTC熊市做空
            elif btc_dir == 'neutral':
                raw = 0.3
            else:
                raw = 0.0

        return {
            'factor': 'BTC',
            'raw_vote': raw,
            'vote': raw,
            'reason': f'BTC={btc_dir}/{btc_regime}',
            'ic': ic,
            'weight': ic_tracker.get_weight('BTC'),
        }


class MACDVoter(FactorVoter):
    """
    MACD因子投票
    MACD柱转正=做多信号，转负=做空信号
    IC基准：0.05
    """
    name = "MACD"

    def vote(self) -> Dict:
        macd_hist = self.md.get('macd_hist', 0)
        if macd_hist is None:
            macd_hist = 0

        ic_tracker = ICTracker()
        ic = ic_tracker.get_weight('MACD') or 0.05

        # MACD柱状图强度（归一化到±2）
        normalized = max(-2, min(2, macd_hist / 50.0))  # 假设50为典型最大柱

        if self.direction == 'long':
            raw = normalized if normalized > 0 else normalized * 0.5
        else:
            raw = -normalized if normalized < 0 else -normalized * 0.5

        return {
            'factor': 'MACD',
            'raw_vote': raw,
            'vote': raw,
            'reason': f'MACD={macd_hist:.2f}',
            'ic': ic,
            'weight': ic_tracker.get_weight('MACD'),
        }


class GemmaVoter(FactorVoter):
    """
    Gemma4-Heretic因子投票（异质因子，不重复计算技术指标）
    负责解读：宏观情绪、链上异动、叙事判断

    输出格式（强制结构化）：
    方向: [强烈做空/做空/中性/做多/强烈做多]
    置信度: [0-100的整数]
    理由: [一句话]

    映射规则：
    强烈做多 + 置信度80% → 原始分 = 2 × 0.8 = 1.6
    做多 + 置信度50%     → 原始分 = 1 × 0.5 = 0.5
    中性/不确定          → 0分（弃权）
    """
    name = "Gemma"

    # Gemma作为因子的基准IC（需要通过实际使用后记录来更新）
    BASE_IC = 0.10

    PROMPT_TEMPLATE = """你是一个专业的加密货币交易因子分析师。
你专注于解读市场语境、宏观情绪和链上异动——而不是重复计算技术指标。

当前市场数据（由系统提供，你不能质疑）：
- 币种: {coin}
- 方向评估: {direction}
- 1h RSI: {rsi}
- ADX: {adx}
- 成交量比率: {vol_ratio}
- BTC方向: {btc_dir}

请回答以下三个判断题（只能回答 Yes 或 No，不能有其他内容）：

1. 从宏观叙事角度看，当前市场环境支持{direction}方向吗？
   （考虑：市场情绪、资金流向、宏观事件）

2. 从价格结构角度看，当前价格是否存在明显的吸筹或派发结构？
   （不考虑RSI，只看价格行为和成交量）

3. 未来72小时内是否有重大风险事件可能逆转当前趋势？
   （如：美联储决议、重大项目公告、关税升级等）

回答格式（严格遵守）：
方向: [强烈做空/做空/中性/做多/强烈做多]
置信度: [0-100的整数]
理由: [一句话，不超过15字]
"""

    def vote(self) -> Dict:
        ic_tracker = ICTracker()
        base_ic = ic_tracker.get_weight('Gemma') or self.BASE_IC

        # 构建Prompt
        prompt = self.PROMPT_TEMPLATE.format(
            coin=self.coin,
            direction=self.direction,
            rsi=self.md.get('rsi_1h', 'N/A'),
            adx=self.md.get('adx_1h', 'N/A'),
            vol_ratio=self.md.get('vol_ratio', 'N/A'),
            btc_dir=self.md.get('btc_direction', 'N/A'),
        )

        # 调用Ollama gemma4-heretic
        raw_response = self._call_ollama(prompt)

        # 解析响应
        direction_str, confidence, reason = self._parse_response(raw_response)

        # 基础分数映射
        dir_to_base = {
            '强烈做多': 2.0,
            '做多': 1.0,
            '中性': 0.0,
            '做空': -1.0,
            '强烈做空': -2.0,
        }
        base_score = dir_to_base.get(direction_str, 0.0)

        # 置信度调整
        conf_factor = confidence / 100.0
        raw_vote = base_score * conf_factor

        return {
            'factor': 'Gemma',
            'raw_vote': raw_vote,
            'vote': raw_vote,
            'reason': reason,
            'confidence': confidence,
            'raw_response': raw_response[:200] if raw_response else 'N/A',
            'ic': base_ic,
            'weight': ic_tracker.get_weight('Gemma'),
        }

    def _call_ollama(self, prompt: str, timeout: int = 30) -> str:
        """调用MiniMax API进行审查"""
        try:
            import requests
            api_key = os.getenv('MINIMAX_API_KEY', '')
            base_url = os.getenv('MINIMAX_BASE_URL', 'https://api.minimaxi.com/v1')
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            }
            data = {
                'model': 'MiniMax-M2.7',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.3,
                'max_tokens': 2500,
            }
            r = requests.post(f'{base_url}/text/chatcompletion_v2', headers=headers, json=data, timeout=(5, timeout))
            result = r.json()
            if 'choices' in result:
                return result['choices'][0]['message']['content']
            return f"ERROR: {result.get('msg', 'unknown')}"
        except Exception as e:
            return f"ERROR: {str(e)[:100]}"

    def _parse_response(self, response: str) -> Tuple[str, int, str]:
        """解析Gemma输出"""
        direction = '中性'
        confidence = 50
        reason = '解析失败'

        for line in response.split('\n'):
            line = line.strip()
            if line.startswith('方向:'):
                for d in ['强烈做多', '做多', '中性', '做空', '强烈做空']:
                    if d in line:
                        direction = d
                        break
            elif line.startswith('置信度:'):
                try:
                    confidence = int(''.join(filter(str.isdigit, line)))
                    confidence = max(0, min(100, confidence))
                except:
                    confidence = 50
            elif line.startswith('理由:'):
                reason = line.replace('理由:', '').strip()

        return direction, confidence, reason


# ========== 一票否决检查 ==========

class VetoChecker:
    """
    一票否决机制 - 任何一项触发，直接禁止交易
    不参与投票，直接拦截
    """

    def __init__(self, coin: str, md: dict, positions: dict, equity: float):
        self.coin = coin
        self.md = md
        self.positions = positions
        self.equity = equity

    def check(self) -> Tuple[bool, Optional[str]]:
        """
        返回 (is_vetoed, veto_reason)
        """
        # 1. 大盘（BTC）20日均线向下 → 做多否决
        btc_ma20_down = self.md.get('btc_ma20_trend', 'neutral')
        if btc_ma20_down == 'down' and self._is_long_candidate():
            return True, f'BTC_MA20向下，做多否决'

        # 2. 该币24h波动率超过历史90分位
        vol_pct = self.md.get('atr_pct', 2.0)
        if vol_pct > 8.0:  # ATR百分比>8%为异常波动
            return True, f'波动率异常高({vol_pct:.1f}%)'

        # 3. 永续合约资金费率极端
        funding_rate = self.md.get('funding_rate', 0.0)
        if abs(funding_rate) > 0.001:  # >0.1%
            return True, f'资金费率极端({funding_rate*100:.2f}%)'

        # 4. 重大事件日历（硬编码已知高风险窗口）
        # 规则：美联储FOMC会议前后24h，不建新仓
        # 通过时间简单判断（实际应查日历）
        now = datetime.now()
        # 简化：周末低流动性警告
        if now.weekday() >= 5:  # 周六日
            pass  # 周末不否决，但提示

        # 5. 单日累计亏损>1%
        daily_loss_pct = self.md.get('daily_loss_pct', 0.0)
        if daily_loss_pct > 1.0:
            return True, f'今日亏损{daily_loss_pct:.1f}%>1%'

        # 6. 仓位已满
        if self._count_positions() >= 3:
            return True, '仓位已满(3/3)'

        # 7. 权益不足（低于保留金）
        reserve = self.equity * 0.10
        if self.equity < reserve:
            return True, f'权益${self.equity:.0f}<保留金${reserve:.0f}'

        return False, None

    def _is_long_candidate(self) -> bool:
        """检查当前是否在评估做多方向"""
        return True  # 简化，实际应传入direction参数

    def _count_positions(self) -> int:
        return len([p for p in self.positions.values() if p.get('pos', 0) > 0])


# ========== 因子去冗余处理 ==========

def apply_factor_orthogonalization(factor_votes: List[Dict]) -> List[Dict]:
    """
    因子去冗余：RSI和布林带高度共线，同时投票时总分-0.5票
    原理：价格暴跌时RSI必<30且布林必打下轨，两者同时+分是重复计算

    处理规则：
    - RSI和Bollinger同时投票同方向 → 总分×0.9（去重10%）
    - ADX和MACD高度相关 → 总分×0.95
    """
    if len(factor_votes) < 2:
        return factor_votes

    # 找出共线因子对
    rsi_vote = next((v for v in factor_votes if v['factor'] == 'RSI'), None)
    bb_vote = next((v for v in factor_votes if v['factor'] == 'Bollinger'), None)

    adjustment_applied = False

    # RSI + Bollinger 同时强烈投票同方向（都是+1.5以上或都是-1.5以上）
    if rsi_vote and bb_vote:
        rsi_sign = 1 if rsi_vote['vote'] > 0 else (-1 if rsi_vote['vote'] < 0 else 0)
        bb_sign = 1 if bb_vote['vote'] > 0 else (-1 if bb_vote['vote'] < 0 else 0)

        if rsi_sign != 0 and rsi_sign == bb_sign:
            # 同向投票且都显著 → 重复计算，标记
            adjustment_applied = True
            # 找到权重更低的因子，降低其权重
            if abs(rsi_vote['vote']) >= abs(bb_vote['vote']):
                # BB权重降低50%（因为和RSI重复）
                for v in factor_votes:
                    if v['factor'] == 'Bollinger':
                        v['_orthogonalized'] = True
                        v['vote'] *= 0.6  # 降低权重
                        v['reason'] += ' [BB~RSI去重]'
            else:
                for v in factor_votes:
                    if v['factor'] == 'RSI':
                        v['_orthogonalized'] = True
                        v['vote'] *= 0.6
                        v['reason'] += ' [RSI~BB去重]'

    return factor_votes


# ========== 投票系统核心 ==========

class VotingSystem:
    """
    多因子投票系统

    使用方法：
        vs = VotingSystem('AVAX', md=market_data, direction='long')
        decision = vs.vote()
    """

    # 因子列表（按优先级）
    FACTOR_CLASSES = [
        RSVoter,
        ADXVoter,
        BollingerVoter,
        VolVoter,
        MACDVoter,
        BTCVoter,
        GemmaVoter,
    ]

    def __init__(
        self,
        coin: str,
        market_data: dict,
        direction: str,
        positions: Optional[dict] = None,
        equity: Optional[float] = None,
    ):
        self.coin = coin
        self.md = market_data
        self.direction = direction  # 'long' or 'short'
        self.positions = positions or {}
        self.equity = float(equity) if equity is not None else 100000.0

    def vote(self) -> Dict:
        """
        执行完整投票流程
        """
        # Step 1: 一票否决检查
        veto = VetoChecker(self.coin, self.md, self.positions, self.equity)
        is_vetoed, veto_reason = veto.check()
        if is_vetoed:
            return {
                'action': 'veto',
                'direction': 'neutral',
                'vote_score': 0.0,
                'vote_pct': 0,
                'confidence': 'none',
                'weight_detail': {},
                'veto_triggered': veto_reason,
                'factor_votes': [],
                'reason': veto_reason,
            }

        # Step 2: 各因子投票
        ic_tracker = ICTracker()
        factor_votes = []

        for FactorClass in self.FACTOR_CLASSES:
            try:
                factor = FactorClass(self.coin, self.md, self.direction)
                result = factor.vote()
                factor_votes.append(result)
            except Exception as e:
                # 单个因子失败不影响整体
                factor_votes.append({
                    'factor': FactorClass.name if hasattr(FactorClass, 'name') else 'Unknown',
                    'raw_vote': 0, 'vote': 0,
                    'reason': f'计算失败: {str(e)[:30]}',
                    'ic': 0, 'weight': 0,
                })

        # Step 3: 因子去冗余
        factor_votes = apply_factor_orthogonalization(factor_votes)

        # Step 4: 计算加权投票分
        weights = ic_tracker.get_all_weights()
        total_weight = sum(weights.values())

        if total_weight == 0:
            # 无权重数据，使用等权
            total_weight = 1.0
            use_default = True
        else:
            use_default = False

        weighted_sum = 0.0
        weight_detail = {}

        # 有效投票 = 原始投票 × 因子权重
        # 由于权重归一化和=1，归一化因子 = 1（即不需要额外乘数）
        for fv in factor_votes:
            factor_name = fv['factor']
            w = weights.get(factor_name, 0.0)
            effective_vote = fv['vote'] * w  # 已经是-2~+2 × 权重
            fv['effective_vote'] = effective_vote
            fv['applied_weight'] = w
            weighted_sum += effective_vote
            weight_detail[factor_name] = {
                'weight': round(w, 4),
                'vote': round(fv['vote'], 2),
                'effective': round(effective_vote, 3),
            }

        # vote_score范围：所有因子权重和=1时，最大±2.0
        vote_score = max(-2.0, min(2.0, weighted_sum))

        # 置信度百分比
        # vote_score范围±2，对应0-100%
        vote_pct = abs(vote_score) / 2.0 * 100

        if vote_pct >= 70:
            confidence = 'high'
        elif vote_pct >= 50:
            confidence = 'medium'
        elif vote_pct >= 30:
            confidence = 'low'
        else:
            confidence = 'none'

        # 方向：如果vote_score>0是long，<0是short
        if abs(vote_score) < 0.3:
            final_direction = 'neutral'
        else:
            final_direction = self.direction

        # 汇总原因
        reasons = [fv['reason'] for fv in factor_votes if abs(fv['vote']) >= 0.5]
        reason_str = '; '.join(reasons[:4]) if reasons else '无明显信号'

        return {
            'action': 'open' if confidence != 'none' else 'wait',
            'direction': final_direction,
            'vote_score': round(vote_score, 3),
            'vote_pct': round(vote_pct, 1),
            'confidence': confidence,
            'weight_detail': weight_detail,
            'total_weight_sum': round(total_weight, 3),
            'veto_triggered': None,
            'factor_votes': factor_votes,
            'reason': reason_str,
            # 仓位建议
            'position_size': self._suggest_position_size(vote_pct),
        }

    def _suggest_position_size(self, vote_pct: float) -> str:
        """根据置信度建议仓位"""
        if vote_pct >= 70:
            return '100%仓位（满仓）'
        elif vote_pct >= 60:
            return '50%仓位'
        elif vote_pct >= 50:
            return '20%仓位（轻仓试探）'
        else:
            return '不建仓'


# ========== 便利函数 ==========

def evaluate_coin(coin: str, md: dict, positions: dict, equity: float) -> Dict:
    """
    对单个币种做完整的多空评估
    返回：
    {
        'long': VotingSystem.vote()结果,
        'short': VotingSystem.vote()结果,
        'best_direction': 'long'/'short'/'neutral',
        'best_score': float,
    }
    """
    long_system = VotingSystem(coin, md, 'long', positions, equity)
    short_system = VotingSystem(coin, md, 'short', positions, equity)

    long_result = long_system.vote()
    short_result = short_system.vote()

    # 选择高分方向
    if long_result['vote_pct'] > short_result['vote_pct']:
        best = 'long'
        best_score = long_result['vote_pct']
    elif short_result['vote_pct'] > long_result['vote_pct']:
        best = 'short'
        best_score = short_result['vote_pct']
    else:
        best = 'neutral'
        best_score = long_result['vote_pct']

    return {
        'coin': coin,
        'long': long_result,
        'short': short_result,
        'best_direction': best,
        'best_score': best_score,
    }


# ========== IC批量回测计算工具 ==========
# 用于每日/每周批量计算所有因子的IC（后台任务）

def compute_factor_ic_batch(coin: str, bar: str = '1H', lookback: int = 500) -> Dict[str, float]:
    """
    批量计算某币种所有因子的IC
    用于Walk-Forward回测

    返回: {factor_name: ic_value}
    """
    try:
        import pandas as pd
        from scipy.stats import spearmanr

        # 获取数据
        df = _fetch_ohlcv_for_ic(coin, bar, limit=lookback)
        if df is None or len(df) < 100:
            return {}

        closes = df['close'].values.astype(float)
        highs = df['high'].values.astype(float)
        lows = df['low'].values.astype(float)
        volumes = df['volume'].values.astype(float)

        # 计算未来收益率（forward 1-period return），与价格序列对齐
        future_return = np.diff(closes)  # n-1 个收益（价格差，非百分比）
        # 因变量序列长度 = n-1，所以所有自变量也要截断到n-1
        n = len(future_return)  # = len(closes) - 1

        def safe_spearman(signal: np.ndarray, target: np.ndarray) -> float:
            """安全的Spearman IC计算"""
            s = signal[-n:]  # 对齐到n
            t = target
            mask = ~(np.isnan(s) | np.isnan(t) | np.isinf(s) | np.isinf(t))
            if mask.sum() < 30:
                return 0.0
            s, t = s[mask], t[mask]
            corr, _ = spearmanr(s, t)
            return float(corr) if not np.isnan(corr) else 0.0

        results = {}

        # RSI IC（最近n个点）
        rsi_vals = _calc_rsi_series(closes, 14)
        results['RSI'] = safe_spearman(rsi_vals, future_return)

        # ADX IC
        adx_vals = _calc_adx_series(highs, lows, closes, 14)
        results['ADX'] = safe_spearman(adx_vals, future_return)

        # Bollinger IC（价格与布林带位置）
        bb_lower, bb_mid, bb_upper = _calc_bb_series(closes)
        bb_pos = (closes - bb_lower) / (bb_upper - bb_lower + 1e-10)
        results['Bollinger'] = safe_spearman(bb_pos, future_return)

        # Vol IC（成交量比率）
        s_vol = pd.Series(volumes)
        vol_ma20 = s_vol.rolling(20, min_periods=1).mean().values
        vol_ratio = volumes / (vol_ma20 + 1e-10)
        results['Vol'] = safe_spearman(vol_ratio, future_return)

        # MACD IC
        macd_hist = _calc_macd_series(closes)
        results['MACD'] = safe_spearman(macd_hist, future_return)

        # BTC IC（用BTC收益率作为市场代理）
        btc_df = _fetch_ohlcv_for_ic('BTC', bar, limit=lookback)
        if btc_df is not None and len(btc_df) >= 2:
            btc_return = np.diff(btc_df['close'].values.astype(float))
            btc_return = btc_return[-n:]  # 对齐
            coin_return = future_return / (closes[-n-1:-1] + 1e-10)  # 百分比收益率
            results['BTC'] = safe_spearman(btc_return, coin_return)
        else:
            results['BTC'] = 0.0

        # Gemma：默认IC 0.10（需要通过历史评估记录计算）
        results['Gemma'] = 0.10

        return results

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {}


def _fetch_ohlcv_for_ic(coin: str, bar: str, limit: int):
    """获取数据用于IC计算"""
    try:
        import pandas as pd
        sys.path.insert(0, str(Path(__file__).parent))
        from kronos_multi_coin import get_ohlcv
        data = get_ohlcv(coin, bar, limit)
        if not data:
            return None
        df = pd.DataFrame(data)
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['volume'] = df['volume'].astype(float)
        return df
    except:
        return None


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """计算Spearman相关系数"""
    mask = ~(np.isnan(x) | np.isnan(y))
    if mask.sum() < 30:
        return 0.0
    x, y = x[mask], y[mask]
    corr, _ = spearmanr(x, y)
    return float(corr) if not np.isnan(corr) else 0.0


def _calc_rsi_series(prices: np.ndarray, period: int = 14) -> np.ndarray:
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gains = np.convolve(gains, np.ones(period) / period, mode='same')
    avg_losses = np.convolve(losses, np.ones(period) / period, mode='same')
    rs = avg_gains / (avg_losses + 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _calc_adx_series(high, low, close, period: int = 14) -> np.ndarray:
    """简化ADX计算"""
    tr = np.maximum(high - low, np.maximum(
        np.abs(high - np.roll(close, 1)),
        np.abs(low - np.roll(close, 1))
    ))
    plus_dm = np.maximum(high - np.roll(high, 1), 0)
    minus_dm = np.maximum(np.roll(low, 1) - low, 0)
    plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0)
    minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0)

    atr = np.convolve(tr, np.ones(period) / period, mode='same')
    plus_di = 100 * np.convolve(plus_dm, np.ones(period) / period, mode='same') / (atr + 1e-10)
    minus_di = 100 * np.convolve(minus_dm, np.ones(period) / period, mode='same') / (atr + 1e-10)

    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx = np.convolve(dx, np.ones(period) / period, mode='same')
    return adx


def _calc_bb_series(prices: np.ndarray, period: int = 20) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    import pandas as pd
    s = pd.Series(prices)
    mid = s.rolling(period).mean().values
    std = s.rolling(period).std().values
    upper = mid + 2 * std
    lower = mid - 2 * std
    return lower, mid, upper


def _calc_macd_series(prices: np.ndarray) -> np.ndarray:
    import pandas as pd
    s = pd.Series(prices)
    ema12 = s.ewm(span=12).mean()
    ema26 = s.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    hist = macd - signal
    return hist.values


if __name__ == '__main__':
    # 简单测试
    print("=== 多因子投票系统自检 ===")
    print("VotingSystem 类已加载")
    print(f"IC权重缓存文件: {ICTracker.CACHE_FILE}")

    # 测试权重加载
    tracker = ICTracker()
    weights = tracker.get_all_weights()
    print(f"当前因子权重: {weights}")

    print("=== 自检完成 ===")
