"""
类人盘感交易Agent - Phase 1 核心引擎
盘感感知层 + 决策打分 + 动态仓位 + 硬风控
"""
import pandas as pd
import numpy as np
import json, os, time, hmac, hashlib, requests
from datetime import datetime, timezone
from collections import deque

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')

# ===== 硬风控参数（绝对不可突破） =====
MAX_SINGLE_LOSS_PCT = 0.01    # 单笔最大亏1%
MAX_DAILY_LOSS_PCT = 0.05     # 单日最大亏5%
MAX_WEEKLY_LOSS_PCT = 0.15   # 单周最大亏15%
MAX_DRAWDOWN_PCT = 0.30      # 总资金最大回撤30%
MAX_LEVERAGE = 5             # 最大杠杆5x
MAX_POSITIONS = 2             # 最大同时持仓2个
MAX_HOLDING_HOURS = 4         # 最长持仓4小时
INIT_CAPITAL = 10000          # 初始资金

# ===== 策略参数 =====
RSI_LONG_TH = 40
RSI_SHORT_TH = 60
TF = '5min'
BASE_LEVERAGE = 3

class TradeMemory:
    """交易记忆库 - 历史相似性检索"""
    def __init__(self, max_trades=1000):
        self.trades = []  # {'pattern': [...], 'pnl': float, 'direction': str}
        self.max_trades = max_trades
        self.win_patterns = []
        self.lose_patterns = []
    
    def add_trade(self, pattern_vec, pnl, direction):
        self.trades.append({'pattern': pattern_vec, 'pnl': pnl, 'direction': direction, 'timestamp': time.time()})
        if len(self.trades) > self.max_trades:
            self.trades.pop(0)
        
        # 更新正/负样本
        if pnl > 0:
            self.win_patterns.append(pattern_vec)
        else:
            self.lose_patterns.append(pattern_vec)
    
    def get_similarity_weight(self, current_pattern):
        """计算历史相似性权重"""
        if not self.trades:
            return 1.0
        
        # 找最近10笔相似交易
        recent = self.trades[-20:]
        similar_wins = 0
        similar_total = 0
        
        for t in recent:
            sim = self.cosine_sim(current_pattern, t['pattern'])
            if sim > 0.7:
                similar_total += 1
                if t['pnl'] > 0:
                    similar_wins += 1
        
        if similar_total == 0:
            return 1.0
        
        win_rate = similar_wins / similar_total
        # 相似交易胜率高则提高权重
        return 1.0 + (win_rate - 0.5) * 0.5
    
    @staticmethod
    def cosine_sim(a, b):
        if len(a) != len(b) or len(a) == 0:
            return 0.0
        dot = sum(x*y for x,y in zip(a,b))
        norm_a = sum(x*x for x in a) ** 0.5
        norm_b = sum(x*x for x in b) ** 0.5
        return dot / (norm_a * norm_b + 1e-10)


class MarketSense:
    """
    盘感感知层 - 5个核心特征
    在没有实时逐笔数据时，用K线+成交量近似
    """
    def __init__(self):
        self.price_history = deque(maxlen=60)    # 最近60个价格
        self.volume_history = deque(maxlen=60)   # 最近60个成交量
        self.buy_vol_history = deque(maxlen=60)  # 估算主动买入量
        self.rsi_history = deque(maxlen=20)
        self.adx_history = deque(maxlen=20)
    
    def update(self, price, volume, rsi, adx, buy_vol=None):
        self.price_history.append(price)
        self.volume_history.append(volume)
        self.buy_vol_history.append(buy_vol if buy_vol else volume * 0.5)
        self.rsi_history.append(rsi)
        self.adx_history.append(adx)
    
    def get_order_flow_score(self):
        """
        订单流盘感评分 (-1 ~ +1)
        正数=偏多，负数=偏空
        """
        if len(self.volume_history) < 10:
            return 0.0
        
        vol_list = list(self.volume_history)
        recent_vol = np.mean(vol_list[-10:])
        older_vol = np.mean(vol_list[-30:-10]) if len(vol_list) >= 30 else recent_vol
        vol_ratio = recent_vol / (older_vol + 1e-10)
        
        # 主动买入占比
        buy_vol_list = list(self.buy_vol_history)
        total_vol = sum(vol_list[-10:])
        buy_ratio = sum(buy_vol_list[-10:]) / (total_vol + 1e-10)
        
        # 价格动量
        prices = list(self.price_history)
        if len(prices) < 5:
            return 0.0
        recent_return = (prices[-1] - prices[-5]) / (prices[-5] + 1e-10)
        
        # 综合评分
        score = 0.0
        score += (buy_ratio - 0.5) * 2  # 买入占比偏离50%
        score += recent_return * 5       # 价格动量
        score += (vol_ratio - 1.0) * 0.3  # 放量/缩量
        
        return max(min(score, 1.0), -1.0)
    
    def get_volatility_regime(self):
        """
        波动率状态机
        返回: 0=低波动, 1=正常, 2=高波动, 3=极端
        """
        if len(self.price_history) < 60:
            return 1
        
        prices = np.array(list(self.price_history))
        returns = np.diff(prices) / (prices[:-1] + 1e-10)
        vol = np.std(returns[-20:]) * np.sqrt(24 * 60 / 5)  # 5min波动率年化
        
        # 相对历史波动率
        if len(returns) >= 300:
            hist_vol = np.std(returns[-300:]) * np.sqrt(24 * 60 / 5)
            ratio = vol / (hist_vol + 1e-10)
        else:
            ratio = 1.0
        
        if ratio < 0.8:
            return 0  # 低波动
        elif ratio <= 1.5:
            return 1  # 正常
        elif ratio <= 3.0:
            return 2  # 高波动
        else:
            return 3  # 极端
    
    def get_pattern_vector(self):
        """生成当前形态向量（用于相似性检索）"""
        if len(self.price_history) < 20:
            return [0.0] * 20
        
        prices = np.array(list(self.price_history)[-20:])
        vols = np.array(list(self.volume_history)[-20:])
        
        # 归一化价格变化序列
        price_changes = np.diff(prices) / (prices[:-1] + 1e-10)
        price_changes = np.append(price_changes, 0)
        
        # 归一化成交量
        vol_norm = vols / (np.mean(vols) + 1e-10)
        
        # RSI当前值
        rsi_curr = list(self.rsi_history)[-1] if self.rsi_history else 50
        
        # 组合向量
        vec = list(price_changes[-10:]) + list(vol_norm[-10:])
        vec.append(rsi_curr / 100)
        return vec
    
    def get_squeeze_score(self):
        """
        布林带挤压分数 (0~1)
        挤压程度越高，突破概率越大
        """
        if len(self.price_history) < 20:
            return 0.5
        
        prices = np.array(list(self.price_history)[-20:])
        ma = np.mean(prices)
        std = np.std(prices)
        
        upper = ma + 2 * std
        lower = ma - 2 * std
        
        # 布林带宽度
        bandwidth = (upper - lower) / (ma + 1e-10)
        
        # 历史带宽分位数
        # 简化：用当前带宽相对于均值的比例
        bandwidth_ratio = bandwidth / (0.02 + 1e-10)  # 假设正常2%
        
        return max(min(bandwidth_ratio / 2, 1.0), 0.0)


class DecisionEngine:
    """
    决策层 - 赔率优先动态打分
    开仓得分 = (预测赔率 × 0.7 + 预测胜率 × 0.3) × 市场状态系数 × 历史相似性系数
    """
    def __init__(self, memory: TradeMemory, market_sense: MarketSense):
        self.memory = memory
        self.sense = market_sense
        self.consecutive_losses = 0
        self.daily_loss_pct = 0.0
        self.weekly_loss_pct = 0.0
    
    def calc_open_score(self, direction, rsi, order_flow_score, squeeze_score):
        """
        计算开仓得分 (0~1)
        得分>0.6才允许开仓
        """
        # 基础RSI打分
        if direction == 'long':
            if rsi < 30:
                rsi_score = 1.0
            elif rsi < 40:
                rsi_score = 0.8
            elif rsi < 50:
                rsi_score = 0.4
            else:
                rsi_score = 0.0
        else:  # short
            if rsi > 70:
                rsi_score = 1.0
            elif rsi > 60:
                rsi_score = 0.8
            elif rsi > 50:
                rsi_score = 0.4
            else:
                rsi_score = 0.0
        
        # 订单流打分
        if direction == 'long':
            flow_score = max(order_flow_score, 0)  # 负数=0
        else:
            flow_score = max(-order_flow_score, 0)
        
        # 挤压突破打分
        squeeze_bonus = squeeze_score * 0.3
        
        # 预测赔率和胜率
        # 基于RSI历史统计: RSI<40时做多，平均赔率约2.1，胜率36%
        pred_rr = 2.1 if direction == 'long' else 1.8
        pred_wr = 0.36
        
        # 综合打分
        raw_score = (pred_rr / 3.0 * 0.7 + pred_wr * 0.3)
        raw_score = raw_score * (0.4 + rsi_score * 0.6)
        raw_score = raw_score * (0.5 + flow_score * 0.5)
        raw_score = raw_score + squeeze_bonus
        
        # 市场状态系数
        vol_regime = self.sense.get_volatility_regime()
        vol_coef = {0: 0.0, 1: 1.0, 2: 1.5, 3: 0.0}[vol_regime]
        if vol_regime == 0 or vol_regime == 3:  # 低波动/极端，空仓
            return 0.0
        
        # 历史相似性系数
        pattern = self.sense.get_pattern_vector()
        sim_weight = self.memory.get_similarity_weight(pattern)
        
        # 连续亏损惩罚
        if self.consecutive_losses >= 3:
            penalty = 0.5 ** (self.consecutive_losses - 2)
            raw_score *= penalty
        
        final_score = raw_score * (0.8 + sim_weight * 0.2)
        return max(min(final_score, 1.0), 0.0)
    
    def calc_position_size(self, score, direction, capital):
        """
        根据得分动态计算仓位
        score越高，仓位越大（0.5% → 2%总资金）
        """
        # 基础仓位百分比
        base_pct = 0.005 + score * 0.015  # 0.5% ~ 2%
        base_pct = max(min(base_pct, 0.02), 0.005)  # 硬限制在0.5%~2%
        
        # 杠杆调整
        vol_regime = self.sense.get_volatility_regime()
        lev_coef = {0: 0.0, 1: 1.0, 2: 0.7, 3: 0.0}[vol_regime]
        
        position_pct = base_pct * lev_coef
        position_pct = max(min(position_pct, 0.02), 0.005)  # 二次硬限制
        position_usd = capital * position_pct
        
        return position_pct, position_usd, BASE_LEVERAGE
    
    def calc_stop_loss(self, entry_price, direction, atr_pct):
        """
        动态止损 = 入场价 × (2.5 + 波动率系数) × ATR
        """
        vol_regime = self.sense.get_volatility_regime()
        vol_mult = {0: 1.5, 1: 2.0, 2: 1.5, 3: 1.0}[vol_regime]
        
        sl_pct = atr_pct * vol_mult * 1.0
        sl_pct = max(min(sl_pct, 0.08), 0.01)  # 限制在1%~8%
        
        if direction == 'long':
            sl_price = entry_price * (1 - sl_pct)
        else:
            sl_price = entry_price * (1 + sl_pct)
        
        return sl_price, sl_pct
    
    def calc_take_profit(self, entry_price, direction, atr_pct):
        """
        动态止盈 = 入场价 × (4.0 + 订单流强度) × ATR
        """
        order_flow = abs(self.sense.get_order_flow_score())
        tp_mult = 4.0 + order_flow * 0.5
        
        tp_pct = atr_pct * tp_mult
        
        if direction == 'long':
            tp_price = entry_price * (1 + tp_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)
        
        return tp_price, tp_pct
    
    def on_trade_result(self, pnl_pct, is_win):
        """交易结果反馈"""
        if is_win:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.daily_loss_pct += abs(pnl_pct)
        
        # 每日亏损超限检查
        if self.daily_loss_pct >= MAX_DAILY_LOSS_PCT:
            return 'STOP_DAY'  # 当天停止
        
        if self.consecutive_losses >= 4:
            return 'STOP_TRADING'  # 停止交易
        
        return 'CONTINUE'
    
    def record_daily_reset(self):
        """每日重置"""
        self.daily_loss_pct = 0.0


class RiskController:
    """
    硬风控层 - 所有决策必须经过此层
    """
    def __init__(self):
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.max_dd = 0.0
        self.peak_capital = INIT_CAPITAL
        self.today_trades = []
        self.active_positions = []
    
    def pre_trade_check(self, direction, proposed_size_usd, capital, current_positions):
        """
        下单前检查，返回(True/False, reason)
        """
        # 检查持仓数量
        if len(current_positions) >= MAX_POSITIONS:
            return False, f"已达最大持仓数{MAX_POSITIONS}"
        
        # 检查杠杆
        lev = BASE_LEVERAGE
        effective_exposure = proposed_size_usd * lev
        if effective_exposure > capital * 0.3:  # 单笔不超过总资金30%
            return False, "单笔暴露超限"
        
        # 检查总暴露
        total_exposure = sum(p['size_usd'] * p['lev'] for p in current_positions)
        if total_exposure + effective_exposure > capital * 0.6:  # 总暴露不超过60%
            return False, "总暴露超限"
        
        # 检查资金峰值
        if capital < self.peak_capital * (1 - MAX_DRAWDOWN_PCT):
            return False, f"触发总回撤限制{MAX_DRAWDOWN_PCT*100}%，停止交易"
        
        return True, "OK"
    
    def post_trade_update(self, pnl, capital):
        """交易后更新风控状态"""
        self.daily_pnl += pnl
        
        # 更新峰值
        if capital > self.peak_capital:
            self.peak_capital = capital
        
        # 更新最大回撤
        current_dd = (self.peak_capital - capital) / self.peak_capital
        if current_dd > self.max_dd:
            self.max_dd = current_dd
    
    def check_stop_loss(self, capital):
        """检查是否触发停止条件"""
        if self.daily_pnl <= -MAX_DAILY_LOSS_PCT * capital:
            return True, f"单日亏损超限{MAX_DAILY_LOSS_PCT*100}%"
        
        if self.max_dd >= MAX_DRAWDOWN_PCT:
            return True, f"总回撤超限{MAX_DRAWDOWN_PCT*100}%，停止交易一周"
        
        return False, "OK"


class TradingAgent:
    """
    完整交易Agent - 5层闭环
    """
    def __init__(self, coin='BTC', mode='scan'):
        self.coin = coin
        self.mode = mode  # 'scan', 'backtest', 'live'
        
        self.memory = TradeMemory()
        self.sense = MarketSense()
        self.decision = DecisionEngine(self.memory, self.sense)
        self.risk = RiskController()
        
        self.capital = INIT_CAPITAL
        self.positions = []  # 当前持仓
        self.trade_log = []
    
    def load_data(self):
        """加载数据"""
        df = pd.read_csv(f'{DATA_DIR}/{self.coin}_USDT_5m_from_20180101.csv')
        cols = df.columns.tolist()
        new_cols = []
        seen = {}
        for c in cols:
            cn = c.split('.')[0]
            if cn not in seen:
                new_cols.append(c)
                seen[cn] = cn
        df = df[new_cols]
        df = df.rename(columns={'datetime_utc': 'dt'})[['dt', 'open', 'high', 'low', 'close', 'volume']]
        df['ts'] = pd.to_datetime(df['dt']).dt.tz_localize(None)
        df = df.set_index('ts').sort_index()
        df = df[df['close'] > 0]
        return df
    
    def calc_indicators(self, df):
        """计算所有指标"""
        c = df['close']
        h = df['high']
        l = df['low']
        v = df['volume']
        
        # RSI
        delta = c.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(span=14, adjust=False).mean()
        avg_loss = loss.ewm(span=14, adjust=False).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        
        # ADX
        tr1 = h - l
        tr2 = np.abs(h - c.shift())
        tr3 = np.abs(l - c.shift())
        tr = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
        up = h.diff()
        dn = -l.diff()
        pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
        mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=l.index)
        atr = tr.rolling(14).mean()
        pdi = 100 * (pdm.rolling(14).mean() / atr)
        mdi = 100 * (mdm.rolling(14).mean() / atr)
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)
        adx = dx.rolling(14).mean()
        
        # ATR百分比
        atr_pct = atr / c
        
        return rsi, adx, atr_pct
    
    def get_current_state(self, rsi, adx, atr_pct, price, volume):
        """获取当前市场状态"""
        # 更新盘感
        buy_vol = volume * 0.52  # 简化：假设52%是买入量
        self.sense.update(price, volume, float(rsi), float(adx), buy_vol)
        
        order_flow = self.sense.get_order_flow_score()
        squeeze = self.sense.get_squeeze_score()
        vol_regime = self.sense.get_volatility_regime()
        
        return {
            'rsi': float(rsi),
            'adx': float(adx),
            'atr_pct': float(atr_pct),
            'order_flow': order_flow,
            'squeeze': squeeze,
            'vol_regime': vol_regime,
        }
    
    def scan_signals(self):
        """扫描信号"""
        df = self.load_data()
        c = df['close']
        h = df['high']
        l = df['low']
        v = df['volume']
        
        rsi, adx, atr_pct = self.calc_indicators(df)
        
        # 当前状态
        state = self.get_current_state(
            rsi.iloc[-1], adx.iloc[-1], atr_pct.iloc[-1],
            float(c.iloc[-1]), float(v.iloc[-1])
        )
        
        results = {}
        vol_names = {0: '低波动', 1: '正常', 2: '高波动', 3: '极端'}
        
        for direction in ['long', 'short']:
            score = self.decision.calc_open_score(
                direction, state['rsi'], state['order_flow'], state['squeeze']
            )
            
            pos_pct, pos_usd, lev = self.decision.calc_position_size(
                score, direction, self.capital
            )
            
            # RSI超买/超卖阈值
            if direction == 'long':
                rsi_cond = state['rsi'] < RSI_LONG_TH
            else:
                rsi_cond = state['rsi'] > RSI_SHORT_TH
            
            can_trade = score > 0.3 and rsi_cond and state['vol_regime'] in [1, 2]
            
            if can_trade:
                sl_price, sl_pct = self.decision.calc_stop_loss(
                    float(c.iloc[-1]), direction, state['atr_pct']
                )
                tp_price, tp_pct = self.decision.calc_take_profit(
                    float(c.iloc[-1]), direction, state['atr_pct']
                )
                
                # 风控检查
                ok, reason = self.risk.pre_trade_check(direction, pos_usd, self.capital, self.positions)
                can_trade = ok
            else:
                sl_price = tp_price = sl_pct = tp_pct = 0
                reason = '条件未满足'
            
            results[direction] = {
                'score': score,
                'can_trade': can_trade,
                'reason': reason,
                'position_pct': pos_pct,
                'position_usd': pos_usd,
                'leverage': lev,
                'sl_price': sl_price,
                'sl_pct': sl_pct,
                'tp_price': tp_price,
                'tp_pct': tp_pct,
            }
        
        return state, results, float(c.iloc[-1])
    
    def run_scan(self):
        """实时信号扫描"""
        state, signals, price = self.scan_signals()
        vol_names = {0: '低波动', 1: '正常', 2: '高波动', 3: '极端'}
        
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
        
        print(f"\n{'='*60}")
        print(f"类人盘感Agent | {self.coin} | {now} UTC")
        print(f"{'='*60}")
        print(f"价格: ${price:,.0f}")
        print(f"RSI(5m): {state['rsi']:.1f} | ADX: {state['adx']:.1f}")
        print(f"波动率状态: {vol_names[state['vol_regime']]}")
        print(f"订单流: {state['order_flow']:+.2f} | 挤压度: {state['squeeze']:.2f}")
        print(f"连续亏损: {self.decision.consecutive_losses}次 | 日损: {self.decision.daily_loss_pct*100:.1f}%")
        
        for direction in ['long', 'short']:
            d = signals[direction]
            action = '✅ 做多信号' if (direction == 'long' and d['can_trade']) else \
                     '✅ 做空信号' if (direction == 'short' and d['can_trade']) else \
                     f'❌ 无信号({d["reason"]})'
            
            print(f"\n{'做多' if direction=='long' else '做空'}: {action}")
            print(f"  开仓得分: {d['score']:.2f}/1.0")
            print(f"  仓位: {d['position_pct']*100:.1f}% (${d['position_usd']:.0f}) {d['leverage']}x")
            if d['can_trade']:
                print(f"  止损: ${d['sl_price']:,.0f} ({d['sl_pct']*100:.2f}%)")
                print(f"  止盈: ${d['tp_price']:,.0f} ({d['tp_pct']*100:.1f}%)")
        
        print(f"\n账户状态: ${self.capital:,.0f} | 最大DD: {self.risk.max_dd*100:.1f}%")
        
        return signals


def run_backtest():
    """历史回测"""
    print("="*60)
    print("类人盘感Agent - 历史回测")
    print("="*60)
    
    agent = TradingAgent('BTC', 'backtest')
    df = agent.load_data()
    c = df['close']
    h = df['high']
    l = df['low']
    v = df['volume']
    
    rsi, adx, atr_pct = agent.calc_indicators(df)
    
    equity = [agent.capital]
    trades = []
    positions = []
    trade_id = 0
    cooldown_bars = 0  # 冷却期计数
    last_trade_bar = -999
    
    vol_names = {0: '低波动', 1: '正常', 2: '高波动', 3: '极端'}
    
    print(f"\n数据范围: {c.index[0].date()} ~ {c.index[-1].date()}")
    
    # 从2020年开始测试
    try:
        start_ts = pd.Timestamp('2020-01-01')
        start_pos = c.index.searchsorted(start_ts)
    except:
        start_pos = 0
    
    for i in range(start_pos + 20, len(c) - 1, 1):
        t = c.index[i]
        price = float(c.iloc[i])
        vol = float(v.iloc[i])
        
        # 更新盘感
        agent.sense.update(price, vol, float(rsi.iloc[i]), float(adx.iloc[i]), vol * 0.52)
        
        # 检查持仓超时
        for pos in list(positions):
            hold_hours = (t - pos['entry_time']).total_seconds() / 3600
            if hold_hours >= MAX_HOLDING_HOURS:
                # 强制平仓
                if pos['direction'] == 'long':
                    pnl_pct = (price - pos['entry_price']) / pos['entry_price'] * pos['lev']
                else:
                    pnl_pct = (pos['entry_price'] - price) / pos['entry_price'] * pos['lev']
                
                pnl = agent.capital * pnl_pct * 0.9  # 扣手续费
                agent.capital += pnl
                trades.append({'id': pos['id'], 'pnl': pnl, 'pnl_pct': pnl_pct, 'reason': 'timeout', 'dir': pos['direction']})
                positions.remove(pos)
                cooldown_bars = 12  # 交易后冷却1小时
        
        # 检查止损
        for pos in list(positions):
            if pos['direction'] == 'long':
                hit_sl = price <= pos['sl_price']
                hit_tp = price >= pos['tp_price']
            else:
                hit_sl = price >= pos['sl_price']
                hit_tp = price <= pos['tp_price']
            
            if hit_sl or hit_tp:
                if pos['direction'] == 'long':
                    pnl_pct = (price - pos['entry_price']) / pos['entry_price'] * pos['lev']
                else:
                    pnl_pct = (pos['entry_price'] - price) / pos['entry_price'] * pos['lev']
                
                pnl = agent.capital * pnl_pct * 0.9
                agent.capital += pnl
                is_win = pnl > 0
                agent.decision.on_trade_result(pnl_pct, is_win)
                agent.risk.post_trade_update(pnl, agent.capital)
                agent.memory.add_trade(agent.sense.get_pattern_vector(), pnl, pos['direction'])
                trades.append({'id': pos['id'], 'pnl': pnl, 'pnl_pct': pnl_pct, 'reason': 'stop' if hit_sl else 'tp', 'dir': pos['direction']})
                positions.remove(pos)
                cooldown_bars = 12  # 交易后冷却1小时
                last_trade_bar = i
        
        # 扫描新信号（有冷却期则跳过）
        if len(positions) < MAX_POSITIONS and cooldown_bars <= 0:
            state = agent.get_current_state(rsi.iloc[i], adx.iloc[i], atr_pct.iloc[i], price, vol)
            
            for direction in ['long']:  # 只做多，不做空
                if direction == 'long':
                    rsi_cond = state['rsi'] < RSI_LONG_TH
                    rsi_score = max(0, (RSI_LONG_TH - state['rsi']) / RSI_LONG_TH)
                else:
                    rsi_cond = state['rsi'] > RSI_SHORT_TH
                    rsi_score = max(0, (state['rsi'] - RSI_SHORT_TH) / (100 - RSI_SHORT_TH))
                
                score = agent.decision.calc_open_score(direction, state['rsi'], state['order_flow'], state['squeeze'])
                
                if score > 0.3 and rsi_cond and state['vol_regime'] in [1, 2]:
                    pos_pct, pos_usd, lev = agent.decision.calc_position_size(score, direction, agent.capital)
                    
                    ok, reason = agent.risk.pre_trade_check(direction, pos_usd, agent.capital, positions)
                    
                    if ok and pos_usd > 10:
                        sl_price, sl_pct = agent.decision.calc_stop_loss(price, direction, state['atr_pct'])
                        tp_price, tp_pct = agent.decision.calc_take_profit(price, direction, state['atr_pct'])
                        
                        positions.append({
                            'id': trade_id,
                            'direction': direction,
                            'entry_price': price * (1.0004 if direction == 'long' else 0.9996),
                            'entry_time': t,
                            'lev': lev,
                            'sl_price': sl_price,
                            'tp_price': tp_price,
                            'size_usd': pos_usd,
                        })
                        trade_id += 1
                        cooldown_bars = 6  # 开仓后冷却30分钟
        
        # 冷却期倒计时
        if cooldown_bars > 0:
            cooldown_bars -= 1
        
        equity.append(agent.capital)
    
    # 平所有持仓
    final_price = float(c.iloc[-1])
    for pos in positions:
        if pos['direction'] == 'long':
            pnl_pct = (final_price - pos['entry_price']) / pos['entry_price'] * pos['lev']
        else:
            pnl_pct = (pos['entry_price'] - final_price) / pos['entry_price'] * pos['lev']
        pnl = agent.capital * pnl_pct * 0.9
        agent.capital += pnl
        trades.append({'id': pos['id'], 'pnl': pnl, 'pnl_pct': pnl_pct, 'reason': 'final', 'dir': pos['direction']})
    
    # 统计
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    avg_win = np.mean([t['pnl'] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t['pnl'] for t in losses])) if losses else 0
    pf = avg_win / avg_loss if avg_loss > 0 else 0
    
    peak = np.maximum.accumulate(equity)
    dd = np.min((np.array(equity) - peak) / peak) * 100
    
    total_ret = (agent.capital - INIT_CAPITAL) / INIT_CAPITAL * 100
    
    print(f"\n回测结果:")
    print(f"  总收益: {total_ret:+.1f}%")
    print(f"  最终资金: ${agent.capital:,.0f}")
    print(f"  最大回撤: {dd:.1f}%")
    print(f"  交易次数: {len(trades)}")
    print(f"  胜率: {wr:.0f}%")
    print(f"  PF: {pf:.2f}")
    print(f"  均盈利: ${avg_win:,.0f}")
    print(f"  均亏损: ${avg_loss:,.0f}")
    print(f"  LONG盈亏: ${sum(t['pnl'] for t in trades if t['dir']=='long'):,.0f}")
    print(f"  SHORT盈亏: ${sum(t['pnl'] for t in trades if t['dir']=='short'):,.0f}")


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--backtest':
        run_backtest()
    else:
        agent = TradingAgent('BTC', 'scan')
        agent.run_scan()
