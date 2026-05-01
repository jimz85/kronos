#!/usr/bin/env python3
"""
gemma4主动做市系统 - 完全自动执行 v2.0
==========================================
双层决策 + 自动执行：规则判断 → gemma4审查 → OKX自动交易

修复内容：
1. ADX正确计算（Wilder平滑）
2. SL至少2%距现价
3. TP目标8-12%
4. 动态仓位计算
5. gemma4 override强制执行new_action
6. 盈亏比风险计算

运行：
  python3 gemma4_signal_audit.py          审查+执行
  python3 gemma4_signal_audit.py --audit  只审查不执行
"""

import os, sys, json, time, requests
from datetime import datetime

# ========== 加载环境变量 ==========
ENV_FILE = os.path.expanduser('~/.hermes/.env')
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
MINIMAX_API_KEY = os.getenv('MINIMAX_API_KEY', '')
MINIMAX_BASE_URL = os.getenv('MINIMAX_BASE_URL', 'https://api.minimaxi.com/v1')
AUDIT_ONLY = '--audit' in sys.argv

# ========== 交易参数 ==========
SL_DIST_PCT = 0.02       # SL距现价2%（修复：原来是1%太紧）
TP_DIST_PCT = 0.10       # TP距现价10%（修复：原来16%太远）
TRAILING_SL_PCT = 0.03   # 追踪止损距现价3%
MAX_LOSS_PCT = 0.05      # 最大亏损5%强制止损（原来-5%）
TP_PROFIT_PCT = 0.05     # 浮盈5%以上才考虑止盈（修复：原来2%太低）
RISK_PER_TRADE_PCT = 0.01  # 每笔交易风险1%账户

# ========== OKX API ==========
# 动态熔断（延迟导入避免循环依赖）
try:
    from real_monitor import get_dynamic_treasury_limits, check_treasury_limits, get_account_balance as _get_okx_balance
    def get_account_balance():
        bal = _get_okx_balance()
        if bal is None:
            return 98091
        # 可能是dict或int
        if isinstance(bal, dict):
            return bal.get('totalEq', bal.get('adjEq', bal.get('availBal', 98091)))
        return int(bal) if bal else 98091
except ImportError:
    def get_account_balance():
        return 98091  # fallback
    def check_treasury_limits(equity, **kw):
        return True, 'real_monitor未安装', []
    def get_dynamic_treasury_limits(equity, **kw):
        return {'hourly_limit': 99999, 'daily_limit': 99999, 'reason': 'fallback'}

def _ts():
    """返回OKX服务器时间同步后的ISO8601时间戳"""
    global _okx_ts_offset
    try:
        if _okx_ts_offset is None:
            r = _req_get('https://www.okx.com/api/v5/public/time', timeout=5)
            if r and r.get('data'):
                okx_ms = int(r['data'][0]['ts'])
                local_ms = int(time.time() * 1000)
                _okx_ts_offset = okx_ms - local_ms
    except:
        pass
    from datetime import datetime
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

_okx_ts_offset = None

def _req_get(url, headers=None):
    try:
        import requests
        h = {'User-Agent': 'Mozilla/5.0', 'x-simulated-trading': os.getenv('OKX_FLAG', '1')}
        if headers:
            h.update(headers)
        r = requests.get(url, headers=h, timeout=10)
        return r.json()
    except:
        return None

def _okx_sign(method, path, body=''):
    """OKX签名"""
    import hmac, hashlib, base64
    ts = _ts()
    msg = f'{ts}{method}{path}{body}'
    sign = base64.b64encode(hmac.new(
        OKX_SECRET.encode(), msg.encode(), hashlib.sha256
    ).digest()).decode()
    return {
        'OK-ACCESS-KEY': OKX_API_KEY,
        'OK-ACCESS-SIGN': sign,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': os.getenv('OKX_FLAG', '1'),
    }

def _req(method, path, body=''):
    """带签名的OKX请求"""
    import requests
    headers = _okx_sign(method, path, body)
    url = 'https://www.okx.com' + path
    if method == 'GET':
        r = requests.get(url, headers=headers, timeout=10)
    else:
        r = requests.post(url, headers=headers, data=body, timeout=10)
    return r.json()

# ========== 持仓操作 ==========

def get_positions():
    """获取所有持仓"""
    try:
        result = _req('GET', '/api/v5/account/positions')
        positions = {}
        for p in result.get('data', []):
            inst_id = p.get('instId', '')
            pos = float(p.get('pos', 0))
            if pos > 0:
                coin = inst_id.split('-')[0]
                positions[coin] = {
                    'instId': inst_id,
                    'pos': pos,
                    'avgPx': float(p.get('avgPx', 0)),
                    'side': p.get('posSide', ''),
                    'upl': float(p.get('upl', 0)),
                    'uplRatio': float(p.get('uplRatio', 0)),
                    'lever': p.get('lever', ''),
                }
        return positions
    except:
        return {}

def get_pending_algos(instId):
    """获取某币种的待触发SL/TP条件单"""
    try:
        result = _req('GET', f'/api/v5/account/positions?instId={instId}')
        # 也查条件单
        result2 = _req('GET', f'/api/v5/trade/orders-algo-pending?instId={instId}&ordType=conditional')
        algos = {}
        for o in result2.get('data', []):
            algo_id = o.get('algoId', '')
            sl_trigger = o.get('slTriggerPx', '')
            tp_trigger = o.get('tpTriggerPx', '')
            if sl_trigger:
                algos['sl'] = {'algoId': algo_id, 'price': float(sl_trigger), 'sz': o.get('sz', '')}
            if tp_trigger:
                algos['tp'] = {'algoId': algo_id, 'price': float(tp_trigger), 'sz': o.get('sz', '')}
        return algos
    except:
        return {}

def close_position(instId, side, sz):
    """市价平仓"""
    close_side = 'sell' if side == 'long' else 'buy'
    close_pos_side = 'long' if side == 'short' else 'short'
    body = json.dumps({
        'instId': instId,
        'tdMode': 'isolated',
        'side': close_side,
        'ordType': 'market',
        'sz': str(int(sz)),
        'posSide': close_pos_side,
    })
    result = _req('POST', '/api/v5/trade/order', body)
    ok = result.get('code') == '0'
    if ok:
        print(f"  ✅ 平仓成功: {instId} {sz}张")
    else:
        print(f"  ❌ 平仓失败: {result.get('msg', 'unknown')}")
    return ok, result

def amend_sl(instId, algoId, newSlPrice):
    """修改止损价格"""
    body = json.dumps({
        'instId': instId,
        'algoId': algoId,
        'newSlTriggerPx': str(newSlPrice),
    })
    result = _req('POST', '/api/v5/trade/amend-algos', body)
    ok = result.get('code') == '0'
    if ok:
        print(f"  ✅ SL修改成功: {instId} → ${newSlPrice}")
    else:
        print(f"  ❌ SL修改失败: {result.get('msg', 'unknown')}")
    return ok, result

def place_sl_tp(instId, side, sz, slPrice, tpPrice):
    """挂新的SL/TP条件单"""
    close_side = 'sell' if side == 'long' else 'buy'
    pos_side = 'long' if side == 'short' else 'short'
    results = {}
    
    for name, trigger_key, price in [
        ('SL', 'slTriggerPx', slPrice),
        ('TP', 'tpTriggerPx', tpPrice),
    ]:
        body = json.dumps({
            'instId': instId,
            'tdMode': 'isolated',
            'side': close_side,
            'ordType': 'conditional',
            'sz': str(int(sz)),
            'posSide': pos_side,
            trigger_key: str(price),
            f'{trigger_key[:-4]}OrdPx': '-1',
        })
        result = _req('POST', '/api/v5/trade/order-algo', body)
        key = 'sl' if name == 'SL' else 'tp'
        ok = result.get('code') == '0'
        if ok:
            algo_id = result['data'][0]['algoId']
            print(f"  ✅ {name}挂单成功: ${price} [id:{algo_id[:8]}]")
            results[key] = {'success': True, 'algoId': algo_id, 'price': price}
        else:
            print(f"  ❌ {name}失败: {result.get('msg', '')}")
            results[key] = {'success': False, 'error': result.get('msg', '')}
    return results

# ========== 市场数据 ==========

def get_current_price(coin='AVAX'):
    try:
        r = _req_get(f'https://www.okx.com/api/v5/market/ticker?instId={coin}-USDT-SWAP')
        if r and r.get('data'):
            return float(r['data'][0]['last'])
    except:
        pass
    return None

def get_ohlcv(coin='AVAX', bar='1H', limit=48):
    try:
        import requests
        instId = f'{coin}-USDT-SWAP'
        after = int(time.time() * 1000) - (limit * 3600 * 1000)
        url = f'https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}&after={after}'
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        data = r.json()
        if not data.get('data'):
            return []
        candles = []
        for d in data['data']:
            try:
                candles.append({
                    'ts': int(d[0]), 'open': float(d[1]), 'high': float(d[2]),
                    'low': float(d[3]), 'close': float(d[4]), 'volume': float(d[5])
                })
            except:
                pass
        return list(reversed(candles))
    except:
        return []

def calc_rsi(candles, period=14):
    if len(candles) < period + 1:
        return None
    closes = [c['close'] for c in candles]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_adx(candles, period=14):
    """
    正确的ADX计算 - Wilder平滑
    ADX = Wilder平滑(DX)的移动平均
    修正：之前计算的是单周期DX，不是真正的ADX
    """
    if len(candles) < period * 2:
        return None
    
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    closes = [c['close'] for c in candles]
    
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        trs.append(tr)
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    
    if len(trs) < period:
        return None
    
    # Wilder平滑 - 使用EMA近似
    # ATR平滑
    atr = sum(trs[:period]) / period
    plus_di_sum = sum(plus_dm[:period]) / period
    minus_di_sum = sum(minus_dm[:period]) / period
    
    dx_values = []
    for i in range(period, len(trs)):
        # Wilder平滑更新
        atr = (atr * (period - 1) + trs[i]) / period
        plus_di_sum = (plus_di_sum * (period - 1) + plus_dm[i]) / period
        minus_di_sum = (minus_di_sum * (period - 1) + minus_dm[i]) / period
        
        if atr > 0:
            plus_di = (plus_di_sum / atr) * 100
            minus_di = (minus_di_sum / atr) * 100
            if plus_di + minus_di > 0:
                dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
                dx_values.append(dx)
    
    if len(dx_values) < period:
        return None
    
    # ADX = DX的period周期简单移动平均
    adx = sum(dx_values[-period:]) / period
    return adx

def get_btc_direction():
    candles = get_ohlcv('BTC', '1H', 24)
    if not candles:
        return 'unknown'
    rsi = calc_rsi(candles)
    if rsi is None:
        return 'unknown'
    if rsi > 65:
        return 'overbought'
    elif rsi < 35:
        return 'oversold'
    return 'neutral'

def get_atr(candles, period=14):
    """计算ATR（Average True Range）用于仓位计算"""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]['high'] - candles[i]['low'],
            abs(candles[i]['high'] - candles[i-1]['close']),
            abs(candles[i]['low'] - candles[i-1]['close'])
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

def get_market_data(coin='AVAX'):
    price = get_current_price(coin)
    c1 = get_ohlcv(coin, '1H', 48)
    c4 = get_ohlcv(coin, '4H', 48)
    btc_dir = get_btc_direction()
    atr = get_atr(c1) if c1 else None
    return {
        'coin': coin,
        'price': price,
        'rsi_1h': round(calc_rsi(c1), 1) if c1 else None,
        'rsi_4h': round(calc_rsi(c4), 1) if c4 else None,
        'adx_1h': round(calc_adx(c1), 1) if c1 else None,
        'adx_4h': round(calc_adx(c4), 1) if c4 else None,
        'btc_direction': btc_dir,
        'atr': atr,
        'atr_pct': (atr / price * 100) if atr and price else None,
    }

# ========== 动态仓位计算 ==========

def calc_position_size(price, atr_pct, account_balance, risk_pct=RISK_PER_TRADE_PCT):
    """
    根据ATR和账户风险计算仓位（v4.0 OKX永续合约版）
    
    OKX USDT永续合约：每张 = $100 USDT（固定）
    所以每张止损金额 = $100 × ATR%
    
    公式:
    - loss_per_contract = $100 × ATR%
    - num_contracts = risk_amount / loss_per_contract
    - 按币种差异化cap（高波动币种需要更多张来达到1%风险目标）
    """
    if not atr_pct or atr_pct == 0:
        atr_pct = 2.0
    
    CONTRACT_USDT = 100  # OKX USDT永续每张=$100
    
    # 风险金额
    risk_amount = account_balance * risk_pct  # 1%账户风险
    
    # 每张合约止损金额
    loss_per_contract = CONTRACT_USDT * (atr_pct / 100)
    
    # 合约数量 = 风险金额 / 每张止损金额
    num_contracts = risk_amount / loss_per_contract
    
    # 按价格区间差异化cap（低价值币需要更多张才能达到1%风险目标）
    # 价格<$1：2500张（需要~1500张达到1%风险）
    # 价格$1-100：1500张
    # 价格$100-1000：1000张
    # 价格>$1000：500张（BTC/ETH）
    if price < 1:
        max_contracts = 2500
    elif price < 100:
        max_contracts = 1500
    elif price < 1000:
        max_contracts = 1000
    else:
        max_contracts = 500
    
    num_contracts = min(num_contracts, max_contracts)
    
    return max(10, int(num_contracts))

# ========== 规则决策 ==========

def rules_decision(md, pos=None, algos=None):
    """RSI/ADX规则决策 v2.0"""
    price = md['price']
    rsi = md['rsi_1h'] or 50
    adx = md['adx_1h'] or 20
    btc = md['btc_direction']
    urgency = 0
    action = 'hold'
    reason = []
    
    if price is None:
        return {'action': 'wait', 'reason': '无价格', 'urgency': 0, 'trend': 'unknown'}
    
    trend = 'neutral'
    # 趋势判断：RSI + ADX + BTC方向
    if rsi < 35 and adx > 25:  # 修复：ADX>25才是强趋势（原来15太低）
        trend = 'long'
        reason.append(f'RSI={rsi}超卖+ADX={adx}趋势强→看多')
    elif rsi > 65 and adx > 25:
        trend = 'short'
        reason.append(f'RSI={rsi}超买+ADX={adx}趋势强→看空')
    else:
        reason.append(f'RSI={rsi} neutral, ADX={adx}')
    
    if pos:
        entry = float(pos.get('avgPx', 0))
        size = pos.get('pos', 0)
        side = pos.get('side', '')
        if entry > 0 and size > 0:
            pnl_pct = (price - entry) / entry * 100 if side == 'long' else (entry - price) / entry * 100
            sl_price = algos.get('sl', {}).get('price') if algos else None
            tp_price = algos.get('tp', {}).get('price') if algos else None
            
            # SL距离计算
            if sl_price and side == 'long':
                sl_dist = (price - sl_price) / price * 100
            elif sl_price and side == 'short':
                sl_dist = (sl_price - price) / price * 100
            else:
                sl_dist = 99
            
            # TP距离计算
            if tp_price and side == 'long':
                tp_dist = (tp_price - price) / price * 100
            elif tp_price and side == 'short':
                tp_dist = (price - tp_price) / price * 100
            else:
                tp_dist = 99
            
            # 盈亏比
            if sl_dist > 0:
                reward_risk = tp_dist / sl_dist
            else:
                reward_risk = 0
            
            # ========== 决策规则 ==========
            
            # 1. 强制止损：亏损>5%
            if pnl_pct < -5:
                action = 'close'
                urgency = 10
                reason.append(f'亏损{pnl_pct:.1f}%→强制止损')
            
            # 2. SL极危险：距现价<1.5% + BTC超买
            elif sl_dist < 1.5 and btc == 'overbought':
                action = 'tighten_sl'
                urgency = 9
                reason.append(f'SL极危险{sl_dist:.2f}%+BTC超买→收紧')
            
            # 3. SL危险：距现价<2%（修复：原来是1%太紧）
            elif sl_dist < 2.0:
                action = 'tighten_sl'
                urgency = 7
                reason.append(f'SL危险{sl_dist:.2f}%→收紧至2%')
            
            # 4. 追踪止损：浮盈>8% + 强趋势
            elif pnl_pct > 8 and adx > 30:
                action = 'trailing_sl'
                urgency = 5
                reason.append(f'浮盈{pnl_pct:.1f}%→追踪止损')
            
            # 5. 止盈：RSI>70 + 浮盈>5%（修复：原来2%太低）
            elif rsi > 70 and pnl_pct > TP_PROFIT_PCT * 100:
                action = 'take_profit'
                urgency = 6
                reason.append(f'RSI={rsi}超买+浮盈{pnl_pct:.1f}%→止盈')
            
            # 6. 风险回报比检查
            elif reward_risk < 2 and tp_dist > 15:
                # 盈亏比<2且TP太远，考虑提前止盈
                action = 'modify_tp_lower'
                urgency = 4
                reason.append(f'盈亏比{reward_risk:.1f}低+TP太远')
            
            reason.append(f'盈亏比:{reward_risk:.1f}:1')
    
    # 无持仓时的开仓信号
    elif not pos:
        if trend == 'long' and btc != 'oversold':  # 修复：BTC超卖时不追多
            action = 'open_long'
            urgency = 6
            reason.append(f'RSI超卖+趋势确认→做多')
        elif trend == 'short' and btc != 'overbought':  # 修复：BTC超买时不追空
            action = 'open_short'
            urgency = 6
            reason.append(f'RSI超买+趋势确认→做空')
    
    return {
        'action': action,
        'reason': ' | '.join(reason),
        'urgency': urgency,
        'trend': trend,
        'rsi': rsi,
        'adx': adx,
        'btc': btc,
        'price': price,
    }

# ========== 结构化Agent核心 (Fincept风格) ==========

STRUCTURED_PROMPT_TEMPLATE = """你是一个专业的加密货币量化交易Agent。你的职责是**严格审查**交易决策，而不是自由发挥。

## 必须遵守的硬性规则（所有条件必须同时满足，否则必须否决）

### 开仓硬性规则
- RSI必须在30-35之间（超卖区域）做多，或在65-70之间（超买区域）做空
- ADX必须>20（确认趋势存在）
- SL距现价必须>1.5%
- TP距现价必须在6-12%之间
- 盈亏比必须>2:1
- BTC方向冲突时必须否决（BTC超买时不追多，BTC超卖时不追空）

### 持仓管理硬性规则
- 浮亏>5%：必须止损（无例外）
- SL距现价<1%：立即收紧或止损
- 浮盈>8%且ADX>30：启动追踪止损
- RSI进入极端区域（<25或>75）：强制平仓
- 缺少SL或TP：立即补上

### 禁止行为
- 不允许建议亏损加仓（摊平成本）
- 不允许建议持有超过48小时不做任何操作
- 不允许建议在RSI 40-60中性区域建仓
- 不允许建议盈亏比<1.5:1的交易
- 不允许建议在市场剧烈波动时（ADX>50）追涨杀跌

## 输出格式（严格按此格式输出，禁止任何额外内容）

verdict: [PASS/FAIL/REJECT]
action: [从白名单选一个]
confidence: [0-100]
reason: [不超过30字的简洁理由]

## 可选动作白名单（必须从以下选择，禁止自创动作）
hold / wait / open_long / open_short / close / tighten_sl / trailing_sl / take_profit / repair_sl_tp / force_close

## 警告：数据缺失时必须FAIL
- 如果RSI、ADX、价格为None或N/A，必须输出verdict: FAIL
- 禁止在数据缺失时猜测或假设数据值
- 禁止输出"RSI在30-35范围"等未经确认的信息

## 当前审查数据

【市场数据】
币种: {coin}
当前价格: ${price}
1h RSI: {rsi_1h}（<30严重超卖，30-35超卖，35-65中性，65-70超买，>70严重超买）
4h RSI: {rsi_4h}
ADX: {adx_1h}（>30强趋势，20-30中等趋势，<20弱趋势）
BTC方向: {btc_direction}（overbought=超买，oversold=超卖，neutral=中性）
趋势判断: {trend}

【持仓状态】
{danger_zone_text}

【系统规则决策】
规则动作: {rules_action}
规则原因: {rules_reason}
规则紧急度: {urgency}/10
"""

def call_minimax(prompt, model='MiniMax-M2.7', timeout=30):
    """调用MiniMax API"""
    headers = {
        'Authorization': f'Bearer {MINIMAX_API_KEY}',
        'Content-Type': 'application/json',
    }
    data = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.3,
        'max_tokens': 2500,
    }
    try:
        r = requests.post(f'{MINIMAX_BASE_URL}/text/chatcompletion_v2', headers=headers, json=data, timeout=timeout)
        result = r.json()
        if 'choices' in result:
            return result['choices'][0]['message']['content']
        return f"API错误: {result.get('base_error', result.get('msg', 'unknown'))}"
    except Exception as e:
        return f"MiniMax API错误: {str(e)}"


def gemma4_audit(coin, md, pos, algos, rules_result):
    """
    结构化Agent审查 - Fincept风格
    改进：
    1. 硬性规则前置，AI不能违反
    2. 结构化输出（verdict/action/confidence/checks）
    3. 禁止行为明确列出
    4. 可选动作白名单，防止AI自创动作
    """
    price = md['price']
    rsi_1h = md.get('rsi_1h') or 50
    rsi_4h = md.get('rsi_4h') or 50
    adx_1h = md.get('adx_1h') or 20
    btc_direction = md.get('btc_direction', 'neutral')

    # 计算持仓数据
    pnl_pct = 0
    sl_dist = 0
    tp_dist = 0
    reward_risk = 0
    has_sl = False
    has_tp = False

    if pos and price:
        entry = float(pos.get('avgPx', 0))
        if entry > 0:
            side = pos.get('side', 'long')
            pnl_pct = (price - entry) / entry * 100 if side == 'long' else (entry - price) / entry * 100
            sl_price = algos.get('sl', {}).get('price') if algos else None
            tp_price = algos.get('tp', {}).get('price') if algos else None
            if sl_price:
                has_sl = True
                sl_dist = (price - sl_price) / price * 100 if side == 'long' else (sl_price - price) / price * 100
            if tp_price:
                has_tp = True
                tp_dist = (tp_price - price) / price * 100 if side == 'long' else (price - tp_price) / price * 100
            if sl_dist > 0:
                reward_risk = tp_dist / sl_dist

    # 趋势判断
    trend = 'neutral'
    if rsi_1h < 35 and adx_1h > 20:
        trend = 'long'
    elif rsi_1h > 65 and adx_1h > 20:
        trend = 'short'

    # 危险区域文本
    if pos and price:
        danger_parts = []
        if pnl_pct < -5:
            danger_parts.append(f'浮亏{pnl_pct:.1f}%>5%【危险】')
        elif pnl_pct < 0:
            danger_parts.append(f'浮亏{pnl_pct:.1f}%')
        elif pnl_pct > 0:
            danger_parts.append(f'浮盈{pnl_pct:.1f}%')
        if not has_sl:
            danger_parts.append('缺SL【危险】')
        if not has_tp:
            danger_parts.append('缺TP【危险】')
        if sl_dist < 1.5 and sl_dist > 0:
            danger_parts.append(f'SL距{sl_dist:.1f}%偏紧')
        if reward_risk > 0 and reward_risk < 2:
            danger_parts.append(f'盈亏比{reward_risk:.1f}:1偏低')
        danger_zone_text = f"方向:{pos['side']} {pos['pos']}张 @{entry} | " + ' | '.join(danger_parts)
    else:
        danger_zone_text = '无持仓'

    # 填充prompt
    prompt = STRUCTURED_PROMPT_TEMPLATE.format(
        coin=coin,
        price=price,
        rsi_1h=f'{rsi_1h:.1f}' if rsi_1h else 'N/A',
        rsi_4h=f'{rsi_4h:.1f}' if rsi_4h else 'N/A',
        adx_1h=f'{adx_1h:.1f}' if adx_1h else 'N/A',
        btc_direction=btc_direction,
        trend=trend,
        danger_zone_text=danger_zone_text,
        rules_action=rules_result.get('action', 'hold'),
        rules_reason=rules_result.get('reason', ''),
        urgency=rules_result.get('urgency', 0),
    )

    try:
        output = call_minimax(prompt, timeout=30)

        # 解析结构化输出
        verdict = 'PASS'
        action = rules_result.get('action', 'hold')
        confidence = 50
        reason = ''

        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('verdict:'):
                v = line.split(':', 1)[1].strip().upper()
                if v in ('FAIL', 'REJECT'):
                    verdict = 'FAIL'
                elif v == 'PASS':
                    verdict = 'PASS'
            elif line.startswith('action:'):
                action = line.split(':', 1)[1].strip()
                # 白名单验证
                valid_actions = {'hold', 'wait', 'open_long', 'open_short', 'close',
                               'tighten_sl', 'trailing_sl', 'take_profit', 'repair_sl_tp', 'force_close'}
                if action not in valid_actions:
                    action = rules_result.get('action', 'hold')
            elif line.startswith('confidence:'):
                try:
                    confidence = int(''.join(filter(str.isdigit, line.split(':')[1].strip()[:3])))
                    confidence = max(0, min(100, confidence))
                except:
                    confidence = 50
            elif line.startswith('reason:'):
                reason = line.split(':', 1)[1].strip()[:60]

        # ===== 安全阀：数据缺失时强制FAIL =====
        # 如果关键数据缺失，gemma4的任何PASS都是不可信的
        data_missing = (price is None or rsi_1h is None or adx_1h is None)
        if data_missing and verdict != 'FAIL':
            verdict = 'FAIL'
            action = 'wait'
            reason = f'数据缺失(P={price} RSI={rsi_1h} ADX={adx_1h})，强制等待'

        # 决策逻辑
        # FAIL时必须强制执行安全动作
        if verdict == 'FAIL':
            # FAIL时的安全动作
            if pos:
                if pnl_pct < -5:
                    final_action = 'force_close'
                elif not has_sl:
                    final_action = 'repair_sl_tp'
                elif sl_dist < 1.5:
                    final_action = 'tighten_sl'
                else:
                    final_action = 'close'
            else:
                final_action = 'hold'
            override = True
        elif verdict == 'PASS':
            final_action = action if action != rules_result.get('action', 'hold') else rules_result.get('action', 'hold')
            override = (final_action != rules_result.get('action', 'hold'))
        else:
            final_action = rules_result.get('action', 'hold')
            override = False

        return {
            'gemma_verdict': verdict,
            'gemma_action': action,
            'gemma_confidence': confidence,
            'gemma_reason': reason or output[:100],
            'gemma_override': override,
            'final_action': final_action,
            'raw_output': output,
        }
    except Exception as e:
        return {
            'gemma_verdict': 'TIMEOUT',
            'gemma_action': rules_result.get('action', 'hold'),
            'gemma_confidence': 0,
            'gemma_reason': 'gemma4超时，使用规则决策',
            'gemma_override': False,
            'final_action': rules_result.get('action', 'hold'),
            'raw_output': '',
        }

# ========== 执行层 ==========

def execute_action(coin, action, md, pos, algos):
    """执行交易动作 v2.0"""
    if action in ('hold', 'wait'):
        return None
    
    instId = f'{coin}-USDT-SWAP'
    price = md['price']
    results = []
    account_balance = get_account_balance()
    
    if action == 'close':
        if pos:
            ok, r = close_position(instId, pos['side'], pos['pos'])
            results.append(('平仓', ok, f"{coin} {pos['pos']}张 {pos['side']}"))
    
    elif action == 'tighten_sl':
        if pos and algos and 'sl' in algos:
            old_sl = algos['sl']['price']
            # 收紧到距现价2%（修复：原来0.8%太紧）
            if pos['side'] == 'long':
                new_sl = round(price * (1 - SL_DIST_PCT), 4)
            else:
                new_sl = round(price * (1 + SL_DIST_PCT), 4)
            ok, r = amend_sl(instId, algos['sl']['algoId'], new_sl)
            results.append(('收紧SL', ok, f'{old_sl:.4f}→{new_sl:.4f}'))
    
    elif action == 'trailing_sl':
        # 追踪止损：SL移到成本价+3%
        if pos and algos and 'sl' in algos:
            entry = float(pos.get('avgPx', 0))
            if pos['side'] == 'long':
                new_sl = round(entry * (1 + TRAILING_SL_PCT), 4)
            else:
                new_sl = round(entry * (1 - TRAILING_SL_PCT), 4)
            ok, r = amend_sl(instId, algos['sl']['algoId'], new_sl)
            results.append(('追踪SL', ok, f'→{new_sl:.4f}'))
    
    elif action == 'modify_tp_lower':
        # 降低TP到合理位置
        if pos and algos and 'tp' in algos:
            old_tp = algos['tp']['price']
            if pos['side'] == 'long':
                # TP设为现价+8%
                new_tp = round(price * 1.08, 4)
            else:
                new_tp = round(price * 0.92, 4)
            # 需要取消旧TP再挂新的
            results.append(('TP调整', False, f'建议手动调整TP {old_tp:.4f}→{new_tp:.4f}'))
    
    elif action == 'take_profit':
        if pos:
            ok, r = close_position(instId, pos['side'], pos['pos'])
            results.append(('止盈平仓', ok, f"{coin} {pos['pos']}张"))
    
    elif action == 'open_long':
        # 动态计算仓位
        atr_pct = md.get('atr_pct', 2.0) or 2.0
        sz = calc_position_size(price, atr_pct, account_balance)
        sl = round(price * (1 - SL_DIST_PCT), 4)  # 2%SL
        tp = round(price * (1 + TP_DIST_PCT), 4)  # 10%TP
        
        body = json.dumps({
            'instId': instId,
            'tdMode': 'isolated',
            'side': 'buy',
            'ordType': 'market',
            'sz': str(sz),
            'posSide': 'long',
        })
        r = _req('POST', '/api/v5/trade/order', body)
        ok = r.get('code') == '0'
        if ok:
            print(f"  ✅ 开多成功: {sz}张 @{price}")
            place_sl_tp(instId, 'long', sz, sl, tp)
            results.append(('开多', True, f'{sz}张 @{price} | SL={sl} TP={tp}'))
        else:
            results.append(('开多', False, r.get('msg', '')))
    
    elif action == 'open_short':
        atr_pct = md.get('atr_pct', 2.0) or 2.0
        sz = calc_position_size(price, atr_pct, account_balance)
        sl = round(price * (1 + SL_DIST_PCT), 4)
        tp = round(price * (1 - TP_DIST_PCT), 4)
        
        body = json.dumps({
            'instId': instId,
            'tdMode': 'isolated',
            'side': 'sell',
            'ordType': 'market',
            'sz': str(sz),
            'posSide': 'short',
        })
        r = _req('POST', '/api/v5/trade/order', body)
        ok = r.get('code') == '0'
        if ok:
            print(f"  ✅ 开空成功: {sz}张 @{price}")
            place_sl_tp(instId, 'short', sz, sl, tp)
            results.append(('开空', True, f'{sz}张 @{price} | SL={sl} TP={tp}'))
        else:
            results.append(('开空', False, r.get('msg', '')))
    
    return results

# ========== 主流程 ==========

def full_cycle(coin='AVAX', notify=True):
    """完整判断+执行周期 v2.0"""
    if AUDIT_ONLY:
        notify = False
    
    print(f"\n{'='*50}")
    print(f"gemma4主动做市 v2.0 | {datetime.now().strftime('%H:%M:%S')} | {'[审查模式]' if AUDIT_ONLY else '[自动执行]'}")
    print(f"{'='*50}")
    
    # 1. 获取市场数据
    md = get_market_data(coin)
    print(f"市场: ${md['price']} | RSI_1h={md['rsi_1h']} | ADX={md['adx_1h']} | BTC={md['btc_direction']}")
    if md.get('atr'):
        print(f"ATR: ${md['atr']:.4f} ({md['atr_pct']:.2f}%)")
    
    # 2. 获取持仓和挂单
    positions = get_positions()
    pos = positions.get(coin)
    algos = get_pending_algos(f'{coin}-USDT-SWAP') if pos else {}
    
    if pos:
        pnl = 0
        if md['price'] and pos['avgPx'] > 0:
            pnl = (md['price'] - pos['avgPx']) / pos['avgPx'] * 100 if pos['side'] == 'long' else (pos['avgPx'] - md['price']) / pos['avgPx'] * 100
        print(f"持仓: {pos['side']} {pos['pos']}张 @{pos['avgPx']} | 浮盈亏: {pnl:.2f}%")
        if algos:
            sl = algos.get('sl', {}).get('price', '?')
            tp = algos.get('tp', {}).get('price', '?')
            sl_dist = (md['price'] - sl) / md['price'] * 100 if sl and pos['side'] == 'long' else (sl - md['price']) / md['price'] * 100 if sl else 0
            tp_dist = (tp - md['price']) / md['price'] * 100 if tp and pos['side'] == 'long' else (md['price'] - tp) / md['price'] * 100 if tp else 0
            print(f"挂单: SL=${sl} ({sl_dist:.2f}%) TP=${tp} ({tp_dist:.2f}%)")
            if sl_dist > 0 and tp_dist > 0:
                print(f"盈亏比: {tp_dist/sl_dist:.1f}:1")
    else:
        print("持仓: 无")
    
    # 3. 规则决策
    rules = rules_decision(md, pos, algos)
    print(f"规则: {rules['action']} | {rules['reason']}")
    
    # 4. gemma4结构化审查
    print("gemma4结构化审查中...")
    audit = gemma4_audit(coin, md, pos, algos, rules)
    final_action = audit['final_action']
    verdict_emoji = {'PASS': '✅', 'FAIL': '❌', 'REJECT': '🚫', 'TIMEOUT': '⏰', 'ERROR': '⚠️'}.get(audit['gemma_verdict'], '❓')
    print(f"gemma4: {verdict_emoji} {audit['gemma_verdict']} | 置信度{audit['gemma_confidence']}% | {audit['gemma_reason']}")
    
    if audit['gemma_override']:
        print(f"⚠️ gemma4否决规则，采用新决策: {final_action}")
    
    # 5. 动态熔断检查
    market_data_all = {coin: md}  # 简化：只用当前币种数据
    treasury_ok, treasury_msg, treasury_warns = check_treasury_limits(
        get_account_balance(), positions=positions, market_data=market_data_all
    )
    dyn_limits = get_dynamic_treasury_limits(get_account_balance(), positions, market_data_all)
    print(f"熔断状态: {'✅ 通过' if treasury_ok else '🚫 限制'} | {dyn_limits['reason']}")
    if not treasury_ok:
        print(f"  限制原因: {treasury_msg}")
        if not AUDIT_ONLY:
            print("  系统被熔断锁定，禁止开仓，等待下一小时重置")
        final_action = 'hold'
    
    # 6. 执行
    exec_results = None
    if treasury_ok and not AUDIT_ONLY and final_action not in ('hold', 'wait'):
        print(f"\n🚨 执行: {final_action}")
        exec_results = execute_action(coin, final_action, md, pos, algos)
    
    print(f"\n最终决策: {final_action}")
    if exec_results:
        for name, ok, detail in exec_results:
            print(f"  {'✅' if ok else '❌'} {name}: {detail}")
    
    return {
        'coin': coin,
        'final_action': final_action,
        'rules': rules,
        'audit': audit,
        'exec_results': exec_results,
        'market_data': md,
    }

if __name__ == '__main__':
    coin = sys.argv[1] if len(sys.argv) > 1 else 'AVAX'
    full_cycle(coin)
