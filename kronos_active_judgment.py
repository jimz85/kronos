#!/usr/bin/env python3
"""
Kronos 主动判断决策执行系统
每7分钟运行一次，主动判断市场方向并执行决策

核心原则：截断亏损，让利润奔跑

判断逻辑：
1. 检查持仓状态（盈亏、SL距离、市场环境）
2. 判断当前趋势（上涨/下跌概率）
3. 决策：持仓/收紧SL/移动TP/平仓
4. 执行操作并记录
"""
import sys
import os
import json
import time
import requests
from datetime import datetime
from pathlib import Path

# 加载.env
from dotenv import load_dotenv
load_dotenv(Path.home() / '.hermes' / '.env', override=True)

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np
from real_monitor import (
    get_real_positions, get_real_sl_tp_orders, get_account_balance,
    _req, _ts, cancel_algo_orders, get_atr_stop
)
from kronos_multi_coin import SL_DANGER_PCT  # P1统一SL危险阈值(0.5%)

# ============ 配置 ============
LOCK_FILE = os.path.join(os.path.dirname(__file__), '.judgment.lock')
LOCK_TIMEOUT = 60

# SL移动规则：浮盈达到多少时收紧SL到成本价
# ========== P0优化：真正的移动止损（跟利润，不跟价格）==========
# 原理：均值回归策略高胜率，紧止损+移动止损锁定利润
# 规则：浮盈达到N×SL距离时，SL锁定M×SL距离利润
# 例：SL=1.5%时，pr=1.1x → SL移至入场+0.5xSL = 保本+0.8%
# 做多：new_SL = entry × (1 + M × SL_pct)
SL_TRAIL_RULES = [
    # profit_ratio(浮盈/原始SL) → lock_ratio(锁定多少倍SL距离利润)
    # 含义：浮盈达到N×SL距离时，SL锁定M×SL距离利润
    # 做多：new_SL = entry × (1 + M × SL_pct)
    (1.0, 0.50),    # 浮盈达到1×SL → SL锁定0.5×SL距离利润（保本+0.5×SL）
    (2.0, 1.00),    # 浮盈达到2×SL → SL锁定1×SL距离利润（1×SL）
    (3.0, 2.00),    # 浮盈达到3×SL → SL锁定2×SL距离利润（2×SL）
    (5.0, 3.00),    # 浮盈达到5×SL → SL锁定3×SL距离利润（3×SL）
]
# SL_TRAIL_RULES格式：(profit_ratio, lock_profit_ratio)
# profit_ratio = 浮盈/原始SL距离
# lock_profit_ratio = 移动止损应锁定的利润比例（相对于原始SL距离）
# 逻辑：if profit_ratio >= N, new_SL = entry × (1 + M × SL_pct)
#   其中M = lock_profit_ratio

# TP调整规则：趋势强时提高TP
TP_BOOST_RULES = [
    (2.0, 0.10),    # 浮盈达到2×SL+趋势强时，TP提高10%
    (3.0, 0.15),    # 浮盈达到3×SL+趋势更强时，TP提高15%
]

# 止损规则
MAX_LOSS_PCT = 0.05    # 最大亏损5%强制止损
# 注意：SL_DANGER_PCT已从kronos_multi_coin导入统一值0.5%（原值1.0%已废弃）

# ============ 工具函数 ============
def get_price(coin):
    """获取当前价格"""
    try:
        r = _req('GET', f'/api/v5/market/ticker?instId={coin}-USDT-SWAP')
        if r.get('code') == '0' and r.get('data'):
            return float(r['data'][0]['last'])
    except:
        pass
    return None

def get_1h_data(coin):
    """获取1h K线数据（使用OKX公开API）"""
    try:
        url = f'https://www.okx.com/api/v5/market/history-candles?instId={coin}-USDT-SWAP&bar=1h&limit=100'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get('code') != '0' or not data.get('data'):
            return None
        
        # data格式: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
        candles = data['data']
        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm'])
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
        df.set_index('timestamp', inplace=True)
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        
        return df[['open', 'high', 'low', 'close', 'volume']]
    except:
        return None

def calc_rsi(prices, period=14):
    delta = np.diff(prices, prepend=prices[0])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    ag = pd.Series(gains).rolling(period).mean()
    al = pd.Series(losses).rolling(period).mean()
    rs = ag / (al + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_adx(high, low, close, period=14):
    high_d = np.diff(high, prepend=high[0])
    low_d = -np.diff(low, prepend=low[0])
    plus_dm = np.where(high_d > low_d, high_d, 0.0)
    minus_dm = np.where(low_d > high_d, low_d, 0.0)
    tr = np.maximum(high - low, np.maximum(np.abs(high - np.roll(close, 1)), np.abs(low - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    
    atr = pd.Series(tr).rolling(period).mean()
    plus_di = 100 * pd.Series(plus_dm).rolling(period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm).rolling(period).mean() / atr
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx = pd.Series(dx).rolling(14).mean()
    return adx, plus_di, minus_di

def get_market_regime(coin):
    """判断市场环境，返回dict"""
    df = get_1h_data(coin)
    if df is None or len(df) < 30:
        return None
    
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    
    rsi = calc_rsi(closes)
    adx, plus_di, minus_di = calc_adx(highs, lows, closes)
    
    rsi_now = rsi.iloc[-1]
    adx_now = adx.iloc[-1] if not pd.isna(adx.iloc[-1]) else 0
    plus_di_now = plus_di.iloc[-1] if not pd.isna(plus_di.iloc[-1]) else 0
    minus_di_now = minus_di.iloc[-1] if not pd.isna(minus_di.iloc[-1]) else 0
    
    # 趋势强度：ADX > 25 认为趋势明确
    trend_strength = 'strong' if adx_now > 25 else 'weak'
    
    # 方向偏好：比较+DI和-DI
    if plus_di_now > minus_di_now * 1.2:
        direction = 'bullish'
    elif minus_di_now > plus_di_now * 1.2:
        direction = 'bearish'
    else:
        direction = 'neutral'
    
    # RSI极端值
    if rsi_now < 35:
        rsi_extreme = 'oversold'
    elif rsi_now > 65:
        rsi_extreme = 'overbought'
    else:
        rsi_extreme = 'normal'
    
    return {
        'rsi': rsi_now,
        'adx': adx_now,
        'plus_di': plus_di_now,
        'minus_di': minus_di_now,
        'trend': trend_strength,
        'direction': direction,
        'rsi_extreme': rsi_extreme,
    }

def calc_profit_pct(entry, current):
    """计算盈亏百分比"""
    return (current - entry) / entry

def calc_sl_distance_pct(sl, current):
    """计算SL距现价百分比"""
    return abs(current - sl) / current

# ============ 飞书通知 ============
FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
FEISHU_CHAT_ID = os.getenv('FEISHU_CHAT_ID', 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0')

_f_token = None
_f_expire = 0

def get_feishu_token():
    global _f_token, _f_expire
    if _f_token and time.time() < _f_expire:
        return _f_token
    try:
        resp = requests.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET},
            timeout=10
        )
        data = resp.json()
        if data.get('code') == 0:
            _f_token = data['tenant_access_token']
            _f_expire = time.time() + data.get('expire', 3600) - 60
            return _f_token
    except:
        pass
    return None

def feishu_notify(text):
    """发送飞书消息"""
    try:
        token = get_feishu_token()
        if not token:
            return
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        chat_id = os.getenv("HERMES_SESSION_KEY", "").split(":")[-1]
        if not chat_id:
            chat_id = FEISHU_CHAT_ID
        payload = {
            'receive_id': chat_id,
            'msg_type': 'text',
            'content': json.dumps({'text': text})
        }
        resp = requests.post(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
            headers=headers, json=payload, timeout=10
        )
    except:
        pass

# ============ 决策执行 ============
def trail_sl(coin, current_price, entry_price, current_sl, new_sl, position_side, position_size):
    """移动SL（全量止损）"""
    if new_sl >= current_sl:
        print(f"  ⏭️ {coin}: SL无需移动 (当前{current_sl:.4f} >= 新{new_sl:.4f})")
        return False

    try:
        # 取消当前SL
        sl_orders, _ = get_real_sl_tp_orders()
        if coin in sl_orders and 'sl' in sl_orders[coin]:
            algo_id = sl_orders[coin]['sl']['algoId']
            cancel_algo_orders(f'{coin}-USDT-SWAP', [algo_id])

        # 重新挂SL（用新价格，全量）
        instId = f'{coin}-USDT-SWAP'
        # 做多止损=sell平多，做空止损=buy平空
        order_side = 'sell' if position_side == 'buy' else 'buy'
        body = [{
            'instId': instId,
            'tdMode': 'isolated',
            'side': order_side,
            'ordType': 'conditional',
            'sz': str(int(position_size)),  # 全量止损
            'slTriggerPx': str(round(new_sl, 4)),
            'slTriggerCond': 'last',
        }]
        result = _req('POST', '/api/v5/trade/order-algo', json.dumps(body))
        
        if result.get('code') == '0':
            print(f"  ✅ {coin}: SL从${current_sl:.4f}收紧到${new_sl:.4f}")
            feishu_notify(f"🔄 {coin} SL收紧\n从${current_sl:.4f}→${new_sl:.4f}\n现价${current_price:.4f}")
            return True
        else:
            print(f"  ❌ {coin}: SL收紧失败 {result.get('msg')}")
            return False
    except Exception as e:
        print(f"  ❌ {coin}: SL收紧异常 {e}")
        return False

def place_sl(coin, side, size, sl_price):
    """P0: 补缺失的SL（全量止损）"""
    try:
        instId = f'{coin}-USDT-SWAP'
        # 做多止损=sell，做空止损=buy
        order_side = 'sell' if side == 'buy' else 'buy'
        body = [{
            'instId': instId,
            'tdMode': 'isolated',
            'side': order_side,
            'ordType': 'conditional',
            'sz': str(int(size)),  # 全量止损
            'slTriggerPx': str(round(sl_price, 4)),
            'slTriggerCond': 'last',
        }]
        result = _req('POST', '/api/v5/trade/order-algo', json.dumps(body))
        if result.get('code') == '0':
            print(f"  ✅ P0修复: {coin} 补SL@{sl_price:.4f}({size}张)")
            feishu_notify(f"🚨 {coin} SL缺失！已自动补SL@{sl_price:.4f}({size}张)")
            return True
        else:
            print(f"  ❌ {coin} 补SL失败: {result.get('msg')}")
            return False
    except Exception as e:
        print(f"  ❌ {coin} 补SL异常: {e}")
        return False

def place_tp_only(coin, side, size, tp_price):
    """P0: 补缺失的TP（全量止盈）"""
    try:
        instId = f'{coin}-USDT-SWAP'
        order_side = 'sell' if side == 'buy' else 'buy'
        body = [{
            'instId': instId,
            'tdMode': 'isolated',
            'side': order_side,
            'ordType': 'conditional',
            'sz': str(int(size)),  # 全量止盈
            'tpTriggerPx': str(round(tp_price, 4)),
            'tpTriggerCond': 'last',
        }]
        result = _req('POST', '/api/v5/trade/order-algo', json.dumps(body))
        if result.get('code') == '0':
            print(f"  ✅ P0修复: {coin} 补TP@{tp_price:.4f}({size}张)")
            feishu_notify(f"🚨 {coin} TP缺失！已自动补TP@{tp_price:.4f}({size}张)")
            return True
        else:
            print(f"  ❌ {coin} 补TP失败: {result.get('msg')}")
            return False
    except Exception as e:
        print(f"  ❌ {coin} 补TP异常: {e}")
        return False

def boost_tp(coin, current_tp, new_tp, position_size):
    """提高TP"""
    if new_tp <= current_tp:
        print(f"  ⏭️ {coin}: TP无需调整")
        return False
    
    try:
        # 取消当前TP
        sl_orders, _ = get_real_sl_tp_orders()
        if coin in sl_orders and 'tp' in sl_orders[coin]:
            algo_id = sl_orders[coin]['tp']['algoId']
            cancel_algo_orders(f'{coin}-USDT-SWAP', [algo_id])
        
        # 挂新TP（全量）
        instId = f'{coin}-USDT-SWAP'
        body = [{
            'instId': instId,
            'tdMode': 'isolated',
            'side': 'sell' if position_size > 0 else 'buy',  # 做多止盈=sell
            'ordType': 'conditional',
            'sz': str(int(abs(position_size))),  # 全量止盈
            'tpTriggerPx': str(round(new_tp, 4)),
            'tpTriggerCond': 'last',
        }]
        result = _req('POST', '/api/v5/trade/order-algo', json.dumps(body))
        
        if result.get('code') == '0':
            print(f"  ✅ {coin}: TP从${current_tp:.4f}提高到${new_tp:.4f}")
            feishu_notify(f"📈 {coin} TP提高\n从${current_tp:.4f}→${new_tp:.4f}")
            return True
        else:
            print(f"  ❌ {coin}: TP调整失败 {result.get('msg')}")
            return False
    except Exception as e:
        print(f"  ❌ {coin}: TP调整异常 {e}")
        return False

def close_position(coin, side, size, reason=''):
    """平仓"""
    try:
        instId = f'{coin}-USDT-SWAP'
        close_side = 'sell' if side == 'buy' else 'buy'
        
        body = {
            'instId': instId,
            'tdMode': 'isolated',
            'side': close_side,
            'ordType': 'market',
            'sz': str(int(size)),
        }
        result = _req('POST', '/api/v5/trade/order', json.dumps(body))
        
        if result.get('code') == '0':
            print(f"  ✅ {coin}: 平仓完成 ({reason})")
            feishu_notify(f"✅ {coin} 平仓\n{reason}")
            return True
        else:
            print(f"  ❌ {coin}: 平仓失败 {result.get('msg')}")
            return False
    except Exception as e:
        print(f"  ❌ {coin}: 平仓异常 {e}")
        return False

def force_close_by_market(coin, side, size):
    """市价强平 - 模拟盘需要 posSide + reduceOnly"""
    try:
        instId = f'{coin}-USDT-SWAP'
        # 模拟盘格式：close long = buy + posSide=long；close short = sell + posSide=short
        if side == 'buy':  # long → 用buy+posSide=long关闭
            close_side = 'buy'
            pos_side = 'long'
        else:  # short → 用sell+posSide=short关闭
            close_side = 'sell'
            pos_side = 'short'
        body = {
            'instId': instId,
            'tdMode': 'isolated',
            'side': close_side,
            'ordType': 'market',
            'sz': str(int(size)),
            'posSide': pos_side,
            'reduceOnly': True,
        }
        result = _req('POST', '/api/v5/trade/order', json.dumps(body))

        if result.get('code') == '0':
            print(f"  🚨 {coin}: 市价强平成功")
            feishu_notify(f"🚨 {coin} 市价强平")
            return True
        else:
            print(f"  🚨 {coin}: 市价强平失败 {result.get('msg')}")
            return False
    except Exception as e:
        print(f"  🚨 {coin}: 市价强平异常 {e}")
        return False

# ============ 主动判断主逻辑 ============
def judgment_cycle():
    """一次完整的主动判断"""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] === 主动判断开始 ===")
    
    # 获取账户信息
    bal = get_account_balance()
    equity = bal.get('totalEq', 0)
    print(f"账户权益: ${equity:.2f}")
    
    # 获取持仓
    positions, err = get_real_positions()
    if err or not positions:
        print("无持仓，等待信号")
        return
    
    # 获取SL/TP
    sl_tp_orders, _ = get_real_sl_tp_orders()
    
    actions_taken = []
    
    for coin, pos in positions.items():
        side = pos['side']  # 'buy'=long, 'sell'=short
        size = pos['size']
        entry = pos['entry']
        unrealized_pnl = pos.get('unrealized_pnl', 0)
        
        # 获取当前价格
        current = get_price(coin)
        if not current:
            print(f"{coin}: 无法获取价格，跳过")
            continue
        
        # 计算盈亏
        if side == 'buy':
            profit_pct = (current - entry) / entry
        else:
            profit_pct = (entry - current) / entry
        
        profit_pct = unrealized_pnl / (entry * size) if entry * size > 0 else profit_pct
        
        # 获取市场环境
        regime = get_market_regime(coin)
        
        # 获取当前SL/TP
        current_sl = None
        current_tp = None
        if coin in sl_tp_orders:
            if 'sl' in sl_tp_orders[coin]:
                current_sl = float(sl_tp_orders[coin]['sl']['price'])
            if 'tp' in sl_tp_orders[coin]:
                current_tp = float(sl_tp_orders[coin]['tp']['price'])
        
        print(f"\n{coin}: {side} {size}张 @ ${entry:.4f} | 现价${current:.4f} | 盈亏{profit_pct*100:.1f}%")
        if regime:
            print(f"  市场: RSI={regime['rsi']:.0f} ADX={regime['adx']:.0f} 趋势={regime['trend']} 方向={regime['direction']}")
        
        # ========== 决策逻辑 ==========
        # 先计算sl_pct（供移动止损和TP调整共用）
        sl_pct = 0
        if current_sl and side == 'buy':
            sl_pct = (entry - current_sl) / entry
        elif current_sl and side == 'sell':
            sl_pct = (current_sl - entry) / entry
        profit_ratio = profit_pct / sl_pct if sl_pct > 0 else 0

        # 1. 止损判断：亏损超过阈值强制止损
        if profit_pct < -MAX_LOSS_PCT:
            print(f"  🚨 {coin}: 亏损{abs(profit_pct)*100:.1f}% > {MAX_LOSS_PCT*100:.0f}%阈值，强制止损")
            if force_close_by_market(coin, side, size):
                actions_taken.append(f"{coin}强制止损({profit_pct*100:.1f}%)")
            continue
        
        # ========== P0: SL或TP完全缺失 → 立即补 ==========
        if current_sl is None or current_tp is None:
            sl_pct_actual, tp_pct_actual = get_atr_stop(coin)
            if current_sl is None:
                new_sl = current * (1 - sl_pct_actual) if side == 'buy' else current * (1 + sl_pct_actual)
                print(f"  🚨 P0: {coin} 无SL！自动补{sl_pct_actual*100:.1f}%止损")
                if place_sl(coin, side, size, new_sl):
                    actions_taken.append(f"{coin}补SL@{new_sl:.4f}")
            if current_tp is None:
                new_tp = current * (1 + tp_pct_actual) if side == 'buy' else current * (1 - tp_pct_actual)
                print(f"  🚨 P0: {coin} 无TP！自动补{tp_pct_actual*100:.1f}%止盈")
                if place_tp_only(coin, side, size, new_tp):
                    actions_taken.append(f"{coin}补TP@{new_tp:.4f}")
            continue

        # ========== 2. SL极度危险判断 ==========
        if current_sl and side == 'buy':
            sl_distance = (current - current_sl) / current
            sl_distance_long = (current - current_sl) / current if side == 'buy' else (current_sl - current) / current
            
            if sl_distance_long < SL_DANGER_PCT:
                print(f"  🚨 {coin}: SL极度危险！距现价{sl_distance_long*100:.1f}%")
                # 判断：趋势继续看空？收紧SL或止损
                if regime and regime['direction'] == 'bearish' and regime['trend'] == 'strong':
                    print(f"  📉 市场看跌，趋势强，止损离场")
                    if close_position(coin, side, size, f"SL危险+趋势看跌"):
                        actions_taken.append(f"{coin}SL危险止损")
                else:
                    # 暂时持有但发出警告
                    feishu_notify(f"🚨 {coin} SL仅距现价{sl_distance_long*100:.1f}%，极度危险！")
        
        # 3. 趋势跟踪：浮盈时动态管理
        if profit_pct > 0 and side == 'buy':
            # 趋势强时不急于止盈
            if regime and regime['trend'] == 'strong' and regime['direction'] == 'bullish':
                # 动能强劲，上涨概率大，继续持有
                print(f"  📈 {coin}: 趋势强劲看涨，继续持有")
                
                # 如果浮盈大（>=2x SL距离），可以提高TP
                best_boost = 0
                for pr, boost in TP_BOOST_RULES:
                    if profit_ratio >= pr:
                        best_boost = max(best_boost, boost)
                if best_boost > 0 and current_tp:
                    new_tp = current_tp * (1 + best_boost)
                    if boost_tp(coin, current_tp, new_tp, size):
                        actions_taken.append(f"{coin}TP提高{best_boost*100:.0f}%")

            
            # 弱势市场，接近TP时考虑止盈（profit_ratio >= 1意味着浮盈达到1x SL距离）
            elif current_tp and profit_ratio >= 1.0:
                tp_distance_pct = (current_tp - current) / current
                if tp_distance_pct < 0.02:  # 离TP不到2%
                    print(f"  🎯 {coin}: 接近TP({current_tp:.4f})，考虑止盈")
                    # 检查RSI是否超买
                    if regime and regime['rsi_extreme'] == 'overbought':
                        print(f"  📤 {coin}: RSI超买，止盈离场")
                        if close_position(coin, side, size, f"RSI超买止盈"):
                            actions_taken.append(f"{coin}RSI超买止盈({profit_pct*100:.1f}%)")
        
        # 4. SL移动（追踪止损）：真正的利润追踪
        # 格式：profit_ratio=浮盈/原始SL距离，lock_ratio=锁定多少倍SL距离利润
        if profit_pct > 0 and side == 'buy' and current_sl and sl_pct > 0:
            best_lock_ratio = 0
            for pr, lr in SL_TRAIL_RULES:
                if profit_ratio >= pr:
                    best_lock_ratio = max(best_lock_ratio, lr)  # 选最高的

            if best_lock_ratio > 0:
                # 计算新SL：entry × (1 + lock_ratio × sl_pct)
                new_sl = entry * (1 + best_lock_ratio * sl_pct)
                if new_sl > current_sl:
                    print(f"  📍 {coin}: 浮盈{profit_pct*100:.1f}%（{profit_ratio:.1f}x SL），锁定{best_lock_ratio}x利润→SL=${new_sl:.4f}")
                    if trail_sl(coin, current, entry, current_sl, new_sl, side, size):
                        actions_taken.append(f"{coin}SL锁定({best_lock_ratio}x利润)")
        
        # 5. 反向信号判断：如果趋势反转是否要平仓
        if side == 'buy' and regime and regime['direction'] == 'bearish' and regime['trend'] == 'strong':
            # 做多但市场强烈看跌
            if profit_pct < 0:
                print(f"  📉 {coin}: 做多但市场强烈看跌，亏损中考虑止损")
                # 亏损+趋势反转，止损
                if profit_pct < -0.02:  # 亏损超过2%
                    print(f"  ❌ {coin}: 亏损{abs(profit_pct)*100:.1f}%，趋势反转，止损")
                    if close_position(coin, side, size, f"趋势反转止损"):
                        actions_taken.append(f"{coin}趋势反转止损")
    
    # 汇总
    if actions_taken:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 本轮操作:")
        for a in actions_taken:
            print(f"  - {a}")
        feishu_notify(f"📊 主动判断完成\n{' | '.join(actions_taken)}")
    else:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 本轮无操作，继续持有")

# ============ 主入口 ============
if __name__ == '__main__':
    from filelock import FileLock
    
    lock = FileLock(LOCK_FILE, timeout=LOCK_TIMEOUT)
    try:
        lock.acquire()
        judgment_cycle()
    except Exception as e:
        print(f"执行异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            lock.release()
        except:
            pass
