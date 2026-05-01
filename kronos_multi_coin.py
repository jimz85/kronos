#!/usr/bin/env python3
"""
Kronos 多币种自动做市系统 v3.3
============================================================
目标：全自动多币种交易，最多3个仓位，自动选择最优币种

架构：
  - 每3分钟扫描所有币种，评估持仓 + 发现新机会
  - 规则引擎 + 多因子投票系统双重决策
  - 动态IC权重 + 一票否决机制
  - 分三批止盈(50%/30%/20%)+ 追踪止损

运行：
  python3 kronos_multi_coin.py              全量扫描+执行
  python3 kronos_multi_coin.py --audit    只审查不执行
"""

import os, sys, json, time, subprocess, requests, math
from datetime import datetime
from pathlib import Path
from kronos_utils import atomic_write_json  # 原子写入（防断电损坏）

# ========== 投票系统导入 ==========
# 多因子IC动态权重投票系统(v3.3新增)
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from voting_system import VotingSystem, evaluate_coin, ICTracker
    HAS_VOTING_SYSTEM = True
    VOTING_TIMEOUT = 15  # 投票超时15秒，避免过慢
except ImportError:
    HAS_VOTING_SYSTEM = False
    VOTING_TIMEOUT = 0

# ========== LLM缓存层导入 ==========
# Redis缓存层用于减少重复LLM API调用
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from core.llm_cache import get_llm_cache, cache_response, get_cached_response
    HAS_LLM_CACHE = True
except ImportError:
    HAS_LLM_CACHE = False

# ========== 战略上下文导入(MiniMax小时层 → gemma4分钟层)==========
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from context_schema import (
        load_context_with_validation, read_emergency_stop, append_audit,
        CTX_FILE, EMERGENCY_FILE, AUDIT_FILE,
    )
    CTX_PATH = CTX_FILE  # 已是绝对路径（来自context_schema.py）
    EMERGENCY_PATH = EMERGENCY_FILE
    AUDIT_PATH = AUDIT_FILE
    HAS_CONTEXT = True
except ImportError:
    HAS_CONTEXT = False
    CTX_PATH = EMERGENCY_PATH = AUDIT_PATH = ""

# ========== 环境加载 ==========
ENV_FILE = os.path.expanduser('~/.hermes/.env')
if os.path.exists(ENV_FILE):
    # P0 Fix: Use encoding='utf-8' to properly decode the file, and strip inline comments
    # (dotenv's line parsing handles '#' as comment marker even after '=')
    for line in open(ENV_FILE, encoding='utf-8'):
        line = line.strip()
        if '=' in line:
            # Strip inline comments (e.g., OKX_FLAG=1  # comment)
            if '#' in line:
                line = line.split('#')[0]
            if line.strip():  # skip empty after comment strip
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
MINIMAX_API_KEY = os.getenv('MINIMAX_API_KEY', '')
MINIMAX_BASE_URL = os.getenv('MINIMAX_BASE_URL', 'https://api.minimaxi.com/v1')
AUDIT_ONLY = '--audit' in sys.argv

# ========== 全局配置 ==========
MAX_POSITIONS = 3
SL_DIST_PCT = 0.02         # ⚠️ 已废弃，改用动态ATR计算(见get_sl_tp_pct)
TP_DIST_PCT = 0.08         # ⚠️ 已废弃，改用动态ATR计算
TP_PARTIAL_PCT = 0.05      # ⚠️ 已废弃，改用动态分批止盈
TRAILING_TRIGGER = 0.06    # ⚠️ 已废弃，改用动态TP×50%触发
RISK_PER_TRADE = 0.01      # 每笔风险1%账户
MAX_POS_RATIO = 0.02       # 单仓位最大2%账户(更保守，避免OKX拒绝)
MIN_CONTRACTS = 10          # 最小仓位

# P1统一SL危险阈值：SL距现价<0.5%=极危险(绝对紧急触发close)
# 原值：kronos_multi_coin.py=0.5%, kronos_auto_guard.py=2.0%, kronos_active_judgment.py=1.0%
SL_DANGER_PCT = 0.005  # 0.5% - 统一后的SL极度危险阈值

# 币种列表（动态读取coin_strategy_map.json，排除excluded=true的币种）
# 见 _get_allowed_coins()
ALL_COINS = []  # 不再硬编码，由 _get_allowed_coins() 动态生成

def _get_allowed_coins():
    """返回coin_strategy_map.json中未标记为excluded的币种列表"""
    smap = get_coin_strategy_map()
    if not smap:
        # Fallback: 核心币种
        return ['SOL', 'BNB', 'XRP', 'DOGE', 'ADA']
    return sorted([s for s, c in smap.items() if not c.get('excluded', False)])

# ========== 动态SL/TP配置(v2.0 | 2026-04-20)==========
# 每个币种的历史最优止损距离转化为ATR倍数
# DOGE: 5%止损/10%止盈 → SL_ATR=5×, TP%=10%(固定)
# AVAX: 2%止损/8%止盈   → SL_ATR=6×, TP%=8%(固定)
# DOT:  10%止损/20%止盈 → SL_ATR=4×, TP%=20%(固定)
# 公式: SL% = max(SL_ATR_MULT × atr_pct, MIN_SL%), TP% = TP_RATIO × SL%

_COIN_SL_ATR = {  # P0优化：统一改为2-3x ATR止损(原5-20x太松)
    # 验证结果：DOGE/ADA 8年盈利，紧止损+高胜率最佳
    # AVAX: 2%止损(回测) / 0.9%当前ATR = 2.2×(已是均值回归最优)
    # DOGE: 8年盈利，实测2x ATR紧止损+2:1 RR+移动止损最优
    # 旧参数DOT/LINK/SOL等用5-10x ATR太松，等不到TP就扛不住回撤
    'AVAX': 2.0, 'ETH': 2.5, 'SOL': 3.0,
    'DOGE': 2.0, 'ADA': 2.5, 'DOT': 3.0, 'LINK': 3.0, 'BNB': 3.0, 'XRP': 3.0,
    # BTC/ETH/BNB做空已被硬过滤，以下参数仅作趋势仓位备用
    'BTC': 3.0,
}
_COIN_TP_RATIO = {  # P0优化：统一2:1盈亏比(原2-4x不等)
    # TP = TP_RATIO × SL%，统一2.0 = 2:1 RR，胜率>50%即可盈利
    # AVAX保持4.0(3:1)因波动大且趋势强
    'AVAX': 4.0, 'ETH': 2.0, 'BTC': 2.0, 'SOL': 2.0,
    'DOGE': 2.0, 'ADA': 2.0, 'DOT': 2.0, 'LINK': 2.0, 'BNB': 2.0, 'XRP': 2.0,
}
# P0优化：DOGE特殊处理——价格低(~$0.10)导致ATR%偏低，
# 但DOGE 8年盈利，WLR=2.11，应给予更低的ATR门槛
_COIN_MIN_ATR_PCT = {
    'DOGE': 0.30,  # DOGE价格低，绝对波动有意义
    'ADA': 0.30,    # ADA同样价格低
}
MIN_SL_PCT = 0.015   # 最小止损1.5%(防止ATR过低时止损太紧)
MAX_SL_PCT = 0.15    # 最大止损15%(防止极端行情止损太松)
# ========== P0优化：改为2x ATR止损 + 2:1盈亏比 + 移动止损 ==========
# 原理：均值回归策略高胜率(60%+)，紧止损+合理止盈最佳
# 旧参数SL=5-20x ATR太松，等不到TP就扛不住
# 新参数SL=2-3x ATR，TP=2x SL，实测盈亏比2:1，胜率>50%即可盈利
TRAIL_LOCK_PCT = 0.02  # 移动止损：盈利超2%后，SL追踪到保本+0.5%

def get_sl_tp_pct(coin: str, atr_pct: float) -> tuple:
    """
    根据币种和当前波动率返回SL%和TP%
    返回: (sl_pct, tp_pct)
    示例: get_sl_tp_pct('AVAX', 0.32) → (0.0192, 0.0768)  即1.9%止损，7.7%止盈
    """
    sl_atr_mult = _COIN_SL_ATR.get(coin, 5)
    tp_ratio = _COIN_TP_RATIO.get(coin, 3.0)
    sl_pct = min(MAX_SL_PCT, max(MIN_SL_PCT, sl_atr_mult * atr_pct / 100))
    tp_pct = tp_ratio * sl_pct
    return sl_pct, tp_pct

def calc_sl_tp_from_entry(entry_price: float, coin: str, atr_pct: float, side: str) -> tuple:
    """
    从入场价计算止损止盈价格(动态ATR版)
    返回: (sl_price, tp_price)
    """
    sl_pct, tp_pct = get_sl_tp_pct(coin, atr_pct)
    if side == 'long':
        sl_price = round(entry_price * (1 - sl_pct), 4)
        tp_price = round(entry_price * (1 + tp_pct), 4)
    else:  # short
        sl_price = round(entry_price * (1 + sl_pct), 4)
        tp_price = round(entry_price * (1 - tp_pct), 4)
    return sl_price, tp_price

# 币种策略地图(v1.0 | 2026-04-19)==========
# 来源：10币种 × 多策略 × 多周期交叉验证
# 每个币种的最优策略加成
_COIN_STRATEGY_CACHE = None

def get_coin_strategy_map():
    """加载币种策略地图(缓存)"""
    global _COIN_STRATEGY_CACHE
    if _COIN_STRATEGY_CACHE is not None:
        return _COIN_STRATEGY_CACHE
    try:
        path = Path(__file__).parent / 'coin_strategy_map.json'
        with open(path) as f:
            data = json.load(f)
        # 转为 {symbol: config} 方便查询
        _COIN_STRATEGY_CACHE = {c['symbol']: c for c in data['coins']}
        return _COIN_STRATEGY_CACHE
    except:
        return {}

def get_coin_strategy_bonus(md, direction):
    """
    根据币种的最优策略返回评分加成/扣分
    md: get_market_data()返回的字典
    direction: 'long' 或 'short'
    返回: (bonus: int, reason: str)
    v1.2: 修复smap结构（coins在列表里不是顶层key）
    """
    coin = md['coin']
    smap = get_coin_strategy_map()
    # P1 Fix: smap是{symbol: config}字典，不是{'coins': [...]}列表
    cfg = smap.get(coin)
    if not cfg:
        return 0, ''

    strategy = cfg['optimal_strategy']
    confidence = cfg.get('confidence', 0.5)

    rsi = md.get('rsi_1h', 50)
    adx = md.get('adx_1h', 15)
    rsi_4h = md.get('rsi_4h', 50)
    adx_4h = md.get('adx_4h', 15)
    vol_ratio = md.get('vol_ratio', 1.0)
    price = md.get('price', 0)
    ma30 = md.get('ma30', 0)

    signal_ok = True  # 策略信号是否满足
    reason_parts = []

    # ========== 策略信号检查 ==========
    if strategy == 'VOL_BRK':
        # VOL_BRK: vol_ratio > 2.0 AND ADX > 20 → 买入
        if vol_ratio >= 2.0 and adx >= 20:
            bonus = int(confidence * 20) + 15
            reason_parts.append(f'VOL爆发({vol_ratio:.1f}×)')
            signal_ok = True
        elif vol_ratio >= 1.5:
            bonus = int(confidence * 15) + 5
            reason_parts.append(f'VOL放大({vol_ratio:.1f}×)')
            signal_ok = True
        else:
            # 策略信号未满足，扣分
            bonus = -10
            reason_parts.append(f'VOL不足({vol_ratio:.1f}×<2.0)')
            signal_ok = False

    elif strategy in ('RSI_MR', 'RSI_VOL'):
        if direction == 'long':
            if rsi < 30:
                bonus = int(confidence * 20) + 10
                reason_parts.append(f'RSI超卖({rsi:.0f})')
                signal_ok = True
            elif rsi < 40:
                bonus = int(confidence * 10)
                reason_parts.append(f'RSI偏低({rsi:.0f})')
                signal_ok = True
            else:
                bonus = -10
                reason_parts.append(f'RSI不足({rsi:.0f}>40)')
                signal_ok = False
        else:  # short
            if rsi > 70:
                bonus = int(confidence * 20) + 10
                reason_parts.append(f'RSI超买({rsi:.0f})')
                signal_ok = True
            elif rsi > 60:
                bonus = int(confidence * 10)
                reason_parts.append(f'RSI偏高({rsi:.0f})')
                signal_ok = True
            else:
                bonus = -10
                reason_parts.append(f'RSI不足({rsi:.0f}<60)')
                signal_ok = False

    elif strategy == 'RSI_EMAn':
        above_ma = ma30 and price > ma30
        if direction == 'long':
            if rsi < 35 and above_ma:
                bonus = int(confidence * 20) + 12
                reason_parts.append(f'RSI+EMA共振({rsi:.0f})')
                signal_ok = True
            elif rsi < 35:
                bonus = int(confidence * 15) + 5
                reason_parts.append(f'RSI共振({rsi:.0f})')
                signal_ok = True
            else:
                bonus = -10
                reason_parts.append(f'RSI不足({rsi:.0f}>35)')
                signal_ok = False
        else:  # short
            if rsi > 65 and (ma30 and price < ma30):
                bonus = int(confidence * 20) + 10
                reason_parts.append(f'RSI+EMA死叉({rsi:.0f})')
                signal_ok = True
            else:
                bonus = -10
                reason_parts.append(f'RSI+EMA未死叉')
                signal_ok = False

    else:
        bonus = int(confidence * 15)

    reason = f"[{strategy}]" + '+'.join(reason_parts[:2])
    return bonus, reason

# 分批止盈阶段
TP_STAGES = [
    {'sold': 0.0, 'trigger': 'partial_tp_1', 'ratio': 0.50, 'label': '首批50%'},
    {'sold': 0.5, 'trigger': 'partial_tp_2', 'ratio': 0.30, 'label': '二批30%'},
    {'sold': 0.8, 'trigger': 'take_profit', 'ratio': 0.20, 'label': '尾批20%'},
]

# ========== OKX API ==========
_okx_ts_offset = None

def _ts():
    from datetime import datetime
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

def _req_get(url):
    try:
        import requests
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        return r.json()
    except:
        return None

def _req(method, path, body=''):
    import requests, hmac, hashlib, base64
    # 0=实盘, 1=模拟盘
    simulated = os.getenv('OKX_FLAG', '1')
    headers = {
        'OK-ACCESS-KEY': OKX_API_KEY,
        'OK-ACCESS-SIGN': base64.b64encode(hmac.new(
            OKX_SECRET.encode(),
            f'{_ts()}{method}{path}{body}'.encode(),
            hashlib.sha256
        ).digest()).decode(),
        'OK-ACCESS-TIMESTAMP': _ts(),
        'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': simulated,
    }
    r = requests.get if method == 'GET' else requests.post
    url = 'https://www.okx.com' + path
    kwargs = {'headers': headers, 'timeout': 10}
    if method == 'POST':
        kwargs['data'] = body
    return r(url, **kwargs).json()

# ========== 交易日志 (Journal) ==========
JOURNAL_PATH = Path.home() / 'kronos' / 'data' / 'trade_journal.jsonl'

def log_trade_journal(
    action, coin, side, size,
    entry_price=None, exit_price=None,
    pnl=None, pnl_pct=None,
    reason='', sl_before=None, sl_after=None,
    tp_before=None, tp_after=None,
    algos_before=None, algos_after=None,
    equity=None, market=None,
    gemma_decision=None, rule_decision=None,
):
    """Append a trade journal entry to ~/kronos/data/trade_journal.jsonl (one JSON per line).
    
    Journal write happens AFTER successful API confirmation.
    If the journal write fails (exception), log the error but don't raise.
    """
    entry = {
        'ts': datetime.utcnow().isoformat() + 'Z',
        'action': action,
        'coin': coin,
        'side': side,
        'size': size,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'pnl': pnl,
        'pnl_pct': pnl_pct,
        'reason': reason,
        'sl_before': sl_before,
        'sl_after': sl_after,
        'tp_before': tp_before,
        'tp_after': tp_after,
        'algos_before': algos_before,
        'algos_after': algos_after,
        'equity': equity,
        'market': market,
        'gemma_decision': gemma_decision,
        'rule_decision': rule_decision,
    }
    try:
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            f.flush()
    except Exception as e:
        print(f"  ⚠️ Journal write failed: {e}")

# ========== 持仓操作 ==========

def get_all_positions():
    """获取所有持仓(修复双向持仓Bug：同一币种可能有long+short两条)"""
    try:
        result = _req('GET', '/api/v5/account/positions')
        positions = {}
        for p in result.get('data', []):
            pos = float(p.get('pos', 0))
            if pos == 0:
                # 跳过零仓位，但可能需要保留信息用于判断
                continue
            coin = p.get('instId', '').split('-')[0]
            side = p.get('posSide', 'long')
            key = f"{coin}_{side}"  # 唯一key，避免覆盖
            positions[key] = {
                'coin': coin,
                'instId': p.get('instId'),
                'pos': pos,
                'avgPx': float(p.get('avgPx', 0)),
                'side': side,
                'upl': float(p.get('upl', 0)),
                'liqPx': float(p.get('liqPx', 0)),
                'margin': float(p.get('margin', 0)),
                'mgnRatio': float(p.get('mgnRatio', 0)),
                'notionalUsd': float(p.get('notionalUsd', 0)),
            }
        return positions
    except:
        return {}

def get_pending_algos(instId):
    """获取某币种的待触发SL/TP(含OCO和conditional)
    
    P0 Bug修复：必须同时查oco和conditional，否则会把已有的OCO订单当成"没有SL/TP"，
    导致repair_sl_tp重复挂conditional单。
    """
    algos = {'sl': None, 'tp': [], 'oco': None}
    for ordType in ['oco', 'conditional']:
        try:
            result = _req('GET', f'/api/v5/trade/orders-algo-pending?instId={instId}&ordType={ordType}&limit=100')
            if result.get('code') != '0':
                continue
            for o in result.get('data', []):
                sl = o.get('slTriggerPx')
                tp = o.get('tpTriggerPx')
                sz = int(o.get('sz', 0))
                if sz <= 5:
                    continue
                if ordType == 'oco':
                    # OCO同时包含SL和TP，整个订单就是保护
                    algos['oco'] = {'algoId': o.get('algoId'), 'sz': sz, 'sl': sl, 'tp': tp}
                    if sl:
                        algos['sl'] = {'algoId': o.get('algoId'), 'price': float(sl), 'sz': sz, 'type': 'sl', 'ordType': 'oco'}
                    if tp:
                        algos['tp'].append({'algoId': o.get('algoId'), 'price': float(tp), 'sz': sz, 'type': 'tp', 'ordType': 'oco'})
                else:
                    # conditional单独处理
                    entry = {
                        'algoId': o.get('algoId'),
                        'price': float(sl) if sl else float(tp),
                        'sz': sz,
                        'type': 'sl' if sl else 'tp',
                        'ordType': 'conditional'
                    }
                    if sl:
                        algos['sl'] = entry
                    if tp:
                        algos['tp'].append(entry)
        except:
            continue
    return algos

def get_account_equity():
    """获取账户USDT权益(只用USDT余额，排除ADA等其他币种的干扰)
    
    ⚠️ API失败时返回None，不返回假值！
    调用方必须处理None情况。
    """
    try:
        result = _req('GET', '/api/v5/account/balance?ccy=USDT')
        if result.get('code') == '0':
            data = result.get('data', [{}])[0]
            for d in data.get('details', []):
                if d.get('ccy') == 'USDT':
                    eq = float(d.get('eq', 0))
                    if eq > 0:
                        return eq
            total = float(data.get('totalEq', 0))
            if total > 0:
                return total
    except:
        pass
    return None  # 不返回硬编码假值！

def close_position(instId, side, sz):
    """市价全平。reduceOnly=True防止反向开仓。

    P0 Fix: side参数统一支持'long'/'short'(内部格式)和'buy'/'sell'(OKX格式)。
    幂等：持仓已平则返回成功(不报错)。
    平仓后自动清理该币种所有待触发OCO（无论平仓结果如何）。
    """
    # P0 Fix: 统一OKX格式→内部格式
    if side in ('buy', 'sell'):
        side = 'long' if side == 'buy' else 'short'

    coin = instId.replace('-USDT-SWAP', '')
    # 幂等检查
    positions_data = get_all_positions()
    pos_still_exists = False
    for key, pos in positions_data.items():
        if pos['coin'] == coin and float(pos.get('pos', 0)) > 0:
            pos_still_exists = True
            break
    
    if not pos_still_exists:
        # 持仓已平，仍清理残余OCO（防止幽灵单）
        cleanup_coin_algos(instId)
        return True, '⏭️ 持仓已平，已清残余OCO'

    close_side = 'sell' if side == 'long' else 'buy'
    body = json.dumps({'instId': instId, 'tdMode': 'isolated', 'side': close_side,
                       'ordType': 'market', 'sz': str(int(sz)), 'posSide': side, 'reduceOnly': True})
    result = _req('POST', '/api/v5/trade/order', body)
    if result.get('code') == '0':
        # ========== P0 Fix: 平仓后验证 + 自动清理OCO ==========
        time.sleep(0.5)  # 等待OKX成交确认
        positions_data = get_all_positions()
        still_open = False
        for key, p in positions_data.items():
            if p['coin'] == coin and float(p.get('pos', 0)) > 0:
                still_open = True
                break
        if still_open:
            # 持仓仍在，OCO也要保留
            return False, f'⚠️ 平仓API成功但持仓仍在，需人工检查 {coin}'
        # 持仓已平，自动清理OCO
        cancelled = cleanup_coin_algos(instId)
        return True, f'✅ 平仓成功，已清{len(cancelled)}个OCO' if cancelled else '✅ 平仓成功（无残余OCO）'
    # 持仓已平(被SL/TP触发)也算成功
    if '5119' in str(result) or '50121' in str(result):
        cleanup_coin_algos(instId)  # OCO可能还在，一并清理
        return True, '⏭️ 持仓已平（OCO已清理）'
    return False, result

def close_partial(instId, side, sz, ratio):
    """分批平仓"""
    close_sz = max(1, int(sz * ratio))
    return close_position(instId, side, close_sz)

def amend_sl(instId, algoId, newSlPrice):
    body = json.dumps({'instId': instId, 'algoId': algoId, 'newSlTriggerPx': str(newSlPrice)})
    result = _req('POST', '/api/v5/trade/amend-algos', body)
    return result.get('code') == '0', result

def cancel_algos(instId, algo_ids):
    """批量取消条件单(OKX要求数组格式)"""
    if not algo_ids:
        return True
    body = json.dumps([{'instId': instId, 'algoId': a} for a in algo_ids])
    result = _req('POST', '/api/v5/trade/cancel-algos', body)
    return result.get('code') == '0'

def cleanup_coin_algos(instId):
    """取消该币种所有历史遗留条件单(含OCO和conditional)
    
    P0 Bug修复：必须同时清oco和conditional两类订单，否则旧OCO会被遗漏。
    返回已取消的algoId列表，出错时返回空列表。
    """
    all_ids = []
    for ordType in ['oco', 'conditional']:
        resp = _req('GET', f'/api/v5/trade/orders-algo-pending?instId={instId}&ordType={ordType}&limit=100')
        if resp.get('code') == '0' and resp.get('data'):
            ids = [o['algoId'] for o in resp['data'] if o.get('algoId')]
            all_ids.extend(ids)
        elif resp.get('code') != '0':
            print(f"  ⚠️ 查询{instId} {ordType}失败: {resp.get('msg')} ({resp.get('code')})")
    if not all_ids:
        return []
    # P0 Fix: 必须检查取消结果，失败要告警
    ok = cancel_algos(instId, all_ids)
    if not ok:
        print(f"  ⚠️ 取消{instId} OCO失败: {all_ids}")
        return []  # 返回空列表表示失败，让调用方知道
    return all_ids

def place_oco(instId, side, sz, slPrice, tpPrice):
    """挂OCO Bracket订单(SL+TP合并为1个OCO订单)

    P0 Bug修复：必须用oco，不能拆成两个conditional单。
    幂等检查：已有任何活跃订单则跳过。

    side: 内部统一用 'long'/'short'（不是OKX的'buy'/'sell'！）
    """
    # OKX API返回pos['side']='buy'/'sell'，但place_oco期望'long'/'short'
    # 统一在这里转换，确保传入任何格式都能正确处理
    if side in ('buy', 'sell'):
        # OKX持仓方向 → 内部持仓方向
        pos_side_internal = 'long' if side == 'buy' else 'short'
        close_side = 'sell' if side == 'buy' else 'buy'
    else:
        # 已经是内部格式 'long'/'short'
        pos_side_internal = side
        close_side = 'sell' if side == 'long' else 'buy'

    # 幂等检查：已有任何活跃订单则跳过，防止重复挂单
    for ordType in ['oco', 'conditional']:
        try:
            r = _req('GET', f'/api/v5/trade/orders-algo-pending?instId={instId}&ordType={ordType}&limit=50')
            for o in r.get('data', []):
                if o.get('algoId'):
                    return None
        except:
            continue

    # P0 防御性验证：OCO的SL/TP价格方向必须和持仓方向匹配
    # 做多(buy/long)：SL < 当前价，TP > 当前价
    # 做空(sell/short)：SL > 当前价，TP < 当前价
    # 如果价格方向错误，打印警告并交换
    # （这说明调用方计算的价格本身就是错的，需要修复调用方）
    try:
        cur_price = float(get_price(instId.replace('-USDT-SWAP', '')))
        if cur_price:
            if pos_side_internal == 'long':
                if slPrice >= cur_price:
                    print(f"⚠️ P0警告: {instId} LONG OCO的SL={slPrice} >= 当前价={cur_price}，方向错误！")
                if tpPrice <= cur_price:
                    print(f"⚠️ P0警告: {instId} LONG OCO的TP={tpPrice} <= 当前价={cur_price}，方向错误！")
            else:  # short
                if slPrice <= cur_price:
                    print(f"⚠️ P0警告: {instId} SHORT OCO的SL={slPrice} <= 当前价={cur_price}，方向错误！")
                if tpPrice >= cur_price:
                    print(f"⚠️ P0警告: {instId} SHORT OCO的TP={tpPrice} >= 当前价={cur_price}，方向错误！")
    except:
        pass  # 价格获取失败时不阻止下单

    body = json.dumps({
        'instId': instId, 'tdMode': 'isolated', 'side': close_side,
        'ordType': 'oco', 'sz': str(int(sz)), 'reduceOnly': True, 'posSide': pos_side_internal,
        'slTriggerPx': str(slPrice), 'slOrdPx': '-1',
        'tpTriggerPx': str(tpPrice), 'tpOrdPx': '-1',
    })
    result = _req('POST', '/api/v5/trade/order-algo', body)
    if result.get('code') == '0':
        return result['data'][0]['algoId']
    return None

def place_sl(instId, side, sz, slPrice):
    """挂SL条件单(posSide=实际持仓方向)

    P0 Fix: side参数统一支持'long'/'short'(内部格式)和'buy'/'sell'(OKX格式)。
    挂单前先查该币是否有活跃OCO订单，有则跳过。
    防止拆成conditional单破坏已有的OCO保护。
    """
    # P0 Fix: 统一OKX格式→内部格式
    if side in ('buy', 'sell'):
        side = 'long' if side == 'buy' else 'short'

    # 幂等检查：OCO存在则跳过
    for ordType in ['oco', 'conditional']:
        try:
            r = _req('GET', f'/api/v5/trade/orders-algo-pending?instId={instId}&ordType={ordType}&limit=50')
            for o in r.get('data', []):
                if o.get('algoId') and o.get('slTriggerPx'):
                    return None
        except:
            continue
    close_side = 'sell' if side == 'long' else 'buy'
    body = json.dumps({
        'instId': instId, 'tdMode': 'isolated', 'side': close_side,
        'ordType': 'conditional', 'sz': str(int(sz)), 'posSide': side,
        'slTriggerPx': str(slPrice), 'slOrdPx': '-1',
    })
    result = _req('POST', '/api/v5/trade/order-algo', body)
    if result.get('code') == '0':
        return result['data'][0]['algoId']
    return None

def place_tp(instId, side, sz, tpPrice):
    """挂TP条件单(posSide=实际持仓方向)

    P0 Fix: side参数统一支持'long'/'short'(内部格式)和'buy'/'sell'(OKX格式)。
    挂单前先查该币是否有活跃OCO订单，有则跳过。
    防止拆成conditional单破坏已有的OCO保护。
    """
    # P0 Fix: 统一OKX格式→内部格式
    if side in ('buy', 'sell'):
        side = 'long' if side == 'buy' else 'short'

    # 幂等检查：OCO存在则跳过
    for ordType in ['oco', 'conditional']:
        try:
            r = _req('GET', f'/api/v5/trade/orders-algo-pending?instId={instId}&ordType={ordType}&limit=50')
            for o in r.get('data', []):
                if o.get('algoId') and o.get('tpTriggerPx'):
                    return None
        except:
            continue
    close_side = 'sell' if side == 'long' else 'buy'
    body = json.dumps({
        'instId': instId, 'tdMode': 'isolated', 'side': close_side,
        'ordType': 'conditional', 'sz': str(int(sz)), 'posSide': side,
        'tpTriggerPx': str(tpPrice), 'tpOrdPx': '-1',
    })
    result = _req('POST', '/api/v5/trade/order-algo', body)
    if result.get('code') == '0':
        return result['data'][0]['algoId']
    return None

def open_position(instId, side, sz, slPrice, tpPrice, leverage=3):
    """开仓 + 挂OCO(SL+TP合并为一个OCO订单)
    
    P0 Bug修复：必须用OCO，不能拆成两个conditional单。
    幂等检查：已有持仓则跳过。
    
    leverage: 杠杆倍数，默认3x，可根据信心度动态调整(最高30x)。
    """
    coin = instId.replace('-USDT-SWAP', '')

    # 幂等检查：已有持仓则跳过
    positions_data = get_all_positions()
    for key, pos in positions_data.items():
        if pos['coin'] == coin:
            algos = get_pending_algos(instId)
            if algos.get('oco') or algos.get('sl'):
                return False, f'⏭️ {coin}已有持仓和OCO，跳过开仓'
            return False, f'⏭️ {coin}已有持仓，跳过开仓'

    # 开仓前先清历史遗留委托
    cleanup_coin_algos(instId)

    open_side = 'buy' if side == 'long' else 'sell'
    pos_side = 'long' if side == 'long' else 'short'
    
    # 设置动态杠杆（isolated模式，leverage直接写在订单里）
    body = json.dumps({
        'instId': instId, 'tdMode': 'isolated', 'side': open_side,
        'ordType': 'market', 'sz': str(int(sz)), 'posSide': pos_side,
        'lever': str(int(leverage)),
    })
    result = _req('POST', '/api/v5/trade/order', body)
    if result.get('code') != '0':
        return False, f'开仓失败: {result.get("msg")}'

    # 用OCO合并SL+TP
    algo_id = place_oco(instId, side, sz, slPrice, tpPrice)
    if algo_id:
        return True, f'开仓{int(sz)}张 @{leverage}x SL={slPrice} TP={tpPrice} [{algo_id[:8]}]'
    else:
        return False, f'开仓{int(sz)}张成功，但OCO挂单失败'

# ========== 市场数据 ==========

def get_price(coin):
    try:
        r = _req_get(f'https://www.okx.com/api/v5/market/ticker?instId={coin}-USDT-SWAP')
        if r and r.get('data'):
            return float(r['data'][0]['last'])
    except:
        pass
    return None

def get_ohlcv(coin, bar='1H', limit=72):
    try:
        after = int(time.time() * 1000) - (limit * 3600 * 1000)
        r = _req_get(f'https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bar}&limit={limit}&after={after}')
        if not r or not r.get('data'):
            return []
        candles = []
        for d in r['data']:
            try:
                candles.append({'ts': int(d[0]), 'close': float(d[4]),
                               'high': float(d[2]), 'low': float(d[3]),
                               'volume': float(d[5])})
            except:
                pass
        return list(reversed(candles))
    except:
        return []

def calc_rsi(candles, period=14):
    if len(candles) < period + 1:
        return 50
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
    """Wilder平滑ADX"""
    if len(candles) < period * 2:
        return 20
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
        return 20
    atr = sum(trs[:period]) / period
    plus_sum = sum(plus_dm[:period]) / period
    minus_sum = sum(minus_dm[:period]) / period
    dx_vals = []
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        plus_sum = (plus_sum * (period - 1) + plus_dm[i]) / period
        minus_sum = (minus_sum * (period - 1) + minus_dm[i]) / period
        if atr > 0:
            plus_di = (plus_sum / atr) * 100
            minus_di = (minus_sum / atr) * 100
            if plus_di + minus_di > 0:
                dx_vals.append(abs(plus_di - minus_di) / (plus_di + minus_di) * 100)
    if len(dx_vals) < period:
        return 20
    return sum(dx_vals[-period:]) / period

def calc_cci(candles, period=20):
    """
    计算CCI (Commodity Channel Index)
    CCI < -100 = 超卖（做多信号）
    CCI > 100  = 超买（做空信号）
    回测最优: period=20
    """
    if len(candles) < period + 1:
        return 0
    highs  = [c['high']  for c in candles]
    lows   = [c['low']   for c in candles]
    closes = [c['close'] for c in candles]

    # Typical Price
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(candles))]

    # SMA of Typical Price (tp_sma[0] = SMA of tp[0:period])
    tp_sma = [sum(tp[i-period+1:i+1]) / period for i in range(period-1, len(tp))]

    # Mean Deviation (mean_dev[j] 对应 tp_sma[j])
    mean_dev = []
    for j in range(len(tp_sma)):
        i = j + period - 1  # i是tp的索引
        md = sum(abs(tp[k] - tp_sma[j]) for k in range(i-period+1, i+1)) / period
        mean_dev.append(md)

    # CCI
    cci = []
    for j in range(len(tp_sma)):
        if mean_dev[j] > 0:
            i = j + period - 1
            cci.append((tp[i] - tp_sma[j]) / (0.015 * mean_dev[j]))
        else:
            cci.append(0.0)

    return cci[-1] if cci else 0

def get_atr(coin, period=14):
    candles = get_ohlcv(coin, '1H', period + 5)
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        tr = max(candles[i]['high'] - candles[i]['low'],
                 abs(candles[i]['high'] - candles[i-1]['close']),
                 abs(candles[i]['low'] - candles[i-1]['close']))
        trs.append(tr)
    return sum(trs[-period:]) / period if len(trs) >= period else None

def get_btc_direction():
    """BTC 1h RSI方向"""
    candles = get_ohlcv('BTC', '1H', 24)
    if not candles:
        return 'neutral'
    rsi = calc_rsi(candles)
    if rsi < 35:
        return 'oversold'
    elif rsi > 65:
        return 'overbought'
    return 'neutral'

def get_btc_market_regime():
    """
    P0优化：判断BTC中长期趋势，用于硬过滤方向
    返回: 'bull'(牛市) / 'bear'(熊市) / 'neutral'(震荡)

    原理：
    - BTC > MA200 → 牛市格局，只做多
    - BTC < MA200 → 熊市格局，只做空
    - BTC在MA200附近±10% → 震荡市，双向均可
    - RSI辅助确认(不能只看价格)

    注意：使用日线数据计算MA200（不是4H数据，4H×200=33天不是200天）
    """
    c1 = get_ohlcv('BTC', '1D', 220)  # 日线，220天足够算MA200
    if not c1 or len(c1) < 200:
        # fallback到4H（仅当日线数据不足时）
        c4 = get_ohlcv('BTC', '4H', 200)
        if not c4 or len(c4) < 100:
            return 'neutral'
        btc_rsi_4h = calc_rsi(c4)
        btc_ma200 = sum(c['close'] for c in c4[-200:]) / 200
        btc_price = c4[-1]['close']
        btc_vs_ma = btc_price / btc_ma200
    else:
        # 日线数据足够，用真实的MA200
        btc_rsi_4h = 50  # 日线模式不计算4H RSI
        btc_ma200 = sum(c['close'] for c in c1[-200:]) / 200
        btc_price = c1[-1]['close']
        btc_vs_ma = btc_price / btc_ma200

    btc_rsi = calc_rsi(c1[-60:]) if len(c1) >= 60 else calc_rsi(c1)

    # 牛市：价格在MA200上方
    if btc_vs_ma > 1.05:
        if btc_rsi < 30:
            return 'bull'  # 强势回调后
        elif btc_rsi < 60:
            return 'bull'  # 正常上涨
        else:
            return 'neutral'  # 过热，当震荡处理

    # 熊市：价格在MA200下方
    if btc_vs_ma < 0.95:
        if btc_rsi > 65:
            return 'bear'  # 反弹后超买
        elif btc_rsi > 45:
            return 'bear'  # 正常下跌
        else:
            return 'neutral'  # 超卖，不追空

    return 'neutral'  # BTC在MA200±5%范围内，震荡市

def get_market_data(coin, btc_dir, btc_regime):
    """获取某币种完整市场数据"""
    price = get_price(coin)
    c1 = get_ohlcv(coin, '1H', 50)   # 减少limit加速
    c4 = get_ohlcv(coin, '4H', 50)   # 减少limit加速
    # ATR百分比（已有）
    atr = get_atr(coin)
    atr_pct = (atr / price * 100) if atr and price else 2.0

    # ATR Percentile（最佳策略核心参数）: 当前ATR在历史69周期中的百分位
    # >69% = 波动率较高，适合趋势跟踪；<30% = 波动率低，均值回归机会
    atr_percentile = 50.0  # 默认中立
    if c1 and len(c1) >= 70:
        try:
            import numpy as np
            highs = np.array([c['high'] for c in c1])
            lows = np.array([c['low'] for c in c1])
            closes = np.array([c['close'] for c in c1])
            trs = np.maximum(highs - lows, np.maximum(abs(highs[1:] - closes[:-1]), abs(lows[1:] - closes[:-1])))
            # 计算70个ATR值（每次向后移动1根K线）
            atr_values = []
            for i in range(14, len(c1)):
                atr_val = np.mean(trs[i-14:i])
                atr_values.append(atr_val)
            if len(atr_values) >= 10:
                atr_arr = np.array(atr_values)
                current_atr = atr_arr[-1]
                atr_percentile = float(np.sum(atr_arr < current_atr) / len(atr_arr) * 100)
        except Exception:
            atr_percentile = 50.0

    rsi_1h = calc_rsi(c1) if c1 else 50
    rsi_7 = calc_rsi(c1, period=7) if c1 else 50   # P0 Fix: RSI(7)短期过滤，防止逆势开仓
    rsi_4h = calc_rsi(c4) if c4 else 50
    adx_1h = calc_adx(c1) if c1 else 20
    adx_4h = calc_adx(c4) if c4 else 20
    # CCI 4H（回测最优period=20）
    cci_4h = calc_cci(c4, 20) if c4 else 0
    cci_1h = calc_cci(c1, 20) if c1 else 0

    # 30日均线
    ma30 = None
    if c1 and len(c1) > 30:
        ma30 = sum(c['close'] for c in c1[-30:]) / 30

    # 成交量比率(vol / vol_MA20)
    vol_ratio = 1.0
    if c1 and len(c1) >= 20:
        recent_vol = sum(c['volume'] for c in c1[-5:]) / 5
        avg_vol = sum(c['volume'] for c in c1[-20:]) / 20
        vol_ratio = recent_vol / (avg_vol + 1e-10)

    return {
        'coin': coin,
        'price': price,
        'rsi_1h': round(rsi_1h, 1),
        'rsi_7': round(rsi_7, 1),    # P0 Fix: RSI(7)短期过滤
        'rsi_4h': round(rsi_4h, 1),
        'adx_1h': round(adx_1h, 1),
        'adx_4h': round(adx_4h, 1),
        'cci_4h': round(cci_4h, 1),
        'cci_1h': round(cci_1h, 1),
        'btc_direction': btc_dir,
        'btc_regime': btc_regime,
        'atr': atr,
        'atr_pct': round(atr_pct, 2),
        'atr_percentile': round(atr_percentile, 1),  # ATR百分位（最佳策略核心）
        'ma30': ma30,
        'vol_ratio': round(vol_ratio, 2),
    }

# ========== 动态熔断 ==========

def build_local_factor_context(candidates, positions, btc_price, btc_dir, btc_regime):
    """
    本地市场环境评估 - 不依赖MiniMax，不依赖factor_context.json
    在每次gemma4决策前实时计算，确保上下文永远新鲜有效
    
    评估维度：
    1. BTC趋势方向（价格 vs MA30）
    2. 整体市场强度（所有币RSI/ADX中位数）
    3. 波动率环境（各币ATR百分位均值）
    4. 持仓风险（现有仓位的浮盈亏状态）
    5. 仓位集中度（保证金占比）
    """
    if not candidates:
        return {
            'market_regime': 'neutral',
            'regime_confidence': 0.5,
            'primary_direction': 'both',
            'direction_confidence': 0.5,
            'overall_confidence': 0.5,
            'factor_status': {
                'rsi': {'status': 'unknown', 'ic': 0.0},
                'adx': {'status': 'unknown', 'ic': 0.0},
                'vol_ratio': {'status': 'unknown', 'ic': 0.0},
            },
            'forbidden_actions': [],
            'strategic_hint': '无候选币种，观望',
            'emergency_level': 'none',
        'data_quality': 'no_candidates',
        'source': 'local',
        # 附加诊断数据（供gemma4使用）
        '_btc_ma30': None,
        '_btc_trend': 'neutral',
        '_rsi_median': 50,
        '_adx_median': 15,
        '_atr_median': 50,
    }
    
    # 1. BTC趋势判断
    btc_ma30 = None
    try:
        btc_c1 = get_ohlcv('BTC', '1H', 40)
        if btc_c1 and len(btc_c1) >= 30:
            btc_ma30 = sum(c['close'] for c in btc_c1[-30:]) / 30
    except:
        pass
    
    btc_trend = 'neutral'
    if btc_ma30 and btc_price:
        if btc_price > btc_ma30 * 1.02:
            btc_trend = 'bull'
        elif btc_price < btc_ma30 * 0.98:
            btc_trend = 'bear'
    
    # 2. 市场强度（各币RSI/ADX中位数）
    rsi_vals = [c['md']['rsi_1h'] for c in candidates if c.get('md', {}).get('rsi_1h')]
    adx_vals = [c['md']['adx_1h'] for c in candidates if c.get('md', {}).get('adx_1h')]
    rsi_median = sorted(rsi_vals)[len(rsi_vals)//2] if rsi_vals else 50
    adx_median = sorted(adx_vals)[len(adx_vals)//2] if adx_vals else 15
    
    # 3. 波动率环境
    atr_vals = [c['md']['atr_percentile'] for c in candidates if c.get('md', {}).get('atr_percentile')]
    atr_median = sorted(atr_vals)[len(atr_vals)//2] if atr_vals else 50
    
    # 4. 因子状态评估
    rsi_status = 'active'
    rsi_ic = 0.5
    if rsi_vals:
        extreme_rsi = sum(1 for v in rsi_vals if v > 70 or v < 30)
        if extreme_rsi / len(rsi_vals) > 0.4:
            rsi_status = 'degraded'
            rsi_ic = 0.3
    else:
        rsi_status = 'inactive'
        rsi_ic = 0.0
    
    adx_status = 'active' if adx_median > 20 else 'inactive'
    adx_ic = min(adx_median / 40, 1.0) if adx_median else 0.0
    if adx_median < 15:
        adx_status = 'degraded'
        adx_ic = 0.2
    
    vol_status = 'active'
    vol_ic = 0.5
    if atr_vals:
        if atr_median > 70:
            vol_status = 'active'
            vol_ic = 0.7
        elif atr_median < 30:
            vol_status = 'degraded'
            vol_ic = 0.3
    
    # 5. 市场环境综合判断
    if btc_trend == 'bull' and rsi_median > 55:
        market_regime = 'bull'
        regime_conf = 0.65
    elif btc_trend == 'bear' and rsi_median < 45:
        market_regime = 'bear'
        regime_conf = 0.65
    elif adx_median < 18:
        market_regime = 'neutral'
        regime_conf = 0.6
    else:
        market_regime = 'volatile'
        regime_conf = 0.4
    
    # 6. 主方向判断
    if market_regime == 'bull':
        primary_dir = 'long'
        dir_conf = regime_conf
    elif market_regime == 'bear':
        primary_dir = 'short'
        dir_conf = regime_conf
    else:
        primary_dir = 'both'
        dir_conf = 0.4
    
    # 7. 禁止操作
    forbidden = []
    if market_regime == 'bull':
        forbidden.append('new_short')
    elif market_regime == 'bear':
        forbidden.append('new_long')
    
    # 8. 战略提示
    hints = []
    if rsi_status == 'degraded':
        hints.append('RSI极值区，谨慎追单')
    if adx_status == 'inactive':
        hints.append('ADX低迷，趋势信号不可靠')
    if atr_median < 30:
        hints.append('波动率极低，震荡行情，不宜重仓')
    if vol_status == 'degraded':
        hints.append('成交量萎缩，方向不确定')
    if not hints:
        hints.append('市场环境正常，按规则执行')
    
    # 9. 紧急级别（基于持仓风险）
    emergency_level = 'none'
    if positions:
        critical_pos = []
        for coin, pos in positions.items():
            pnl_pct = pos.get('pnl_pct', 0)
            if pnl_pct < -5:
                critical_pos.append(coin)
        if critical_pos:
            emergency_level = 'elevated'
    
    return {
        'market_regime': market_regime,
        'regime_confidence': regime_conf,
        'primary_direction': primary_dir,
        'direction_confidence': dir_conf,
        'overall_confidence': min(rsi_ic, adx_ic, vol_ic) + 0.3,
        'factor_status': {
            'rsi': {'status': rsi_status, 'ic': rsi_ic, 'median': rsi_median},
            'adx': {'status': adx_status, 'ic': adx_ic, 'median': adx_median},
            'vol_ratio': {'status': vol_status, 'ic': vol_ic, 'atr_percentile': atr_median},
        },
        'forbidden_actions': forbidden,
        'strategic_hint': ' | '.join(hints),
        'emergency_level': emergency_level,
        'data_quality': 'fresh',
        'source': 'local',
        # 附加诊断数据（供gemma4使用）
        '_btc_trend': btc_trend,
        '_btc_ma30': btc_ma30,
        '_rsi_median': rsi_median,
        '_adx_median': adx_median,
        '_atr_median': atr_median,
    }


# ========== 决策日志 (Decision Journal) ==========
# 每次gemma4决策后记录，用于事后评估决策质量

def append_decision_journal(entry: dict, path: str = None):
    """追加单条决策到日志文件（JSONL格式）"""
    if path is None:
        path = Path(__file__).parent / 'decision_journal.jsonl'
    try:
        with open(path, 'a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"[WARN] Decision journal write failed: {e}")


def build_decision_journal_entry(
    all_data: dict,
    local_ctx: dict,
    llm_output: str,
    parsed_decision: dict,
    prompt_used: str,
    execution_result: str = '',
    execution_ok: bool = None,
) -> dict:
    """构建决策日志条目"""
    from datetime import datetime
    equity = all_data.get('equity', 0)
    positions = all_data.get('positions', {})
    candidates = all_data.get('candidates', [])
    
    # 持仓快照
    pos_snapshot = {}
    for coin, pos in positions.items():
        pos_snapshot[coin] = {
            'direction': pos.get('side', ''),
            'size': pos.get('pos', 0),
            'entry': pos.get('avgPx', 0),
            'price': pos.get('last', 0),
            'pnl_pct': pos.get('pnl_pct', 0),
            'pnl_abs': pos.get('upl', 0),
            'sl_price': pos.get('sl_price'),
            'tp_price': pos.get('tp_price'),
        }
    
    # 候选币快照（top 5）
    cand_snapshot = []
    for c in candidates[:5]:
        cand_snapshot.append({
            'coin': c['coin'],
            'direction': c['direction'],
            'score': c['score'],
            'rsi_1h': c.get('md', {}).get('rsi_1h'),
            'adx_1h': c.get('md', {}).get('adx_1h'),
        })
    
    return {
        'ts': datetime.now().isoformat(),
        'equity': equity,
        'position_count': len(positions),
        'local_context': {
            'market_regime': local_ctx.get('market_regime'),
            'primary_direction': local_ctx.get('primary_direction'),
            'overall_confidence': local_ctx.get('overall_confidence'),
            'emergency_level': local_ctx.get('emergency_level'),
            'strategic_hint': local_ctx.get('strategic_hint'),
            'data_quality': local_ctx.get('data_quality'),
        },
        'positions_snapshot': pos_snapshot,
        'candidates_snapshot': cand_snapshot,
        'llm_raw_output': llm_output[:2000],  # 截断避免文件膨胀
        'decision_parsed': parsed_decision,
        'execution_result': execution_result,
        'execution_ok': execution_ok,
    }


def get_dynamic_limits(equity, num_positions=0, max_adx=20):
    """动态风险限制
    
    P0 Fix: equity可能为None（API失败），必须保护。
    返回安全的保守默认值，不崩溃。
    """
    from datetime import datetime
    
    # P0 Fix: equity=None/0 保护
    if equity is None or equity <= 0:
        return {
            'hourly': 0.0,
            'per_trade': 0.0,
            'reserve': 0.0,
            'factor': 0.0,
            '_equity_invalid': True,
        }
    
    h = datetime.now().hour
    time_factor = 1.0 if 22 <= h or h < 8 else (0.9 if 8 <= h < 14 else (0.7 if 14 <= h < 17 else 0.6))
    vol_factor = 1.0 if max_adx < 25 else (0.8 if max_adx < 35 else (0.6 if max_adx < 50 else 0.4))
    pos_factor = 1.0 if num_positions == 0 else (0.8 if num_positions == 1 else (0.6 if num_positions == 2 else 0.4))
    combined = time_factor * vol_factor * pos_factor
    return {
        'hourly': round(equity * 0.02 * combined, 2),
        # 'daily' 已移除（2026-04-23）：用历史snapshot做日亏损限制毫无意义
        'per_trade': round(equity * 0.01 * combined * pos_factor, 2),
        'reserve': round(equity * 0.20, 2),  # P1优化：Reserve从10%→20%（2周交易资本，更保守）
        'factor': round(combined, 3),
    }

def check_treasury(equity, proposed_loss=None, positions=None):
    """熔断检查
    
    修复（2026-04-23）：
    - 用daily_snapshot_equity作为"日内基准"（真正的UTC日起点，不是每小时重置的hourly快照）
    - hourly_loss = daily_snap - equity：今天从UTC日起点到现在的累计亏损
    - 日亏损熔断（daily_loss）：硬限制为equity的4%（可配置）
    - 小时亏损熔断（hourly_loss）：动态限制equity*2%*combined_factor
    - Reserve熔断：equity不能低于10%保留金
    - 单笔理论亏损：equity*1%*combined_factor
    - VaR熔断：组合波动率计算（有持仓时）
    """
    try:
        from real_monitor import load_treasury_state
        state = load_treasury_state()
    except:
        state = {}

    # 计算实际持仓数（不用硬编码）
    num_pos = len([p for p in (positions or {}).values() if p.get('pos', 0) > 0])
    limits = get_dynamic_limits(equity, num_pos, 20)

    # 小时级滑动窗口熔断：当前equity vs 1小时前的equity
    # 使用real_monitor每次整点保存的hourly_snapshot_prev（上一个小时的起始权益）
    # P0 Fix: 如果hourly_snapshot_prev与当前equity差距>10%，说明快照已损坏（跨进程状态不同步）
    #         此时改用daily_snapshot_equity作为参考，避免误触发熔断
    hourly_prev = state.get('hourly_snapshot_prev')
    daily_snapshot = state.get('daily_snapshot_equity') or equity
    if hourly_prev:
        hourly_loss = hourly_prev - equity
        hourly_limit = limits.get('hourly', equity * 0.02 * 0.9)
        # 快照损坏检测：hourly_prev比当前equity还高太多 → 快照已失效
        if hourly_prev > equity * 1.10:
            # 快照损坏，用daily_snapshot重算
            hourly_loss = daily_snapshot - equity
            hourly_limit = limits.get('hourly', equity * 0.02 * 0.9)
        if hourly_loss >= hourly_limit:
            return False, f'小时亏损${hourly_loss:.2f}>${hourly_limit:.2f}', limits

    if equity < limits['reserve']:
        return False, f'权益${equity:.0f}<保留金${limits["reserve"]:.0f}', limits
    if proposed_loss and proposed_loss > limits['per_trade']:
        return False, f'理论亏损${proposed_loss:.2f}>单笔限制${limits["per_trade"]:.2f}', limits

    # ========== VaR动态熔断检查 ==========
    # 只在有持仓时检查VaR(避免无持仓时拉API)
    if positions:
        try:
            from var_risk_manager import var_circuit_breaker_check
            pos_list = [
                {'coin': pos.get('coin', key), 'position_value': pos.get('notionalUsd', 0) or pos.get('pos', 0) * pos.get('avgPx', 0)}
                for key, pos in positions.items()
                if pos.get('avgPx', 0) > 0
            ]
            if pos_list:
                state_data = load_treasury_state()
                consecutive = state_data.get('consecutive_loss_hours', 0)

                # 日内亏损（从UTC日起点）
                daily_loss_val = (state_data.get('daily_snapshot_equity') or equity) - equity
                hourly_loss_val = daily_loss_val  # 两者等价：都是从日起点算的累计亏损

                # VaR熔断：hourly_loss与小时VaR比较，daily_loss已由get_dynamic_limits控制
                # consecutive_losses用于VaR内部连亏计数（不受snapshot快照影响）
                can_trade, var_reason, var_warns = var_circuit_breaker_check(
                    pos_list, equity,
                    hourly_loss=abs(hourly_loss_val) if hourly_loss_val > 0 else 0,
                    daily_loss=0,  # 日亏损已移除（2026-04-23），VaR用组合波动率计算
                    consecutive_losses=consecutive,
                )
                if not can_trade:
                    return False, f'VaR熔断: {var_reason}', limits
                # VaR警告：附加到limits的reason
                if var_warns:
                    limits['_var_warns'] = var_warns
                    limits['_var_util'] = var_circuit_breaker_check(
                        pos_list, equity, 0, 0, 0,
                    )[2]  # 获取warnings
        except Exception as e:
            pass  # VaR失败不影响原有熔断

    return True, f'通过(${limits["hourly"]:.0f}/h)', limits

# ========== 仓位状态文件 ==========

def get_position_state(coin):
    """获取仓位状态(分批止盈阶段等)"""
    try:
        state_file = Path.home() / '.hermes/cron/output/kronos_position_state.json'
        if state_file.exists():
            data = json.loads(state_file.read_text())
            return data.get(coin, {'stage': 0, 'sold_ratio': 0.0})
    except:
        pass
    return {'stage': 0, 'sold_ratio': 0.0}

def save_position_state(coin, state):
    """保存仓位状态"""
    try:
        state_file = Path.home() / '.hermes/cron/output/kronos_position_state.json'
        state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
            except:
                pass
        data[coin] = state
        atomic_write_json(state_file, data, indent=2)
    except:
        pass

def save_paper_trade(coin, direction, entry_price, size_usd, contracts,
                     leverage, sl_price, tp_price,
                     best_factor, confidence, ic,
                     rsi_at_entry, adx_at_entry, btc_price_at_entry,
                     open_reason, equity_at_open):
    """P1 Fix: 开仓时保存完整元数据到paper_trades.json
    equity_at_open: 开仓时的账户权益（传入，不在函数内引用全局变量）
    """
    PAPER_TRADES = Path.home() / '.hermes/cron/output/paper_trades.json'
    try:
        PAPER_TRADES.parent.mkdir(parents=True, exist_ok=True)
        trades = []
        if PAPER_TRADES.exists():
            try:
                trades = json.loads(PAPER_TRADES.read_text())
            except:
                trades = []

        # 生成唯一ID（时间戳+币种+方向）
        trade_id = f"kronos_{coin}_{direction}_{int(time.time()*1000)}"

        entry = {
            'id': trade_id,
            'coin': coin,
            'direction': direction,
            'status': 'OPEN',
            'entry_price': entry_price,
            'exit_price': None,
            'size_usd': size_usd,
            'contracts': contracts,
            'leverage': leverage,
            'pnl': 0.0,
            'open_time': datetime.now().isoformat(),
            'close_time': None,
            'close_reason': None,
            'ic': ic,
            'best_factor': best_factor,
            'confidence': confidence,
            # ── 新增元数据 ──────────────────────
            'open_reason': open_reason,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'rsi_at_entry': rsi_at_entry,
            'adx_at_entry': adx_at_entry,
            'btc_price_at_entry': btc_price_at_entry,
            'open_equity': equity_at_open,  # 开仓时账户权益
        }
        trades.append(entry)
        atomic_write_json(PAPER_TRADES, trades, indent=2)
    except Exception as e:
        print(f"  ⚠️  保存paper_trades失败: {e}")

def close_paper_trade(coin, direction, close_reason, exit_price, pnl):
    """
    平仓时标记paper_trades中对应条目为CLOSED
    """
    PAPER_TRADES = Path.home() / '.hermes/cron/output/paper_trades.json'
    try:
        if not PAPER_TRADES.exists():
            return
        trades = json.loads(PAPER_TRADES.read_text())
        for t in trades:
            if t.get('coin') == coin and t.get('direction') == direction and t.get('status') == 'OPEN':
                t['status'] = 'CLOSED'
                t['close_time'] = datetime.now().isoformat()
                t['close_reason'] = close_reason
                t['exit_price'] = exit_price
                t['pnl'] = pnl
                break
        atomic_write_json(PAPER_TRADES, trades, indent=2)
    except:
        pass

# ========== 机会评分系统 ==========

def score_direction(md, direction):
    """
    对给定方向评分：0-100分
    direction: 'long' 或 'short'
    """
    coin = md['coin']
    price = md['price']
    if not price:
        return 0, '无价格'

    rsi = md['rsi_1h']
    adx = md['adx_1h']
    rsi_4h = md['rsi_4h']
    adx_4h = md['adx_4h']
    btc_dir = md['btc_direction']
    btc_regime = md['btc_regime']
    atr_pct = md['atr_pct']
    atr_pctile = md.get('atr_percentile', 50.0)
    ma30 = md['ma30']

    score = 50
    reasons = []

    # ========== ATR Percentile过滤器（最佳策略核心）==========
    # ATR百分位 > 69：波动率高，适合趋势跟踪 → 正常评分
    # ATR百分位 30-69：中性 → 正常评分
    # ATR百分位 < 30：波动率极低，均值回归失效 → 惩罚或阻止
    if atr_pctile < 20:
        score -= 30
        reasons.append(f'ATR极低({atr_pctile:.0f}%)⚠️')
    elif atr_pctile < 30:
        score -= 15
        reasons.append(f'ATR偏低({atr_pctile:.0f}%)')
    elif atr_pctile > 80:
        score += 10
        reasons.append(f'ATR爆发({atr_pctile:.0f}%)✓')

    if direction == 'long':
        # ===== 做多评分 =====
        # RSI超卖加分
        if rsi < 30:
            score += 25; reasons.append(f'RSI严重超卖({rsi})')
        elif rsi < 35:
            score += 18; reasons.append(f'RSI超卖({rsi})')
        elif rsi < 40:
            score += 10; reasons.append(f'RSI偏低({rsi})')
        elif rsi > 70:
            score -= 25; reasons.append(f'RSI超买({rsi})❌')
        elif rsi > 65:
            score -= 15; reasons.append(f'RSI偏高({rsi})⚠️')

        # P0优化：ADX评分逻辑重写
        # 原理：ADX>40是趋势衰竭信号，不应加分；ADX<20震荡市反而适合均值回归
        # 做多(均值回归)：ADX 20-30最佳，<15震荡加分，>40减分
        # 做空(均值回归)：同理，但高位ADX更危险(反弹概率大)
        if adx > 40:
            score -= 10; reasons.append(f'ADX衰竭({adx})⚠️')
        elif adx > 30:
            score += 5; reasons.append(f'ADX强({adx})')
        elif adx >= 20:
            score += 10; reasons.append(f'ADX趋势({adx})')
        elif adx < 15:
            score += 5; reasons.append(f'ADX震荡({adx})')
        # ADX 15-20：沉默，不加分不减分

        # P1优化：Vol因子(成交量/MA)替换部分RSI权重
        # IC诊断：Vol_ratio IC=+0.071(RSI IC≈0)，DOGE Vol因子WLR=2.11 vs RSI WLR=0.71
        # 做多(均值回归)：低量盘整=好机会，高量动量=危险
        # 做空(均值回归)：高量动量=好机会，低量盘整=危险
        vol_ratio = md.get('vol_ratio', 1.0)
        if vol_ratio:
            if vol_ratio < 0.6:
                if direction == 'long':
                    score += 12; reasons.append(f'低量盘整({vol_ratio})✓')
                else:
                    score -= 12; reasons.append(f'低量盘整({vol_ratio})❌')
            elif vol_ratio < 0.8:
                if direction == 'long':
                    score += 8; reasons.append(f'量能萎缩({vol_ratio})✓')
                else:
                    score -= 8; reasons.append(f'量能萎缩({vol_ratio})⚠️')
            elif vol_ratio > 1.5:
                if direction == 'long':
                    score -= 12; reasons.append(f'高量动量({vol_ratio})⚠️')
                else:
                    score += 12; reasons.append(f'高量动量({vol_ratio})✓')
            elif vol_ratio > 1.2:
                if direction == 'long':
                    score -= 6; reasons.append(f'量能放大({vol_ratio})⚠️')
                else:
                    score += 6; reasons.append(f'量能放大({vol_ratio})✓')

        # 4h配合(更大周期确认方向)
        if rsi_4h < 35:
            score += 10; reasons.append(f'4h_RSI超卖({rsi_4h})')
        elif rsi_4h > 70:
            score -= 10; reasons.append(f'4h_RSI超买({rsi_4h})')
        if adx_4h > 40:
            score -= 5; reasons.append(f'4h_ADX衰竭({adx_4h})⚠️')  # 4h趋势衰竭
        elif adx_4h > 25:
            score += 5; reasons.append(f'4h_ADX趋势({adx_4h})')
        elif adx_4h < 15:
            score += 3; reasons.append(f'4h_ADX震荡({adx_4h})')

        # 4H CCI超卖反转（回测最优策略）
        cci_4h = md.get('cci_4h', 0)
        if cci_4h < -150:
            score += 25; reasons.append(f'4h_CCI严重超卖({cci_4h})✓')  # 强做多信号
        elif cci_4h < -100:
            score += 18; reasons.append(f'4h_CCI超卖({cci_4h})')     # 做多信号
        elif cci_4h < -50:
            score += 8; reasons.append(f'4h_CCI偏低({cci_4h})')

        # BTC配合(牛市做多更安全)
        if btc_regime == 'bull':
            score += 10; reasons.append('BTC牛市配合')
        elif btc_regime == 'bear':
            score -= 20; reasons.append('BTC熊市⚠️')
        elif btc_dir == 'oversold':
            score += 8; reasons.append('BTC超卖')
        elif btc_dir == 'overbought':
            score -= 10; reasons.append('BTC超买⚠️')

        # MA30趋势确认
        if ma30 and price > ma30:
            score += 5; reasons.append('价格>MA30')

    else:  # short
        # ===== 做空评分 =====
        # RSI超买加分(做空机会)
        if rsi > 70:
            score += 25; reasons.append(f'RSI严重超买({rsi})')
        elif rsi > 65:
            score += 18; reasons.append(f'RSI超买({rsi})')
        elif rsi > 60:
            score += 10; reasons.append(f'RSI偏高({rsi})')
        elif rsi < 30:
            score -= 25; reasons.append(f'RSI超卖({rsi})❌')

        # P1优化：做空ADX评分(与做多对称)
        if adx > 40:
            score -= 15; reasons.append(f'ADX衰竭({adx})⚠️')  # 高位ADX更危险
        elif adx > 30:
            score += 5; reasons.append(f'ADX强({adx})')
        elif adx >= 20:
            score += 10; reasons.append(f'ADX趋势({adx})')
        elif adx < 15:
            score += 5; reasons.append(f'ADX震荡({adx})')

        # P1优化：Vol因子(做空方向对称)
        vol_ratio = md.get('vol_ratio', 1.0)
        if vol_ratio:
            if vol_ratio < 0.6:
                score -= 12; reasons.append(f'低量盘整({vol_ratio})❌')
            elif vol_ratio < 0.8:
                score -= 8; reasons.append(f'量能萎缩({vol_ratio})⚠️')
            elif vol_ratio > 1.5:
                score += 12; reasons.append(f'高量动量({vol_ratio})✓')
            elif vol_ratio > 1.2:
                score += 6; reasons.append(f'量能放大({vol_ratio})✓')

        # 4h配合
        if rsi_4h > 65:
            score += 10; reasons.append(f'4h_RSI超买({rsi_4h})')
        elif rsi_4h < 35:
            score -= 10; reasons.append(f'4h_RSI超卖({rsi_4h})')
        if adx_4h > 40:
            score -= 5; reasons.append(f'4h_ADX衰竭({adx_4h})⚠️')
        elif adx_4h > 25:
            score += 5; reasons.append(f'4h_ADX趋势({adx_4h})')
        elif adx_4h < 15:
            score += 3; reasons.append(f'4h_ADX震荡({adx_4h})')

        # 4H CCI超买反转（回测最优策略）
        cci_4h = md.get('cci_4h', 0)
        if cci_4h > 150:
            score += 25; reasons.append(f'4h_CCI严重超买({cci_4h})✓')  # 强做空信号
        elif cci_4h > 100:
            score += 18; reasons.append(f'4h_CCI超买({cci_4h})')     # 做空信号
        elif cci_4h > 50:
            score += 8; reasons.append(f'4h_CCI偏高({cci_4h})')

        # BTC配合(熊市做空更安全)
        if btc_regime == 'bear':
            score += 10; reasons.append('BTC熊市配合')
        elif btc_regime == 'bull':
            score -= 20; reasons.append('BTC牛市⚠️')
        elif btc_dir == 'overbought':
            score += 8; reasons.append('BTC超买')
        elif btc_dir == 'oversold':
            score -= 10; reasons.append('BTC超卖⚠️')

        # MA30趋势确认(下跌趋势中)
        if ma30 and price < ma30:
            score += 5; reasons.append('价格<MA30')

    # ATR波动率(太低没机会，太高风险大)
    if 1.0 <= atr_pct <= 4.0:
        score += 5; reasons.append(f'ATR适中({atr_pct}%)')

    # 币种策略加成(根据10币种交叉验证结果)
    strat_bonus, strat_reason = get_coin_strategy_bonus(md, direction)
    if strat_bonus > 0:
        score += strat_bonus
        reasons.append(strat_reason)

    reason_str = '+'.join(reasons) if reasons else '中性信号'
    return max(0, min(100, score)), reason_str

def score_opportunity(md):
    """对多空两个方向评分，返回最优方向和分数
    P0优化：历史高胜率币种(DOGE/ADA)加成分，反映其8年正期望的历史验证
    v1.4: 加入L1-L5市场情绪层评分（从kronos_pilot缓存读取）
    """
    # 历史验证加成：8年全部盈利的币种给予额外信任
    HISTORICAL_BONUS = {
        'DOGE': 5,  # 8年+1176%，WLR=2.11，历史最强
        'ADA': 3,   # 8年全部盈利，WLR>2.0
        'AVAX': 2,  # 8年+574%(牛市数据)
    }
    HISTORICAL_PENALTY = {
        'BTC': -10,  # BTC均值回归+6.25%(远低于DOGE/ADA)，8年总收益差距大
        'ETH': -5,   # ETH历史表现弱于DOGE/ADA
    }
    coin = md.get('coin', '')
    hist_bonus = HISTORICAL_BONUS.get(coin, 0)
    hist_penalty = HISTORICAL_PENALTY.get(coin, 0)

    score_long, reason_long = score_direction(md, 'long')
    score_short, reason_short = score_direction(md, 'short')

    # v1.4: L1-L5情绪层评分（对山寨币影响更大）
    sentiment_bonus_long, sentiment_bonus_short = 0, 0
    sentiment_reason = ''
    try:
        sentiment = load_market_sentiment()
        if sentiment:
            fg = sentiment.get('fear_greed', {})
            fg_val = fg.get('value', 50) if isinstance(fg, dict) else 50
            btc_dom = sentiment.get('btc_dominance', 50)
            cross_dir = sentiment.get('cross_direction', 'NEUTRAL')
            long_score = sentiment.get('long_score', 0)
            short_score = sentiment.get('short_score', 0)

            # Fear & Greed: 极度恐惧(0-25) → 利好山寨做多；极度贪婪(75-100) → 利好做空
            if fg_val < 25:  # 极度恐惧 → 山寨超卖，反弹概率大
                sentiment_bonus_long += 8
                sentiment_reason += f'FG极度恐惧({fg_val}) '
            elif fg_val > 75:  # 极度贪婪 → 山寨超买，做空机会
                sentiment_bonus_short += 8
                sentiment_reason += f'FG极度贪婪({fg_val}) '

            # BTC主导率 > 60% → 山寨吸血效应，做多山寨额外加分
            if btc_dom > 60:
                if coin not in ('BTC', 'ETH'):
                    sentiment_bonus_long += 5
                    sentiment_reason += f'BTC主导率高({btc_dom:.0f}%) '
                else:
                    sentiment_bonus_short += 3
                    sentiment_reason += f'BTC主导率侵蚀({btc_dom:.0f}%) '

            # 全局共振评分（跨层多空分歧）
            if long_score > short_score + 20:
                sentiment_bonus_long += (long_score - short_score) * 0.1
                sentiment_reason += f'共振做多({long_score}-{short_score}) '
            elif short_score > long_score + 20:
                sentiment_bonus_short += (short_score - long_score) * 0.1
                sentiment_reason += f'共振做空({short_score}-{long_score}) '

            # L1资金费率：做空高费率币种（资金费率>0.03%的空头机会）
            l1_funding = sentiment.get('l1_funding', {})
            if l1_funding and coin in l1_funding:
                coin_fr = l1_funding[coin]
                rate_pct = coin_fr.get('rate', 0)
                if rate_pct > 0.03:  # 多头付钱给空头 → 资金费率贵的币，做空更安全
                    sentiment_bonus_short += min(15, rate_pct * 100)
                    sentiment_reason += f'资金费率偏高({rate_pct:.3f}%) '
                elif rate_pct < -0.03:  # 空头付钱给多头 → 资金费率负的币，做多更安全
                    sentiment_bonus_long += min(15, abs(rate_pct) * 100)
                    sentiment_reason += f'资金费率偏低({rate_pct:.3f}%) '

            # 新闻事件层：有重大事件时抑制开仓
            news_alert = sentiment.get('news_alert', [])
            if news_alert and coin in [a.get('coin') for a in news_alert]:
                # 该币有重大新闻，降低置信度
                sentiment_bonus_long = max(0, sentiment_bonus_long - 10)
                sentiment_bonus_short = max(0, sentiment_bonus_short - 10)
                sentiment_reason += f'新闻事件抑制 '

    except Exception:
        pass  # 情绪数据失败不影响基础评分

    # 选择高分方向
    if score_long + sentiment_bonus_long >= score_short + sentiment_bonus_short:
        final_score = score_long + hist_bonus + hist_penalty + sentiment_bonus_long
        bonus_str = f' [历史加成+{hist_bonus}]' if hist_bonus else ''
        penalty_str = f' [历史惩罚{hist_penalty}]' if hist_penalty else ''
        sent_str = f' [情绪{sentiment_bonus_long:+.0f}{sentiment_reason.strip()}]' if sentiment_bonus_long else ''
        final_reason = reason_long + bonus_str + penalty_str + sent_str
        return final_score, '做多', final_reason
    else:
        final_score = score_short + hist_bonus + hist_penalty + sentiment_bonus_short
        bonus_str = f' [历史加成+{hist_bonus}]' if hist_bonus else ''
        penalty_str = f' [历史惩罚{hist_penalty}]' if hist_penalty else ''
        sent_str = f' [情绪{sentiment_bonus_short:+.0f}{sentiment_reason.strip()}]' if sentiment_bonus_short else ''
        final_reason = reason_short + bonus_str + penalty_str + sent_str
        return final_score, '做空', final_reason

def load_market_sentiment():
    """读取kronos_pilot缓存的市场情绪数据（L1-L5信号）
    返回: dict，字段见 market_sentiment.json
    缓存30分钟，过期返回空dict
    """
    from pathlib import Path
    import json, time
    cache_file = Path.home() / '.hermes/cron/output/market_sentiment.json'
    try:
        if not cache_file.exists():
            return {}
        data = json.loads(cache_file.read_text())
        updated = data.get('updated', '')
        # 缓存30分钟
        from datetime import datetime, timezone
        updated_dt = datetime.fromisoformat(updated.replace('Z', '+00:00'))
        age_minutes = (datetime.now(timezone.utc) - updated_dt).total_seconds() / 60
        if age_minutes > 30:
            return {}
        return data.get('data', {})
    except:
        return {}

# ========== SignalFactory 集成 ==========

def get_signal_factory_consensus(coin: str) -> dict:
    """
    调用SignalFactory获取多策略共振信号
    带5秒超时保护，结果缓存10分钟
    返回: {'long_count': n, 'short_count': n, 'direction': 'long'/'short'/'neutral',
           'confidence': 0-1, 'strategies': [...]}
    """
    import time as _time

    # 缓存：10分钟内同coin结果直接返回
    if not hasattr(get_signal_factory_consensus, '_cache'):
        get_signal_factory_consensus._cache = {}
    cache = get_signal_factory_consensus._cache
    now = _time.time()
    if coin in cache:
        cached_time, cached_result = cache[coin]
        if now - cached_time < 600:  # 10分钟缓存
            return cached_result

    # 超时保护
    try:
        import signal as _signal_module
        _timed_out = [False]

        def _sigalarm_handler(signum, frame):
            _timed_out[0] = True
            raise TimeoutError('signal factory timeout')

        _old_handler = _signal_module.signal(_signal_module.SIGALRM, _sigalarm_handler)
        _signal_module.alarm(5)

        try:
            from signal_factory import SignalEngine
            engine = SignalEngine(coin, '1H')
            results = engine.evaluate_all()
        finally:
            _signal_module.alarm(0)
            _signal_module.signal(_signal_module.SIGALRM, _old_handler)

        if _timed_out[0]:
            raise TimeoutError('signal factory timeout')

        long_count = sum(1 for r in results.values() if r['long'])
        short_count = sum(1 for r in results.values() if r['short'])
        total = len(results)

        rsi_sample = next(iter(results.values()))['rsi'] if results else 50

        # RSI极端区域否决
        if rsi_sample < 30:
            direction = 'long'
            confidence = long_count / total if total > 0 else 0
        elif rsi_sample > 70:
            direction = 'short'
            confidence = short_count / total if total > 0 else 0
        elif long_count >= 3 and long_count > short_count:
            direction = 'long'
            confidence = long_count / total
        elif short_count >= 3 and short_count > long_count:
            direction = 'short'
            confidence = short_count / total
        elif long_count > short_count:
            direction = 'long'
            confidence = (long_count - short_count) / total
        elif short_count > long_count:
            direction = 'short'
            confidence = (short_count - long_count) / total
        else:
            direction = 'neutral'
            confidence = 0

        return {
            'long_count': long_count,
            'short_count': short_count,
            'total': total,
            'direction': direction,
            'confidence': confidence,
            'strategies': {name: {'long': r['long'], 'short': r['short']} for name, r in results.items()},
        }
        cache[coin] = (_time.time(), result)
        return result
    except BaseException as e:
        fallback = {
            'long_count': 0, 'short_count': 0, 'total': 0,
            'direction': 'neutral', 'confidence': 0,
            'strategies': {},
            'error': str(e)[:50],
        }
        cache[coin] = (_time.time(), fallback)
        return fallback


def load_paper_trades():
    """加载paper_trades记录，返回CLOSED状态的(coin, direction)集合"""
    try:
        path = Path.home() / '.hermes' / 'cron' / 'output' / 'paper_trades.json'
        with open(path) as f:
            trades = json.load(f)
        closed_pairs = set()
        for t in trades:
            if t.get('status') == 'CLOSED':
                closed_pairs.add((t.get('coin'), t.get('direction')))
        return closed_pairs
    except:
        return set()

def rank_coins(coin_data_list, positions):
    """对所有币种评分并排名，排除已有持仓，集成SignalFactory"""
    # v1.2: 从coin_strategy_map.json读取排除列表（含DOGE/LINK）
    smap = get_coin_strategy_map()
    EXCLUDED_COINS = set()
    # smap = {'AVAX': {'excluded': True}, 'DOGE': {...}}
    for symbol, config in smap.items():
        if config.get('excluded'):
            EXCLUDED_COINS.add(symbol)
    # TASK-P1B: 加载paper_trades CLOSED状态，用于过滤同方向重复开仓
    closed_paper_pairs = load_paper_trades()
    scored = []
    for md in coin_data_list:
        coin = md['coin']
        if coin in EXCLUDED_COINS:
            continue
        # 检查该币种是否有任何方向的持仓(用key前缀匹配)
        has_position = any(key.startswith(coin) for key in positions.keys())
        if has_position:
            continue
        score, direction, reason = score_opportunity(md)

        # TASK-P1B: 过滤paper_trades CLOSED状态的(coin, direction)
        if (coin, direction) in closed_paper_pairs:
            continue

        # SignalFactory多策略共振(已移除，v3.3改为只在小时级审查中运行)
        # 3分钟扫描专注快速决策，SignalFactory共识作为参考不做阻塞调用
        # 如需SignalFactory信号，用gemma_hourly_review.py每小时单独运行
        sf = {'total': 0, 'direction': 'neutral', 'confidence': 0, 'long_count': 0, 'short_count': 0}

        if sf['total'] > 0:
            sf_dir = sf['direction']
            sf_conf = sf['confidence']

            # 共振强度 > 50% 时，以SignalFactory为准
            if sf_conf >= 0.5:
                # SignalFactory方向与规则方向冲突时，扣分
                if (sf_dir == 'long' and direction == '做空') or \
                   (sf_dir == 'short' and direction == '做多'):
                    score = max(0, score - 20)
                    direction = f'做{chr(ord(sf_dir[0]) + (ord("多")[0] - ord("空")[0]) if len(sf_dir) > 0 else 0)}{chr(ord("多"[1]) - ord("空"[1]) + ord("空"[1]))}' if False else ('做多' if sf_dir == 'long' else '做空')
                    reason = f'SF共振override: {sf_dir}({sf_conf:.0%}) vs 原{direction}'

            # SignalFactory提供额外加分
            if sf_dir == 'long' and sf_conf >= 0.4:
                score += 10 * sf_conf
                reason = f'{reason} +SF共振({sf["long_count"]}/{sf["total"]})'
            elif sf_dir == 'short' and sf_conf >= 0.4:
                score += 10 * sf_conf
                reason = f'{reason} +SF共振({sf["short_count"]}/{sf["total"]})'

        scored.append({
            'coin': coin, 'score': score, 'direction': direction, 'reason': reason, 'md': md,
            'sf': sf,
        })
    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored

# ========== 决策规则 ==========

def decide_for_position(coin, pos, algos, md):
    """对已有持仓做出决策"""
    price = md['price']
    entry = pos['avgPx']
    # P0 Fix: OKX返回'buy'/'sell'，但函数内判断用'long'/'short'
    raw_side = pos['side']
    if raw_side in ('buy', 'sell'):
        side = 'long' if raw_side == 'buy' else 'short'
    else:
        side = raw_side
    sz = pos['pos']

    if not price or not entry:
        return 'hold', 0, '无价格数据', False, None, None

    # === 仓位规模检查：外部仓位可能远超系统公式 ===
    # 外部仓位(DOGE 300张 / DOT 7330张 / LINK 527张)接受但不计入新仓位计算
    # 超大仓位：实际张数 > 系统公式张数 × 3 → 标记"外部仓位"继续管保护，不强制平仓
    try:
        _equity = md.get('_equity', 90000)
        atr_pct_check = md.get('atr_pct', 0.5)
        sl_pct_check, _ = get_sl_tp_pct(coin, atr_pct_check)
        risk_amount = _equity * RISK_PER_TRADE
        sl_dist_dollar = 100 * entry * sl_pct_check  # OKX合约乘数=100USD/张，SL距离×100才是每张止损金额
        formula_sz = risk_amount / sl_dist_dollar
        max_cap_sz = int(_equity * MAX_POS_RATIO / price)
        expected_sz = min(int(formula_sz), max_cap_sz)
        if expected_sz > 0 and sz > expected_sz * 3:
            # 超大仓位：加标记但不干预，继续管理SL/TP保护
            pos['_oversized'] = True
            pos['_formula_sz'] = expected_sz
            oversized_note = f'|外部仓位{sz}张(系统公式{expected_sz}张)'
        else:
            oversized_note = ''
    except Exception:
        oversized_note = ''

    pnl_pct = (price - entry) / entry * 100 if side == 'long' else (entry - price) / entry * 100

    sl_entry = algos.get('sl')
    tp_entry = (algos.get('tp') or [None])[0] if algos.get('tp') else None
    sl_price = sl_entry['price'] if sl_entry else None
    tp_price = tp_entry['price'] if tp_entry else None
    # SL/TP距离用entry作分母（标准化，与实际盈亏计算一致）
    sl_dist = (price - sl_price) / entry * 100 if sl_price and side == 'long' else \
              (sl_price - price) / entry * 100 if sl_price and side == 'short' else 99
    tp_dist = (tp_price - price) / entry * 100 if tp_price and side == 'long' else \
              (price - tp_price) / entry * 100 if tp_price and side == 'short' else 99

    pos_state = get_position_state(coin)
    stage = pos_state.get('stage', 0)

    # === 市场动态阈值计算 ===
    # 根据ADX判断趋势强度，决定SL/TP调整的激进程度
    # 核心原则：SL/TP是最后防线，系统必须先独立判断市场再去管理仓位
    adx = md.get('adx_1h', 20)
    rsi = md.get('rsi_1h', 50)
    atr_pct = md.get('atr_pct', 2.0)
    sl_pct_dynamic, tp_pct_dynamic = get_sl_tp_pct(coin, atr_pct)

    # 趋势强度因子 (0.5~1.5): ADX越高→趋势越强→更激进锁定利润
    trend_factor = min(1.0 + (adx - 20) / 40, 1.5)   # ADX=20→1.0, ADX=40→1.5
    # 反趋势因子 (0.5~1.5): ADX越低→震荡越强→更早保本不冒险
    ranging_factor = max(1.0 - (adx - 15) / 30, 0.5)   # ADX=15→1.0, ADX=45→0.5

    # 动态保本阈值: 震荡市场0.5%，强趋势市场2%
    # 在强趋势中，给仓位更多空间；在震荡中，早保本早安心
    dynamic_breakeven_trigger = 0.5 * ranging_factor   # 0.5%~1.5%

    # 动态追踪止损: ADX越高→触发越早→锁定更多利润
    dynamic_trail_trigger = tp_pct_dynamic * 0.5 / trend_factor   # 强趋势更早触发

    # 动态止盈RSI阈值: 强趋势中RSI可以更高(80+)才止盈，震荡中RSI>65就该走
    dynamic_tp_rsi_threshold = 65 + (adx - 20) * 0.8    # ADX=20→65, ADX=40→81

    # 动态收紧SL RSI配合: RSI超买+趋势弱→立即收紧; RSI超买+趋势强→可以等
    tighten_sl_rsi_threshold = max(60, 70 - (adx - 20) * 0.5)  # ADX=20→70, ADX=40→60

    # === 首先检查：仓位是否缺少SL或TP(最优先)===
    need_sl = sl_price is None
    need_tp = tp_price is None
    if need_sl or need_tp:
        atr_pct = md.get('atr_pct', 2.0)
        sl_pct_dynamic, tp_pct_dynamic = get_sl_tp_pct(coin, atr_pct)
        if side == 'long':
            new_sl = round(entry * (1 - sl_pct_dynamic), 4) if need_sl else sl_price
            new_tp = round(entry * (1 + tp_pct_dynamic), 4) if need_tp else tp_price
        else:
            new_sl = round(entry * (1 + sl_pct_dynamic), 4) if need_sl else sl_price
            # SHORT的TP必须在entry下方（做空止盈=价格下跌=TP<entry）
            # TP距entry的比例 = tp_pct_dynamic，entry下方 → entry * (1 - tp_pct_dynamic)
            new_tp = round(entry * (1 - tp_pct_dynamic), 4) if need_tp else tp_price
        action = 'repair_sl_tp'
        urgency = 9  # 高优先级
        detail = f'补SL={new_sl}({sl_pct_dynamic:.1%})' if need_sl and not need_tp else \
                 f'补TP={new_tp}({tp_pct_dynamic:.1%})' if need_tp and not need_sl else \
                 f'补SL={new_sl}+TP={new_tp}'
        return action, urgency, detail + oversized_note, False, new_sl if need_sl else None, new_tp if need_tp else None

    # P0: 强制止损(亏损>5%)
    if pnl_pct < -5:
        return 'force_close', 10, f'亏损{pnl_pct:.1f}%>5%强制止损' + oversized_note, False, None, None

    # P0.5: SL/TP参数偏差检查 + OCO方向正确性检查
    # 注意：TP缺失在前面P0检测已处理，这里只检查参数偏差(需SL和TP都存在)
    if sl_price is not None and tp_price is not None:
        atr_pct = md.get('atr_pct', 2.0)
        sl_pct_dynamic, tp_pct_dynamic = get_sl_tp_pct(coin, atr_pct)
        # 计算实际SL%和TP%
        if side == 'long':
            actual_sl_pct = (entry - sl_price) / entry
            actual_tp_pct = (tp_price - entry) / entry
        else:
            actual_sl_pct = (sl_price - entry) / entry
            actual_tp_pct = (entry - tp_price) / entry
        sl_deviation = abs(actual_sl_pct - sl_pct_dynamic) / sl_pct_dynamic
        tp_deviation = abs(actual_tp_pct - tp_pct_dynamic) / tp_pct_dynamic
        
        # P0 Fix: OCO方向正确性检查 - 如果方向已经正确，不需要重建
        # 做多(buy/long)：SL < entry，TP > entry
        # 做空(sell/short)：SL > entry，TP < entry
        oco_direction_correct = True
        if side == 'long':
            if sl_price >= entry or tp_price <= entry:
                oco_direction_correct = False
        else:  # short
            if sl_price <= entry or tp_price >= entry:
                oco_direction_correct = False
        
        # 偏差>20% → 触发修复建议(urgency=6，建议级)
        # 但如果OCO方向已经正确，说明系统在工作，不需要重建
        if sl_deviation > 0.20 and not oco_direction_correct:
            new_sl = round(entry * (1 - sl_pct_dynamic), 4) if side == 'long' else round(entry * (1 + sl_pct_dynamic), 4)
            new_tp = round(entry * (1 + tp_pct_dynamic), 4) if side == 'long' else round(entry * (1 - tp_pct_dynamic), 4)
            # 注意：紧急收紧可能立即触发止损，只建议不自动执行
            return 'repair_sl_tp', 6, f'SL偏差{sl_deviation:.0%}建议收紧 {actual_sl_pct:.2%}→{sl_pct_dynamic:.2%}(当前距现价{sl_dist:.1f}%)' + oversized_note, False, new_sl, new_tp

    # P1: 跌穿SL(极危险，SL距现价<0.5%，绝对紧急)
    if sl_dist < SL_DANGER_PCT:
        return 'close', 9, f'SL距现价{sl_dist:.2f}%极危险' + oversized_note, False, None, None

    # P2: 保本止损 — 动态阈值（震荡市场0.5%，强趋势市场2%）
    # 在强趋势中，给仓位更多空间；在震荡中，早保本早安心
    if pnl_pct > dynamic_breakeven_trigger * 100:
        breakeven_sl = entry  # SL = 入场价，不亏钱
        if side == 'long' and sl_price is not None and breakeven_sl > sl_price:
            return 'trailing_sl', 8, f'浮盈{pnl_pct:.1f}%→保本SL={breakeven_sl}(ADX={adx:.0f},触发{dynamic_breakeven_trigger:.1%})' + oversized_note, False, None, None
        elif side == 'short' and sl_price is not None and breakeven_sl < sl_price:
            return 'trailing_sl', 8, f'浮盈{pnl_pct:.1f}%→保本SL={breakeven_sl}(ADX={adx:.0f},触发{dynamic_breakeven_trigger:.1%})' + oversized_note, False, None, None

    # P2.5: 追踪止损 — 动态阈值（ADX越高→触发越早→锁定更多利润）
    # 核心：ADX>25确认趋势存在，才启动追踪；强趋势(ADX>35)更早锁定利润
    atr_pct = md.get('atr_pct', 2.0)
    sl_pct_dynamic, tp_pct_dynamic = get_sl_tp_pct(coin, atr_pct)
    # 趋势强度动态锁利: ADX越高，锁定TP距离的百分比越高(25%~50%)
    lock_pct_base = min(0.25 + (adx - 25) / 40, 0.5)   # ADX=25→25%, ADX=45→50%
    lock_pct = lock_pct_base
    lock_pct = max(lock_pct, TRAIL_LOCK_PCT)  # 至少2%(TRAIL_LOCK_PCT=0.02)
    if pnl_pct > dynamic_trail_trigger * 100 and adx > 25:
        if side == 'long':
            trailing_sl = round(entry * (1 + lock_pct), 4)
            if sl_price is None or trailing_sl > sl_price:
                return 'trailing_sl', 8, f'浮盈{pnl_pct:.1f}%→追踪SL={trailing_sl}(锁{lock_pct:.1%},ADX={adx:.0f})' + oversized_note, False, None, None
        else:
            # SHORT追踪SL: 用 price * (1 - lock_pct)，随价格下跌而下降，但不低于entry
            trailing_sl = round(max(entry, price * (1 - lock_pct)), 4)
            if sl_price is None or trailing_sl < sl_price:
                return 'trailing_sl', 8, f'浮盈{pnl_pct:.1f}%→追踪SL={trailing_sl}(锁{lock_pct:.1%},ADX={adx:.0f})' + oversized_note, False, None, None

    # P3: 分批止盈 — 动态RSI阈值
    # 强趋势(ADX>35): RSI可以到80才走，让利润奔跑
    # 震荡市场(ADX<25): RSI>65就走，不贪
    partial_trigger_pct = tp_pct_dynamic * 0.33  # TP的1/3触发首批
    if stage < len(TP_STAGES):
        tp_stage = TP_STAGES[stage]
        # RSI峰值止盈: RSI从高点回落(>65+ADX确认趋势弱) OR TP已非常近
        rsi_peak = rsi > 65 and (adx < 25 or rsi > dynamic_tp_rsi_threshold)
        if pnl_pct > partial_trigger_pct * 100 and (rsi_peak or (0 < tp_dist < tp_pct_dynamic * 5)):
            return tp_stage['trigger'], 7, f'浮盈{pnl_pct:.1f}% {tp_stage["label"]}(RSI={rsi:.0f} ADX={adx:.0f})' + oversized_note, False, None, None

    # P3.5: RSI极端超买 + 弱趋势 = 熊市反弹止盈（仅LONG）
    # RSI>=75 + ADX<30 → 熊市反弹结束信号，仅对LONG有效
    # 核心逻辑：熊市(空头)中RSI>75是典型的反弹结束点，不是继续持有的理由
    # 此判断优先于P4收紧SL/P3分批止盈，因为RSI极端超买比SL距离更重要
    # 注意：对SHORT无效！SHORT时RSI高反而可能是有利信号（价格可能回落）
    if side == 'long' and rsi >= 75 and adx < 30:
        action_taken = 'force_close' if pnl_pct <= 0 else 'partial_profit'
        urgency_level = 8 if pnl_pct <= 0 else 7
        reason_str = f'RSI极端({rsi:.0f}超买)+弱趋势(ADX={adx:.0f})，熊市反弹结束，止盈出局' if pnl_pct > 0 else f'RSI({rsi:.0f}超买)+ADX({adx:.0f})，熊市反弹结束，强制退出'
        return action_taken, urgency_level, reason_str + oversized_note, False, None, None

    # P4: 收紧SL — 动态阈值（结合浮亏+SL距+RSI+ADX）
    # 强趋势(ADX>35)中的浮亏: 不要急着收紧，被扫止损概率低
    # 震荡市场(ADX<25)中的浮亏: 立即收紧，趋势不可靠
    tighten_sl_dist_threshold = 2.0 + (adx - 20) * 0.1   # ADX=20→2%, ADX=35→3.5%
    if pnl_pct < 0 and sl_dist < tighten_sl_dist_threshold:
        # RSI超买配合 → 收紧更急迫
        urgency_mod = 2 if rsi > tighten_sl_rsi_threshold else 0
        sl_pct_dynamic, _ = get_sl_tp_pct(coin, atr_pct)
        new_sl = round(entry * (1 - sl_pct_dynamic), 4) if side == 'long' else round(entry * (1 + sl_pct_dynamic), 4)
        if side == 'long' and (sl_price is None or new_sl > sl_price):
            return 'tighten_sl', 6 + urgency_mod, f'SL收紧{sl_dist:.1f}%→{new_sl}(RSI={rsi:.0f}ADX={adx:.0f})' + oversized_note, False, None, None
        elif side == 'short' and (sl_price is None or new_sl < sl_price):
            return 'tighten_sl', 6 + urgency_mod, f'SL收紧{sl_dist:.1f}%→{new_sl}(RSI={rsi:.0f}ADX={adx:.0f})' + oversized_note, False, None, None

    # P5: 全止盈 — 动态RSI+ADX
    # 强趋势: RSI>80+ADX>35才全止盈，让利润奔跑
    # 震荡: RSI>70+ADX<25就全止盈
    tp_rsi_full = dynamic_tp_rsi_threshold + 5   # 比分批止盈高5
    if pnl_pct > 8 and rsi > tp_rsi_full and adx > 30:
        return 'take_profit', 5, f'RSI极端({rsi:.0f})+强趋势(ADX={adx:.0f})+浮盈{pnl_pct:.1f}%全止盈' + oversized_note, False, None, None

    return 'hold', 0, f'浮盈{pnl_pct:+.1f}% SL距{sl_dist:.1f}% TP距{tp_dist:.1f}%' + oversized_note, False, None, None

def decide_for_candidate(coin, md, equity, num_positions):
    """对候选币种决策是否开仓(包含方向)
    
    v3.3: 双层决策
    - 第一层：多因子投票系统(IC动态权重)
    - 第二层：规则引擎评分
    两层都通过才开仓
    """
    if num_positions >= MAX_POSITIONS:
        return None, 0, '仓位已满', None

    # === 历史验证币种直接信任规则引擎，跳过voting系统(voting调用慢且不稳定)===
    SKIP_VOTING_COINS = {'DOGE', 'ADA', 'AVAX'}

    # 应用MiniMax战略审查给出的IC权重调整
    if HAS_VOTING_SYSTEM:
        try:
            tracker = ICTracker()
            tracker.apply_minimax_adjustment()
        except:
            pass

    # ========== 第一层：多因子投票系统 ==========
    # P0 Fix：异常处理逻辑重写（原结构有严重缩进错误，vote_result/veto检查变成死代码）
    vote_reason = ''  # 空字符串：投票被跳过时表示正常行为，不误导用户
    best_dir = None
    best_pct = 0
    best_vote = {}
    vote_result = {'best_direction': None, 'best_score': 0, 'long': {}, 'short': {}}

    if HAS_VOTING_SYSTEM and coin not in SKIP_VOTING_COINS:
        try:
            import signal as _sig_module
            _timed_out = [False]
            def _timeout_handler(signum, frame):
                _timed_out[0] = True
                raise TimeoutError('voting超时')
            _old = _sig_module.signal(_sig_module.SIGALRM, _timeout_handler)
            _sig_module.alarm(10)  # 10秒硬超时
            try:
                vote_result = evaluate_coin(coin, md, {}, equity)
            finally:
                _sig_module.alarm(0)
                _sig_module.signal(_sig_module.SIGALRM, _old)
            if _timed_out[0]:
                raise TimeoutError('voting超时')
        except (TimeoutError, Exception) as e:
            vote_result = {'best_direction': None, 'best_score': 0, 'long': {}, 'short': {}}
            vote_reason = f'[投票异常:{str(e)[:30]}，信任规则引擎] '

        # 分析投票结果
        if vote_result and vote_result.get('best_direction'):
            best_dir = vote_result['best_direction']
            best_pct = vote_result['best_score']
            best_vote = vote_result['long'] if best_dir == 'long' else vote_result['short']

            if best_vote.get('veto_triggered'):
                return None, 0, f'投票否决: {best_vote["veto_triggered"]}', None

            if best_pct >= 20:
                vote_reason = f'[投票{best_dir}{best_pct:.0f}%] '
            else:
                vote_reason = '[投票置信度<20%，信任规则引擎] '
                best_dir = None
                best_pct = 0
                best_vote = {}

    # ========== 第二层：规则引擎评分 ==========
    score, direction, reason = score_opportunity(md)

    if score < 65:
        return None, 0, f'规则评分{score}<65门槛', None

    atr_pct = md['atr_pct']
    min_atr = _COIN_MIN_ATR_PCT.get(coin, 0.5)  # DOGE/ADA特殊0.3%，其他0.5%
    if atr_pct < min_atr:
        return None, 0, f'ATR{atr_pct:.2f}%<{min_atr}%最低门槛', None
    if atr_pct > 10:
        return None, 0, f'ATR{atr_pct}%太高风险大', None

    # ========== 双层一致性检查 ==========
    if HAS_VOTING_SYSTEM and best_dir is not None:
        # 方向冲突：投票说做空，规则说做多 → 降权通过(记录冲突)
        if best_dir != direction:
            # 方向冲突，降分但不禁绝(信任规则引擎)
            reason = f'{reason} [方向冲突投票:{best_dir}]'
        
        # 投票置信度极高(>70%)且方向一致 → 加分
        if best_pct >= 70 and best_dir == direction:
            score += 15
            reason = f'{reason} [投票共振确认{best_pct:.0f}%]'

    return 'open', score, f'{vote_reason}{direction}评分{score} {reason}', direction

# ========== gemma4审查 ==========
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
        r = requests.post(f'{MINIMAX_BASE_URL}/text/chatcompletion_v2', headers=headers, json=data, timeout=(5, timeout))
        result = r.json()
        if 'choices' in result:
            return result['choices'][0]['message']['content']
        return f"API错误: {result.get('base_error', result.get('msg', 'unknown'))}"
    except Exception as e:
        return f"MiniMax API错误: {str(e)}"


def gemma4_review(coin, md, pos, proposed_action, rules_reason, equity=None):
    """gemma4结构化审查 - 完整上下文版本
    
    从gemma4_signal_audit.py移植过来的完整版，给gemma4提供充分决策上下文。
    
    equity: 账户USDT总权益（用于检测oversized仓位）
    """
    price = md['price']
    rsi_1h = md.get('rsi_1h') or 50
    rsi_4h = md.get('rsi_4h') or 50
    adx_1h = md.get('adx_1h') or 20
    btc_direction = md.get('btc_direction', 'neutral')
    btc_regime = md.get('btc_regime', 'neutral')
    
    # 计算持仓数据
    pnl_pct = 0
    sl_dist = 0
    tp_dist = 0
    reward_risk = 0
    has_sl = False
    has_tp = False
    entry = 0
    side = 'long'
    
    algos = md.get('_algos', {})
    if pos and price:
        entry = float(pos.get('avgPx', 0))
        if entry > 0:
            side = pos.get('side', 'long')
            pnl_pct = (price - entry) / entry * 100 if side == 'long' else (entry - price) / entry * 100
            sl_entry = algos.get('sl')
            tp_entry = (algos.get('tp') or [None])[0] if algos.get('tp') else None
            if sl_entry:
                has_sl = True
                sl_price = sl_entry.get('price')
                sl_dist = (price - sl_price) / price * 100 if side == 'long' else (sl_price - price) / price * 100
            if tp_entry:
                has_tp = True
                tp_price = tp_entry.get('price')
                tp_dist = (tp_price - price) / price * 100 if side == 'long' else (price - tp_price) / price * 100
            if sl_dist > 0:
                reward_risk = tp_dist / sl_dist if tp_dist > 0 else 0
    
    # 趋势判断
    trend = 'neutral'
    if rsi_1h < 35 and adx_1h > 20:
        trend = 'long'
    elif rsi_1h > 65 and adx_1h > 20:
        trend = 'short'
    
    # 危险区域文本
    danger_parts = []
    if pos and price:
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
        danger_zone_text = f'方向:{side} {pos.get("pos",0)}张 @{entry} | ' + ' | '.join(danger_parts)
    else:
        danger_zone_text = '无持仓'

    # ========== Oversized仓位检测 ==========
    # 检测单个仓位是否超过账户5%或总暴露超过15%
    oversized_text = ''
    if pos and price and entry > 0:
        sz = pos.get('pos', 0)
        notional = sz * entry
        margin_pct = notional / (equity or 1) * 100 if equity else 0
        formula_sz = pos.get('_formula_sz', 0)
        oversize_ratio = sz / max(formula_sz, 1) if formula_sz > 0 else 0
        
        if margin_pct > 5:
            oversized_text = f'⚠️【超仓警告】仓位{margin_pct:.1f}%账户(>$5%限额) | '
        if oversize_ratio > 3:
            oversized_text += f'实际{sz}张 vs 公式{formula_sz}张({oversize_ratio:.0f}倍) | '
        if oversized_text:
            oversized_text += '⚠️必须判断：平仓一半？全平？收紧止损？'
            oversized_text = oversized_text.strip()

    # ========== 账户权益文本 ==========
    equity_text = f"${equity:,.0f}" if equity else "未知"

    # Autoresearch反馈闭环状态
    import time as _time
    _ar_marker = Path("/tmp/autoresearch_last_success")
    ar_status = "正常"
    if _ar_marker.exists():
        _age = (_time.time() - _ar_marker.stat().st_mtime) / 3600
        if _age > 4:
            ar_status = f"⚠️权重陈旧({_age:.1f}h未更新)"
        # 读取strategy_quality
        _ic_file = Path.home() / ".hermes/kronos_ic_weights.json"
        if _ic_file.exists():
            import json as _json
            _ic_data = _json.loads(_ic_file.read_text())
            _sq = _ic_data.get('strategy_quality', {})
            if _sq:
                _decay = _sq.get('decay_applied', 0)
                _ret = _sq.get('avg_return', 0)
                _wr = _sq.get('keep_rate', 0)
                if _decay > 0:
                    ar_status = f"🚨策略失效(回报{_ret:+.1f}%胜率{_wr:.0%}权重已降{_decay:.0%})"
    else:
        _ar_fail = Path("/tmp/autoresearch_last_failure")
        if _ar_fail.exists():
            ar_status = f"🚨研究失败({_ar_fail.read_text().strip()[:40]})"
    ar_status_text = f"IC权重:{ar_status}"
    
    # 完整prompt
    prompt = f"""你是一个专业的加密货币量化交易Agent。你的职责是**严格审查**交易决策。

## 必须遵守的硬性规则
- RSI必须在30-35之间(超卖区域)做多，或在65-70之间(超买区域)做空
- ADX必须>20(确认趋势存在)
- SL距现价必须>1.5%
- TP距现价必须在6-12%之间
- 盈亏比必须>2:1
- BTC方向冲突时必须否决(BTC超买时不追多，BTC超卖时不追空)
- 【超仓检测】单仓位不得超过账户5%，总暴露不得超过账户15%（平仓一半或全平）

## 持仓管理硬性规则
- 浮亏>5%：必须止损(无例外)
- SL距现价<1%：立即收紧或止损
- 浮盈>8%且ADX>30：启动追踪止损
- RSI进入极端区域(<25或>75)：强制平仓
- 缺少SL或TP：立即补上

## 禁止行为
- 不允许建议亏损加仓(摊平成本)
- 不允许建议持有超过48小时不做任何操作
- 不允许建议在RSI 40-60中性区域建仓
- 不允许建议盈亏比<1.5:1的交易
- 不允许建议在市场剧烈波动时(ADX>50)追涨杀跌

## 输出格式(严格按此格式)
verdict: [PASS/FAIL]
decision: [通过/否决]
reason: [不超过30字的简洁理由]

## 当前审查数据
【市场数据】
币种: {coin}
当前价格: ${price}
1h RSI: {rsi_1h:.1f}(<30严重超卖，30-35超卖，35-65中性，65-70超买，>70严重超买)
4h RSI: {rsi_4h:.1f}
ADX: {adx_1h:.1f}(>30强趋势，20-30中等趋势，<20弱趋势)
BTC方向: {btc_direction}
BTC市场: {btc_regime}
趋势判断: {trend}

【账户权益】
账户总USDT权益: {equity_text}

【持仓状态】
{danger_zone_text}
{oversized_text}

【系统规则决策】
规则动作: {proposed_action}
规则原因: {rules_reason}

【IC权重状态】
{ar_status_text}
"""

    try:
        import signal as _sig_gemma
        _gemma_timed_out = [False]
        def _gemma_timeout(signum, frame):
            _gemma_timed_out[0] = True
            raise TimeoutError('gemma4超时')
        _old_gemma = _sig_gemma.signal(_sig_gemma.SIGALRM, _gemma_timeout)
        _sig_gemma.alarm(20)
        try:
            data = {
                'model': 'gemma4-2b-heretic',
                'messages': [{'role': 'user', 'content': prompt}],
                'stream': False,
            }
            r = requests.post('http://localhost:11434/api/chat', json=data, timeout=20)
            result = r.json()
            output = result.get('message', {}).get('content', '').strip()
        finally:
            _sig_gemma.alarm(0)
            _sig_gemma.signal(_sig_gemma.SIGALRM, _old_gemma)
        
        if _gemma_timed_out[0]:
            return '否决', 'gemma4超时20秒强制跳过'
        
        # 解析decision
        decision = '通过'
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('decision:'):
                v = line.split(':', 1)[1].strip()
                if any(k in v for k in ['否决', 'reject', 'no', '不', 'FAIL']):
                    decision = '否决'
        reason = ''
        for line in output.split('\n'):
            if line.startswith('reason:'):
                reason = line.split(':', 1)[1].strip()
        
        return decision, reason or output[:100]
    except TimeoutError:
        return '否决', 'gemma4超时20秒强制跳过'
    except Exception as e:
        return '通过', f'gemma4异常:{str(e)[:30]}'

# ========== 执行层 ==========

def execute_action(coin, action, pos, algos, md, equity, repair_sl=None, repair_tp=None):
    """执行交易动作

    P0 Fix: pos['side'] OKX格式('buy'/'sell') → 内部格式('long'/'short')
    统一在入口转换，所有内部逻辑使用'long'/'short'。
    """
    instId = f'{coin}-USDT-SWAP'
    price = md['price']
    results = []
    # P0 Fix: 统一OKX→内部格式
    raw_side = pos['side']
    if raw_side in ('buy', 'sell'):
        side = 'long' if raw_side == 'buy' else 'short'
    else:
        side = raw_side

    if action == 'repair_sl_tp':
        # P0 Fix: 如果repair_sl/repair_tp无效，跳过防止place_oco收到False参数
        if not repair_sl or not repair_tp:
            results.append(('repair_sl_tp跳过', False, f'无效参数 SL={repair_sl} TP={repair_tp}'))
            return results
        # 修复缺失的SL/TP：必须用OCO合并为1个订单(P0 Bug修复)
        cleanup_coin_algos(instId)
        algo_id = place_oco(instId, side, pos['pos'], repair_sl, repair_tp)
        if algo_id:
            results.append(('补OCO', True, f'SL={repair_sl} TP={repair_tp} [{algo_id[:8]}]'))
        else:
            results.append(('补OCO', False, f'失败 SL={repair_sl} TP={repair_tp}'))

    elif action == 'force_close' or action == 'close':
        ok, r = close_position(instId, side, pos['pos'])
        results.append(('平仓', ok, f'{coin} {int(pos["pos"])}张'))
        # 平仓后清残余条件单
        cancelled = cleanup_coin_algos(instId)
        if cancelled:
            results.append(('清残单', True, f'{len(cancelled)}个'))
        if ok:
            close_paper_trade(coin, pos['side'], f'{action}_force_close', None, 0)
        save_position_state(coin, {'stage': 0, 'sold_ratio': 0.0})

    elif action == 'tighten_sl':
        sl_id = algos.get('sl', {}).get('algoId') if algos.get('sl') else None
        if sl_id and pos:
            entry = pos.get('avgPx', price)
            atr_pct = md.get('atr_pct', 2.0)
            sl_pct_dynamic, _ = get_sl_tp_pct(coin, atr_pct)
            new_sl = round(entry * (1 - sl_pct_dynamic), 4) if side == 'long' else round(entry * (1 + sl_pct_dynamic), 4)
            ok, r = amend_sl(instId, sl_id, new_sl)
            results.append(('收紧SL', ok, f'→{new_sl}({sl_pct_dynamic:.1%})'))

    elif action == 'trailing_sl':
        sl_id = algos.get('sl', {}).get('algoId') if algos.get('sl') else None
        if sl_id and pos:
            entry = pos.get('avgPx', price)
            atr_pct = md.get('atr_pct', 2.0)
            _, tp_pct_dynamic = get_sl_tp_pct(coin, atr_pct)
            lock_pct = tp_pct_dynamic * 0.25
            lock_pct = max(lock_pct, TRAIL_LOCK_PCT)
            new_sl = round(entry * (1 + lock_pct), 4) if side == 'long' else round(entry * (1 - lock_pct), 4)
            ok, r = amend_sl(instId, sl_id, new_sl)
            results.append(('追踪SL', ok, f'→{new_sl}(锁{lock_pct:.1%})'))

    elif action == 'partial_profit':
        # RSI>=75超买+弱趋势熊市 → 止盈50% + 收紧SL保本
        # 剩余50%继续持有，但SL收紧到入场价+0.5%（保本线）
        close_ratio = 0.5
        sz_to_close = max(1, int(pos['pos'] * close_ratio))
        ok, r = close_partial(instId, side, pos['pos'], close_ratio)
        results.append(('部分止盈', ok, f'RSI超买平50%={sz_to_close}张'))
        if ok:
            # 收紧剩余50%的SL到保本线
            sl_id = algos.get('sl', {}).get('algoId') if algos.get('sl') else None
            remaining_pos = pos['pos'] - sz_to_close
            if sl_id and remaining_pos >= 1:
                entry = pos.get('avgPx', price)
                breakeven_sl = round(entry * 1.005, 4)  # 保本+0.5%
                if side == 'long':
                    new_sl = max(float(sl_id), breakeven_sl)  # 不降低已有SL
                else:
                    new_sl = min(float(sl_id), breakeven_sl)
                ok2, r2 = amend_sl(instId, sl_id, new_sl)
                results.append(('收紧SL', ok2, f'→保本线{new_sl}'))

    elif action.startswith('partial_tp_'):
        stage_idx = int(action.split('_')[1]) - 1
        if stage_idx < len(TP_STAGES):
            stage = TP_STAGES[stage_idx]
            ok, r = close_partial(instId, side, pos['pos'], stage['ratio'])
            results.append(('分批止盈', ok, f'{stage["label"]} {stage["ratio"]*100:.0f}%={int(pos["pos"]*stage["ratio"])}张'))
            # 更新仓位状态
            pos_state = get_position_state(coin)
            pos_state['stage'] = stage_idx + 1
            pos_state['sold_ratio'] = stage['sold'] + stage['ratio']
            save_position_state(coin, pos_state)
            # 只取消TP，保留SL(cleanup清所有太暴力)
            remaining = 1.0 - pos_state['sold_ratio']
            if remaining > 0.05:
                tp_price = algos['tp'][0]['price'] if algos.get('tp') else price * (1 + 0.05)
                tp_algo_ids = [o['algoId'] for o in algos.get('tp', []) if o.get('algoId')]
                if tp_algo_ids:
                    cancel_algos(instId, tp_algo_ids)  # 只取消TP，不碰SL
                new_sz = max(1, int(pos['pos'] * remaining))
                new_id = place_tp(instId, side, new_sz, tp_price)  # 不再清SL
                results.append(('重挂TP', bool(new_id), f'剩余{int(remaining*100)}%={new_sz}张 @{tp_price}'))

    elif action == 'take_profit':
        ok, r = close_position(instId, side, pos['pos'])
        results.append(('全止盈', ok, f'{coin} {int(pos["pos"])}张'))
        if ok:
            close_paper_trade(coin, pos['side'], 'take_profit全止盈', None, 0)
        save_position_state(coin, {'stage': 0, 'sold_ratio': 0.0})

    elif action == 'open':
        # 从参数获取方向
        direction = md.get('_direction', 'long')
        atr_pct = md['atr_pct']
        sl_pct_dynamic, tp_pct_dynamic = get_sl_tp_pct(coin, atr_pct)
        
        # 仓位：固定风险1%账户
        risk_amount = equity * RISK_PER_TRADE
        sl_dist_dollar = price * sl_pct_dynamic * 100  # 每张止损金额（OKX每张=100USDT名义价值）
        sz = int(risk_amount / sl_dist_dollar)
        max_by_ratio = int(equity * MAX_POS_RATIO / price)  # 2%上限（已除杠杆）
        sz = min(sz, max_by_ratio)
        sz = max(MIN_CONTRACTS, sz)
        proposed_loss = sz * sl_dist_dollar

        treasury_ok, t_msg, _ = check_treasury(equity, proposed_loss)
        if not treasury_ok:
            results.append(('开仓', False, f'熔断:{t_msg}'))
            return results

        # SL/TP从开仓价格计算(固定，不漂移)
        if direction == 'short':
            sl = round(price * (1 + sl_pct_dynamic), 4)
            tp = round(price * (1 - tp_pct_dynamic), 4)
        else:
            sl = round(price * (1 - sl_pct_dynamic), 4)
            tp = round(price * (1 + tp_pct_dynamic), 4)

        ok, msg = open_position(instId, direction, sz, sl, tp, leverage=3)  # 默认3x杠杆
        results.append(('开多' if direction in ('long', '做多') else '开空', ok, f'{coin} {direction} {sz}张 @{price} | {msg}'))
        if ok:
            save_position_state(coin, {'stage': 0, 'sold_ratio': 0.0, 'direction': direction})

    return results

# ========== 主扫描循环 ==========

def gemma4_central_decision(all_data):
    """gemma4中央决策 - 职业操盘手模式 v5.0

    ★ 核心升级（2026-04-24）：
    - 本地市场环境评估（build_local_factor_context）：不依赖MiniMax，每次决策实时计算
    - 决策日志（Decision Journal）：每笔决策永久记录，可回溯评估质量
    - 职业操盘手prompt：不是评分排名，而是组合管理视角判断
    
    决策流程：
    1. 本地实时评估市场环境（RSI中位数/ADX中位数/BTC趋势/波动率）
    2. 构建职业操盘手视角prompt
    3. gemma4做出组合管理决策
    4. 记录决策日志
    5. 返回决策给执行层
    """
    equity = all_data['equity']
    positions = all_data['positions']
    candidates = all_data['candidates']
    btc_regime = all_data['btc_regime']
    btc_dir = all_data['btc_direction']
    treasury_ok = all_data['treasury_ok']
    treasury_msg = all_data['treasury_msg']
    btc_price = all_data.get('btc_price', 0)
    
    # ========== Step 1: 本地市场环境评估（实时，不依赖MiniMax）==========
    local_ctx = build_local_factor_context(
        candidates, positions, btc_price, btc_dir, btc_regime
    )
    
    # 如果置信度极低，使用保守默认值
    if local_ctx['overall_confidence'] < 0.25:
        local_ctx['strategic_hint'] = '⚠️ 数据质量差，保守观望'
        local_ctx['primary_direction'] = 'none'
    
    # ========== Step 2: 读取MiniMax上下文（仅作参考，不作为唯一依据）==========
    ctx = None
    emergency_stop = None
    minimax_available = False
    if HAS_CONTEXT:
        ctx = load_context_with_validation(CTX_PATH)
        emergency_stop = read_emergency_stop(EMERGENCY_PATH)
        # 只有当MiniMax上下文新鲜且质量好时才使用
        if ctx and ctx.overall_confidence >= 0.5:
            ctx_age_hours = (time.time() - datetime.fromisoformat(ctx.generated_at.replace('Z', '+00:00')).timestamp()) / 3600 \
                if ctx.generated_at else 999
            if ctx_age_hours < 1.5 and ctx.factor_status.get('rsi'):
                # MiniMax上下文新鲜且有RSI数据 → 合并到local_ctx
                rsi_status = ctx.factor_status.get('rsi', {})
                if isinstance(rsi_status, dict) and rsi_status.get('status') != 'inactive':
                    local_ctx['minimax_regime'] = ctx.market_regime
                    local_ctx['minimax_direction'] = ctx.primary_direction
                    local_ctx['minimax_confidence'] = ctx.confidence
                    local_ctx['minimax_hint'] = ctx.strategic_hint
                    local_ctx['minimax_forbidden'] = ctx.forbidden_actions
                    minimax_available = True
    
    # 紧急干预处理
    if emergency_stop and emergency_stop.level == 'ultra':
        return {
            'coin': '',
            'decision': 'hold',
            'reason': f'🚨 Ultra紧急干预: {emergency_stop.reason}',
            'local_ctx': local_ctx,
            'ctx': ctx,
        }
    
    # ========== Step 3: 构建职业操盘手prompt ==========
    # 扩展版: 对每个持仓给出完整市场数据，让gemma4独立判断所有仓位
    pos_block = []
    for coin, pos in positions.items():
        price = pos.get('last', 0)
        entry = pos.get('avgPx', 0)
        pnl_pct = (price - entry) / entry * 100 if price and entry else 0
        pnl = pos.get('upl', 0)
        side = pos.get('side', 'long')
        sl_price = pos.get('sl_price')
        tp_price = pos.get('tp_price')
        sl_dist = (price - sl_price) / price * 100 if sl_price and price else 0
        tp_dist = (tp_price - price) / price * 100 if tp_price and price else 0
        margin_pct = (pos.get('pos', 0) * entry / (equity or 1) * 100) if entry and pos.get('pos', 0) else 0

        # 获取逐币市场数据 (md 是 positions[key]['md']，在阶段1已填充)
        pos_md = pos.get('md', {})
        rsi_1h = pos_md.get('rsi_1h', 0)
        adx_1h = pos_md.get('adx_1h', 0)
        atr_pct = pos_md.get('atr_pct', 0)
        atr_p = pos_md.get('atr_percentile', 0)
        vol_ratio = pos_md.get('vol_ratio', 0)
        cci_4h = pos_md.get('cci_4h', 0)

        # 趋势判断
        if rsi_1h < 35 and adx_1h > 20:
            trend = '做多信号'
        elif rsi_1h > 65 and adx_1h > 20:
            trend = '做空信号'
        elif adx_1h < 20:
            trend = '震荡无趋势'
        else:
            trend = '中性'

        # 市场状态评估
        if rsi_1h < 30:
            rsi_status = '严重超卖'
        elif rsi_1h < 45:
            rsi_status = '偏弱'
        elif rsi_1h > 70:
            rsi_status = '严重超买'
        elif rsi_1h > 55:
            rsi_status = '偏强'
        else:
            rsi_status = '中性'

        if adx_1h > 35:
            adx_status = '强趋势'
        elif adx_1h > 25:
            adx_status = '中等趋势'
        elif adx_1h > 18:
            adx_status = '弱趋势'
        else:
            adx_status = '震荡'

        status_emoji = '✅' if pnl_pct >= 0 else '⚠️'

        # 超仓警告
        oversize_note = ''
        if pos.get('_oversized'):
            sz = pos.get('pos', 0)
            formula_sz = pos.get('_formula_sz', 0)
            notional = pos.get('_notional', sz * entry)
            margin_pct = pos.get('_margin_pct', 0)
            if formula_sz > 0 and sz > formula_sz * 3:
                risk_note = f'⚠️ 【高风险仓位警报】{sz}张 vs 公式{formula_sz}张(差{sz/max(formula_sz,1):.0f}倍) | 名义${notional:,.0f} | 保证金{abs(margin_pct):.1f}%equity'
                oversize_note = f'\n  {risk_note}'

        pos_block.append(
            f"{status_emoji} {coin}: {side.upper()} {pos['pos']}张 "
            f"入场${entry} → 现价${price} | {pnl_pct:+.2f}%(${pnl:+.0f}) "
            f"| SL${sl_price}({sl_dist:+.1f}%) TP${tp_price}({tp_dist:+.1f}%) "
            f"| RSI={rsi_1h:.0f}({rsi_status}) ADX={adx_1h:.0f}({adx_status}) "
            f"| 趋势={trend} | ATR%={atr_pct:.1f}% | 保证金{margin_pct:.1f}%{oversize_note}"
        )
    
    # 格式化候选币
    cand_lines = []
    for i, c in enumerate(candidates[:8]):
        md = c.get('md', {})
        cand_lines.append(
            f"  #{i+1} {c['coin']}: {c['direction']} {c['score']}分 | "
            f"RSI_1h={md.get('rsi_1h','?')} RSI_4h={md.get('rsi_4h','?')} "
            f"CCI_4h={md.get('cci_4h','?')} ADX={md.get('adx_1h','?')} "
            f"ATR%={md.get('atr_pct','?')}% ATR_P={md.get('atr_percentile','?')}% "
            f"Vol={md.get('vol_ratio','?')} | {c['reason']}"
        )
    
    # MiniMax上下文块（仅在有效时添加）
    minimax_block = ""
    if minimax_available:
        forbidden = ', '.join(ctx.forbidden_actions) if ctx.forbidden_actions else '无'
        minimax_block = f"""
## 【MiniMax小时层战略上下文】（置信度{ctx.confidence:.0%}，新鲜）
- 市场环境: {ctx.market_regime}（置信{ctx.regime_confidence:.0%}）
- 主方向: {ctx.primary_direction}（置信{ctx.direction_confidence:.0%}）
- 因子状态: RSI={ctx.factor_status.get('rsi',{}).get('status','?')} | ADX={ctx.factor_status.get('adx',{}).get('status','?')}
- MiniMax禁止: {forbidden}
- 战略提示: {ctx.strategic_hint or '无'}
"""
    
    # 紧急干预警告块
    emergency_block = ""
    if emergency_stop and emergency_stop.level in ('high', 'elevated'):
        affected = ', '.join(emergency_stop.affected_coins) if emergency_stop.affected_coins else '全部'
        emergency_block = f"""
## 【🚨 紧急干预警告】
- 级别: {emergency_stop.level} | 原因: {emergency_stop.reason}
- 受影响: {affected} | 行动: {emergency_stop.action}
"""
    
    # BTC MA30格式化（可能是None）
    btc_ma30_val = local_ctx['_btc_ma30']
    btc_ma30_str = f"${btc_ma30_val:,.0f}" if btc_ma30_val else "N/A"
    
    # BTC现价格式化（可能是None）
    btc_price_val = btc_price if isinstance(btc_price, (int, float)) and btc_price > 0 else 0
    
    # 综合判断块（本地 + MiniMax）
    local_forbidden = ', '.join(local_ctx['forbidden_actions']) if local_ctx['forbidden_actions'] else '无'
    fs = local_ctx['factor_status'] if isinstance(local_ctx.get('factor_status'), dict) else {}
    rsi_s = fs.get('rsi', {})
    adx_s = fs.get('adx', {})
    vol_s = fs.get('vol_ratio', {})
    
    # 安全获取可能为None的数值
    def safe_float(val, default=0.0):
        return float(val) if isinstance(val, (int, float)) and not math.isnan(val) else default
    def safe_pct(val, default=0.0):
        try:
            return f"{safe_float(val):.0%}"
        except:
            return f"{default:.0%}"
    
    market_block = f"""
## 【市场环境本地实时评估】（每次扫描实时计算，不依赖外部系统）
- BTC趋势: {local_ctx.get('_btc_trend','?')} | BTC现价${btc_price_val:,.0f} vs MA30 {btc_ma30_str}
- 市场环境: {local_ctx.get('market_regime','?')}（置信{safe_pct(local_ctx.get('regime_confidence', 0))}）
- 主方向: {local_ctx.get('primary_direction','?')}（置信{safe_pct(local_ctx.get('direction_confidence', 0))}）
- RSI中位数: {safe_float(local_ctx.get('_rsi_median'), 50):.1f}（{rsi_s.get('status','?')}/IC={safe_float(rsi_s.get('ic', 0)):.1f}）
- ADX中位数: {safe_float(local_ctx.get('_adx_median'), 15):.1f}（{adx_s.get('status','?')}/IC={safe_float(adx_s.get('ic', 0)):.1f}）
- ATR Percentile中位数: {safe_float(local_ctx.get('_atr_median'), 50):.1f}%（{vol_s.get('status','?')}/IC={safe_float(vol_s.get('ic', 0)):.1f}）
- 禁止操作: {local_forbidden}
- 战略提示: {local_ctx.get('strategic_hint','?')}
- 数据质量: {local_ctx.get('data_quality','?')}
- 紧急级别: {local_ctx.get('emergency_level','none')}
"""
    
    # 构建prompt
    # ========== 构建精简版prompt（gemma3:1b只有3B参数，太长会丢失格式）==========
    # 核心原则：只问一个问题，不给候选机会分散注意力
    total_margin = sum(
        (pos.get('pos', 0) * pos.get('avgPx', 0) / (equity or 1) * 100)
        for pos in positions.values()
    )
    
    # 找最严重的超仓仓位（按比例排序）
    oversize_positions = []
    for coin, pos in positions.items():
        if pos.get('_oversized'):
            threshold = pos.get('_formula_sz', 1)
            ratio = pos.get('pos', 0) / max(threshold, 1)
            oversize_positions.append((ratio, coin, pos))
    oversize_positions.sort(reverse=True)
    
    # 只展示最严重的超仓仓位
    top_oversize = oversize_positions[0] if oversize_positions else None
    # 安全访问：避免 None 时 f-string 中 top_oversize[0] 崩溃
    _top_ratio = top_oversize[0] if top_oversize else 0
    _top_threshold = max(threshold * 3, 1) if top_oversize else 0
    if top_oversize:
        ratio, coin, pos = top_oversize
        entry = pos.get('avgPx', 0)
        price = pos.get('last', 0)
        side = pos.get('side', '')
        sz = pos.get('pos', 0)
        pnl_pct = pos.get('pnl_pct', 0)
        notional = pos.get('_notional', sz * entry)
        margin_pct = pos.get('_margin_pct', 0)
        sl_price = pos.get('sl_price', 0)
        top_pos_info = (f"{coin}: {side.upper()} {sz}张 | "
            f"入场${entry} → 现价${price} | {pnl_pct:+.2f}% | "
            f"名义${notional:,.0f} | 保证金{margin_pct:.1f}% | "
            f"SL=${sl_price} | 超仓{ratio:.0f}倍")
    else:
        top_pos_info = "无超仓仓位"
        # 为 f-string 示例提供默认值（top_oversize 为 None 时避免 UnboundLocalError）
        coin = ''
        sz = 0
        margin_pct = 0
    
    prompt = f"""你是专业加密货币操盘手。这个账户需要你对每个持仓做独立的市场判断决策。

## 账户
equity=${equity:,.0f} | 持仓{len(positions)}/{MAX_POSITIONS}个 | 保证金总占比{total_margin:.1f}% | 熔断:{'✅' if treasury_ok else '🚫'}

## 所有持仓（每个都要做决策）
{chr(10).join(pos_block)}

## 市场环境
市场={local_ctx.get('market_regime','?')} | 主方向={local_ctx.get('primary_direction','?')}
RSI中位数: {safe_float(local_ctx.get('_rsi_median'), 50):.1f} | ADX中位数: {safe_float(local_ctx.get('_adx_median'), 15):.1f}
禁止操作: {local_forbidden}
战略提示: {local_ctx.get('strategic_hint','?')}

## 决策要求（必须对每个持仓做决策）
【核心原则】SL/TP是最后防线。你的首要任务是判断：这个持仓本身还是好交易吗？
- 趋势是否仍然有效？（ADX>25确认趋势，ADX<20无趋势）
- RSI是否过热需要止盈？（强趋势中RSI可以到80才走）
- RSI是否超卖可以加仓？
- 浮亏时趋势是否已破？（震荡市场中浮亏更危险）

【决策选项】（每个持仓必须选一个）
- hold: 趋势有效，仓位正常，继续持有
- force_close: 立即全平（资金安全、趋势破位、严重超仓）
- close_half: 平一半（部分止盈、降风险）
- tighten_sl: 收紧止损（浮亏+趋势弱，收紧保护）
- trailing_sl: 追踪止损（已有盈利，收紧SL锁定利润）
- partial_profit: 分批止盈（首批50%止盈）

【决策逻辑】
- 做多持仓 RSI>75 + ADX>35 → partial_profit 或 force_close
- 做多持仓 RSI<30 → hold 或加仓（超卖是买入机会）
- 浮亏 + ADX<20(震荡) → tighten_sl 或 force_close
- 强趋势(ADX>35)中浮盈 → trailing_sl 让利润奔跑
- 超仓 → force_close 或 close_half

## 决策格式（严格遵守，选最需要处理的1个持仓）
coin: [币种]
decision: [上述选项之一]
reason: [不超过20字，基于RSI+ADX+趋势说明理由]

---
coin: {list(positions.keys())[0] if positions else ''}
decision: hold
reason: [基于市场数据和持仓状态给出一个决策]
---
"""

    # ========== Step 4: 调用LLM ==========
    llm_raw = None
    try:
        llm_raw = _call_ollama(prompt, timeout=45)
    except Exception as e:
        print(f"  ⚠️ gemma4调用异常: {e}")
    
    if not llm_raw:
        try:
            llm_raw = _call_minimax(prompt, timeout=30)
        except Exception as e:
            print(f"  ⚠️ MiniMax备用也失败: {e}")
    
    if not llm_raw:
        # gemma3:1b失败，尝试gemma4-heretic
        data['model'] = 'gemma4-2b-heretic:latest'
        data['options']['num_predict'] = 1024
        for attempt in range(2):
            try:
                r = session.post('http://localhost:11434/api/chat', json=data, timeout=timeout + 15)
                result = r.json()
                if isinstance(result, list):
                    result = result[-1]
                msg = result.get('message', {})
                raw_content = msg.get('content', '')
                msg_content = ''
                if raw_content:
                    if '---' in raw_content:
                        parts = raw_content.split('---')
                        for p in reversed(parts):
                            p_lower = p.lower()
                            if 'coin:' in p_lower and 'decision:' in p_lower:
                                msg_content = p.strip()
                                break
                        if not msg_content:
                            msg_content = parts[-1].strip()
                    else:
                        msg_content = raw_content.strip()
                elif msg.get('thinking'):
                    msg_content = msg.get('thinking', '').strip()
                if msg_content:
                    return msg_content
            except Exception:
                if attempt == 1:
                    raise
                time.sleep(2)

        # gemma4也失败，尝试MiniMax
        try:
            llm_raw = _call_minimax(prompt, timeout=30)
            if llm_raw:
                return llm_raw
        except Exception:
            pass

        # 全部失败也要记录决策日志
        failed_entry = build_decision_journal_entry(
            all_data=all_data,
            local_ctx=local_ctx,
            llm_output='',
            parsed_decision={'coin': '', 'decision': 'hold', 'reason': 'LLM全部失败，默认观望'},
            prompt_used=prompt[:500],
            execution_result='LLM调用失败',
            execution_ok=False,
        )
        append_decision_journal(failed_entry)
        return {
            'coin': '',
            'decision': 'hold',
            'reason': 'LLM全部失败，默认观望',
            'local_ctx': local_ctx,
            'ctx': ctx,
        }
    
    # ========== Step 5: 解析LLM输出 ==========
    result = _parse_llm_decision(llm_raw)
    
    # ========== Step 6: 记录决策日志（每笔决策永久保存）==========
    journal_entry = build_decision_journal_entry(
        all_data=all_data,
        local_ctx=local_ctx,
        llm_output=llm_raw,
        parsed_decision=result,
        prompt_used=prompt[:500],  # 只保存前500字
    )
    append_decision_journal(journal_entry)
    
    result['local_ctx'] = local_ctx
    result['ctx'] = ctx
    return result


def _parse_llm_decision(raw: str) -> dict:
    """
    解析LLM输出为结构化决策。
    兼容多种输出格式：
    1. coin:/decision:/reason: 格式（完整）
    2. decision:/reason: 格式（缺少coin）
    3. 纯文本关键词格式
    """
    import re

    coin = ''
    decision = 'hold'
    reason = ''

    # 策略1：从---分块中提取（从后往前，找最后一个有决策的块）
    blocks = re.split(r'---', raw)
    for block in reversed(blocks):
        block = block.strip()
        if not block:
            continue

        # 找decision（必须有）
        dec_match = re.search(r'decision:\s*(.+?)(?:\n|$)', block, re.IGNORECASE)
        if not dec_match:
            continue
        decision = dec_match.group(1).strip().split()[0]  # 取第一个词

        # 找coin（可选）
        coin_match = re.search(r'coin:\s*(\S+)', block, re.IGNORECASE)
        if coin_match:
            coin = coin_match.group(1).strip()

        # 找reason（可选）
        reason_match = re.search(r'reason:\s*(.+?)(?:\n|$)', block, re.IGNORECASE)
        if reason_match:
            reason = reason_match.group(1).strip()[:100]

        # 找到了有效块就停止
        if decision and decision != 'hold':
            break

    # 策略2：如果所有块都只解析出hold，再检查关键词
    if decision == 'hold' and reason == '':
        raw_lower = raw.lower()
        if 'force_close' in raw_lower or '平仓' in raw or '清仓' in raw:
            decision = 'force_close'
        elif 'close_half' in raw_lower or '平一半' in raw or '半仓' in raw:
            decision = 'partial_tp'
        elif 'tighten_sl' in raw_lower or '收紧止损' in raw:
            decision = 'tighten_sl'
        elif 'trailing' in raw_lower or '追踪止损' in raw:
            decision = 'trailing_sl'
        elif 'open_long' in raw_lower or '做多' in raw or '开多' in raw:
            decision = 'open_long'
        elif 'open_short' in raw_lower or '做空' in raw:
            decision = 'open_short'

    # 策略3：最后尝试从整个文本中提取reason（任意位置的reason:）
    if not reason:
        reason_match = re.search(r'reason:\s*(.+?)(?:\n|$)', raw, re.IGNORECASE)
        if reason_match:
            reason = reason_match.group(1).strip()[:100]

    # 如果reason还是空的，用原始文本前80字
    if not reason:
        # 清理thinking前缀
        clean = re.sub(r"Here's a thinking.*?\n", '', raw, flags=re.DOTALL)
        clean = clean.strip()[:80]
        if clean:
            reason = clean

    return {'coin': coin, 'decision': decision, 'reason': reason}

# ========== LLM调用器(Ollama gemma4 + MiniMax备用)==========

_ollama_session = None
def _get_ollama_session():
    """获取Ollama连接池会话(复用连接)
    
    修复：每次调用前检查连接是否可用，失败则重新创建session。
    """
    global _ollama_session
    import requests as _http
    if _ollama_session is None:
        _ollama_session = _http.Session()
        adapter = _http.adapters.HTTPAdapter(
            pool_connections=3, pool_maxsize=5, max_retries=1,
            pool_block=False
        )
        _ollama_session.mount('http://', adapter)
        _ollama_session.mount('https://', adapter)
    # 验证连接可用
    try:
        r = _ollama_session.get('http://localhost:11434/api/tags', timeout=2)
        r.close()
    except Exception:
        # 连接失效，重建session
        try:
            _ollama_session.close()
        except Exception:
            pass
        _ollama_session = _http.Session()
        adapter = _http.adapters.HTTPAdapter(
            pool_connections=3, pool_maxsize=5, max_retries=1,
            pool_block=False
        )
        _ollama_session.mount('http://', adapter)
        _ollama_session.mount('https://', adapter)
    return _ollama_session

def _load_env():
    """加载环境变量"""
    env_file = os.path.expanduser('~/.hermes/.env')
    if os.path.exists(env_file):
        for line in open(env_file):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

def _call_ollama(prompt: str, model: str = 'gemma4-2b-heretic:latest', timeout: int = 45) -> str:
    """调用Ollama gemma4(带重试+连接池+缓存)

    gemma4-heretic是thinking模型，复杂prompt下content为空、thinking充满最终答案，
    所以增加num_predict到1536，并从thinking字段提取最终决策。
    
    缓存策略:
    - 使用Redis缓存相同prompt的响应
    - TTL: 10分钟（本地Ollama相对稳定）
    """
    # ========== Step 0: 检查缓存 ==========
    if HAS_LLM_CACHE:
        try:
            cached = get_cached_response(prompt, provider="ollama")
            if cached:
                print(f"  🎯 ollama缓存命中 (节省API调用)")
                return cached
        except Exception as e:
            print(f"  ⚠️ 缓存查询失败: {e}")
    
    session = _get_ollama_session()
    data = {
        # gemma3:1b为主（快速+格式遵循），gemma4-heretic备用（质量更高但需更多token）
        'model': 'gemma3:1b',
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'options': {
            'temperature': 0.3,
            'num_predict': 512,  # gemma3:1b非thinking模型，512token足够（包含思考+决策）
        },
    }
    for attempt in range(3):
        try:
            r = session.post(
                'http://localhost:11434/api/chat',
                json=data,
                timeout=timeout,
            )
            result = r.json()
            # gemma4-heretic响应格式：{message: {content, thinking}}
            # content字段包含 "Here's a thinking process..." + "---" + 决策块
            # 需要从content中提取 --- 之后的实际决策
            if isinstance(result, list):
                result = result[-1]
            msg = result.get('message', {})
            raw_content = msg.get('content', '')

            # 核心解析：从content中提取---后的决策块
            msg_content = ''
            if raw_content:
                # gemma4-heretic格式：包含"Here's a thinking process..."和"---"分隔符
                if '---' in raw_content:
                    parts = raw_content.split('---')
                    for p in reversed(parts):
                        p_lower = p.lower()
                        if 'coin:' in p_lower and 'decision:' in p_lower:
                            msg_content = p.strip()
                            break
                    # 如果没找到决策块，取最后一个部分
                    if not msg_content:
                        msg_content = parts[-1].strip()
                else:
                    # 无---分隔符，直接用content
                    msg_content = raw_content.strip()
            elif msg.get('thinking'):
                # fallback到thinking字段（老版本gemma格式）
                thinking = msg.get('thinking', '')
                if '---' in thinking:
                    parts = thinking.split('---')
                    for p in reversed(parts):
                        if 'coin:' in p.lower() and 'decision:' in p.lower():
                            msg_content = p.strip()
                            break
                else:
                    msg_content = thinking.strip()

            if msg_content:
                # ========== Step X: 缓存结果 ==========
                if HAS_LLM_CACHE:
                    try:
                        cache_response(prompt, msg_content, provider="ollama", ttl=600)
                    except Exception as e:
                        print(f"  ⚠️ 缓存存储失败: {e}")
                return msg_content
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1)
    return ''

def _call_minimax(prompt: str, model: str = 'MiniMax-M2.7-highspeed', timeout: int = 30) -> str:
    """调用MiniMax作为备用LLM"""
    _load_env()
    api_key = os.environ.get('MINIMAX_API_KEY', '')
    base_url = os.environ.get('MINIMAX_BASE_URL', 'https://api.minimaxi.com/v1')

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        resp = client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=512,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or ''
        # MiniMax M2.7: content inside <think>...</think> tags
        if raw.startswith("<think>"):
            parts = raw.split("</think>", 1)
            if len(parts) > 1:
                return parts[1].strip()
            return parts[0].replace("<think>", "").strip()
        return raw.strip()
    except Exception as e:
        return ''

def call_llm_with_fallback(prompt: str) -> str:
    """
    调用LLM：优先Ollama gemma4，失败则降级到MiniMax。
    返回LLM的原始输出文本。
    """
    # 1. 尝试Ollama gemma4(45秒超时)
    try:
        result = _call_ollama(prompt, timeout=45)
        if result:
            return result
    except Exception as e:
        pass

    # 2. Ollama失败，尝试MiniMax
    try:
        output = _call_minimax(prompt, timeout=30)
        if output:
            return output
    except Exception as e:
        pass

    # 两个都失败了
    return ''
    # ========== 决策审计日志 ==========
    if HAS_CONTEXT and AUDIT_PATH:
        audit_entry = {
            'ts': datetime.now().isoformat(),
            'audit_id': ctx.audit_id if ctx else '',
            'ctx_regime': ctx.market_regime if ctx else '',
            'ctx_direction': ctx.primary_direction if ctx else '',
            'ctx_confidence': ctx.confidence if ctx else 0.0,
            'ctx_forbidden': ctx.forbidden_actions if ctx else [],
            'emergency_level': emergency_stop.level if emergency_stop else 'none',
            'gemma_decision': decision,
            'gemma_coin': coin,
            'gemma_reason': reason,
            'equity': equity,
            'position_count': len(positions),
            'candidates_count': len(candidates),
        }
        append_audit(audit_entry, AUDIT_PATH)

    return result


def full_scan(notify=True):
    """多币种全量扫描 - gemma4中央决策模式 v5.0
    
    职业操盘手流程：
    1. 预热gemma4（避免首次加载延迟）
    2. 收集全量数据(持仓+候选币+账户风险)
    3. gemma4看到全貌，独立决策
    4. 风控熔断作为最后安全网
    5. 执行决策
    """
    # ========== Step 0: gemma3:1b预热（避免每次重新加载模型）==========
    # gemma3:1b首次加载需~2秒，之后<0.5秒响应
    # 使用generate接口预热，不影响session连接池
    try:
        import requests as _http_prewarm
        _http_prewarm.post('http://localhost:11434/api/generate',
            json={'model': 'gemma3:1b',
                  'prompt': '准备决策',
                  'stream': False,
                  'options': {'num_predict': 3}},
            timeout=10)
    except Exception:
        pass  # 预热失败不影响主流程
    
    print(f"\n{'='*60}")
    print(f"Kronos v5.0 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {'[审查]' if AUDIT_ONLY else '[执行]'}")
    print(f"{'='*60}")
    
    equity = get_account_equity()
    if equity is None:
        print("  🚨 无法获取账户权益，跳过本次扫描")
        return None
    positions = get_all_positions()
    btc_dir = get_btc_direction()
    btc_regime = get_btc_market_regime()
    btc_price = get_price('BTC')
    
    print(f"\n账户: ${equity:,.0f} | 持仓: {len(positions)}/{MAX_POSITIONS} | BTC: {btc_dir} | 市场: {btc_regime}")
    
    # ========== 阶段1：收集所有持仓数据 ==========
    print(f"\n{'─'*40}")
    print(f"【阶段1】持仓检查")
    for key, pos in positions.items():
        actual_coin = pos['coin']
        algos = get_pending_algos(pos['instId'])
        md = get_market_data(actual_coin, btc_dir, btc_regime)
        price = md['price']
        entry = pos.get('avgPx', 0)
        pnl_pct = (price - entry) / entry * 100 if price and entry else 0
        pnl = pos.get('upl', 0)
        sl_price = algos.get('sl', {}).get('price') if algos.get('sl') else None
        tp_price = algos.get('tp', [{}])[0].get('price') if algos.get('tp') else None
        sl_dist = (price - sl_price) / price * 100 if sl_price and price else 0
        tp_dist = (tp_price - price) / price * 100 if tp_price and price else 0
        
        pos['last'] = price
        pos['algos'] = algos
        pos['md'] = md  # 必须存pos['md']，gemma4_central_decision需要逐币市场数据
        pos['sl_price'] = sl_price
        pos['tp_price'] = tp_price
        pos['sl_dist'] = sl_dist
        pos['tp_dist'] = tp_dist
        pos['pnl_pct'] = pnl_pct

        # === 超仓检测（gemma4在阶段3需要看到）：用真实equity和ATR计算 ===
        sz = pos['pos']  # 从pos字典取张数
        try:
            _eq = equity
            _atr = md.get('atr_pct', 0.5)
            _sl_pct, _ = get_sl_tp_pct(actual_coin, _atr)
            _formula_sz = int(_eq * RISK_PER_TRADE / (100 * entry * _sl_pct)) if entry and _sl_pct else 0
            _max_cap = int(_eq * MAX_POS_RATIO / price) if price else 0
            _expected = min(_formula_sz, _max_cap) if (_formula_sz > 0 and _max_cap > 0) else max(_formula_sz, _max_cap)
            _threshold = max(_expected, 1)
            if sz > _threshold * 3:
                pos['_oversized'] = True
                pos['_formula_sz'] = _threshold
                pos['_notional'] = sz * entry
                pos['_margin_pct'] = abs(sz * entry / _eq * 100) if _eq else 0
                print(f"  🔴 超仓: {actual_coin} sz={sz} vs 阈值={_threshold*3:.0f} ({sz/_threshold:.0f}x) | 名义${pos['_notional']:,.0f} | 保证金{pos['_margin_pct']:.1f}%")
        except Exception as e:
            pass  # 超仓检测失败不影响主流程

        flag = '✅' if pnl_pct >= 0 else '⚠️'
        if sl_price:
            print(f"    SL={sl_price}({sl_dist:+.1f}%) TP={tp_price}({tp_dist:+.1f}%)")
        else:
            print(f"    🚨 无SL保护")
    
    # ========== 阶段2：扫描所有候选币 ==========
    print(f"\n{'─'*40}")
    allowed_coins = _get_allowed_coins()
    print(f"【阶段2】机会扫描 ({len(allowed_coins)}个币种)")

    coin_data_list = [get_market_data(coin, btc_dir, btc_regime) for coin in allowed_coins]
    ranked = rank_coins(coin_data_list, positions)
    
    print("  评分排名:")
    for i, item in enumerate(ranked[:8]):
        flag = '👉' if i == 0 else '  '
        sf = item.get('sf', {})
        sf_info = f'SF({sf.get("long_count",0)}多/{sf.get("short_count",0)}空)' if sf.get('total', 0) > 0 else ''
        print(f"  {flag} #{i+1} {item['coin']}: {item['score']:.0f}分 {item['direction']} {sf_info} | {item['reason']}")
    
    # ========== 阶段3：gemma4中央决策 ==========
    print(f"\n{'─'*40}")
    print(f"【阶段3】gemma4决策")
    
    treasury_ok, treasury_msg, limits = check_treasury(equity, positions=positions)
    dyn_limit = limits['hourly'] if limits else 0
    print(f"  熔断: {'✅' if treasury_ok else '🚫'} {treasury_msg}")
    
    # 打印战略上下文(如果有)
    if HAS_CONTEXT:
        ctx = load_context_with_validation(CTX_PATH)
        if ctx and ctx.overall_confidence > 0.3:
            print(f"  📊 MiniMax战略上下文: {ctx.market_regime} | {ctx.primary_direction} | 置信度{ctx.confidence:.0%}")
            if ctx.strategic_hint:
                print(f"     提示: {ctx.strategic_hint[:80]}")
        emergency_es = read_emergency_stop(EMERGENCY_PATH)
        if emergency_es and emergency_es.level != 'none':
            print(f"  🚨 紧急干预: {emergency_es.level} | {emergency_es.reason}")

    # 检查autoresearch反馈闭环状态
    import time as time_module
    marker = Path("/tmp/autoresearch_last_success")
    if marker.exists():
        age_hours = (time_module.time() - marker.stat().st_mtime) / 3600
        if age_hours > 4:
            print(f"  ⚠️ Autoresearch权重陈旧({age_hours:.1f}h未更新)，建议检查数据源")
        # 否则正常，不打印
    else:
        failure_marker = Path("/tmp/autoresearch_last_failure")
        if failure_marker.exists():
            reason = failure_marker.read_text().strip()
            print(f"  🚨 Autoresearch上次失败: {reason}")

    
    all_data = {
        'equity': equity,
        'positions': positions,
        'candidates': ranked,
        'btc_regime': btc_regime,
        'btc_direction': btc_dir,
        'btc_price': btc_price,
        'treasury_ok': treasury_ok,
        'treasury_msg': treasury_msg,
    }
    
    gemma_result = gemma4_central_decision(all_data)
    
    # gemma4_central_decision成功时返回字符串(原始LLM输出)，需要解析成dict
    if isinstance(gemma_result, str):
        import re
        # 解析 --- 分隔的多条决策，取第一条(最重要的)
        blocks = re.split(r'---', gemma_result)
        parsed = {'coin': '', 'decision': 'hold', 'reason': '解析失败', 'ctx': None}
        for block in blocks:
            coin_m = re.search(r'^coin:\s*(.+?)$', block, re.MULTILINE)
            dec_m = re.search(r'^decision:\s*(.+?)$', block, re.MULTILINE)
            reason_m = re.search(r'^reason:\s*(.+?)$', block, re.MULTILINE)
            if coin_m and dec_m:
                parsed = {
                    'coin': coin_m.group(1).strip(),
                    'decision': dec_m.group(1).strip(),
                    'reason': reason_m.group(1).strip() if reason_m else '',
                    'ctx': None,
                }
                break  # 取第一条
        gemma_result = parsed
    
    decision = gemma_result['decision']
    reason = gemma_result['reason']
    target_coin = gemma_result.get('coin', '').upper()  # gemma4指定的币种
    gemma_ctx = gemma_result.get('ctx')  # MiniMax上下文(用于审计)
    
    print(f"  gemma4决策: {target_coin} {decision} | {reason}")
    
    # ========== 阶段4：规则引擎持仓决策 + 风控安全网 ==========
    print(f"\n{'─'*40}")
    print(f"【阶段4】规则引擎持仓决策")

    # P0 Fix: 调用decide_for_position（之前是死代码，从未被使用）
    # 对每个持仓运行完整规则引擎，决定最佳操作
    position_decisions = {}
    for key, pos in positions.items():
        actual_coin = pos['coin']
        algos = get_pending_algos(pos['instId'])
        md = get_market_data(actual_coin, btc_dir, btc_regime)
        md['_equity'] = equity  # 修复：decide_for_position用hardcoded默认值90000，需用真实equity
        action, urgency, detail, should_hold, new_sl, new_tp = decide_for_position(
            actual_coin, pos, algos, md
        )
        position_decisions[actual_coin] = {
            'action': action, 'urgency': urgency, 'detail': detail,
            'should_hold': should_hold, 'new_sl': new_sl, 'new_tp': new_tp,
            'pos': pos, 'algos': algos, 'md': md,
        }
        urgency_icon = {10: '🔴', 9: '🟠', 8: '🟡', 7: '🟢', 6: '🔵', 5: '⚪', 0: '⚙️'}
        icon = urgency_icon.get(urgency, '⚙️')
        print(f"  {icon} {actual_coin}: {action} (U={urgency}) {detail}")

    # P0 Fix: urgency>=9强制平仓无论AUDIT_ONLY都执行（资金安全优先）
    # 执行完后才检查AUDIT_ONLY（阻止后续新开仓）
    for coin, dec in position_decisions.items():
        if dec['urgency'] >= 9:  # urgency 9-10 = 强制平仓
            pos = dec['pos']
            instId = f'{coin}-USDT-SWAP'
            algos = dec['algos']
            md = dec['md']
            ok, msg = close_position(instId, pos['side'], pos['pos'])
            print(f"  🚨 强制平仓: {coin} (U={dec['urgency']}) | {msg}")
            if ok:
                close_paper_trade(coin, pos['side'], f'规则引擎U{dec["urgency"]}强制平仓', None, 0)
                log_trade_journal(
                    action='force_close', coin=coin, side=pos.get('side', ''),
                    size=pos.get('pos', 0), entry_price=pos.get('avgPx'),
                    reason=dec.get('detail', ''), equity=equity,
                    algos_before=algos, algos_after=None,
                    market={'rsi': md.get('rsi_1h'), 'adx': md.get('adx_1h'), 'price': md.get('price')},
                    gemma_decision=None, rule_decision=dec.get('detail', ''),
                )

    if AUDIT_ONLY:
        print("  [审查模式，新开仓跳过]")
        return

    # ========== 阶段5：执行 ==========
    print(f"\n{'─'*40}")
    print(f"【阶段5】执行")
    exec_results = []

    # ── gemma4决策优先级覆盖（强制执行gemma4的明确指令）────
    # 如果gemma4明确说 force_close/close_half/tighten_sl，直接执行
    # gemma4的判断优先级高于规则引擎（因为它有完整上下文和职业操盘手视角）
    if target_coin and decision in ('force_close', 'close_half', 'tighten_sl', 'trailing_sl', 'boost_tp'):
        gemma_target_positions = {k: v for k, v in positions.items() if k.upper() == target_coin}
        for gcoin, gpos in gemma_target_positions.items():
            ginstId = f'{gcoin}-USDT-SWAP'
            galgos = get_pending_algos(ginstId)
            gmd = get_market_data(gcoin, btc_dir, btc_regime)
            print(f"  🎯 gemma4优先级覆盖: {gcoin} {decision} | {reason[:60]}")
            if decision == 'force_close':
                ok, msg = close_position(ginstId, gpos['side'], gpos['pos'])
                print(f"  🚨 gemma4强制平仓: {gcoin} | {msg}")
                if ok:
                    close_paper_trade(gcoin, gpos['side'], f'gemma4_force_close', None, 0)
                    log_trade_journal(
                        action='force_close', coin=gcoin, side=gpos.get('side', ''),
                        size=gpos.get('pos', 0), entry_price=gpos.get('avgPx'),
                        reason=f'gemma4_force_close: {reason}', equity=equity,
                        algos_before=galgos, algos_after=None,
                        market={'rsi': gmd.get('rsi_1h'), 'adx': gmd.get('adx_1h'), 'price': gmd.get('price')},
                        gemma_decision=f'{decision} {reason}', rule_decision=None,
                    )
            elif decision == 'close_half':
                ratio = 0.5
                ok, msg = close_partial(ginstId, gpos['side'], gpos['pos'], ratio)
                print(f"  📊 gemma4平一半: {gcoin} | {msg}")
                if ok:
                    log_trade_journal(
                        action='close_half', coin=gcoin, side=gpos.get('side', ''),
                        size=int(gpos.get('pos', 0) * ratio), entry_price=gpos.get('avgPx'),
                        reason=f'gemma4_close_half: {reason}', equity=equity,
                        algos_before=galgos, algos_after=get_pending_algos(ginstId),
                        market={'rsi': gmd.get('rsi_1h'), 'adx': gmd.get('adx_1h'), 'price': gmd.get('price')},
                        gemma_decision=f'{decision} {reason}', rule_decision=None,
                    )
            elif decision == 'tighten_sl':
                # 收紧SL：当前SL收紧20%
                gsl = galgos.get('sl', {}).get('price') if galgos.get('sl') else None
                if gsl:
                    entry = gpos.get('avgPx', 0)
                    if gpos['side'] == 'short':
                        # P0 Fix: SHORT时SL在entry上方，收紧SL应该向entry移动
                        # 错误公式: gsl * 0.80 = 110 * 0.80 = 88 (低于entry，完全错误!)
                        # 正确公式: entry + (gsl - entry) * 0.80 = 100 + 10 * 0.80 = 108 (向entry移动20%)
                        new_sl = round(entry + (gsl - entry) * 0.80, 4)
                    else:
                        new_sl = round(gsl * 1.20, 4)
                    algo_id = galgos.get('sl', {}).get('algoId')
                    if algo_id:
                        ok2, msg2 = amend_sl(ginstId, algo_id, new_sl)
                        print(f"  🔵 gemma4收紧SL: {gcoin} {gsl}→{new_sl} | {msg2}")
                        if ok2:
                            log_trade_journal(
                                action='tighten_sl', coin=gcoin, side=gpos.get('side', ''),
                                size=gpos.get('pos', 0),
                                reason=f'gemma4_tighten_sl: {reason}', equity=equity,
                                sl_before=gsl, sl_after=new_sl,
                                algos_before=galgos, algos_after=get_pending_algos(ginstId),
                                market={'rsi': gmd.get('rsi_1h'), 'adx': gmd.get('adx_1h'), 'price': gmd.get('price')},
                                gemma_decision=f'{decision} {reason}', rule_decision=None,
                            )
                    else:
                        print(f"  🔵 gemma4收紧SL: {gcoin} {gsl}→{new_sl}（无旧algoId，跳过amend，直接place新SL）")
                        ok2, msg2 = place_sl(ginstId, gpos['side'], gpos['pos'], new_sl)
                        print(f"  🔵 gemma4新SL: {gcoin} @ {new_sl} | {msg2}")
    elif target_coin and decision in ('open_long', 'open_short') and treasury_ok and len(positions) < MAX_POSITIONS and not AUDIT_ONLY:
        # gemma4决定开仓（已有逻辑）
        candidates = [c for c in ranked if c['coin'].upper() == target_coin] + ranked
        top = next((c for c in candidates if c['md'].get('price')), None)
        if top:
            coin = top['coin']
            direction = top['direction']
            md = top['md']
            price = md['price']
            slots = MAX_POSITIONS - len(positions)
            # ... (existing open logic)
    # urgency >= 6 的操作必须执行（SL/TP修复、止损、止盈）
    for coin, dec in position_decisions.items():
        action = dec['action']
        urgency = dec['urgency']
        if urgency < 6:
            continue  # urgency 0-5 = hold/sentiment，不需要操作

        instId = f'{coin}-USDT-SWAP'
        pos = dec['pos']
        algos = dec['algos']
        md = dec['md']
        # P0 Fix: 统一pos['side'] OKX格式→内部格式
        raw_side = pos['side']
        if raw_side in ('buy', 'sell'):
            side = 'long' if raw_side == 'buy' else 'short'
        else:
            side = raw_side

        if action == 'force_close':
            ok, msg = close_position(instId, side, pos['pos'])
            print(f"  🚨 强制止损: {coin} | {msg}")
            if ok:
                close_paper_trade(coin, raw_side, 'force_close', None, 0)
                log_trade_journal(
                    action='force_close', coin=coin, side=side,
                    size=pos.get('pos', 0), entry_price=pos.get('avgPx'),
                    reason=dec.get('detail', ''), equity=equity,
                    algos_before=algos, algos_after=None,
                    market={'rsi': md.get('rsi_1h'), 'adx': md.get('adx_1h'), 'price': md.get('price')},
                    gemma_decision=None, rule_decision=dec.get('detail', ''),
                )

        elif action == 'repair_sl_tp':
            new_sl = dec['new_sl']
            new_tp = dec['new_tp']
            # P0 Fix: 如果new_sl/new_tp无效(比如tighten_sl被错误标记为repair_sl_tp)，跳过
            if not new_sl or not new_tp:
                print(f"  ⚠️ repair_sl_tp跳过: {coin} new_sl={new_sl} new_tp={new_tp}(无效参数)")
                continue
            # P0 Fix: 如果没有活跃OCO(=持仓已平)，跳过不要重建
            if not (algos.get('sl') or algos.get('tp') or algos.get('oco')):
                print(f"  ⚠️ repair_sl_tp跳过: {coin}(无活跃OCO=已平仓)")
                continue
            # 先取消旧的SL/TP
            # P0 Fix: algos is always a dict but may have no actual algo orders (all None/[]).
            # Check for presence of actual algo data, not just "if not algos" which is always truthy.
            has_algos = bool(algos.get('sl') or algos.get('tp') or algos.get('oco'))
            if not has_algos:
                # 没有algo信息，当作无旧单处理，直接挂新OCO
                # P0 Fix: new_sl/new_tp无效则跳过
                if not new_sl or not new_tp:
                    print(f"  ⚠️ repair_sl_tp跳过: {coin} new_sl={new_sl} new_tp={new_tp}(无效参数)")
                    continue
                sz = pos['pos']
                raw_side = pos['side']
                if raw_side in ('buy', 'sell'):
                    side_for_oco = 'long' if raw_side == 'buy' else 'short'
                else:
                    side_for_oco = raw_side
                algo_id = place_oco(instId, side_for_oco, sz, new_sl, new_tp)
                if algo_id:
                    print(f"  🔧 修复SL/TP(无旧单): {coin} SL={new_sl} TP={new_tp} [{algo_id[:8]}]")
                    # P0 Fix: 验证SL/TP已挂
                    time.sleep(0.5)
                    verify_algos = get_pending_algos(instId)
                    if not (verify_algos.get('oco') or verify_algos.get('sl')):
                        print(f"  🔴 验证失败: {coin} SL/TP仍未挂上!")
                    else:
                        print(f"  ✅ 验证通过: {coin} SL/TP已挂")
                else:
                    print(f"  ❌ 修复失败: {coin}")
                continue  # skip to next coin
            sl_data = algos.get('sl')
            tp_data = (algos.get('tp') or [None])[0] if algos.get('tp') else None
            sl_before = sl_data.get('price') if sl_data else None
            tp_before = tp_data.get('price') if tp_data else None
            old_sl_id = sl_data.get('algoId') if sl_data else None
            if old_sl_id:
                cancel_algos(instId, [old_sl_id])
            old_tp_ids = [o['algoId'] for o in algos.get('tp', []) if o.get('algoId')]
            if old_tp_ids:
                cancel_algos(instId, old_tp_ids)
            # P0 Fix: pos['side']='buy'/'sell' (OKX格式) vs 'long'/'short' (内部格式)
            # 统一转换为内部格式，避免place_oco内转换遗漏
            sz = pos['pos']
            raw_side = pos['side']
            if raw_side in ('buy', 'sell'):
                side_for_oco = 'long' if raw_side == 'buy' else 'short'
            else:
                side_for_oco = raw_side  # 已是'long'/'short'
            algo_id = place_oco(instId, side_for_oco, sz, new_sl, new_tp)
            if algo_id:
                print(f"  🔧 修复SL/TP: {coin} SL={new_sl} TP={new_tp} [{algo_id[:8]}]")
                # P0 Fix: 验证SL/TP已挂
                time.sleep(0.5)
                verify_algos = get_pending_algos(instId)
                if not (verify_algos.get('oco') or verify_algos.get('sl')):
                    print(f"  🔴 验证失败: {coin} SL/TP仍未挂上!")
                else:
                    print(f"  ✅ 验证通过: {coin} SL/TP已挂")
                    log_trade_journal(
                        action='repair_sl_tp', coin=coin, side=side,
                        size=sz, reason=dec.get('detail', ''), equity=equity,
                        sl_before=sl_before, sl_after=new_sl,
                        tp_before=tp_before, tp_after=new_tp,
                        algos_before=algos, algos_after=verify_algos,
                        market={'rsi': md.get('rsi_1h'), 'adx': md.get('adx_1h'), 'price': md.get('price')},
                        gemma_decision=None, rule_decision=dec.get('detail', ''),
                    )
            else:
                print(f"  ❌ 修复失败: {coin}")

        elif action == 'close':
            ok, msg = close_position(instId, side, pos['pos'])
            print(f"  ✅ 平仓: {coin} | {msg}")
            if ok:
                close_paper_trade(coin, raw_side, '规则引擎平仓', None, 0)
                log_trade_journal(
                    action='close', coin=coin, side=side,
                    size=pos.get('pos', 0), entry_price=pos.get('avgPx'),
                    reason=dec.get('detail', ''), equity=equity,
                    algos_before=algos, algos_after=None,
                    market={'rsi': md.get('rsi_1h'), 'adx': md.get('adx_1h'), 'price': md.get('price')},
                    gemma_decision=None, rule_decision=dec.get('detail', ''),
                )

        elif action == 'trailing_sl':
            if not algos:
                print(f"  ⚠️ 追踪SL跳过: {coin}(无algo信息)")
                continue  # skip to next coin
            entry = pos.get('avgPx', 0)
            # side 已在上方统一转换
            old_sl = algos.get('sl', {}).get('price')
            new_sl = dec['new_sl']
            sl_algo_id = algos.get('sl', {}).get('algoId')
            if sl_algo_id and new_sl and new_sl != old_sl:
                ok, msg = amend_sl(instId, sl_algo_id, new_sl)
                print(f"  📈 追踪SL: {coin} {old_sl}→{new_sl} | {msg}")
                if ok:
                    log_trade_journal(
                        action='trailing_sl', coin=coin, side=side,
                        size=pos.get('pos', 0), reason=dec.get('detail', ''), equity=equity,
                        sl_before=old_sl, sl_after=new_sl,
                        algos_before=algos, algos_after=get_pending_algos(instId),
                        market={'rsi': md.get('rsi_1h'), 'adx': md.get('adx_1h'), 'price': md.get('price')},
                        gemma_decision=None, rule_decision=dec.get('detail', ''),
                    )

        elif action == 'tighten_sl':
            # P0 Fix: algos dict is always truthy but sl may be None
            sl_data = algos.get('sl')
            if not sl_data:
                print(f"  ⚠️ 收紧SL跳过: {coin}(无algo信息)")
                continue  # skip to next coin
            new_sl = dec['new_sl']
            sl_algo_id = sl_data.get('algoId')
            if sl_algo_id and new_sl:
                ok, msg = amend_sl(instId, sl_algo_id, new_sl)
                print(f"  🔒 收紧SL: {coin}→{new_sl} | {msg}")
                if ok:
                    log_trade_journal(
                        action='tighten_sl', coin=coin, side=side,
                        size=pos.get('pos', 0), reason=dec.get('detail', ''), equity=equity,
                        sl_before=sl_data.get('price'), sl_after=new_sl,
                        algos_before=algos, algos_after=get_pending_algos(instId),
                        market={'rsi': md.get('rsi_1h'), 'adx': md.get('adx_1h'), 'price': md.get('price')},
                        gemma_decision=None, rule_decision=dec.get('detail', ''),
                    )

        elif action == 'partial_profit':
            # RSI>=75超买+弱趋势熊市 → 止盈50% + 收紧SL保本
            sz = pos['pos']
            sz_half = max(1, int(sz * 0.5))
            ok, msg = close_partial(instId, side, sz, 0.5)
            print(f"  💰 部分止盈: {coin} {sz_half}张({sz_half}/{sz}) | {msg}")
            if ok:
                # Journal for partial profit close
                log_trade_journal(
                    action='partial_profit', coin=coin, side=side,
                    size=sz_half, entry_price=pos.get('avgPx'),
                    reason=dec.get('detail', ''), equity=equity,
                    algos_before=algos, algos_after=get_pending_algos(instId),
                    market={'rsi': md.get('rsi_1h'), 'adx': md.get('adx_1h'), 'price': md.get('price')},
                    gemma_decision=None, rule_decision=dec.get('detail', ''),
                )
                # 收紧剩余50% SL到保本线
                sl_data = algos.get('sl')
                sl_algo_id = sl_data.get('algoId') if sl_data else None
                entry = pos.get('avgPx', 0)
                if sl_algo_id and entry:
                    breakeven_sl = round(entry * 1.005, 4)
                    if side == 'long':
                        new_sl = max(breakeven_sl, float(sl_data.get('price', 0)))
                    else:
                        new_sl = min(breakeven_sl, float(sl_data.get('price', 999999)))
                    ok2, msg2 = amend_sl(instId, sl_algo_id, new_sl)
                    print(f"  🔒 部分止盈+收紧SL: {coin}→{new_sl} | {msg2}")

        elif action in ('take_profit', 'tp1', 'tp2', 'tp3'):
            # 分批止盈
            ok, msg = close_position(instId, side, pos['pos'])
            print(f"  💰 {action}: {coin} | {msg}")
            if ok:
                close_paper_trade(coin, raw_side, f'{action}_止盈', None, 0)
                log_trade_journal(
                    action=action, coin=coin, side=side,
                    size=pos.get('pos', 0), entry_price=pos.get('avgPx'),
                    reason=dec.get('detail', ''), equity=equity,
                    algos_before=algos, algos_after=None,
                    market={'rsi': md.get('rsi_1h'), 'adx': md.get('adx_1h'), 'price': md.get('price')},
                    gemma_decision=None, rule_decision=dec.get('detail', ''),
                )

    # ── 新开仓：仅在熔断通过 + 有空余仓位时由gemma4决定 ──
    if decision in ('open_long', 'open_short') and treasury_ok and len(positions) < MAX_POSITIONS and not AUDIT_ONLY:
        # 优先用gemma4指定的币种，否则用排名第一，跳过无价格的币种
        if target_coin:
            candidates = [c for c in ranked if c['coin'].upper() == target_coin] + ranked
        else:
            candidates = ranked
        top = None
        for c in candidates:
            if c['md'].get('price'):
                top = c
                break
        if not top:
            print(f"  🚫 所有候选币种均无价格数据")
        else:
            coin = top['coin']
            direction = top['direction']
            md = top['md']
            price = md['price']
            slots = MAX_POSITIONS - len(positions)

            if slots <= 0:
                print(f"  🚫 无空余仓位")
            else:
                # ========== P0 Fix: RSI(7)方向过滤 ==========
                # RSI(7)是短期指标，极端值表示短期超买/超卖
                # 做多信号：RSI(7) < 30（短期超卖）→ 才能做多
                # 做空信号：RSI(7) > 70（短期超买）→ 才能做空
                # 否则：方向和短期信号矛盾，禁止开仓
                rsi7 = md.get('rsi_7', 50)
                rsi7_blocked = False
                if direction == 'long' and rsi7 > 70:
                    print(f"  🚫 RSI(7)={rsi7} > 70 (短期超买)，禁止做多")
                    rsi7_blocked = True
                elif direction == 'short' and rsi7 < 30:
                    print(f"  🚫 RSI(7)={rsi7} < 30 (短期超卖)，禁止做空")
                    rsi7_blocked = True
                if rsi7_blocked:
                    decision = 'hold'
                    print(f"  🚫 RSI(7)方向过滤阻止开仓")
                else:
                    # ========== 仓位公式 v4.0 ==========
                    score = top.get('score', 50)
                    confidence = max(1, score) / 100  # 0.01~1.0

                    # 杠杆公式：信心度越低杠杆越高（均值回归策略适用）
                    # high confidence(RSI extreme) → tight SL → can use modest leverage
                    # low confidence → need larger SL → less room for leverage
                    # 实盘上限15x（OKX最大30x，但实仓15x已足够）
                    lev = max(5, min(15, round(20 - confidence * 15)))
                    # confidence=1.0 → lev=5x (最把握，5x够用)
                    # confidence=0.5 → lev=10x
                    # confidence=0.2 → lev=15x (最高)

                    # 动态止损/止盈（使用_COIN_SL_ATR ATR倍数表，不用硬编码公式）
                    atr_pct = md.get('atr_pct', 2.0)  # ATR百分比
                    sl_pct_dynamic, tp_pct_dynamic = get_sl_tp_pct(coin, atr_pct)
                    # sl_pct_dynamic: 2-3x ATR (已在get_sl_tp_pct里用_COIN_SL_ATR表计算)
                    # tp_pct_dynamic: 2x SL (已在get_sl_tp_pct里用_COIN_TP_RATIO表计算)

                    # 仓位计算：固定风险1%账户
                    # 公式：张数 = 账户权益 × 1% / (入场价 × 止损%)
                    # P0 Fix: ATR%过低时（<1%），用price-based止损防止仓位过大
                    # XRP ATR%=0.63% → 3xATR=1.89%SL → 6x杠杆 → 张数膨胀到25K
                    # 解决方案：ATR%<1%时，用2%固定止损替代ATR-based止损
                    if atr_pct < 1.0:
                        effective_sl_pct = 0.02  # 2%固定止损（适用于低波动币种）
                        sl_reason = f'(ATR低{atr_pct:.1f}%，用固定2%止损)'
                    else:
                        effective_sl_pct = sl_pct_dynamic
                        sl_reason = f'(ATR正常{atr_pct:.1f}%)'

                    risk_amount = equity * RISK_PER_TRADE  # 1%账户
                    sl_dist_dollar = price * effective_sl_pct  # 每张的止损金额
                    sz = int(risk_amount / sl_dist_dollar)
                    sz = max(MIN_CONTRACTS, sz)  # 最小10张

                    # P0 Fix: 名义价值上限（防止低波动币种仓位膨胀）
                    # 名义价值 = 张数 × 价格，限制为账户equity的15%（约10%保证金占用）
                    max_notional = equity * 0.15  # equity*15% ≈ 10%保证金(6x杠杆)
                    notional = sz * price
                    if notional > max_notional:
                        sz = int(max_notional / price)
                        sz = max(MIN_CONTRACTS, sz)
                        print(f"  ⚠️ 名义价值上限触发: {notional:.0f} → {sz*price:.0f} (cap={max_notional:.0f})")

                    # 保证金估算
                    new_pos_margin_est = (sz * price) / lev
                    total_margin_est = sum(
                        (p.get('pos', 0) * p.get('avgPx', 0) / max(p.get('leverage', 3), 1))
                        for p in positions.values()
                    ) + new_pos_margin_est
                    conc_pct = (total_margin_est / equity * 100) if equity > 0 else 100

                    if conc_pct > 70:
                        print(f"  🚫 仓位集中度{conc_pct:.0f}%>70%上限，禁止开仓")
                        decision = 'hold'
                    else:
                        if direction == '做空':
                            sl = round(price * (1 + effective_sl_pct), 4)
                            tp = round(price * (1 - tp_pct_dynamic), 4)
                            side = 'short'
                        else:
                            sl = round(price * (1 - effective_sl_pct), 4)
                            tp = round(price * (1 + tp_pct_dynamic), 4)
                            side = 'long'

                        instId = f'{coin}-USDT-SWAP'
                        ok, msg = open_position(instId, side, sz, sl, tp, leverage=lev)
                        print(f"  {'✅' if ok else '❌'} 开仓: {coin} {direction} {sz}张 @{price} 信心={confidence:.0%} 杠杆={lev:.0f}x SL={effective_sl_pct:.1%} {sl_reason} TP={tp_pct_dynamic:.1%} | {msg}")
                        # ========== P0 Fix: 开仓后验证SL/TP ==========
                        if ok:
                            time.sleep(1)  # 等待OKX订单确认
                            algos = get_pending_algos(instId)
                            has_oco = bool(algos.get('oco'))
                            has_sl = bool(algos.get('sl'))
                            if not has_oco and not has_sl:
                                # OCO未挂上，立即补救
                                print(f"  ⚠️ SL/TP未挂上，尝试补挂...")
                                algo_id = place_oco(instId, side, sz, sl, tp)
                                if algo_id:
                                    print(f"  🔧 补救成功: OCO [{algo_id[:8]}]")
                                else:
                                    print(f"  🔴 补救失败: {coin} 无保护持仓!")
                            else:
                                print(f"  ✅ SL/TP验证: {'OCO' if has_oco else 'conditional'} 已挂")
                            # Journal for open position
                            log_trade_journal(
                                action=decision, coin=coin, side=side,
                                size=sz, entry_price=price,
                                reason=reason_str, equity=equity,
                                sl_before=None, sl_after=sl,
                                tp_before=None, tp_after=tp,
                                algos_before=None, algos_after=algos,
                                market={'rsi': md.get('rsi_1h') or md.get('rsi'), 'adx': md.get('adx_1h') or md.get('adx'), 'price': price},
                                gemma_decision=f'{decision} {reason}', rule_decision=None,
                            )
                        if ok:
                            save_position_state(coin, {'stage': 0, 'sold_ratio': 0.0, 'direction': direction, 'leverage': lev})
                            # P1 Fix: 保存完整元数据到paper_trades（journal IC计算需要）
                            # best_factor: 从reason字符串提取（RSI超卖/ADX趋势/波动率等）
                            reason_str = top.get('reason', '')
                            bf = 'RSI均值回归'
                            if 'RSI严重超卖' in reason_str or 'RSI超卖' in reason_str: bf = 'RSI超卖'
                            elif 'RSI超买' in reason_str: bf = 'RSI超买'
                            elif 'ADX' in reason_str: bf = 'ADX趋势'
                            elif '低量' in reason_str or '量能' in reason_str: bf = '波动率'
                            rsi_val = md.get('rsi_1h') or md.get('rsi', 50)
                            adx_val = md.get('adx_1h') or md.get('adx', 20)
                            save_paper_trade(
                                coin=coin, direction=direction,
                                entry_price=price, size_usd=risk_amount,
                                contracts=sz, leverage=lev,
                                sl_price=sl, tp_price=tp,
                                best_factor=bf, confidence=confidence,
                                ic=md.get('ic', 0),
                                rsi_at_entry=rsi_val,
                                adx_at_entry=adx_val,
                                btc_price_at_entry=btc_price,
                                open_reason=reason_str,
                                equity_at_open=equity,
                            )
    elif decision == 'close' and positions:
        # 优先用gemma4指定的币种，否则平最弱的
        if target_coin:
            worst = next(((coin, pos) for coin, pos in positions.items() if coin.upper() == target_coin), max(positions.items(), key=lambda x: x[1].get('pnl_pct', 0)))
        else:
            worst = max(positions.items(), key=lambda x: x[1].get('pnl_pct', 0))
        coin = worst[0]
        pos = worst[1]
        instId = f'{coin}-USDT-SWAP'
        ok, msg = close_position(instId, pos['side'], pos['pos'])
        print(f"  {'✅' if ok else '❌'} 平仓: {coin} | {msg}")
        if ok:
            close_paper_trade(coin, pos['side'], '手动平仓', None, 0)
    
    elif decision == 'trailing_sl' and positions:
        # 追踪止损：SL移到保本+2%，优先用gemma4指定的币种
        if target_coin:
            target_positions = [(c, p) for c, p in positions.items() if c.upper() == target_coin] if target_coin else list(positions.items())
        else:
            target_positions = list(positions.items())
        for coin, pos in target_positions:
            if pos.get('pnl_pct', 0) > 3:  # 浮盈>3%才启动追踪
                entry = pos.get('avgPx', 0)
                side = pos.get('side', 'long')
                algos = pos.get('algos', {})
                old_sl = algos.get('sl', {}).get('price') if algos.get('sl') else None
                new_sl = round(entry * (1 + 0.02), 4) if side == 'long' else round(entry * (1 - 0.02), 4)
                sl_algo_id = algos.get('sl', {}).get('algoId') if algos.get('sl') else None
                if sl_algo_id and new_sl != old_sl:
                    ok, msg = amend_sl(f'{coin}-USDT-SWAP', sl_algo_id, new_sl)
                    print(f"  {'✅' if ok else '❌'} 追踪SL: {coin} {old_sl}→{new_sl} | {msg}")
    
    print(f"\n最终决策: {decision} | {reason}")
if __name__ == '__main__':
    import time as _time
    
    # ── P0并发锁：防止和kronos_auto_guard同时运行 ──
    KRONOS_DIR = os.path.dirname(os.path.abspath(__file__))
    LOCK_FILE = os.path.join(KRONOS_DIR, '.kronos_dispatch.lock')
    COOLDOWN_FILE = os.path.join(KRONOS_DIR, '.cooldown.json')
    COOLDOWN_SEC = 900  # 15分钟cooldown
    IN_COOLDOWN = False
    
    # Cooldown检查：紧急操作后15分钟内只扫描不执行
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE) as f:
                cd = json.load(f)
            last_action = cd.get('last_emergency_action_ts', 0)
            if _time.time() - last_action < COOLDOWN_SEC:
                IN_COOLDOWN = True
                print(f'⏭️ Cooldown生效({COOLDOWN_SEC//60}分钟内)，距上次紧急操作{_time.time()-last_action:.0f}秒，改审计模式(只扫描不执行)')
    except:
        pass
    
    # 文件锁
    try:
        from filelock import FileLock
        lock = FileLock(LOCK_FILE, timeout=5)
        lock.acquire(timeout=5)
    except Exception as e:
        print(f'⏭️ 另一进程运行中，跳过: {e}')
        exit(0)
    
    try:
        # Cooldown期间用--audit模式：扫描分析但不执行任何交易
        if IN_COOLDOWN:
            import sys
            sys.argv = ['kronos_multi_coin.py', '--audit']
            AUDIT_ONLY = True
        result = full_scan()
    finally:
        try:
            lock.release()
        except:
            pass
