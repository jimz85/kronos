#!/usr/bin/env python3
"""
Kronos自动驾驶系统v3.0 - 完整版
===================================
真实闭环：
  1. IC分析 → 2. 多周期确认 → 3. 生成信号 → 4. 记录纸质交易
  → 5. 追踪持仓 → 6. 统计胜率 → 7. 飞书日报

运行：
  python3 kronos_pilot.py              快速信号
  python3 kronos_pilot.py --full       完整日报+纸质交易追踪
  python3 kronos_pilot.py --status     查看纸质交易胜率统计
  python3 kronos_pilot.py --log        查看最近交易记录
"""

from __future__ import annotations

import os, sys, json, time, requests as _req
import pathlib, logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# OKX API时间戳：实盘要求Unix毫秒，模拟盘接受ISO。
# 使用OKX服务器时间作为基准，消除本地时钟偏差。
_okx_ts_offset = None

# ── 日志配置（自动轮转：10MB × 5份）───────────────────────────
_log_dir = Path(__file__).parent / 'logs'
_log_dir.mkdir(exist_ok=True)
_pilot_logger = logging.getLogger('kronos_pilot')
_pilot_logger.setLevel(logging.DEBUG)
_pilot_logger.handlers.clear()
_rf = RotatingFileHandler(_log_dir / 'kronos_pilot.log', maxBytes=10*1024*1024,
                          backupCount=5, encoding='utf-8')
_rf.setLevel(logging.DEBUG)
_rf.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
_pilot_logger.addHandler(_rf)
_sh = logging.StreamHandler()
_sh.setLevel(logging.INFO)
_sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
_pilot_logger.addHandler(_sh)

def log_info(msg):
    _pilot_logger.info(msg)
    print(msg)
def log_warn(msg):
    _pilot_logger.warning(msg)
    print(f"⚠️  {msg}")
def log_error(msg):
    _pilot_logger.error(msg)
    print(f"❌ {msg}")

def _ts():
    """返回OKX服务器时间同步后的ISO8601时间戳（实盘必需格式）"""
    global _okx_ts_offset
    try:
        if _okx_ts_offset is None:
            r = _req.get('https://www.okx.com/api/v5/public/time', timeout=5)
            okx_ms = int(r.json()['data'][0]['ts'])
            local_ms = int(time.time() * 1000)
            _okx_ts_offset = okx_ms - local_ms
    except Exception:
        _okx_ts_offset = 0
    # OKX实盘需要ISO8601格式（不是Unix毫秒）
    from datetime import datetime
    return (datetime.utcnow() + timedelta(milliseconds=_okx_ts_offset)).strftime('%Y-%m-%dT%H:%M:%S.000Z')

# 加载 hermes 主目录的 .env
_HERMES_ENV = pathlib.Path.home() / '.hermes' / '.env'
load_dotenv(_HERMES_ENV, override=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.expanduser('~/.hermes/cron/output/')
os.makedirs(CACHE_DIR, exist_ok=True)

# OKX API keys（从环境变量读取）
OKX_API_KEY    = os.getenv('OKX_API_KEY', '')
OKX_SECRET     = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')

# 模拟盘：没有API Key时自动进入模拟模式（回测/开发用）
# 实盘：填入API Key后自动切换
# 注意：模拟盘API Key + x-simulated-trading header 仍然用simulation环境
#       但代码需要知道这是模拟盘（不做真实交易所操作）
# 判断逻辑：API Key以特定前缀开头（如'8aba4d'）= 模拟盘API Key
_is_sim_key = OKX_API_KEY.startswith('8aba4d') if OKX_API_KEY else False
DEMO_MODE = (not OKX_API_KEY) or _is_sim_key

# 启动时验证必需的环境变量
if not OKX_API_KEY or not OKX_SECRET:
    _pilot_logger.error("❌ 错误: OKX_API_KEY 和 OKX_SECRET 环境变量必须设置")
    sys.exit(1)

# OKX账户模式缓存（避免每次API调用）
_okx_pos_mode = None

def get_okx_pos_mode():
    """获取OKX账户持仓模式：net_mode 或 long_short_mode"""
    global _okx_pos_mode
    if _okx_pos_mode is not None:
        return _okx_pos_mode
    if DEMO_MODE:
        _okx_pos_mode = 'long_short_mode'  # 模拟模式默认双向持仓
        return _okx_pos_mode
    import requests, hmac, hashlib, base64
    try:
        ts = _ts()
        path = '/api/v5/account/config'
        msg = ts + 'GET' + path
        sig = base64.b64encode(hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
        h = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sig,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
            'x-simulated-trading': '1',
        }
        # P1 Fix: OKX API call now uses retry wrapper
        ok, resp, err = _okx_request_with_retry(
            'GET', f'https://www.okx.com{path}', headers=h, max_retries=2, timeout=10
        )
        if not ok:
            _pilot_logger.warning(f'get_okx_pos_mode failed: {err}')
            _okx_pos_mode = 'net_mode'
            return _okx_pos_mode
        try:
            result = resp.json()
            if result.get('code') == '0':
                _okx_pos_mode = result['data'][0].get('posMode', 'net_mode')
            else:
                _okx_pos_mode = 'net_mode'
        except Exception:
            _okx_pos_mode = 'net_mode'
    except Exception:
        _okx_pos_mode = 'net_mode'
    return _okx_pos_mode

STATE_FILE = os.path.join(CACHE_DIR, 'kronos_pilot_state.json')
PAPER_LOG = os.path.join(CACHE_DIR, 'paper_trades.json')
IC_HISTORY = os.path.join(CACHE_DIR, 'ic_history.json')
FACTOR_WEIGHTS_FILE = os.path.join(CACHE_DIR, 'factor_weights.json')
PERF_FILE = os.path.join(CACHE_DIR, 'performance.json')

# 根据回测结果优化：只交易有效币种（DOGE/DOT/ADA年化收益>0%）
# BTC/ETH/AVAX/BNB策略无效，回测亏损，回测结果见 docs/strategy_recommendation.md
COINS = ['DOGE', 'DOT', 'ADA', 'XRP', 'SOL']
COIN_INST = {
    'BTC': 'BTC-USDT', 'ETH': 'ETH-USDT', 'ADA': 'ADA-USDT',
    'DOGE': 'DOGE-USDT', 'AVAX': 'AVAX-USDT', 'DOT': 'DOT-USDT', 'SOL': 'SOL-USDT',
    'XRP': 'XRP-USDT', 'BNB': 'BNB-USDT',  # P1 Fix: 补充XRP/BNB合约代码
}
COIN_TICKER = {c: c + '-USD' for c in COINS}

# P1 Fix: 从coin_strategy_map.json读取excluded标志，动态过滤可交易币种
# 不再用硬编码COINS列表，避免BTC/ETH/AVAX/DOT等已排除的币被扫描
def _get_allowed_coins():
    """返回coin_strategy_map.json中未标记为excluded的币种列表"""
    try:
        import json as _json
        from pathlib import Path as _Path
        smap_path = _Path(__file__).parent / 'coin_strategy_map.json'
        with open(smap_path) as f:
            smap = _json.load(f)
        allowed = []
        for c in smap.get('coins', []):
            if not c.get('excluded', False):
                allowed.append(c['symbol'])
        if allowed:
            return allowed
    except Exception:
        pass
    return COINS  # 回退

IC_THRESHOLD = 0.05
IC_STRONG = 0.10
IC_DECAY_ALERT = 0.015
MAX_POSITION = 0.30
STOP_LOSS = 0.05
TAKE_PROFIT = 0.20
LEV = 3
MAX_SINGLE_TRADE_PCT = 0.05   # 单笔最多使用5%账户权益

import yfinance as yf
import numpy as np
import pandas as pd
from scipy.stats import spearmanr, ttest_ind
import requests
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 失效币种黑名单（余额不足等错误后静默跳过）
# ============================================================
BLACKLIST_FILE = os.path.join(CACHE_DIR, 'symbol_blacklist.json')
_blacklist = {}  # {symbol: {'reason': str, 'expires_at': float (unix ts)}}

def load_blacklist():
    """加载黑名单并清理过期条目，返回黑名单字典"""
    global _blacklist
    try:
        with open(BLACKLIST_FILE) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    now = time.time()
    _blacklist = {k: v for k, v in raw.items() if v.get('expires_at', 0) > now}
    if len(raw) != len(_blacklist):
        save_blacklist()
    return _blacklist

def _atomic_write_json(path, data):
    """原子写入JSON：先写.temp文件，再replace"""
    temp_path = path + '.tmp'
    with open(temp_path, 'w') as f:
        json.dump(data, f)
    os.replace(temp_path, path)

def save_blacklist():
    _atomic_write_json(BLACKLIST_FILE, _blacklist)

def is_blacklisted(symbol):
    """
    检查币种是否在黑名单中（自动清理过期条目）

    余额不足类黑名单：每次检查时同步验证当前余额是否已足够开仓
    如果余额已恢复（≥最小可交易门槛），提前解锁
    """
    if symbol not in _blacklist:
        return False

    entry = _blacklist[symbol]
    now = time.time()

    # 未过期：检查是否需要提前解锁
    if entry['expires_at'] > now:
        reason = entry.get('reason', '')
        # 【新】余额不足类：余额已恢复则提前解锁，不再等待TTL过期
        if reason in ('insufficient_balance', 'balance_insufficient'):
            try:
                from real_monitor import get_account_balance
                bal_data = get_account_balance()
                equity = float(bal_data.get('totalEq', 0)) if bal_data else 0
                # 最小门槛：比最小可开仓金额多50% buffer（预防价格波动）
                min_needed = 10.0  # $10 minimum viable equity for any trade
                if equity >= min_needed:
                    # 余额已恢复，提前解锁
                    del _blacklist[symbol]
                    save_blacklist()
                    _pilot_logger.info(f'  ✅ {symbol} 余额已恢复(\\${equity:.2f})，提前解锁')
                    return False
            except:
                pass  # 余额检查失败，维持黑名单
        return True

    # 已过期：清理并解锁
    del _blacklist[symbol]
    save_blacklist()
    return False

def add_to_blacklist(symbol, reason, ttl_days=30):
    """
    将币种加入黑名单
    - 余额不足类(insufficient_balance): 1天TTL（每天都检查余额是否恢复）
    - 其他: ttl_days（默认30天）
    """
    if reason in ('insufficient_balance', 'balance_insufficient'):
        ttl_days = 1  # 余额不足：每天重试，不死等30天
    _blacklist[symbol] = {
        'reason': reason,
        'expires_at': time.time() + ttl_days * 86400,
        'added_at': datetime.now().isoformat(),
    }
    save_blacklist()
    _pilot_logger.warning('  ⛔ %s 已静默加入观察名单（%s天）原因: %s' % (symbol, ttl_days, reason))

def _get_volatility_stop(symbol, hold_hours=72):
    """
    根据14日ATR计算动态止损距离（返回小数，如0.05=5%）

    设计原则：
    - 使用日线ATR作为基准（更能反映72h真实波动）
    - 应用 sqrt(时间) 缩放，使止损宽度与持仓时间成正比
    - hold_hours: 预期持仓小时数，默认72h（3天）
    """
    try:
        inst = COIN_INST.get(symbol, symbol + '-USDT')
        import ccxt
        c = ccxt.okx({'enableRateLimit': True})

        # 优先用日线 ATR（1day × 14 = 14天数据）
        # 日线1根K线 = 24小时，所以14根日线 ≈ 14×24h 数据
        ohlcv_d = c.fetch_ohlcv(inst, '1d', limit=14)
        if len(ohlcv_d) >= 10:
            highs  = np.array([c[2] for c in ohlcv_d])
            lows   = np.array([c[3] for c in ohlcv_d])
            closes = np.array([c[4] for c in ohlcv_d])
            trs = np.maximum(
                highs[1:] - lows[1:],
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]))
            atr = np.mean(trs)  # 日线ATR（美元）
            price = closes[-1]
            # sqrt时间缩放：√(hold_hours / 24) 使止损宽度与√时间成正比
            # 日线ATR基准已隐含24h波动，用sqrt缩放到目标持仓时间
            sqrt_factor = (hold_hours / 24.0) ** 0.5
            stop_pct = (atr / price) * sqrt_factor
            return max(stop_pct, 0.03)  # 最小3%止损（防止极端低波动）

        # 回退：1h ATR（需要sqrt缩放）
        ohlcv_h = c.fetch_ohlcv(inst, '1h', limit=14)
        if len(ohlcv_h) >= 10:
            highs  = np.array([c[2] for c in ohlcv_h])
            lows   = np.array([c[3] for c in ohlcv_h])
            closes = np.array([c[4] for c in ohlcv_h])
            trs = np.maximum(highs[1:] - lows[1:], np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
            atr_h = np.mean(trs)  # 1h ATR（美元）
            price = closes[-1]
            # 1h ATR → 缩放到 hold_hours
            # 1h ATR的√1=1 → 目标时间 √(hold_hours)
            sqrt_factor = (hold_hours / 1.0) ** 0.5
            stop_pct = (atr_h / price) * sqrt_factor
            return max(stop_pct, 0.03)

        return STOP_LOSS  # 回退默认止损
    except Exception:
        return STOP_LOSS

def auto_position_sizing(symbol, available_balance, base_risk_pct=0.02, min_trade_usdt=15):
    """
    余额驱动的自动仓位管理：
    - 基础风险金额 = 总余额 × 2%
    - 根据动态止损距离反推开仓金额
    - 过滤低于最小交易额的噪音信号
    返回: position_usdt (float) 或 None（信号作废）
    """
    stop_pct = _get_volatility_stop(symbol)
    risk_amount = available_balance * base_risk_pct  # 2%风险
    position_usdt = risk_amount / stop_pct
    if position_usdt < min_trade_usdt:
        return None  # 信号作废，不报错
    return position_usdt

# 启动时清理过期黑名单
load_blacklist()

# ============================================================
# 纸质交易日志
# ============================================================
SKIP_LOG = os.path.join(CACHE_DIR, 'skipped_signals.json')

def load_paper_log():
    if os.path.exists(PAPER_LOG):
        with open(PAPER_LOG) as f:
            return json.load(f)
    return []

def load_skip_log():
    if os.path.exists(SKIP_LOG):
        with open(SKIP_LOG) as f:
            return json.load(f)
    return []

def save_paper_log(log):
    _atomic_write_json(PAPER_LOG, log[-300:])

def save_skip_log(log):
    _atomic_write_json(SKIP_LOG, log[-500:])

def log_skipped_signal(coin, direction, strategy, rsi_ic, rsi_v, adx_v, period, skip_reason):
    """记录被跳过的信号（去重：同一币种+策略+原因1小时内不重复）"""
    log = load_skip_log()
    now = datetime.now()
    # 1小时内的相同信号不重复记录
    one_hour_ago = now.timestamp() - 3600
    for s in log:
        if s.get('time'):
            try:
                s_time = datetime.fromisoformat(s['time']).timestamp()
                if (s_time > one_hour_ago and 
                    s['coin'] == coin and 
                    s['strategy'] == strategy and 
                    s['skip_reason'] == skip_reason):
                    return  # 1小时内已记录，跳过
            except:
                pass
    
    log.append({
        'time': now.isoformat(),
        'coin': coin,
        'direction': direction,
        'strategy': strategy,
        'rsi_ic': rsi_ic,
        'rsi_v': rsi_v,
        'adx_v': adx_v,
        'period': period,
        'skip_reason': skip_reason,
    })
    save_skip_log(log)

def get_cross_layer_signals():
    """五层数据采集（Layer1+3+4+5）
    L1: OKX Funding Rate + Open Interest（已移至run_full_report中合并）
    L3: Fear & Greed
    L4: DeFiLlama CEX TVL
    L5: BTC Dominance + 机构持仓 + 区块数据
    """
    try:
        import requests
    except:
        return {}
    
    signals = []
    fg_data = None
    cex_data = {}
    treasury_data = {}
    btc_dom = 0
    
    # ========== Layer3: Fear & Greed ==========
    try:
        r = requests.get('https://api.alternative.me/fng/', timeout=8)
        fg = r.json()['data'][0]
        fg_data = {'value': int(fg['value']), 'class': fg['value_classification']}
        if fg_data['value'] < 25:
            signals.append(('L3', 'LONG', fg_data['value'] * 4, f'Fear&Greed={fg_data["value"]}极度恐惧'))
        elif fg_data['value'] > 75:
            signals.append(('L3', 'SHORT', (fg_data['value'] - 70) * 4, f'Fear&Greed={fg_data["value"]}极度贪婪'))
    except:
        pass
    
    # ========== Layer4: DeFiLlama CEX TVL变化 ==========
    try:
        r = requests.get('https://api.llama.fi/protocols', timeout=8)
        protocols = r.json()
        cex_keywords = {'okx': 'OKX', 'binance cex': 'Binance', 'bybit': 'Bybit', 'bitfinex': 'Bitfinex', 'robinhood': 'Robinhood'}
        for p in protocols:
            sym = p.get('name', '').lower()
            for kw, label in cex_keywords.items():
                if kw in sym:
                    c7d = p.get('change_7d', 0) or 0
                    tvl = p.get('tvl', 0) or 0
                    if c7d > 25:
                        signals.append(('L4', 'LONG', min(90, c7d * 2), f'{label} TVL+{c7d:.0f}%/7d'))
                    elif c7d < -15:
                        signals.append(('L4', 'SHORT', min(90, abs(c7d) * 2), f'{label} TVL{c7d:.0f}%/7d'))
                    cex_data[label] = {'change_7d': c7d, 'tvl': tvl}
                    break
    except:
        pass
    
    # ========== Layer5a: BTC市值占比 ==========
    btc_dom = 0
    try:
        r = requests.get('https://api.coingecko.com/api/v3/global', timeout=8)
        d = r.json().get('data', {})
        mcp = d.get('market_cap_percentage', {})
        # mcp可能是dict{'btc':57.3}或list
        if isinstance(mcp, dict):
            btc_dom = float(mcp.get('btc', 0) or 0)
        elif isinstance(mcp, list):
            for item in mcp:
                if isinstance(item, dict) and item.get('symbol') == 'btc':
                    btc_dom = float(item.get('market_cap_percentage', 0) or 0)
                    break
        tmc = d.get('total_market_cap', {})
        total_mcap = float(tmc) if isinstance(tmc, (int, float)) else float(tmc.get('usd', 0) if isinstance(tmc, dict) else 0)
        if btc_dom > 60:
            signals.append(('L5', 'OBSERVE', (btc_dom - 55) * 4, f'BTC主导率{btc_dom:.1f}%(集中风险)'))
        elif btc_dom < 40:
            signals.append(('L5', 'OBSERVE', (45 - btc_dom) * 4, f'BTC主导率{btc_dom:.1f}%(山寨季预警)'))
    except:
        pass
    
    # ========== Layer5b: CoinGecko机构持仓 (真实鲸鱼) ==========
    try:
        r = requests.get('https://api.coingecko.com/api/v3/companies/public_treasury/bitcoin', timeout=8)
        data = r.json()
        companies = data.get('companies', [])
        
        # 按持仓量排序
        companies_sorted = sorted(companies, key=lambda x: x.get('total_holdings', 0), reverse=True)
        total_btc = sum(c.get('total_holdings', 0) for c in companies)
        
        # Strategy一家独大(65%机构持仓)
        strategy = next((c for c in companies_sorted if 'Strategy' in c.get('name', '')), None)
        strategy_btc = strategy.get('total_holdings', 0) if strategy else 0
        strategy_pct = strategy_btc / total_btc * 100 if total_btc > 0 else 0
        
        # 机构总持仓占流通量比例（~119万BTC / 2000万=6%）
        circulating = 19700000  # 近似流通量
        institutional_pct = total_btc / circulating * 100
        
        treasury_data = {
            'total_btc': total_btc,
            'total_usd': total_btc * 77000,  # 估算
            'companies': len(companies),
            'strategy_btc': strategy_btc,
            'institutional_pct': institutional_pct,
        }
        
        # 机构持仓集中度（Signal: Strategy持仓占比越高=越集中=风险）
        if strategy_pct > 60:
            signals.append(('L5', 'OBSERVE', 60, f'机构持仓集中度:{strategy_pct:.0f}%(Strategy一家独大)'))
        
        # 机构总持仓异常（作为鲸鱼囤积指标）
        if institutional_pct > 5:
            signals.append(('L5', 'LONG', institutional_pct * 10, f'机构囤积BTC:{institutional_pct:.1f}%流通量({total_btc/1e6:.1f}M BTC)'))
        
        # Top3机构动向
        for c in companies_sorted[:3]:
            amt = c.get('total_holdings', 0)
            if amt > 10000:  # 持仓>1万BTC
                signals.append(('L5', 'LONG', 30, f'鲸鱼:{c["name"]}持仓{amt:,.0f}BTC'))
    except:
        pass
    
    # ========== Layer5c: BTC区块数据 ==========
    try:
        r = requests.get('https://blockstream.info/api/blocks/tip/height', timeout=8)
        if r.status_code == 200:
            current_height = int(r.text.strip())
            difficulty_period = (current_height // 2016) * 2016
            period_progress = (current_height - difficulty_period) / 2016 * 100
            
            # 难度周期刚开始=矿工投降期=底部区域
            if period_progress < 10:
                signals.append(('L5', 'LONG', 40, f'挖矿周期开始({period_progress:.0f}%):矿工投降期'))
            elif period_progress > 90:
                signals.append(('L5', 'SHORT', 40, f'挖矿周期末期({period_progress:.0f}%):矿工获利期'))
    except:
        pass
    
    # ========== 跨层共振分析 ==========
    long_score = sum(s[2] for s in signals if s[1] == 'LONG')
    short_score = sum(s[2] for s in signals if s[1] == 'SHORT')
    
    共振分 = long_score * 100 + long_score
    cross_direction = 'LONG' if long_score > short_score else ('SHORT' if short_score > long_score else 'NEUTRAL')
    cross_confidence = max(long_score, short_score)
    
    return {
        'fear_greed': fg_data,
        'cex_flows': cex_data,
        'treasury': treasury_data,
        'btc_dominance': btc_dom,  # 独立key，供报告使用
        'signals': signals,
        '共振分': 共振分,
        'cross_direction': cross_direction,
        'cross_confidence': cross_confidence,
        'long_score': long_score,
        'short_score': short_score,
    }


# ─────────────────────────────────────────────────────────────────────────────
# P0 Fix: OKX API 重试 + 幂等包装器
# 策略：
#   - HTTP 429: 解析 Retry-After，等待后重试，最多 max_retries 次
#   - 网络超时/连接错误: 幂等继续（OKX市价单本身幂等）
#     → 网络失败时保守继续：若订单实际成功则保护生效；若失败则无仓位可保护
#   - HTTP 非0错误码: 不重试，立即返回
# ─────────────────────────────────────────────────────────────────────────────

def _okx_request_with_retry(
    method: str,
    url: str,
    headers: dict | None = None,
    data: str | None = None,
    max_retries: int = 2,
    timeout: int = 10,
) -> tuple[bool, _req.Response, str]:
    """包装 requests，带重试 + Retry-After 解析 + 幂等安全。

    Args:
        method: HTTP方法 ('GET' or 'POST')
        url: 请求URL
        headers: HTTP请求头
        data: 请求体 (JSON字符串)
        max_retries: 最大重试次数
        timeout: 请求超时秒数

    Returns:
        tuple[bool, Response, str]: (是否成功, 响应对象, 错误信息或None)
    """
    import time

    def _backoff(attempt: int, max_retries: int, reason: str) -> bool:
        """Returns True if should retry, False if max retries exceeded."""
        if attempt >= max_retries:
            return False
        wait = 2 ** attempt
        _pilot_logger.warning(f'  ⚠️ {reason}, waiting {wait}s before retry')
        time.sleep(wait)
        return True

    session = requests  # requests 本身线程安全（每个请求独立socket）

    for attempt in range(1, max_retries + 1):
        try:
            if method.upper() == 'POST':
                resp = session.post(url, headers=headers, data=data, timeout=timeout)
            else:
                resp = session.get(url, headers=headers, timeout=timeout)

            # HTTP 429: Rate Limit
            if resp.status_code == 429:
                retry_after = 5.0  # 默认5秒
                for h in ['Retry-After', 'retry-after']:
                    if h in resp.headers:
                        try:
                            retry_after = float(resp.headers[h])
                        except (ValueError, TypeError):
                            pass
                        break
                if attempt < max_retries:
                    _pilot_logger.warning(
                        f'  ⚠️ HTTP 429 (attempt {attempt}/{max_retries}), '
                        f'waiting {retry_after:.1f}s before retry')
                    time.sleep(retry_after)
                    continue
                return False, None, f'HTTP 429 after {max_retries} retries'

            # 网络层错误（超时/连接断开）
            if resp.status_code in (599,):  # requests internal "connection error" code
                if not _backoff(attempt, max_retries, f'Network error (attempt {attempt}/{max_retries})'):
                    return False, None, f'Network error after {max_retries} retries'
                continue

            return True, resp, None

        except requests.exceptions.Timeout:
            if not _backoff(attempt, max_retries, f'Request timeout (attempt {attempt}/{max_retries})'):
                return False, None, f'Request timeout after {max_retries} retries'
            continue

        except requests.exceptions.ConnectionError:
            # 不暴露{e}，避免泄露内部路径信息
            if not _backoff(attempt, max_retries, f'Connection error (attempt {attempt}/{max_retries})'):
                return False, None, f'Connection error after {max_retries} retries'
            continue

        except Exception:
            # 不捕获KeyboardInterrupt/SystemExit，只处理编程错误
            # 不暴露异常详情，避免泄露文件路径/变量名
            return False, None, f'Unexpected error: <{type(e).__name__}>'

    return False, None, 'Max retries exceeded'


def okx_place_order(
    coin: str,
    side: str,
    size_contracts: int,
    lev: int = 3,
    sl_pct: float = 0.05,
    tp_pct: float = 0.20,
    price: float | None = None,
) -> dict:
    """OKX下单（支持模拟盘和实盘）

    Args:
        coin: BTC, ETH, ADA等
        side: 'buy'(做多) 或 'sell'(做空)
        size_contracts: 合约张数
        lev: 杠杆倍数
        sl_pct: 止损百分比
        tp_pct: 止盈百分比
        price: 指定价格（可选，默认市价）

    Returns:
        dict: {
            'success': bool,
            'code': str,
            'entry_price': float,
            'sl': {'success': bool, 'algoId': str|None, 'error': str|None},
            'tp': {'success': bool, 'algoId': str|None, 'error': str|None},
            'position_closed': bool,
        }

    Important: SL/TP通过独立条件单挂出（OKX OCO订单）
    【风控】：SL/TP任一失败 → 立即市价平仓（不能有无保护持仓）
    """
    import hmac, hashlib, base64

    try:
        instId = f'{coin}-USDT-SWAP'

        # P2 Fix: 单笔交易限额（不超过账户5%权益）
        from kronos_utils import get_account_balance
        balance_info = get_account_balance()
        # 获取当前市场价格
        if price is None:
            prices = get_okx_prices()
            price = prices.get(coin, 0)
        if price > 0 and balance_info.get('totalEq', 0) > 0:
            max_size_by_cap = int((float(balance_info.get('totalEq', 0)) * MAX_SINGLE_TRADE_PCT) / price * lev)
            size_contracts = min(size_contracts, max_size_by_cap)

        # Step 1: 下市价单（杠杆在订单里直接指定，不单独set-leverage）
        # posSide: long_short_mode需要，net_mode下会忽略
        ts = _ts()
        body_dict = {
            'instId': instId,
            'tdMode': 'isolated',
            'side': side,
            'ordType': 'market',
            'sz': str(size_contracts),
            'lever': str(lev),
            'posSide': 'long' if side == 'buy' else 'short',
        }
        body = json.dumps(body_dict)
        sign = base64.b64encode(hmac.new(
            OKX_SECRET.encode(),
            f'{ts}POST/api/v5/trade/order{body}'.encode(),
            hashlib.sha256
        ).digest()).decode()
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
            'x-simulated-trading': '1',  # 模拟盘Key路由到simulation环境
        }

        # P0 Fix: 使用重试包装器（幂等安全，网络失败时保守继续）
        ok, resp, err = _okx_request_with_retry(
            'POST', 'https://www.okx.com/api/v5/trade/order',
            headers=headers, data=body, max_retries=2, timeout=10
        )
        if not ok:
            _pilot_logger.error(
                f'  🔴 OKX下单失败（网络/限流）: {err} | '
                f'保守策略：仍尝试挂SL/TP（若订单实际成功则保护生效）')
            # 保守策略：继续尝试获取成交价和挂SL/TP
            # OKX市价单幂等，若订单成功则能查到position；若失败则无仓位可保护
            entry_price = _get_position_entry_price(instId)
            if entry_price <= 0:
                _pilot_logger.error(f'  ❌ 无法确认{coin}订单状态且无持仓，跳过SL/TP')
                return {'success': False, 'code': err, 'entry_price': 0,
                        'sl': {'success': False, 'error': f'Network error: {err}'},
                        'tp': {'success': False, 'error': f'Network error: {err}'}}
            _pilot_logger.warning(f'  ⚠️ {coin}订单状态未知但检测到持仓@${entry_price}，挂SL/TP')
            sl_tp_results = _place_sl_tp_algo(instId, side, size_contracts, entry_price, sl_pct, tp_pct)
            return {'success': True, 'code': 'unknown_confirmed', 'entry_price': entry_price,
                    'sl': sl_tp_results['sl'], 'tp': sl_tp_results['tp'],
                    'position_closed': False, 'order_confirmed': False}

        try:
            order_result = resp.json()
        except:
            _pilot_logger.error(f'  JSON解析失败: status={resp.status_code}')
            return {'success': False, 'code': 'parse_error', 'entry_price': 0,
                    'sl': {'success': False, 'error': 'Request failed'},
                    'tp': {'success': False, 'error': 'Request failed'}}

        code = order_result.get('code', '')
        direction_str = '做多' if side == 'buy' else '做空'

        if code != '0':
            _pilot_logger.error(f'  OKX实盘下单失败: {direction_str} {coin} code={code}')
            return {'success': False, 'code': code, 'entry_price': 0,
                    'sl': {'success': False, 'error': 'Order failed'},
                    'tp': {'success': False, 'error': 'Order failed'}}

        # Step 3: 从订单结果获取实际成交价
        try:
            fill_data = order_result['data'][0]
            entry_price = float(fill_data.get('fillPx', 0))
            if entry_price <= 0:
                entry_price = _get_position_entry_price(instId)
        except:
            entry_price = _get_position_entry_price(instId)

        if entry_price <= 0:
            _pilot_logger.warning(f'  ⚠️ 无法获取{coin}成交价，跳过SL/TP')
            _pilot_logger.info(f'  OKX实盘下单成功: {direction_str} {coin} {size_contracts}张（无保护）')
            # 无保护持仓 → 必须立即平仓
            close_ok = _okx_market_close(instId, side, size_contracts)
            if not close_ok:
                _pilot_logger.error(f'  ❌ 平仓失败！请立即手动处理！')
            return {'success': False, 'code': '0', 'entry_price': 0,
                    'sl': {'success': False, 'error': '无法获取成交价'},
                    'tp': {'success': False, 'error': '无法获取成交价'}}

        _pilot_logger.info(f'  OKX实盘下单成功: {direction_str} {coin} {size_contracts}张 成交价${entry_price:.4f}')

        # Step 4: 用实际成交价挂独立的SL/TP条件单
        sl_tp_results = _place_sl_tp_algo(instId, side, size_contracts, entry_price, sl_pct, tp_pct)

        # Step 5: 【风控】SL/TP任一失败 → 立即平仓（不能有无保护持仓）
        sl_failed = not sl_tp_results['sl']['success']
        tp_failed = not sl_tp_results['tp']['success']

        if sl_failed or tp_failed:
            failed = 'SL' if sl_failed else 'TP'
            err = sl_tp_results['sl']['error'] if sl_failed else sl_tp_results['tp']['error']
            _pilot_logger.error(f'  🔴 【紧急】{failed}挂单失败({err}) → 立即市价平仓（无保护持仓不可接受）')
            close_result = _okx_market_close(instId, side, size_contracts)
            record_trade_outcome(coin, 0, 'sl_tp_failed_force_close')
            if close_result:
                _pilot_logger.info(f'  ✅ 已平仓（无保护持仓）')
            else:
                _pilot_logger.error(f'  ❌ 平仓失败！请立即手动处理！')
            return {'success': False, 'code': '0', 'entry_price': entry_price,
                    'sl': sl_tp_results['sl'],
                    'tp': sl_tp_results['tp'],
                    'position_closed': True,
                    'close_error': 'Unprotected position - closed'}

        # 全部成功
        return {'success': True, 'code': '0', 'entry_price': entry_price,
                'sl': sl_tp_results['sl'],
                'tp': sl_tp_results['tp'],
                'position_closed': False}

    except Exception as e:
        _pilot_logger.error(f'  OKX下单失败: {type(e).__name__}')
        return {'success': False, 'code': 'exception', 'entry_price': 0,
                'sl': {'success': False, 'error': f'Network error: {type(e).__name__}'},
                'tp': {'success': False, 'error': f'Network error: {type(e).__name__}'}}


def _okx_market_close(instId: str, existing_side: str, size_contracts: int) -> bool:
    """市价平仓（紧急用途：SL/TP挂单失败后立即执行）

    Args:
        instId: 合约ID (e.g. 'BTC-USDT-SWAP')
        existing_side: 'buy' → 做空平多；'sell' → 做多平空
        size_contracts: 合约张数

    Returns:
        bool: 是否成功

    P0 Fix: 添加重试机制，网络/限流失败时最多重试2次
    """
    import hmac, hashlib, base64
    close_side = 'sell' if existing_side == 'buy' else 'buy'
    close_pos_side = 'long' if existing_side == 'buy' else 'short'  # 平哪个方向
    try:
        ts = _ts()
        body = json.dumps({
            'instId': instId,
            'tdMode': 'isolated',
            'side': close_side,
            'ordType': 'market',
            'sz': str(int(size_contracts)),
            'posSide': close_pos_side,
            'reduceOnly': True,
        })
        sign = base64.b64encode(hmac.new(
            OKX_SECRET.encode(),
            f'{ts}POST/api/v5/trade/order{body}'.encode(),
            hashlib.sha256
        ).digest()).decode()
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
            'x-simulated-trading': '1',
        }
        # P0 Fix: 市价平仓带重试（reduceOnly安全，重复平仓OKX返回0）
        ok, r, err = _okx_request_with_retry(
            'POST', 'https://www.okx.com/api/v5/trade/order',
            headers=headers, data=body, max_retries=2, timeout=10
        )
        if not ok:
            _pilot_logger.error(f'  ❌ _okx_market_close失败（网络/限流）: {err}，重试后仍失败')
            return False
        result = r.json()
        return result.get('code') == '0'
    except Exception as e:
        _pilot_logger.warning(f'  ⚠️ _okx_market_close异常: {e}')
        return False


def _place_sl_tp_algo(
    instId: str,
    side: str,
    sz: float,
    entry_price: float,
    sl_pct: float,
    tp_pct: float,
) -> dict:
    """为已开仓位挂OCO Bracket订单（SL+TP合并为1个OCO订单）

    Args:
        instId: 合约ID (e.g. 'BTC-USDT-SWAP')
        side: 'buy' 或 'sell'
        sz: 合约张数
        entry_price: 开仓价格
        sl_pct: 止损百分比
        tp_pct: 止盈百分比

    Returns:
        dict: {'sl': {...}, 'tp': {...}} 各含 success/algoId/price/error

    P0 Bug修复（2026-04-26）：
    1. OKX每仓位只允许1个条件单，必须用ordType='oco'合并SL+TP
    2. 幂等性：挂单前先查是否已有活跃OCO订单，有则跳过，防止重复挂单
    3. 原来用conditional分两次下单，OKX会接受但同一持仓有多个活跃条件单，
       实际只生效第一个（SL或TP哪个先到谁成交），另一个变成死单
    """
    import hmac, hashlib, base64

    if side == 'buy':
        sl_price = round(entry_price * (1 - sl_pct), 4)
        tp_price = round(entry_price * (1 + tp_pct), 4)
        close_side = 'sell'
    else:
        sl_price = round(entry_price * (1 + sl_pct), 4)
        tp_price = round(entry_price * (1 - tp_pct), 4)
        close_side = 'buy'

    # ── 幂等检查：先查是否已有活跃OCO订单，有则跳过 ──
    ts_chk = _ts()
    sign_chk = base64.b64encode(hmac.new(
        OKX_SECRET.encode(),
        f'{ts_chk}GET/api/v5/trade/orders-algo-pending?instId={instId}&ordType=oco&limit=10'.encode(),
        hashlib.sha256
    ).digest()).decode()
    h_chk = {
        'OK-ACCESS-KEY': OKX_API_KEY, 'OK-ACCESS-SIGN': sign_chk,
        'OK-ACCESS-TIMESTAMP': ts_chk, 'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'Content-Type': 'application/json', 'x-simulated-trading': '1',
    }
    try:
        ok_chk, r_chk, err_chk = _okx_request_with_retry(
            'GET',
            'https://www.okx.com/api/v5/trade/orders-algo-pending?instId=%s&ordType=oco&limit=10' % instId,
            headers=h_chk, max_retries=2, timeout=10
        )
        if not ok_chk:
            _pilot_logger.warning(f'    ⚠️ 幂等检查失败({err_chk})，继续挂单')
            existing = []
        else:
            existing = r_chk.json().get('data', [])
        if existing:
            algo_id = existing[0].get('algoId', '?')
            _pilot_logger.info(f'    ⏭️ OCO已存在(id:{algo_id[:8]})，跳过挂单')
            return {'sl': {'success': True, 'algoId': algo_id, 'price': sl_price, 'skipped': True},
                    'tp': {'success': True, 'algoId': algo_id, 'price': tp_price, 'skipped': True}}
    except Exception as e:
        _pilot_logger.warning(f'    ⚠️ 幂等检查异常({e})，继续挂单')

    results = {'sl': None, 'tp': None}
    # OCO Bracket：1个订单同时包含SL和TP，触发时互斥
    body = {
        'instId': instId,
        'tdMode': 'isolated',
        'side': close_side,
        'ordType': 'oco',       # OCO = One-Cancels-Other，SL+TP互斥 ✅
        'sz': str(int(sz)),
        'posSide': 'long' if side == 'buy' else 'short',
        'slTriggerPx': str(sl_price),
        'slOrdPx': '-1',        # 市价触发
        'tpTriggerPx': str(tp_price),
        'tpOrdPx': '-1',       # 市价触发
    }
    ts = _ts()
    sign = base64.b64encode(hmac.new(
        OKX_SECRET.encode(),
        f'{ts}POST/api/v5/trade/order-algo{json.dumps(body)}'.encode(),
        hashlib.sha256
    ).digest()).decode()
    headers = {
        'OK-ACCESS-KEY': OKX_API_KEY,
        'OK-ACCESS-SIGN': sign,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': '1',
    }
    # P0 Fix: OCO挂单使用重试包装器（幂等，重复挂单OKX会接受但只生效第一个）
    ok, r, err = _okx_request_with_retry(
        'POST', 'https://www.okx.com/api/v5/trade/order-algo',
        headers=headers, data=json.dumps(body), max_retries=2, timeout=10
    )
    if not ok:
        _pilot_logger.error(f'    ❌ OCO网络/限流失败: {err}，重试后仍失败')
        results['sl'] = {'success': False, 'algoId': None, 'price': sl_price, 'error': err}
        results['tp'] = {'success': False, 'algoId': None, 'price': tp_price, 'error': err}
        return results

    try:
        result = r.json()
        if result.get('code') == '0':
            algo_id = result['data'][0]['algoId']
            _pilot_logger.info(f'    ✅ OCO已挂: SL@${sl_price} + TP@${tp_price} [id:{algo_id[:8]}]')
            results['sl'] = {'success': True, 'algoId': algo_id, 'price': sl_price, 'error': None}
            results['tp'] = {'success': True, 'algoId': algo_id, 'price': tp_price, 'error': None}
        else:
            err_msg = result.get('msg', '')
            _pilot_logger.error(f'    ❌ OCO失败: {err_msg}')
            results['sl'] = {'success': False, 'algoId': None, 'price': sl_price, 'error': err_msg}
            results['tp'] = {'success': False, 'algoId': None, 'price': tp_price, 'error': err_msg}
    except Exception as e:
        _pilot_logger.error(f'    ❌ OCO异常: {e}')
        results['sl'] = {'success': False, 'algoId': None, 'price': sl_price, 'error': str(e)}
        results['tp'] = {'success': False, 'algoId': None, 'price': tp_price, 'error': str(e)}

    return results


def _get_position_entry_price(instId: str) -> float:
    """从当前持仓获取入场价

    Args:
        instId: 合约ID (e.g. 'BTC-USDT-SWAP')

    Returns:
        float: 持仓入场价格，失败返回0.0
    """
    import hmac, hashlib, base64
    try:
        ts = _ts()
        sign = base64.b64encode(hmac.new(
            OKX_SECRET.encode(),
            f'{ts}GET/api/v5/account/positions?instId={instId}'.encode(),
            hashlib.sha256
        ).digest()).decode()
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
            'x-simulated-trading': '1',
        }
        ok, resp, err = _okx_request_with_retry(
            'GET',
            f'https://www.okx.com/api/v5/account/positions?instId={instId}',
            headers=headers, max_retries=2, timeout=10
        )
        if not ok:
            return 0
        data = resp.json()
        for pos in data.get('data', []):
            if pos.get('instId') == instId and float(pos.get('pos', 0)) > 0:
                return float(pos.get('avgPx', 0))
    except:
        pass
    return 0


def _set_leverage(instId, lev):
    """设置合约杠杆"""
    import hmac, hashlib, base64
    try:
        ts = _ts()
        body = json.dumps({'instId': instId, 'lever': str(lev), 'mgnMode': 'isolated'})
        sign = base64.b64encode(hmac.new(
            OKX_SECRET.encode(),
            f'{ts}POST/api/v5/account/set-leverage{body}'.encode(),
            hashlib.sha256
        ).digest()).decode()
        h = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
            'x-simulated-trading': '1',
        }
        ok, resp, err = _okx_request_with_retry(
            'POST',
            'https://www.okx.com/api/v5/account/set-leverage',
            headers=h, data=body, max_retries=2, timeout=10
        )
        if not ok:
            _pilot_logger.error(f'  杠杆设置失败: {err}')
            return
        result = resp.json()
        if result.get('code') != '0':
            _pilot_logger.error(f'  杠杆设置失败: {result.get("msg","")}')
    except Exception as e:
        _pilot_logger.error(f'  杠杆设置异常: {e}')

def okx_get_positions() -> list[dict]:
    """获取当前持仓（OKX实盘）

    Returns:
        list[dict]: 持仓列表，每项包含OKXpositions接口返回的字段
    """
    if DEMO_MODE:
        return {}
    
    import hmac, hashlib, base64
    
    try:
        ts = _ts()
        method = 'GET'
        path = '/api/v5/account/positions?instType=SWAP'
        
        sign = base64.b64encode(hmac.new(
            OKX_SECRET.encode(),
            f'{ts}{method}{path}'.encode(),
            hashlib.sha256
        ).digest()).decode()
        
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
            'x-simulated-trading': '1',
        }

        ok, resp, err = _okx_request_with_retry(
            'GET',
            f'https://www.okx.com{path}',
            headers=headers, max_retries=2, timeout=10
        )
        if not ok:
            _pilot_logger.error(f'  获取OKX持仓失败: {err}')
            return {}
        result = resp.json()
        if result.get('code') == '0':
            return {p['instId'].split('-')[0]: p for p in result.get('data', [])}
    except Exception as e:
        _pilot_logger.error(f'  获取OKX持仓失败: {e}')
    return {}

def okx_close_position(coin, side, size_contracts, pos_side='long'):
    """
    OKX平仓
    coin: BTC, ETH等
    side: 'sell'(平多) 或 'buy'(平空)
    size_contracts: 合约张数
    pos_side: 'long' 或 'short'
    """
    if DEMO_MODE:
        return {'demo': True, 'msg': '模拟盘模式'}
    
    import hmac, hashlib, base64 as b64
    
    try:
        ts = _ts()
        method = 'POST'
        path = '/api/v5/trade/order'
        
        body_dict = {
            'instId': f'{coin}-USDT-SWAP',
            'tdMode': 'isolated',
            'side': side,
            'ordType': 'market',
            'sz': str(size_contracts),
        }
        
        # net_mode 双向模式不需要posSide，只在模拟盘的long_short_mode需要
        pos_mode = get_okx_pos_mode()
        if pos_mode == 'long_short_mode':
            body_dict['posSide'] = pos_side
        
        body = json.dumps(body_dict)
        sign = b64.b64encode(hmac.new(
            OKX_SECRET.encode(),
            f'{ts}{method}{path}{body}'.encode(),
            hashlib.sha256
        ).digest()).decode()
        
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
            'x-simulated-trading': '1',
        }

        ok, resp, err = _okx_request_with_retry(
            'POST',
            f'https://www.okx.com{path}',
            headers=headers, data=body, max_retries=2, timeout=10
        )
        if not ok:
            _pilot_logger.error(f'  OKX平仓失败: {err}')
            return None
        result = resp.json()
        msg = result.get('msg', '')
        if result.get('code') == '0':
            _pilot_logger.info(f'  OKX平仓成功: {coin} {side} {size_contracts}张')
        else:
            sMsg = result.get('data', [{}])[0].get('sMsg', msg)
            _pilot_logger.error(f'  OKX平仓失败: {sMsg}')
        return result
    except Exception as e:
        _pilot_logger.error(f'  OKX平仓异常: {e}')
        return None

def open_paper_trade(signal, price, margin_consumed=0.0):
    """
    开仓记录 + OKX实盘下单

    Args:
        signal: 信号字典
        price: 开仓价格
        margin_consumed: 本批次中已消耗的保证金（美元），用于避免批量开仓超出总余额
    """
    log = load_paper_log()

    # 获取真实余额（用于计算仓位）
    try:
        from real_monitor import get_account_balance
        bal_data = get_account_balance()
        actual_balance = float(bal_data.get('totalEq', 20)) if bal_data else 20.0
    except Exception:
        actual_balance = 20.0  # 回退默认值

    # 批量开仓保护：如果本批次已消耗保证金，从可用余额中扣除
    # 这确保多个信号同时开仓时不会超出总余额
    adjusted_balance = max(actual_balance - margin_consumed, 0)
    if adjusted_balance < 1.0:
        _pilot_logger.warning('  信号作废: %s 可用余额%.2f USDT不足（已消耗保证金%.2f）' % (
            signal['coin'], adjusted_balance, margin_consumed))
        return None

    # 【新】结构性门槛检查：币价太贵则跳过
    # 防止账户太小（<$50）时误开BTC/ETH/SOL等高价值币
    STRUCTURAL_CONTRACT_VAL = {
        'BTC': 0.01, 'ETH': 0.1, 'SOL': 1, 'AVAX': 1,
        'ADA': 100, 'DOT': 1, 'DOGE': 1000,
    }
    try:
        ctVal = STRUCTURAL_CONTRACT_VAL.get(signal['coin'], 1)
        bars = c.fetch_ohlcv('%s-USDT' % signal['coin'], '1h', limit=5)
        if bars:
            cur_price = bars[-1][4]
            margin_per_contract = (cur_price * ctVal) / 3
            if margin_per_contract > actual_balance * 0.20:
                _pilot_logger.warning('  ⛔ %s 结构性不可交易（1张需保证金$%.2f > $%.2f的20%%），跳过' % (
                    signal['coin'], margin_per_contract, actual_balance * 0.20))
                return None
    except:
        pass

    # 计算合约张数（OKX USDT永续合约每张=$100）
    # 仓位 = 可用余额×2%÷止损距离（动态风险管理）
    # 张数 = 仓位÷100（OKX每张合约=$100）
    position_usdt = auto_position_sizing(signal['coin'], adjusted_balance)
    if position_usdt is None:
        _pilot_logger.warning('  信号作废: %s 余额%.2f USDT低于最小交易额' % (signal['coin'], actual_balance))
        return None  # 静默跳过，不报错

    contracts = max(1, int(position_usdt / 100))  # 每张=$100

    # ATR动态止损止盈（取代固定5%/20%）
    # _get_volatility_stop 已内置 sqrt(时间) 缩放，直接返回目标持仓时间的SL距离
    # tp_pct = 1.5×SL（2:1 赔率，比3:1更现实）
    # 持仓时间无强制上限：TP触发就平，SL触发就平，不等72小时
    sl_pct = _get_volatility_stop(signal['coin'], hold_hours=72)
    tp_pct = sl_pct * 1.5  # 2:1 赔率（SL=10.9% → TP=16.4%，72h内心理可达到）

    # 【新】市场状态过滤器：防止策略框架冲突（RSI均值回归 vs ADX趋势跟踪）
    # 当ADX>22（强趋势）+ RSI极端（<35或>65）时，方向与趋势相反的信号过滤
    # 例：ADX>22+RSI<35 → 强下跌趋势中的超卖 = 逆势做多 = 危险
    # 正确的做法：只在ADX<22（震荡市）做均值回归，或在ADX>22时顺势（不逆势）
    try:
        bars = c.fetch_ohlcv('%s-USDT' % signal['coin'], '1h', limit=14)
        if len(bars) >= 14:
            import numpy as np
            highs = np.array([b[2] for b in bars])
            lows = np.array([b[3] for b in bars])
            closes = np.array([b[4] for b in bars])
            dmp = np.mean(np.maximum(highs[1:] - highs[:-1], 0))
            dmm = np.mean(np.maximum(lows[:-1] - lows[1:], 0))
            adx_now = abs(dmp - dmm) / (dmp + dmm + 0.0001) * 100 if (dmp + dmm) > 0 else 0
            rsi_now = 100 - (100 / (1 + (closes[1:] < closes[:-1]).mean() / (closes[1:] > closes[:-1]).mean() + 0.0001))
            rsi_now = rsi_now[-14:].mean() if len(rsi_now) >= 14 else 50

            is_trending = adx_now > 22
            is_rsi_extreme = rsi_now < 35 or rsi_now > 65
            direction = signal['direction']
            is_counter_trend = (
                (direction == 'LONG' and rsi_now < 35 and is_trending) or
                (direction == 'SHORT' and rsi_now > 65 and is_trending)
            )
            if is_counter_trend and is_rsi_extreme and is_trending:
                trend_word = '下跌' if rsi_now < 35 else '上涨'
                _pilot_logger.warning(f'  ⛔ {sig["coin"]} 逆势信号过滤（ADX={adx_now:.0f}>22强趋势中RSI={rsi_now:.0f}{trend_word}，{"逆" if rsi_now < 35 else "逆"}做多危险），跳过')
                return None
    except:
        pass

    # ✅ P1: 预交易模拟验证（防止重复开仓、逆势交易、R:R不合理）
    # 如果任何检查失败，返回None（信号作废，不下单）
    try:
        # 1. 检查是否已有该币种未平仓仓位（防止重复开仓）
        open_for_coin = [t for t in log if t.get('coin') == signal['coin'] and t.get('status') == 'OPEN']
        if open_for_coin:
            _pilot_logger.warning(
                f'  ⛔ {signal["coin"]} 预交易模拟: 已有未平仓仓位'
                f'({open_for_coin[0]["direction"]} @ ${open_for_coin[0].get("entry_price", 0):.4f})，跳过'
            )
            return None

        # 2. R:R 风险收益比检查（止盈至少是止损的1.5倍）
        if sl_pct > 0 and tp_pct > 0:
            rr_ratio = tp_pct / sl_pct
            if rr_ratio < 1.5:
                _pilot_logger.warning(
                    f'  ⛔ {signal["coin"]} 预交易模拟: R:R={rr_ratio:.1f}:1 < 1.5:1 (SL={sl_pct:.2f}% TP={tp_pct:.2f}%)，跳过'
                )
                return None

        # 3. 价格偏移检查（信号价格 vs 当前价格，偏移超过3%要警告）
        if price > 0:
            sig_price = signal.get('price', price)
            if sig_price > 0:
                price_drift = abs(price - sig_price) / sig_price
                if price_drift > 0.03:
                    _pilot_logger.warning(
                        f'  ⛔ {signal["coin"]} 预交易模拟: 价格偏移{price_drift*100:.1f}%（信号${sig_price:.4f} vs 当前${price:.4f})，跳过'
                    )
                    return None

        # 4. 最小仓位检查（防止张数太少无效交易）
        if contracts < 1:
            _pilot_logger.warning(f'  ⛔ {signal["coin"]} 预交易模拟: 张数{contracts}<1，跳过')
            return None

        _pilot_logger.info(f'  ✅ {signal["coin"]} 预交易模拟通过（R:R={rr_ratio:.1f}:1 张数={contracts}）')
    except Exception:
        pass  # 任何检查异常都放行，不阻断交易

    # OKX实盘下单（同时模拟）
    side = 'buy' if signal['direction'] == 'LONG' else 'sell'
    result = okx_place_order(
        coin=signal['coin'],
        side=side,
        size_contracts=contracts,
        lev=LEV,
        sl_pct=sl_pct,
        tp_pct=tp_pct,
        price=price,
    )

    # 【新】使用result['success']判断整体是否完全成功（含SL/TP）
    # result = {
    #   'success': bool,       # 整体成功（含SL/TP）
    #   'code': str,            # 市价单code
    #   'entry_price': float,  # 成交价
    #   'sl': {...}, 'tp': {...},
    #   'position_closed': bool # SL/TP失败后是否已立即平仓
    # }
    if not isinstance(result, dict):
        result = {}

    order_success = result.get('success', False)
    order_code = result.get('code', '0')
    sl_info = result.get('sl', {})
    tp_info = result.get('tp', {})

    if order_success:
        status = 'OPEN'
        entry_price_real = result.get('entry_price', price)
        _pilot_logger.info('  ✅ 实盘开仓成功: %s %s %d张 @ $%.4f (SL=$%.4f TP=$%.4f)' % (
            signal['coin'], signal['direction'], contracts,
            entry_price_real,
            entry_price_real * (1 - sl_pct),
            entry_price_real * (1 + tp_pct)))
        record_trade_outcome(signal['coin'], 0, 'opened')
    else:
        position_closed = result.get('position_closed', False)
        sl_failed = not sl_info.get('success', True)
        tp_failed = not tp_info.get('success', True)

        if position_closed:
            # SL/TP失败 → 已立即平仓 → 记录为FAILED（紧急状态）
            status = 'FAILED'
            failed_type = 'SL' if sl_failed else 'TP'
            err = sl_info.get('error', '') or tp_info.get('error', '')
            _pilot_logger.error('  🔴 SL/TP挂单失败(%s: %s) → 已强制平仓 → 交易作废' % (failed_type, err))
            _pilot_logger.error('  ❌ 实盘开仓失败: %s' % signal['coin'])
        else:
            # 市价单本身失败
            status = 'FAILED'
            err_msg = result.get('sl', {}).get('error', '') or result.get('tp', {}).get('error', '')
            _pilot_logger.error('  ❌ 实盘开仓失败: %s code=%s msg=%s' % (signal['coin'], order_code, err_msg))
            # 余额不足错误自动加入黑名单静默跳过
            if order_code in ('51008', '51009') or 'insufficient balance' in err_msg.lower():
                add_to_blacklist(signal['coin'], 'insufficient_balance')
            elif order_code == '50102':
                add_to_blacklist(signal['coin'], 'timestamp_error', ttl_days=1)
                _pilot_logger.warning('  ⚠️ 时间戳错误，1天后自动重试')

    trade = {
        'id': len(log) + 1,
        'open_time': datetime.now().isoformat(),
        'coin': signal['coin'],
        'direction': signal['direction'],
        'entry_price': result.get('entry_price', price) if order_success else price,  # 真实成交价
        'size_usd': round(position_usdt, 2),  # 真实仓位金额（美元）
        'contracts': contracts,
        'confidence': signal['confidence'],
        'best_factor': signal['best_factor'],   # 触发决策的因子
        'ic': signal['ic'],
        'decay': signal.get('decay', 0),
        # ── P2 Fix: 补充完整交易日志 ───────────────────────────────
        # 开仓决策理由（来自signal_text，gemma4/规则引擎的判断依据）
        'open_reason': signal.get('reason') or signal.get('open_reason') or signal.get('signal_text', ''),
        # 止损止盈价格（计算得出）
        'sl_price': round(price * (1 - sl_pct / 100), 6),
        'tp_price': round(price * (1 + tp_pct / 100), 6),
        # 开仓时市场状态
        'rsi_at_entry': signal.get('rsi'),
        'adx_at_entry': signal.get('adx'),
        'btc_price_at_entry': signal.get('btc_price'),
        # ── 平仓字段（初始为空）────────────────────────────────
        'status': status,
        'exit_time': None,
        'exit_price': None,
        'result_pct': None,
        'pnl': 0,
        'hold_hours': None,
        'close_reason': None,   # 平仓时由close_paper_trade填写
        'okx_result': result,
    }
    if status == 'FAILED':
        trade['close_reason'] = result.get('msg', 'open_failed')[:50] if isinstance(result, dict) else 'open_failed'
    log.append(trade)
    save_paper_log(log)
    return trade

def close_paper_trade(coin, exit_price, reason='MANUAL'):
    """平仓记录 + OKX实盘平仓

    P2 Fix: 区分SL/TP，计算盈亏，填充详细close_reason
    """
    log = load_paper_log()
    for trade in reversed(log):
        if trade['coin'] == coin and trade['status'] == 'OPEN':
            trade['status'] = 'CLOSED'
            trade['exit_time'] = datetime.now().isoformat()
            trade['exit_price'] = exit_price

            entry = trade['entry_price']
            direction = trade['direction']
            contracts = trade.get('contracts', 0)
            sl_price = trade.get('sl_price')
            tp_price = trade.get('tp_price')
            open_reason = trade.get('open_reason', '?')

            # 计算收益率
            if direction == 'LONG':
                ret = (exit_price - entry) / entry
                okx_side = 'sell'
            else:
                ret = (entry - exit_price) / entry
                okx_side = 'buy'

            ret_with_lev = ret * LEV
            trade['result_pct'] = round(ret_with_lev * 100, 2)
            trade['pnl'] = round(ret_with_lev * contracts, 4)

            # 计算持仓时间
            open_dt = datetime.fromisoformat(trade['open_time'])
            close_dt = datetime.fromisoformat(trade['exit_time'])
            trade['hold_hours'] = round((close_dt - open_dt).total_seconds() / 3600, 1)

            # ── P2 Fix: 详细close_reason ─────────────────────────────
            # 区分SL/TP/手动平，并附带盈亏金额
            pnl_val = trade['pnl']
            if reason == 'SL':
                trade['close_reason'] = f'SL止损 | 亏损${abs(pnl_val):.2f} | {open_reason[:30]}'
            elif reason == 'TP':
                trade['close_reason'] = f'TP止盈 | 盈利+${abs(pnl_val):.2f} | {open_reason[:30]}'
            else:
                trade['close_reason'] = f'手动平仓 | {"盈利+" if pnl_val >= 0 else "亏损"}${abs(pnl_val):.2f} | {open_reason[:30]}'

            save_paper_log(log)
            record_trade_outcome(coin, pnl_val, trade['close_reason'])
            
            # OKX实盘平仓（如果非模拟模式）
            if not DEMO_MODE and contracts > 0:
                close_result = okx_close_position(
                    coin=coin,
                    side=okx_side,
                    size_contracts=contracts,
                    pos_side='long' if direction == 'LONG' else 'short'
                )
                trade['okx_close_result'] = close_result
            
            return trade
    return None

def check_stop_take_profit(prices):
    """检查所有持仓是否触发止损/止盈"""
    log = load_paper_log()
    closed = []
    for trade in log:
        if trade['status'] != 'OPEN':
            continue
        coin = trade['coin']
        if coin not in prices:
            continue
        price = prices[coin]
        entry = trade['entry_price']
        direction = trade['direction']
        
        if direction == 'LONG':
            pnl_pct = (price - entry) / entry * 100 * LEV
            if price <= entry * (1 - STOP_LOSS / LEV):
                closed.append((coin, price, 'SL', pnl_pct))
            elif price >= entry * (1 + TAKE_PROFIT / LEV):
                closed.append((coin, price, 'TP', pnl_pct))
        else:
            pnl_pct = (entry - price) / entry * 100 * LEV
            if price >= entry * (1 + STOP_LOSS / LEV):
                closed.append((coin, price, 'SL', pnl_pct))
            elif price <= entry * (1 - TAKE_PROFIT / LEV):
                closed.append((coin, price, 'TP', pnl_pct))
    
    result = []
    for coin, price, reason, pnl_pct in closed:
        t = close_paper_trade(coin, price, reason)
        if t:
            result.append((coin, price, reason, pnl_pct))
    return result

def get_performance_stats():
    """计算胜率统计"""
    log = load_paper_log()
    closed = [t for t in log if t['status'] == 'CLOSED']
    open_trades = [t for t in log if t['status'] == 'OPEN']
    
    if not closed:
        return {
            'total': len(log), 'wins': 0, 'losses': 0, 'win_rate': 0,
            'avg_win': 0, 'avg_loss': 0, 'wlr': 0, 'total_pnl': 0,
            'open': len(open_trades), 'best_coin': None, 'worst_coin': None,
        }
    
    wins = [t for t in closed if (t.get('result_pct') or 0) > 0]
    losses = [t for t in closed if (t.get('result_pct') or 0) < 0]
    
    win_pnl = [t['result_pct'] for t in wins]
    loss_pnl = [t['result_pct'] for t in losses]
    
    total_pnl = sum(t['result_pct'] for t in closed)
    avg_win = np.mean(win_pnl) if win_pnl else 0
    avg_loss = np.mean(loss_pnl) if loss_pnl else 0
    wlr = abs(avg_win / avg_loss) if avg_loss else 0
    
    # 各币种胜率
    coin_stats = {}
    for t in closed:
        c = t['coin']
        if c not in coin_stats:
            coin_stats[c] = {'wins': 0, 'losses': 0, 'total': 0}
        coin_stats[c]['total'] += 1
        if t['result_pct'] > 0:
            coin_stats[c]['wins'] += 1
        else:
            coin_stats[c]['losses'] += 1
    
    best_coin = max(coin_stats, key=lambda c: coin_stats[c]['wins'] / coin_stats[c]['total']) if coin_stats else None
    
    return {
        'total': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': len(wins) / len(closed) * 100,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'wlr': wlr,
        'total_pnl': total_pnl,
        'open': len(open_trades),
        'best_coin': best_coin,
        'coin_stats': coin_stats,
    }

# ============================================================
# OKX实时价格
# ============================================================
def get_okx_prices():
    BASE = 'https://www.okx.com/api/v5/market/ticker?instId='
    prices = {}
    def fetch_one(inst_id, coin):
        try:
            ok, r, err = _okx_request_with_retry('GET', BASE + inst_id, max_retries=2, timeout=5)
            if not ok:
                return coin, None
            d = r.json()
            if d.get('code') == '0':
                return coin, float(d['data'][0]['last'])
        except:
            pass
        return coin, None
    with ThreadPoolExecutor(max_workers=7) as ex:
        # P1 Fix: 用_get_allowed_coins()避免遗漏XRP/BNB/排除币种
        allowed = _get_allowed_coins()
        futures = [ex.submit(fetch_one, coin + '-USDT', coin) for coin in allowed]
        for f in as_completed(futures):
            coin, price = f.result()
            if price:
                prices[coin] = price
    return prices

# ============================================================
# 技术指标
# ============================================================
def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def adx(h, lo, c, p=14):
    pdm = h.diff(); mdm = -lo.diff()
    pdm[pdm < 0] = 0; mdm[mdm < 0] = 0
    tr = np.maximum(h - lo, np.maximum(abs(h - c.shift(1)), abs(lo - c.shift(1))))
    atr = tr.rolling(p).mean()
    pdi = 100 * (pdm.rolling(p).mean() / atr)
    mdi = 100 * (mdm.rolling(p).mean() / atr)
    dx = 100 * abs(pdi - mdi) / (pdi + mdi)
    return dx.rolling(p).mean()

def compute_ic(factors_df, ret_series, window=60):
    results = {}
    for fac in ['rsi', 'rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']:
        valid = factors_df[fac].notna() & ret_series.notna()
        fa = factors_df.loc[valid, fac].values[-window:]
        ra = ret_series.loc[valid].values[-window:]
        if len(fa) >= window and np.std(fa) > 1e-10 and np.std(ra) > 1e-10:
            from scipy.stats import spearmanr
            ic, _ = spearmanr(fa, ra)
            results[fac] = ic if not np.isnan(ic) else 0
        else:
            results[fac] = 0
    return results

# ============================================================
# 多周期IC计算
# ============================================================
COIN_TICKER_1D = {c: c + '-USD' for c in COINS}  # USDT日线用USD后缀
COIN_TICKER_1H = {c: c + '-USD' for c in COINS}  # 1h用USD现货

def analyze_multi_timeframe(coin):
    """计算1d(2年)+1h(30天)的IC，取最优周期"""
    results = {}
    
    # 1d数据（2年，用USD后缀）
    ticker_1d = coin + '-USD'  # 直接构造，避免COIN_TICKER_1D缺失新币
    try:
        df_1d = yf.download(ticker_1d, period='2y', interval='1d', progress=False, auto_adjust=True)
        if not df_1d.empty:
            if isinstance(df_1d.columns, pd.MultiIndex):
                df_1d.columns = [c[0].lower() for c in df_1d.columns]
            else:
                df_1d.columns = [c.lower() for c in df_1d.columns]
            df_1d.index = pd.to_datetime(df_1d.index)
            if hasattr(df_1d.index, 'tz') and df_1d.index.tz:
                df_1d.index = df_1d.index.tz_localize(None)
            results['1d'] = df_1d
    except:
        pass
    
    # 1h数据（30天，用USD后缀）
    ticker_1h = coin + '-USD'  # 直接构造，避免COIN_TICKER_1H缺失新币
    try:
        df_1h = yf.download(ticker_1h, period='30d', interval='1h', progress=False, auto_adjust=True)
        if not df_1h.empty:
            if isinstance(df_1h.columns, pd.MultiIndex):
                df_1h.columns = [c[0].lower() for c in df_1h.columns]
            else:
                df_1h.columns = [c.lower() for c in df_1h.columns]
            df_1h.index = pd.to_datetime(df_1h.index)
            if hasattr(df_1h.index, 'tz') and df_1h.index.tz:
                df_1h.index = df_1h.index.tz_localize(None)
            results['1h'] = df_1h
    except:
        pass
    
    ic_by_period = {}
    best_period = None
    best_abs_ic = 0
    best_signed_ic = 0
    
    for period, df in results.items():
        if df is None or len(df) < 70:
            continue
        
        close = df['close']
        high = df.get('high', close)
        low = df.get('low', close)
        vol = df.get('volume', 0)
        
        f = pd.DataFrame(index=df.index)
        f['close'] = close
        f['high'] = high
        f['low'] = low
        f['volume'] = vol
        f['rsi'] = rsi(close)
        f['rsi_inv'] = 100 - f['rsi']
        f['adx'] = adx(high, low, close)
        f['vol_ma5'] = vol.rolling(5).mean()
        f['vol_ma20'] = vol.rolling(20).mean()
        f['vol_ratio'] = f['vol_ma5'] / f['vol_ma20'].replace(0, np.nan)
        f['trend_ma20'] = close / close.rolling(20).mean() - 1
        f['ret_next'] = close.pct_change().shift(-1)

        fd = f[['rsi', 'rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']]
        window = min(60, len(fd) - 5)
        if window < 20:
            continue
        ic_data = compute_ic(fd, f['ret_next'], window)
        ic_by_period[period] = ic_data
        
        # 用signed IC选最优周期（有方向性的IC）
        rsi_ic = ic_data.get('rsi_inv', 0)
        if abs(rsi_ic) > best_abs_ic:
            best_abs_ic = abs(rsi_ic)
            best_signed_ic = rsi_ic
            best_period = period
    
    if best_period is None:
        return {}, None, 0, {}
    
    return ic_by_period, best_period, best_abs_ic, results

# ============================================================
# 信号生成（多周期+RSI+ADX多空策略）
# ============================================================
def generate_signals():
    """核心信号生成：多周期IC + RSI+ADX多空策略"""
    signals = []
    prices = {}

    # P1 Fix: 只扫描coin_strategy_map中未标记为excluded的币种
    for coin in _get_allowed_coins():
        # 多周期IC分析
        ic_by_period, best_period, best_period_ic, dfs = analyze_multi_timeframe(coin)
        
        if not dfs:
            continue
        
        df = dfs.get(best_period, list(dfs.values())[0])
        if df is None or len(df) < 70:
            continue
        
        close = df['close']
        high = df.get('high', close)
        low = df.get('low', close)
        vol = df.get('volume', 0)
        
        f = pd.DataFrame(index=df.index)
        f['close'] = close
        f['high'] = high
        f['low'] = low
        f['volume'] = vol
        f['rsi'] = rsi(close)
        f['rsi_inv'] = 100 - f['rsi']
        f['adx'] = adx(high, low, close)
        f['vol_ma5'] = vol.rolling(5).mean()
        f['vol_ma20'] = vol.rolling(20).mean()
        f['vol_ratio'] = f['vol_ma5'] / f['vol_ma20'].replace(0, np.nan)
        f['trend_ma20'] = close / close.rolling(20).mean() - 1
        f['ret_next'] = close.pct_change().shift(-1)
        
        prices[coin] = float(close.iloc[-1])
        
        # RSI+ADX策略信号（核心策略）
        rsi_v = f['rsi'].iloc[-1]
        adx_v = f['adx'].iloc[-1]
        close_v = close.iloc[-1]
        
        # IC分析（用60天窗口，signed值用于方向判断）
        # ✅ P2 Fix: RSI和RSI_inv分开检测，SHORT检查RSI IC（动量），LONG检查RSI_inv IC（均值回归）
        fd = f[['rsi', 'rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']]
        ic_now = compute_ic(fd, f['ret_next'], 60)
        ic_7d = compute_ic(fd.iloc[:-7], f['ret_next'].iloc[:-7], 60) if len(f) > 7 else ic_now
        decay = {fac: (ic_now.get(fac, 0) - ic_7d.get(fac, 0)) / 7 for fac in ic_now}

        # IC自适应权重调整置信度
        weights, is_adaptive = get_ic_weights()
        # 计算加权IC分数（综合所有因子）
        ic_score = sum(ic_now.get(f, 0) * weights.get(f, 0) for f in weights)
        # RSI IC：原始RSI（用于SHORT动量验证）；RSI_inv IC：逆RSI（用于LONG均值回归验证）
        rsi_ic = ic_now.get('rsi', 0)       # 原始RSI的IC：负值=高RSI→下跌（动量SHORT确认）
        rsi_inv_ic = ic_now.get('rsi_inv', 0)  # 逆RSI的IC：正值=低RSI→上涨（均值回归LONG确认）
        rsi_ic_abs = abs(rsi_ic)

        # 多空方向（RSI+ADX核心策略）
        direction = None
        strategy = ''
        if rsi_v < 35 and adx_v > 22:
            direction = 'LONG'
            strategy = f'RSI({rsi_v:.0f})<35+ADX({adx_v:.0f})>22'
        elif rsi_v > 65 and adx_v > 22:
            direction = 'SHORT'
            strategy = f'RSI({rsi_v:.0f})>65+ADX({adx_v:.0f})>22'

        if direction is None:
            continue

        # ============================================================
        # IC自适应权重过滤
        # ============================================================
        # 方向一致性：LONG → RSI_inv IC > 0（低RSI→上涨）；SHORT → RSI IC < 0（高RSI→下跌）
        if direction == 'LONG':
            direction_match = rsi_inv_ic > 0   # 均值回归确认
            ic_check_val = rsi_inv_ic
        else:  # SHORT
            direction_match = rsi_ic < 0   # 动量确认：高RSI→下跌
            ic_check_val = rsi_ic

        # IC强度判断（用绝对值）
        ic_dir_str = '做多' if ic_check_val > 0 else '做空'
        if rsi_ic_abs < IC_THRESHOLD:
            skip_reason = f'IC强度不足({rsi_ic_abs:.3f}<{IC_THRESHOLD})'
        elif not direction_match:
            skip_reason = f'方向背离(策略{direction}但IC方向{ic_dir_str})'
        else:
            skip_reason = None

        # 记录被跳过的信号（用于事后分析）
        if skip_reason:
            log_skipped_signal(coin, direction, strategy, ic_check_val, rsi_v, adx_v, best_period, skip_reason)
            continue

        # 衰减降权
        ic_filter_factor = 'rsi_inv'  # 通过IC过滤的因子
        rsi_decay = decay.get('rsi_inv', 0)
        decay_penalty = 0.0
        if abs(rsi_decay) > IC_DECAY_ALERT:
            decay_penalty = 0.3  # 衰减超阈值，仓位降30%

        # IC强度 → 仓位（使用加权IC分数调整）
        # ic_score > 0 表示因子综合表现好 → 放大仓位
        # ic_score < 0 表示因子综合表现差 → 缩小仓位
        ic_strength = min(1.0, rsi_ic_abs * 5)  # IC绝对值×5，最高1.0
        ic_weight_boost = max(0.5, min(1.5, 1.0 + ic_score * 2)) if is_adaptive else 1.0

        # P4: 动态仓位分配（基于IC质量+历史表现）
        coin_alloc = get_per_coin_allocation(coin)  # 0.0-0.4（建议分配比例）
        # alloc_boost = coin_alloc / MAX_POSITION：0.4→1.0, 0.2→0.67, 0→0
        alloc_boost = coin_alloc / MAX_POSITION if coin_alloc >= 0.05 else 0.0
        alloc_boost = max(0.0, min(1.5, alloc_boost))  # 限制0x~1.5x

        position_size = ic_strength * ic_weight_boost * (1.0 - decay_penalty) * MAX_POSITION * alloc_boost
        position_size = max(0.05, min(MAX_POSITION, position_size))  # 5%~30%

        # 0分配（DOGE/SOL黑名单等）→ 跳过该信号
        if coin_alloc <= 0:
            continue

        # 置信度（IC强度×100，加权调整）
        confidence = int(ic_strength * 100 * ic_weight_boost)
        confidence = max(10, min(100, confidence))
        if abs(rsi_decay) > IC_DECAY_ALERT:
            confidence = max(10, confidence - 30)
        
        labels = {'rsi_inv': 'RSI均值', 'vol_ratio': '成交量', 'adx': 'ADX趋势', 'trend_ma20': '动量'}
        
        arrow = '🟢' if direction == 'LONG' else '🔴'
        conf_bar = '█' * (confidence // 10) + '░' * (10 - confidence // 10)
        decay_icon = '📉' if rsi_decay > IC_DECAY_ALERT else '➖'
        period_tag = f"[{best_period.upper()}]"
        
        # ── P2 Fix: 构建有意义的开仓理由 ───────────────────────
        # 原来best_factor永远硬编码为'rsi_inv'，毫无意义
        # 现在的开仓理由描述完整的决策依据
        if direction == 'LONG':
            open_reason = (
                f"RSI={rsi_v:.0f}<35超卖" if rsi_v < 35 else
                f"RSI={rsi_v:.0f}<50均值回归" if rsi_v < 50 else
                f"ADX={adx_v:.0f}强势趋势"
            )
        else:
            open_reason = (
                f"RSI={rsi_v:.0f}>65超买" if rsi_v > 65 else
                f"RSI={rsi_v:.0f}>50均值回归" if rsi_v > 50 else
                f"ADX={adx_v:.0f}下跌趋势"
            )
        best_factor_actual = (
            'RSI均值回归' if rsi_v < 35 or rsi_v > 65 else
            'ADX趋势' if adx_v > 22 else
            '波动率' if rsi_v else
            'rsi_inv'
        )

        signals.append({
            'coin': coin,
            'direction': direction,
            'confidence': confidence,
            'position_size': position_size,
            'best_factor': best_factor_actual,  # P2 Fix: 不再硬编码为'rsi_inv'
            'best_factor_label': 'RSI+ADX多空',
            'ic': rsi_ic,
            'adx_ic': ic_now.get('adx', 0),
            'decay': decay.get('rsi_inv', 0),
            'entry_price': prices[coin],
            'strategy': strategy,
            'best_period': best_period,
            'ic_filter_factor': ic_filter_factor,
            'ic_all': ic_now,
            'rsi': rsi_v,
            'adx': adx_v,
            # ── P2 Fix: 补充开仓决策理由和BTC价格 ─────────────────
            'open_reason': f'{open_reason} | IC={rsi_ic:.3f} | {strategy} | {period_tag}',
            'btc_price': prices.get('BTC'),
            'signal_text': f'{arrow} {period_tag} {coin}: {direction} | {strategy} | RSI_IC={rsi_ic:.3f} | 置信{conf_bar}{confidence}% | 仓位{position_size*100:.0f}% | 衰减{decay_icon}',
            # 信号过期机制（5分钟超时）
            'signal_time': time.time(),
        })

    return signals, prices

# ============================================================
# 飞书推送
# ============================================================
def push_feishu(message):
    try:
        app_id = os.environ.get('FEISHU_APP_ID', '')
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')
        if not app_id or not app_secret:
            _pilot_logger.warning('    ⚠️ 飞书APP_ID/APP_SECRET未配置，跳过推送')
            return False
        tr = requests.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                         json={'app_id': app_id, 'app_secret': app_secret}, timeout=10)
        td = tr.json()
        if td.get('code') != 0:
            return False
        token = td.get('tenant_access_token')
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        payload = {
            'receive_id': 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0',
            'msg_type': 'text',
            'content': json.dumps({'text': message[:4000]}),
        }
        params = {'receive_id_type': 'chat_id'}
        rr = requests.post('https://open.feishu.cn/open-apis/im/v1/messages',
                          headers=headers, json=payload, params=params, timeout=10)
        return rr.json().get('code') == 0
    except:
        return False

# ============================================================
# 完整日报
# ============================================================

def run_full_report():
    """精简日报：只报告真实操作和异常，无操作时静默"""
    now = datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M')
    
    prices = get_okx_prices()
    signals, cross = generate_signals()
    
    # Phase 2: 保存每日IC快照
    save_ic_snapshot(signals)

    # 因子权重计算（先计算，再分析衰减）
    weights, weight_err = compute_ic_weights()
    weights_report = format_ic_weights_report()  # 总是调用，显示等权或自适应

    # 因子衰减分析
    proposal_result, proposal_text = analyze_factor_weights()

    # P4: 动态仓位分配计算
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        from real_monitor import get_account_balance
        bal_data = get_account_balance()
        equity = float(bal_data.get('totalEq', 20)) if bal_data else 20.0
    except Exception as e:
        _pilot_logger.warning('    [WARN] equity获取失败: %s' % e)
        equity = 20.0
    alloc_result, alloc_data = compute_per_coin_allocation(equity)
    allocation_report = format_allocation_report(alloc_data)
    
    # 自动开仓
    log = load_paper_log()
    open_coins = {t['coin'] for t in log if t['status'] == 'OPEN'}
    for sig in signals:
        sig['expected_profit'] = sig.get('confidence', 0) * sig.get('position_size', 0)
    signals_sorted = sorted(signals, key=lambda x: x['expected_profit'], reverse=True)
    new_trades = []
    margin_consumed = 0.0  # 追踪本批次已消耗的保证金

    for sig in signals_sorted:
        if len(new_trades) >= 3 - len(open_coins):
            break
        if sig['coin'] in open_coins or is_blacklisted(sig['coin']) or sig['confidence'] < 50:
            continue

        # 熔断器检查：连续3次亏损后禁止开仓
        try:
            sys.path.insert(0, str(pathlib.Path(__file__).parent))
            from kronos_heartbeat import check_circuit_breaker, record_trade_outcome
            tripped, reason, _ = check_circuit_breaker()
            if tripped:
                _pilot_logger.warning('  ⛔ 熔断已触发，禁止开仓: %s' % reason)
                break
        except Exception:
            pass  # 熔断器模块异常时不阻止交易

        # 信号过期检查（5分钟超时）
        signal_age = time.time() - sig.get('signal_time', 0)
        if signal_age >= 300:
            _pilot_logger.warning('  ⏰ 跳过 %s: 信号已过期(%.0f秒>5分钟)' % (sig['coin'], signal_age))
            continue

        trade = open_paper_trade(
            sig,
            prices.get(sig['coin'], sig['entry_price']),
            margin_consumed=margin_consumed
        )
        if trade:
            new_trades.append(trade)
            # 追踪保证金消耗（每张=$100，保证金=张数×100÷杠杆）
            contracts = trade.get('size_contracts', 1)
            margin_consumed += contracts * 100.0 / LEV  # 累计本批次已消耗保证金

    # 精简报告
    emoji = '\U0001f7e2' if not DEMO_MODE else '\U0001f7e3'
    mode_txt = '实盘' if not DEMO_MODE else '模拟盘'
    lines_out = ['**Kronos %s** %s %s' % (mode_txt, emoji, now_str), '']
    
    if new_trades:
        lines_out.append('OK 新开仓:')
        for t in new_trades:
            lines_out.append('- %s %s @ $%.4f' % (t['coin'], t['direction'], t['entry_price']))
    
    bl = load_blacklist()
    if bl:
        lines_out.extend(['', '\u26d4 黑名单:'])
        for sym, info in bl.items():
            lines_out.append('- %s (%s)' % (sym, info['reason']))
    
    decaying = [s for s in signals if abs(s.get('decay', 0)) > IC_DECAY_ALERT]
    if decaying:
        lines_out.extend(['', 'IC衰减:'])
        for s in decaying:
            lines_out.append('- %s %s: IC%.3f' % (s['coin'], s['strategy'], s.get('ic', 0)))
    
    if proposal_result:
        lines_out.extend(['', '因子调整:'])
        for p in proposal_result.get('applied', []):
            lines_out.append('- %s: %.3f->%.3f (%+.0f%%)' % (p['factor'], p['mean_first'], p['mean_second'], p['change_pct']*100))

    if weights_report and len(weights_report) > 10:
        lines_out.extend(['', 'IC权重:'])
        for line in weights_report.split('\n')[1:]:  # 跳过标题行
            if line.strip():
                lines_out.append('  ' + line.strip())

    # P4: 动态仓位分配
    if allocation_report:
        lines_out.extend(['', '动态仓位:'])
        for line in allocation_report.split('\n')[1:]:  # 跳过标题行
            if line.strip():
                lines_out.append('  ' + line.strip())

    report = '\n'.join(lines_out)
    if len([l for l in lines_out if l.strip()]) < 2 and not new_trades and not proposal_result:
        report = None
    
    if report:
        _pilot_logger.info(report)
        pushed = push_feishu(report)
        _pilot_logger.info('飞书推送: %s' % ('成功' if pushed else '失败'))
    else:
        _pilot_logger.info('日报: 无更新（静默）')
    
    # v1.4: 保存市场情绪数据供 kronos_multi_coin 使用（L1-L5全层）
    try:
        from kronos_utils import get_multi_funding_and_oi
        sentiment = get_cross_layer_signals()
        # L1数据：Funding Rate + Open Interest（OKX公开接口，无需认证）
        COINS_FOR_L1 = ['BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'AVAX', 'DOT', 'LINK', 'XRP', 'BCH']
        l1_data = get_multi_funding_and_oi(COINS_FOR_L1)
        # 整理L1数据为信号（只输出异常值）
        l1_signals = []
        for coin, data in l1_data.items():
            rate_pct = data.get('rate', 0)
            if abs(rate_pct) > 0.03:  # 资金费率异常（>0.03%）
                if rate_pct > 0:
                    l1_signals.append(f'{coin}资金费率{rate_pct:+.3f}%(多头付钱)')
                else:
                    l1_signals.append(f'{coin}资金费率{rate_pct:+.3f}%(空头付钱)')
        sentiment['l1_funding'] = l1_data
        sentiment['l1_signals'] = l1_signals

        # v1.4: L2新闻事件层（RSS抓取，CoinDesk/CoinTelegraph）
        try:
            news_alerts = []
            RSS_FEEDS = [
                ('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/'),
                ('CoinTelegraph', 'https://cointelegraph.com/rss'),
            ]
            for source, url in RSS_FEEDS:
                try:
                    r = requests.get(url, timeout=8)
                    from xml.etree import ElementTree as ET
                    root = ET.fromstring(r.text)
                    for item in root.findall('.//item')[:5]:
                        title = (item.findtext('title') or '').upper()
                        for coin in ['BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'AVAX', 'XRP']:
                            if coin in title:
                                news_alerts.append({'coin': coin, 'source': source, 'title': item.findtext('title', '')[:80]})
                except:
                    pass
            sentiment['news_alert'] = news_alerts[:5]  # 最多5条
            if news_alerts:
                parts = ['%s@%s' % (a['coin'], a['source']) for a in news_alerts[:3]]
                _pilot_logger.info('  📰 新闻事件: %s' % ' | '.join(parts))
        except Exception:
            sentiment['news_alert'] = []

        sentiment_cache = Path.home() / '.hermes/cron/output/market_sentiment.json'
        sentiment_cache.parent.mkdir(parents=True, exist_ok=True)
        sentiment_cache.write_text(json.dumps({
            'updated': datetime.now().isoformat(),
            'data': sentiment,
        }))
        if l1_signals:
            joined = ' | '.join(l1_signals[:3])
            _pilot_logger.info(f'  L1资金费率: {joined}')
    except Exception as e:
        _pilot_logger.warning(f'[WARN] 市场情绪缓存失败: {e}')
    
    return signals, prices, {}


# ============================================================
# Phase 2: 因子权重自动提案系统
# ============================================================

import os
from scipy.stats import ttest_ind

PROPOSAL_FILE = os.path.join(CACHE_DIR, 'pending_proposal.json')

def save_ic_snapshot(signals):
    """每日将IC数据快照保存到ic_history.json（key=日期）"""
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        with open(IC_HISTORY) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    snapshot = {}
    for sig in signals:
        coin = sig['coin']
        ic_all = sig.get('ic_all', {})
        if isinstance(ic_all, dict):
            snapshot[coin] = {k: float(v) for k, v in ic_all.items()}
            snapshot[coin]['best'] = sig.get('best_factor', '—')

    if today in history:
        # 今天已有数据，不覆盖（多次运行取第一次）
        return

    history[today] = snapshot
    with open(IC_HISTORY, 'w') as f:
        json.dump(history, f, indent=2)


def compute_ic_weights():
    """
    根据7天IC历史计算因子归一化权重
    算法：
    1. 收集每个因子在所有币种上的IC均值
    2. 只用IC>0的因子，IC<=0的因子权重=0（表示无效）
    3. 归一化：w_i = IC_i / sum(IC_positive)
    4. 最小权重0.05
    5. 保存到factor_weights.json

    返回：(weights_dict or None, report_str)
    """
    try:
        with open(IC_HISTORY) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None, "IC历史文件不存在"

    dates = sorted(history.keys())

    # 数据不足3天 → 用等权并保存（永远保存文件）
    if len(dates) < 3:
        factors = ['rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']
        weights = {f: 1.0 / len(factors) for f in factors}
        weight_data = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'n_days': len(dates),
            'weights': {f: round(w, 4) for f, w in weights.items()},
            'mean_ic': {f: 0.0 for f in factors},
        }
        with open(FACTOR_WEIGHTS_FILE, 'w') as wf:
            json.dump(weight_data, wf, indent=2)
        return weights, "IC历史不足（%d/7天）" % len(dates)

    # 收集每个因子的IC序列（跨所有币种和日期）
    factors = ['rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']
    factor_ics = {f: [] for f in factors}

    for d in dates:
        for coin, data in history[d].items():
            if isinstance(data, dict):
                # ic_history.json 格式: {coin: {ic: {factor: value}}}
                ic_data = data.get('ic', data)  # 兼容新旧格式
                for f in factors:
                    if f in ic_data and isinstance(ic_data[f], (int, float)):
                        factor_ics[f].append(float(ic_data[f]))

    # 计算每个因子的平均IC
    factor_mean_ic = {}
    for f in factors:
        vals = factor_ics[f]
        if len(vals) >= 3:
            factor_mean_ic[f] = sum(vals) / len(vals)
        else:
            factor_mean_ic[f] = 0.0

    # 归一化权重（只对IC>0的因子）
    positive_ic = {f: v for f, v in factor_mean_ic.items() if v > 0}
    total_positive_ic = sum(positive_ic.values())

    if total_positive_ic <= 0:
        # 所有因子IC都<=0，用等权
        weights = {f: 1.0 / len(factors) for f in factors}
    else:
        raw_weights = {f: v / total_positive_ic for f, v in positive_ic.items()}
        # IC<=0的因子给最小权重
        weights = {}
        for f in factors:
            if f in raw_weights:
                weights[f] = max(raw_weights[f], 0.05)
            else:
                weights[f] = 0.05

    # 重新归一化
    total = sum(weights.values())
    if total > 0:
        weights = {f: w / total for f, w in weights.items()}

    # 保存
    weight_data = {
        'date': dates[-1],
        'n_days': len(dates),
        'weights': {f: round(w, 4) for f, w in weights.items()},
        'mean_ic': {f: round(v, 4) for f, v in factor_mean_ic.items()},
    }
    with open(FACTOR_WEIGHTS_FILE, 'w') as f:
        json.dump(weight_data, f, indent=2)

    return weights, None


def get_ic_weights():
    """
    读取当前因子权重
    返回：(weights_dict, is_adaptive)
    - is_adaptive=True: 从7天历史计算的自适应权重
    - is_adaptive=False: 等权（数据不足）
    """
    try:
        with open(FACTOR_WEIGHTS_FILE) as f:
            data = json.load(f)
        weights = data.get('weights', {})
        is_adaptive = data.get('n_days', 0) >= 3
        return weights, is_adaptive
    except:
        return {'rsi_inv': 0.25, 'vol_ratio': 0.25, 'adx': 0.25, 'trend_ma20': 0.25}, False


def format_ic_weights_report():
    """生成IC权重状态报告"""
    weights, is_adaptive = get_ic_weights()
    n_days = 0
    try:
        with open(FACTOR_WEIGHTS_FILE) as f:
            n_days = json.load(f).get('n_days', 0)
    except:
        pass

    mode = '自适应' if is_adaptive else '等权(数据不足)'
    lines = ['━━━ IC因子权重(%s) ━━━' % mode]
    if is_adaptive:
        lines.append('基于%d天IC历史计算' % n_days)

    factor_labels = {
        'rsi_inv': 'RSI逆因子',
        'vol_ratio': '成交量比',
        'adx': 'ADX趋势',
        'trend_ma20': 'MA20趋势',
    }
    for f, w in sorted(weights.items(), key=lambda x: -x[1]):
        label = factor_labels.get(f, f)
        bar = '█' * int(w * 40) + '░' * (40 - int(w * 40))
        lines.append('  %s %.1f%% %s' % (label, w * 100, bar))

    if not is_adaptive:
        lines.append('  ⚠️ 数据不足3天，使用等权')

    return '\n'.join(lines)


def analyze_factor_weights():
    """
    分析最近7天IC数据，判断因子是否显著衰减。
    显著衰减（p<0.05且|变化|>20%）→ 自动降低权重 → 记录提案
    返回: (proposal_dict or None, report_str)
    """
    try:
        with open(IC_HISTORY) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None, "IC历史不足（需要7天数据）"

    dates = sorted(history.keys())
    if len(dates) < 3:
        return None, "IC历史不足（%d/7天）" % len(dates)

    # 最近7天
    recent = dates[-7:] if len(dates) >= 7 else dates
    mid = len(recent) // 2
    first_half = recent[:mid] if mid > 0 else recent[:1]
    second_half = recent[mid:] if mid > 0 else recent[1:]

    # 收集每个因子的IC序列
    factors = ['rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']
    proposals = []

    for factor in factors:
        ic_first = []
        ic_second = []
        for d in first_half:
            for coin, data in history[d].items():
                if factor in data:
                    ic_first.append(data[factor])
        for d in second_half:
            for coin, data in history[d].items():
                if factor in data:
                    ic_second.append(data[factor])

        if len(ic_first) < 3 or len(ic_second) < 3:
            continue

        mean_first = sum(ic_first) / len(ic_first)
        mean_second = sum(ic_second) / len(ic_second)

        if mean_first == 0:
            continue

        change_pct = (mean_second - mean_first) / abs(mean_first)
        # t检验
        t_stat, p_val = ttest_ind(ic_first, ic_second)
        is_significant = p_val < 0.05 and abs(change_pct) > 0.20

        proposals.append({
            'factor': factor,
            'mean_first': mean_first,
            'mean_second': mean_second,
            'change_pct': change_pct,
            'p_value': p_val,
            'is_significant': is_significant,
            'n_samples': len(ic_first) + len(ic_second),
        })

    # 生成提案报告
    if not proposals:
        return None, "无足够数据生成提案（样本不足）"

    report = "## 📊 因子权重提案 (%s)\n\n" % dates[-1]
    report += "| 因子 | 前半期IC |  后半期IC | 变化 | p值 | 状态 |\n"
    report += "|------|---------|---------|------|-----|------|\n"

    applied = []
    for p in proposals:
        icon = '🔴衰减' if p['is_significant'] and p['change_pct'] < 0 else (
               '🟢增强' if p['is_significant'] and p['change_pct'] > 0 else '⚪稳定')
        status = '建议降低' if p['is_significant'] and p['change_pct'] < 0 else (
                 '建议提高' if p['is_significant'] and p['change_pct'] > 0 else '维持现状')
        report += '| %s | %.3f | %.3f | %+.0f%% | %.2f | %s %s |\n' % (
            p['factor'], p['mean_first'], p['mean_second'],
            p['change_pct']*100, p['p_value'], icon, status)

        # 显著衰减 → 自动调整
        if p['is_significant'] and p['change_pct'] < -0.20:
            applied.append(p)

    if applied:
        report += '\n**已自动调整（显著衰减 p<0.05, |变化|>20%%）**\n'
        for p in applied:
            report += '- %s: %.3f→%.3f (%+.0f%%) → 已降低权重\n' % (
                p['factor'], p['mean_first'], p['mean_second'], p['change_pct']*100)
        # 保存提案
        with open(PROPOSAL_FILE, 'w') as f:
            json.dump({'proposals': proposals, 'applied': applied, 'date': dates[-1]}, f, indent=2)
        return {'applied': applied}, report
    else:
        report += '\n**无显著变化，维持现状**\n'
        return None, report


# ============ 动态仓位管理器（P4） ============
# 基于IC质量 + 历史表现动态分配资金
# 参考: Automaton的自我优化机制

COIN_PERFORMANCE_FILE = os.path.join(CACHE_DIR, 'per_coin_performance.json')


def compute_per_coin_performance():
    """
    从paper_trades计算每个币的历史表现
    返回: {coin: {win_rate, avg_win, avg_loss, wrr, n_trades, total_pnl}}
    """
    try:
        with open(PAPER_TRADES) as f:
            trades = json.load(f)
    except:
        return {}

    from collections import defaultdict
    coin_data = defaultdict(list)
    for t in trades:
        if t.get('status') == 'CLOSED' and t.get('pnl') not in (None, '?', ''):
            coin_data[t['coin']].append(float(t['pnl']))

    result = {}
    for coin, pnls in coin_data.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / len(pnls) if pnls else 0.5
        avg_win = sum(wins) / len(wins) if wins else 0.001
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.001
        wrr = avg_win / avg_loss if avg_loss else 1.0
        result[coin] = {
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'wrr': wrr,
            'n_trades': len(pnls),
            'total_pnl': sum(pnls),
        }

    return result


def compute_per_coin_allocation(equity=None):
    """
    基于IC质量 + 历史表现计算每个币的推荐仓位比例
    算法：
    1. 获取各币的IC均值（从7天IC历史）
    2. 获取各币的历史表现（从paper_trades）
    3. 综合得分 = IC_score × 0.4 + performance_score × 0.6
       其中 IC_score = min(1.0, IC_mean/0.2)
       performance_score = win_rate × 0.5 + min(1.0, WLR/2) × 0.5
    4. 归一化为0-40%的分配比例，DOGE/SOL永远=0
    5. 保留20%现金
    """
    if equity is None:
        equity = 20.0

    # ============ 结构性排除：币种最小交易门槛 ============
    # 如果某币1张合约的保证金超过账户的20%，该币种不适合当前账户规模
    # 永不做分配，等账户规模扩大后再开放
    STRUCTURAL_EXCLUDE = set()
    # OKX合约乘数（ctVal = 每个合约的币数量）
    CONTRACT_VAL = {
        'BTC': 0.01, 'ETH': 0.1, 'SOL': 1, 'AVAX': 1,
        'ADA': 100, 'DOT': 1, 'DOGE': 1000,
        'XRP': 1, 'BNB': 1,  # P1 Fix: 添加XRP/BNB合约乘数
    }
    try:
        import ccxt
        c = ccxt.okx({'enableRateLimit': True})
        for coin in _get_allowed_coins():
            try:
                ctVal = CONTRACT_VAL.get(coin, 1)
                bars = c.fetch_ohlcv('%s-USDT' % coin, '1h', limit=5)
                if not bars:
                    continue
                price = bars[-1][4]
                contract_notional = price * ctVal  # 名义价值
                margin_per_contract = contract_notional / 3  # 3x杠杆
                # 最小可行：保证金不超过账户的20%
                if margin_per_contract > equity * 0.20:
                    STRUCTURAL_EXCLUDE.add(coin)
                    _pilot_logger.warning(f'  ⛔ {coin} 结构性排除（1张需保证金${margin_per_contract:.2f} > ${equity*0.20:.2f}的20%）')
            except:
                pass
    except:
        pass

    # 从动态黑名单读取当前禁止交易的币种（临时失败如timestamp_error不排除）
    blacklist_dict = load_blacklist() or {}
    blacklist = set(k.upper() for k, v in blacklist_dict.items()
                   if v.get('reason') not in ('timestamp_error', 'open_failed'))

    # Step 1: IC均值
    ic_means = {}
    try:
        with open(IC_HISTORY) as f:
            ic_hist = json.load(f)
        dates = sorted(ic_hist.keys())
        if dates:
            recent = dates[-7:] if len(dates) >= 7 else dates
            for coin in _get_allowed_coins():
                ic_vals = []
                for d in recent:
                    if coin in ic_hist[d] and isinstance(ic_hist[d][coin], dict):
                        ic_val = ic_hist[d][coin].get('rsi_inv', 0)
                        ic_vals.append(ic_val)
                ic_means[coin] = sum(ic_vals) / len(ic_vals) if ic_vals else 0.0
    except:
        ic_means = {c: 0.05 for c in _get_allowed_coins()}

    # Step 2: 历史表现
    perf = compute_per_coin_performance()

    # Step 3: 综合得分（使用动态币种列表）
    scores = {}
    for coin in _get_allowed_coins():
        if coin in blacklist or coin in STRUCTURAL_EXCLUDE:
            scores[coin] = 0.0
            continue

        # IC得分（归一化）
        ic_val = abs(ic_means.get(coin, 0))
        ic_score = min(1.0, ic_val / 0.2) if ic_val > 0 else 0.0

        # 表现得分
        if coin in perf and perf[coin]['n_trades'] >= 1:
            p = perf[coin]
            wr_score = p['win_rate']
            wrr_score = min(1.0, p['wrr'] / 2.0)
            perf_score = wr_score * 0.5 + wrr_score * 0.5
        else:
            perf_score = 0.5  # 数据不足 → 中性

        scores[coin] = ic_score * 0.4 + perf_score * 0.6

    # Step 4: 归一化为分配比例（使用动态币种列表）
    total_score = sum(s for s in scores.values() if s > 0)
    if total_score <= 0:
        allocations = {c: 0.0 for c in _get_allowed_coins()}
    else:
        target_total = 0.80  # 保留20%现金
        allocations = {}
        for c in _get_allowed_coins():
            if scores[c] > 0:
                allocations[c] = (scores[c] / total_score) * target_total
            else:
                allocations[c] = 0.0

    # 保存
    alloc_data = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'equity': equity,
        'allocations': {c: round(v, 4) for c, v in allocations.items()},
        'scores': {c: round(v, 4) for c, v in scores.items()},
        'ic_means': {c: round(v, 4) for c, v in ic_means.items()},
        'performance': {
            c: {k: round(v, 4) if isinstance(v, float) else v
                for k, v in p.items()}
            for c, p in perf.items()
        },
    }
    with open(COIN_PERFORMANCE_FILE, 'w') as f:
        json.dump(alloc_data, f, indent=2)

    return allocations, alloc_data


def get_per_coin_allocation(coin=None):
    """读取当前仓位分配"""
    try:
        with open(COIN_PERFORMANCE_FILE) as f:
            data = json.load(f)
        allocs = data.get('allocations', {})
        if coin is not None:
            return allocs.get(coin, 0.0)
        return allocs
    except:
        return {c: 0.0 for c in _get_allowed_coins()}


def format_allocation_report(alloc_data=None):
    """生成仓位分配报告"""
    if alloc_data is None:
        try:
            with open(COIN_PERFORMANCE_FILE) as f:
                alloc_data = json.load(f)
        except:
            return '━━━ 动态仓位分配 ━━━\n  数据不足（等权分配）'

    lines = ['━━━ 动态仓位分配 ━━━']
    lines.append('参考权益: $%.2f' % alloc_data.get('equity', 20))
    lines.append('')

    allocs = alloc_data.get('allocations', {})
    scores = alloc_data.get('scores', {})
    sorted_coins = sorted(allocs.items(), key=lambda x: -x[1])

    lines.append('  币种   分配   得分   IC     胜率  WLR')
    lines.append('  ' + '-' * 45)

    for coin, alloc in sorted_coins:
        if alloc <= 0:
            continue
        score = scores.get(coin, 0)
        perf = alloc_data.get('performance', {}).get(coin, {})
        ic_m = alloc_data.get('ic_means', {}).get(coin, 0)
        wr = perf.get('win_rate', 0)
        wrr = perf.get('wrr', 0)
        n = perf.get('n_trades', 0)
        bar = '█' * int(alloc * 100 / 4) + '░' * (10 - int(alloc * 100 / 4))
        n_str = f'({n}笔)' if n > 0 else ''
        lines.append(
            '  %-4s %.1f%% %s %.3f  %.0f%%  %.2f  %s' % (
                coin, alloc * 100, bar, ic_m,
                wr * 100 if wr else 0, wrr if wrr else 0, n_str
            )
        )

    for coin in ['DOGE', 'SOL']:
        lines.append('  %-4s %.1f%% (黑名单)' % (coin, 0.0))

    lines.append('')
    lines.append('  最大单币: 40% | 保留现金: 20%')
    return '\n'.join(lines)


def run_ic_collection():
    """每日IC历史收集（无报告输出）"""
    prices = get_okx_prices()
    signals, _ = generate_signals()
    save_ic_snapshot(signals)
    _pilot_logger.info('IC快照已保存 (%d个币种)' % len(signals))


def kronos_confirm():
    """应用待确认的提案（/kronos confirm命令调用）"""
    try:
        with open(PROPOSAL_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return "无待确认提案"

    applied = data.get('applied', [])
    if not applied:
        return "提案已应用或无待处理"

    lines = ['✅ 已应用以下因子调整:']
    for p in applied:
        lines.append('  %s: %.3f→%.3f (%+.0f%%)' % (
            p['factor'], p['mean_first'], p['mean_second'], p['change_pct']*100))

    # 清除提案
    os.remove(PROPOSAL_FILE)
    return '\n'.join(lines)


def show_status():
    """查看纸质交易胜率统计"""
    log = load_paper_log()
    if not log:
        _pilot_logger.info('📊 无纸质交易记录')
        return
    total = len(log)
    wins = [x for x in log if x.get('pnl', 0) > 0]
    losses = [x for x in log if x.get('pnl', 0) <= 0]
    win_rate = len(wins) / total * 100 if total else 0
    total_pnl = sum(x.get('pnl', 0) for x in log)
    avg_win = sum(x.get('pnl', 0) for x in wins) / len(wins) if wins else 0
    avg_loss = sum(x.get('pnl', 0) for x in losses) / len(losses) if losses else 0
    _pilot_logger.info(f'📊 纸质交易统计（共{total}笔）')
    _pilot_logger.info(f'  胜率: {len(wins)}/{total} = {win_rate:.1f}%')
    _pilot_logger.info(f'  总损益: ${total_pnl:.2f}')
    if wins:   _pilot_logger.info(f'  平均盈利: ${avg_win:.2f} ({len(wins)}笔)')
    if losses: _pilot_logger.info(f'  平均亏损: ${avg_loss:.2f} ({len(losses)}笔)')
    _pilot_logger.info('')
    for x in log[-10:]:
        pnl = x.get('pnl', 0)
        mark = '✅' if pnl > 0 else '❌'
        _pilot_logger.info(f"  {mark} {x.get('open_time', x.get('time',''))[:19]} {x.get('coin','')} {x.get('direction', x.get('side',''))} ${pnl:.2f}")


def show_log(n=20):
    """查看最近日志"""
    log_file = _log_dir / 'kronos_pilot.log'
    if not log_file.exists():
        _pilot_logger.info(f'📄 无日志文件: {log_file}')
        return
    lines = log_file.read_text(encoding='utf-8').strip().split('\n')
    _pilot_logger.info(f'📄 最近{min(n, len(lines))}行日志:')
    for line in lines[-n:]:
        _pilot_logger.info(line)


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'signal'
    
    if mode == '--full':
        run_full_report()
    elif mode == '--status':
        show_status()
    elif mode == '--log':
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        show_log(n)
    elif mode == '--close':
        coin = sys.argv[2]
        price = float(sys.argv[3]) if len(sys.argv) > 3 else None
        if price is None:
            prices = get_okx_prices()
            price = prices.get(coin)
        if price:
            close_position(coin, price)
        else:
            _pilot_logger.warning('无法获取 %s 当前价格' % coin)
    elif mode == '--check-sltp':
        prices = get_okx_prices()
        closed = check_stop_take_profit(prices)
        open_coins = {t['coin'] for t in load_paper_log() if t['status'] == 'OPEN'}
        if closed:
            for coin, price, reason, pnl in closed:
                _pilot_logger.info('平仓: %s %s @ $%.4f = %+.1f%%' % (coin, reason, price, pnl))
            msg = 'Kronos SL/TP触发:\n' + '\n'.join(['%s: %s @ $%.4f = %+.1f%%' % (c, r, p, pn) for c, p, r, pn in closed])
            push_feishu(msg)
        else:
            _pilot_logger.info('SL/TP检测: 无触发（静默）')
    elif mode == '--collect-ic':
        run_ic_collection()
        # 同时计算因子权重
        weights, err = compute_ic_weights()
        if err:
            _pilot_logger.warning('权重计算: %s' % err)
        else:
            _pilot_logger.info('权重计算: %s' % format_ic_weights_report())
    elif mode == '--analyze':
        # 计算权重 → 然后分析衰减
        weights, err = compute_ic_weights()
        if weights:
            _pilot_logger.info('━━━ IC因子权重 ━━━')
            _pilot_logger.info(format_ic_weights_report())
            _pilot_logger.info('')
        else:
            _pilot_logger.warning('权重: %s' % (err or '数据不足'))
        result, text = analyze_factor_weights()
        _pilot_logger.info(text)
        if result:
            _pilot_logger.info('\n提案已自动应用（p<0.05, |变化|>20%%）')
        else:
            _pilot_logger.info('\n无待处理提案')
    elif mode == '--analyze-weights':
        weights, err = compute_ic_weights()
        if weights:
            _pilot_logger.info('━━━ IC因子权重 ━━━')
            _pilot_logger.info(format_ic_weights_report())
        else:
            _pilot_logger.warning('权重: %s' % (err or '数据不足'))
    elif mode == '--confirm':
        result = kronos_confirm()
        _pilot_logger.info(result)
    else:
        _pilot_logger.info('[%s] Kronos信号' % datetime.now().strftime('%H:%M:%S'))
        signals, prices = generate_signals()
        for sig in signals:
            _pilot_logger.info('  %s' % sig['signal_text'])
