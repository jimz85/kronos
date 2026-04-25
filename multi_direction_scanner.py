#!/usr/bin/env python3
"""
多空趋势跟踪系统 v1
可做多可做空 | ATR动态止损止盈 | RSI+ADX确认 | 置信度打分 | 杠杆3-10x
"""
import sys, json, os, time, requests
from datetime import datetime
from filelock import FileLock
import pandas as pd
import numpy as np

# ============ 配置 ============
DRY_RUN = '--dry-run' in sys.argv

LOCK_FILE = os.path.join(os.path.dirname(__file__), '.trade.lock')
LOCK_TIMEOUT = 55

CONFIG = {
    'BTC': {'rsi_buy': 35, 'rsi_sell': 65, 'adx_min': 20, 'atr_stop': 1.5, 'atr_tp': 4.5, 'lev': 5, 'pct': 0.02},
    'ETH': {'rsi_buy': 35, 'rsi_sell': 65, 'adx_min': 20, 'atr_stop': 1.5, 'atr_tp': 4.5, 'lev': 5, 'pct': 0.02},
    'AVAX': {'rsi_buy': 35, 'rsi_sell': 65, 'adx_min': 20, 'atr_stop': 1.5, 'atr_tp': 4.5, 'lev': 3, 'pct': 0.01},
}

MAX_POSITIONS = 2
MAX_TRADES_PER_DAY = 2
COOLDOWN_HOURS = 4
DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
STATE_FILE = os.path.join(os.path.dirname(__file__), 'multi_direction_state.json')
TRADE_LOG = os.path.join(os.path.dirname(__file__), 'multi_direction_trades.csv')
FEE = 0.0002

# OKX
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET = os.getenv("OKX_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

# Feishu
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

import ccxt
exchange = None  # 延迟初始化

# ============ 飞书通知（精简版） ============
# 原则：有异常才通知，正常静默
_feishu_token = None
_feishu_expire = 0

# 网络状态追踪
_last_network_ok = True
_last_network_check = 0

def get_feishu_token():
    global _feishu_token, _feishu_expire
    if _feishu_token and time.time() < _feishu_expire:
        return _feishu_token
    try:
        resp = requests.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET},
            timeout=10
        )
        data = resp.json()
        if data.get('code') == 0:
            _feishu_token = data['tenant_access_token']
            _feishu_expire = time.time() + data.get('expire', 3600) - 60
            return _feishu_token
    except:
        pass
    return None

def check_network_health():
    """检查网络健康状态，返回(True/False, 错误信息)"""
    global _last_network_ok, _last_network_check
    
    # 1分钟内不重复检查
    if time.time() - _last_network_check < 60:
        return _last_network_ok, None if _last_network_ok else "上次检查网络异常"
    _last_network_check = time.time()
    
    # 检查OKX连接
    try:
        init_exchange()
        exchange.fetch_balance(timeout=5)
    except Exception as e:
        _last_network_ok = False
        return False, f"OKX连接失败: {e}"
    
    # 检查Feishu连接
    try:
        token = get_feishu_token()
        if not token:
            _last_network_ok = False
            return False, "Feishu token获取失败"
    except Exception as e:
        _last_network_ok = False
        return False, f"Feishu连接失败: {e}"
    
    _last_network_ok = True
    return True, None

def send_network_alert(reason):
    """发送网络故障警报（不受静默模式限制）"""
    try:
        token = get_feishu_token()
        if not token:
            print(f"[ALERT] 网络异常但无法发送飞书: {reason}")
            return
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        chat_id = os.getenv("HERMES_SESSION_KEY", "").split(":")[-1]
        if not chat_id:
            chat_id = "oc_bfd8a7cc1a606f190b53e3fd0167f5a0"
        msg = f"🚨 网络故障\nScanner无法连接\n{reason}\n时间: {datetime.now().strftime('%H:%M:%S')}"
        payload = {
            'receive_id': chat_id,
            'msg_type': 'text',
            'content': json.dumps({'text': msg})
        }
        resp = requests.post(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
            headers=headers, json=payload, timeout=10
        )
        print(f"[ALERT] 🚨 网络故障已通知: {reason}")
    except Exception as e:
        print(f"[ALERT] 发送网络故障通知失败: {e}")

def feishu_notify(text, critical=False):
    """
    发送飞书消息
    - critical=True: 强制发送（用于异常告警）
    - critical=False: 默认静默，只打印不发送
    """
    # 网络异常自动告警
    if "失败" in text or "错误" in text or "异常" in text or critical:
        try:
            token = get_feishu_token()
            if not token:
                print(f"[FEISHU] 无token: {text[:50]}")
                return
            headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
            chat_id = os.getenv("HERMES_SESSION_KEY", "").split(":")[-1]
            if not chat_id:
                chat_id = "oc_bfd8a7cc1a606f190b53e3fd0167f5a0"
            payload = {
                'receive_id': chat_id,
                'msg_type': 'text',
                'content': json.dumps({'text': text})
            }
            resp = requests.post(
                'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
                headers=headers, json=payload, timeout=10
            )
            if resp.status_code == 200:
                print(f"[FEISHU] ✅ 已发送")
        except Exception as e:
            print(f"[FEISHU] ❌ {e}")
    else:
        # 静默模式：只打印，不发飞书
        print(f"[静默] {text}")

# ============ 文件锁 ============
def acquire_lock():
    lock = FileLock(LOCK_FILE, timeout=LOCK_TIMEOUT)
    try:
        lock.acquire()
        return lock
    except:
        print("[ERROR] 无法获取锁，可能有其他进程运行中")
        sys.exit(1)

# ============ API安全调用 ============
def safe_api_call(func, fallback=None, retries=3):
    for i in range(retries):
        try:
            return func()
        except Exception as e:
            print(f"  [WARN] API失败({i+1}/{retries}): {e}")
            time.sleep(0.5)
    return fallback

def init_exchange():
    global exchange
    if exchange is None:
        exchange = ccxt.okx({
            'apiKey': OKX_API_KEY, 'secret': OKX_SECRET, 'password': OKX_PASSPHRASE,
            'enableRateLimit': True, 'testnet': True,
        })

def get_all_positions():
    init_exchange()
    return safe_api_call(lambda: exchange.fetch_positions([]), fallback=[]) or []

def get_position_amount(coin):
    init_exchange()
    for p in get_all_positions():
        if coin in p.get('symbol', ''):
            return p.get('contracts', 0)
    return 0

def count_positions():
    return sum(1 for p in get_all_positions() if p.get('contracts', 0) > 0 and 'USDT' in p.get('symbol', ''))

# ============ 等待成交 ============
def wait_for_fill(order_id, inst_id, timeout=30):
    init_exchange()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = exchange.fetch_order(order_id, inst_id)
            if status.get('status') in ['closed', 'filled']:
                return {'filled': float(status.get('filled', 0)), 'success': True}
            elif float(status.get('remaining', 0)) == 0 and float(status.get('filled', 0)) > 0:
                return {'filled': float(status.get('filled', 0)), 'success': True}
        except:
            pass
        time.sleep(0.5)
    # 超时撤单
    try:
        exchange.cancel_order(order_id, inst_id)
    except:
        pass
    return {'filled': 0, 'success': False}

# ============ 数据加载 ============
def load_5m_csv(coin):
    """加载本地5m CSV，处理不同币种的不同列名格式"""
    f = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    if not os.path.exists(f):
        return None
    
    df = pd.read_csv(f)
    
    # 处理时间列
    # 优先用datetime_utc（字符串格式），其次用timestamp（可能是毫秒或纳秒）
    if 'datetime_utc' in df.columns:
        # 清理可能的时区后缀
        dt_col = df['datetime_utc'].astype(str).str.replace(r'[+-]\d{2}:\d{2}$', '', regex=True)
        df['timestamp'] = pd.to_datetime(dt_col)
        df.drop(columns=['datetime_utc'], inplace=True)
    elif 'timestamp' in df.columns:
        ts = df['timestamp']
        # 检测是否为毫秒（>1e12）还是纳秒（>1e15）还是秒（<1e10）
        if ts.max() > 1e15:
            df['timestamp'] = pd.to_datetime(ts, unit='ns')
        elif ts.max() > 1e12:
            df['timestamp'] = pd.to_datetime(ts, unit='ms')
        elif ts.max() > 1e10:
            df['timestamp'] = pd.to_datetime(ts, unit='s')
        else:
            df['timestamp'] = pd.to_datetime(ts)
    
    # 删除重复列（如datetime_utc.1）
    dup_cols = [c for c in df.columns if '.' in c]
    if dup_cols:
        df.drop(columns=dup_cols, inplace=True)
    
    df.set_index('timestamp', inplace=True)
    
    # 统一列名（vol→volume, volCcy→volume）
    # 如果volume列已存在，删除vol列；否则重命名
    if 'volume' in df.columns:
        # volume列已存在，删除重复的vol/volCcy/volCcyQuote
        for c in ['vol', 'volCcy', 'volCcyQuote']:
            if c in df.columns:
                df.drop(columns=[c], inplace=True)
    else:
        # 没有volume列，重命名vol
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if cl == 'vol' or cl == 'volcy' or cl == 'volccyquote':
                col_map[c] = 'volume'
        if col_map:
            df.rename(columns=col_map, inplace=True)
    
    # 只保留需要的列
    needed = ['open', 'high', 'low', 'close', 'volume']
    existing = [c for c in needed if c in df.columns]
    if len(existing) < 5:
        print(f"[WARN] {coin}数据缺少列: {[c for c in needed if c not in df.columns]}")
        return None
    
    df = df[existing].astype(float)
    return df

# 网络状态追踪（用于检测断线）
_network_failure_count = 0
_last_network_alert = 0
_DATA_MAX_AGE_DAYS = 3  # 本地数据超过3天认为陈旧，强制走网络

def _check_data_freshness(df, coin):
    """检查数据新鲜度，超过3天返回True表示需要网络备选"""
    if df is None or len(df) < 100:
        return True
    try:
        latest = df.index[-1]
        now = pd.Timestamp.now(tz='UTC')
        if latest.tz:
            latest = latest.tz_convert('UTC')
        else:
            latest = latest.tz_localize('UTC')
        age_days = (now - latest).total_seconds() / 86400
        if age_days > _DATA_MAX_AGE_DAYS:
            return True
    except:
        pass
    return False

def get_1h_data(coin):
    """获取1h数据（本地优先，网络备选，数据陈旧时强制走网络）"""
    global _network_failure_count, _last_network_alert
    init_exchange()
    df_5m = load_5m_csv(coin)
    
    # 检查本地数据是否足够且新鲜
    if df_5m is not None and len(df_5m) > 100 and not _check_data_freshness(df_5m, coin):
        df_1h = df_5m.resample('1h').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna()
        df_1h.index = df_1h.index.tz_localize(None)
        return df_1h
    
    # 网络备选（本地数据不足或陈旧）
    try:
        ohlcv = exchange.fetch_ohlcv(f'{coin}/USDT:USDT', '1h', limit=200)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        _network_failure_count = 0  # 重置计数
        return df
    except Exception as e:
        _network_failure_count += 1
        # 连续3次网络失败才发警报（避免误报）
        if _network_failure_count >= 3 and time.time() - _last_network_alert > 300:
            send_network_alert(f"数据获取连续失败 {_network_failure_count}次: {coin}")
            _last_network_alert = time.time()
        return None

def get_15m_data(coin):
    """获取15min数据（本地优先，网络备选，数据陈旧时强制走网络）"""
    global _network_failure_count, _last_network_alert
    init_exchange()
    df_5m = load_5m_csv(coin)
    
    # 检查本地数据是否足够且新鲜
    if df_5m is not None and len(df_5m) > 100 and not _check_data_freshness(df_5m, coin):
        df_15m = df_5m.resample('15min').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna()
        df_15m.index = df_15m.index.tz_localize(None)
        return df_15m
    
    # 网络备选
    try:
        ohlcv = exchange.fetch_ohlcv(f'{coin}/USDT:USDT', '15m', limit=200)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        _network_failure_count = 0  # 重置计数
        return df
    except Exception as e:
        _network_failure_count += 1
        # 连续3次网络失败才发警报（避免误报）
        if _network_failure_count >= 3 and time.time() - _last_network_alert > 300:
            send_network_alert(f"数据获取连续失败 {_network_failure_count}次: {coin}")
            _last_network_alert = time.time()
        return None

# ============ 指标计算 ============
def calc_rsi(prices, period=14):
    delta = np.diff(prices, prepend=prices[0])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    ag = pd.Series(gains).rolling(period).mean()
    al = pd.Series(losses).rolling(period).mean()
    rs = ag / (al + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_atr(high, low, close, period=14):
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(period).mean()

def calc_adx(high, low, close, period=14):
    high_d = np.diff(high, prepend=high[0])
    low_d = -np.diff(low, prepend=low[0])
    plus_dm = np.where(high_d > low_d, high_d, 0.0)
    minus_dm = np.where(low_d > high_d, low_d, 0.0)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(period).mean()
    plus_di = pd.Series(plus_dm).rolling(period).mean() / atr * 100
    minus_di = pd.Series(minus_dm).rolling(period).mean() / atr * 100
    dx = abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
    adx = dx.rolling(period).mean()
    return adx, plus_di, minus_di

# ============ 置信度打分 ============
def calc_confidence(df_15m, df_1h, coin):
    """
    置信度打分 0-100
    方向确认: 40分
    动量确认: 30分
    结构确认: 30分
    """
    cfg = CONFIG.get(coin, CONFIG['BTC'])
    score = 0
    details = []
    
    # 1h趋势方向
    rsi_1h = df_1h['rsi'].iloc[-1] if 'rsi' in df_1h.columns else 50
    adx_1h_val = df_1h['adx'].iloc[-1] if 'adx' in df_1h.columns else 20
    trend_dir = 'neutral'
    if adx_1h_val > 20:
        try:
            plus_val = float(plus_1h[-1])
            minus_val = float(minus_1h[-1])
            trend_dir = 'short' if minus_val > plus_val else 'long'
        except:
            trend_dir = 'long'
    
    if trend_dir == 'long':
        score += 40
        details.append('1h多头')
    elif trend_dir == 'short':
        score += 40
        details.append('1h空头')
    else:
        score += 10
        details.append('1h震荡')
    
    # 动量
    rsi_15m = df_15m['rsi'].iloc[-1] if 'rsi' in df_15m.columns else 50
    adx_15m = df_15m['adx'].iloc[-1] if 'adx' in df_15m.columns else 20
    
    if rsi_15m < 35 or rsi_15m > 65:
        score += 15
        details.append('RSI极端')
    
    if adx_15m > 25:
        score += 15
        details.append('ADX强')
    elif adx_15m > cfg['adx_min']:
        score += 10
        details.append('ADX中')
    
    # 结构
    atr_ratio = df_15m['atr_ratio'].iloc[-1] if 'atr_ratio' in df_15m.columns else 1.0
    if atr_ratio > 1.2:
        score += 15
        details.append('波动爆发')
    
    vol_ratio = df_15m['vol_ratio'].iloc[-1] if 'vol_ratio' in df_15m.columns else 1.0
    if vol_ratio > 1.5:
        score += 15
        details.append('量能爆发')
    
    return min(score, 100), details

# ============ 信号检测 ============
def check_signal(coin, df_15m, df_1h, cfg):
    """检测入场信号"""
    if df_15m is None or df_1h is None or len(df_15m) < 50 or len(df_1h) < 50:
        return None
    
    # 计算指标
    c15 = df_15m['close'].values
    h15 = df_15m['high'].values
    l15 = df_15m['low'].values
    c1h = df_1h['close'].values
    h1h = df_1h['high'].values
    l1h = df_1h['low'].values
    
    df_15m['rsi'] = calc_rsi(c15, 14)
    df_15m['atr'] = calc_atr(h15, l15, c15, 14)
    adx_15m, plus_15, minus_15 = calc_adx(h15, l15, c15, 14)
    df_15m['adx'] = adx_15m
    df_15m['atr_ratio'] = df_15m['atr'] / df_15m['atr'].rolling(20).mean()
    df_15m['vol_ratio'] = df_15m['volume'] / df_15m['volume'].rolling(20).mean()
    
    df_1h['rsi'] = calc_rsi(c1h, 14)
    adx_1h, plus_1h, minus_1h = calc_adx(h1h, l1h, c1h, 14)
    df_1h['adx'] = adx_1h
    
    # 计算当前1h趋势（标量）
    adx_last = float(adx_1h.iloc[-1]) if hasattr(adx_1h, 'iloc') else float(adx_1h[-1])
    plus_last = float(plus_1h.iloc[-1]) if hasattr(plus_1h, 'iloc') else float(plus_1h[-1])
    minus_last = float(minus_1h.iloc[-1]) if hasattr(minus_1h, 'iloc') else float(minus_1h[-1])
    
    trend_dir = 'neutral'
    if adx_last > 20:
        trend_dir = 'short' if minus_last > plus_last else 'long'
    df_1h['trend_dir'] = trend_dir
    
    # 置信度
    confidence, details = calc_confidence(df_15m, df_1h, coin)
    if confidence < 60:
        return None  # 置信度不足
    
    rsi_15m = df_15m['rsi'].iloc[-1]
    adx_15m = df_15m['adx'].iloc[-1]
    atr_15m = df_15m['atr'].iloc[-1]
    atr_ratio = df_15m['atr_ratio'].iloc[-1]
    rsi_1h = df_1h['rsi'].iloc[-1]
    trend_dir = df_1h['trend_dir'].iloc[-1]
    
    price = c15[-1]
    entry_price = price * (1 + FEE)
    
    # 做多信号
    if rsi_15m < cfg['rsi_buy'] and adx_15m > cfg['adx_min']:
        stop = entry_price - cfg['atr_stop'] * atr_15m
        tp = entry_price + cfg['atr_tp'] * atr_15m
        rr = cfg['atr_tp'] / cfg['atr_stop']
        lev = min(cfg['lev'] + (2 if confidence > 85 else 0), 10)
        
        return {
            'side': 'long',
            'entry': entry_price,
            'stop': stop,
            'tp': tp,
            'atr': atr_15m,
            'rr': rr,
            'lev': lev,
            'confidence': confidence,
            'details': details,
            'price': price,
        }
    
    # 做空信号
    if rsi_15m > cfg['rsi_sell'] and adx_15m > cfg['adx_min']:
        stop = entry_price + cfg['atr_stop'] * atr_15m
        tp = entry_price - cfg['atr_tp'] * atr_15m
        rr = cfg['atr_tp'] / cfg['atr_stop']
        lev = min(cfg['lev'] + (2 if confidence > 85 else 0), 10)
        
        return {
            'side': 'short',
            'entry': entry_price,
            'stop': stop,
            'tp': tp,
            'atr': atr_15m,
            'rr': rr,
            'lev': lev,
            'confidence': confidence,
            'details': details,
            'price': price,
        }
    
    return None

# ============ 状态管理 ============
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return new_state()

def new_state():
    return {
        'positions': {},
        'trades_today': 0,
        'last_trade_date': None,
        'cooling_until': None,
    }

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)

# ============ 交易执行 ============
def log_trade(coin, side, entry, exit_price, pnl_pct, reason, lev, confidence):
    row = {
        'time': datetime.now().isoformat(),
        'coin': coin, 'side': side, 'entry': entry,
        'exit': exit_price, 'pnl_pct': round(pnl_pct, 2),
        'reason': reason, 'leverage': lev,
        'confidence': confidence, 'dry_run': DRY_RUN,
    }
    with open(TRADE_LOG, 'a') as f:
        if f.tell() == 0:
            f.write('time,coin,side,entry,exit,pnl_pct,reason,leverage,confidence,dry_run\n')
        f.write(f"{row['time']},{row['coin']},{row['side']},{row['entry']},{row['exit']},"
                f"{row['pnl_pct']},{row['reason']},{row['leverage']},{row['confidence']},{row['dry_run']}\n")
    emoji = "🟢" if pnl_pct > 0 else "🔴"
    print(f"[TRADE] {emoji} {side.upper()} {coin} @ {exit_price:.4f} ({pnl_pct:+.2f}%)")

def open_position(coin, sig, cfg):
    """开仓"""
    init_exchange()
    inst_id = f'{coin}-USDT-SWAP'
    lev = sig['lev']
    
    # 设置杠杆
    try:
        exchange.set_leverage(lev, inst_id)
    except Exception as e:
        print(f"  ❌ 杠杆设置失败: {e}")
        return False
    
    # 计算数量
    try:
        bal = safe_api_call(lambda: exchange.fetch_balance(), fallback=None)
        if bal is None:
            print(f"  ❌ 无法获取余额")
            return False
        usdt = bal['USDT']['total']
        pos_value = usdt * cfg['pct'] * lev
        amount = pos_value / sig['price']
        amount = exchange.amount_to_precision(f'{coin}/USDT:USDT', amount)
    except Exception as e:
        print(f"  ❌ 仓位计算失败: {e}")
        return False
    
    # 开仓
    side = 'buy' if sig['side'] == 'long' else 'sell'
    try:
        order = exchange.create_order(
            inst_id, 'market', side, float(amount),
            params={'tdMode': 'isolated'}
        )
        order_id = order['id']
    except Exception as e:
        print(f"  ❌ 开仓失败: {e}")
        return False
    
    # 等待成交
    fill = wait_for_fill(order_id, inst_id)
    if fill['success'] or fill['filled'] > 0:
        print(f"  ✅ 开仓完成: {fill['filled']}张")
        return True
    else:
        print(f"  ❌ 开仓未成交")
        return False

def close_position(coin, pos_state, reason):
    """平仓"""
    init_exchange()
    inst_id = f'{coin}-USDT-SWAP'
    real_pos = get_position_amount(coin)
    if real_pos <= 0:
        print(f"  [WARN] 无持仓")
        return True
    
    side = 'sell' if pos_state['side'] == 'long' else 'buy'
    try:
        order = exchange.create_order(
            inst_id, 'market', side, float(real_pos),
            params={'posSide': 'long' if pos_state['side'] == 'long' else 'short',
                   'tdMode': 'isolated', 'reduceOnly': True}
        )
        order_id = order['id']
    except Exception as e:
        print(f"  ❌ 平仓失败: {e}")
        return False
    
    fill = wait_for_fill(order_id, inst_id)
    pnl = pos_state.get('pnl', 0)
    log_trade(coin, pos_state['side'], pos_state['entry'],
              pos_state.get('entry', 0), pnl, reason,
              pos_state.get('lev', 3), pos_state.get('confidence', 0))
    return True

# ============ 主循环 ============
def run_scan(state):
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 每日重置
    if state.get('last_trade_date') != today:
        state['trades_today'] = 0
        state['last_trade_date'] = today
    
    # 冷却期检查
    cooling = state.get('cooling_until')
    if cooling:
        try:
            cooling_dt = datetime.fromisoformat(cooling)
            if datetime.now() < cooling_dt:
                print(f"[COOLING] 冷却中，剩余: {cooling_dt - datetime.now()}")
                return state
        except:
            pass
    
    # 检查持仓
    positions = state.get('positions', {})
    for coin, pos in list(positions.items()):
        if pos.get('position') == 1:
            # 持仓中：检查是否触发止损/止盈
            try:
                ticker = exchange.fetch_ticker(f'{coin}/USDT:USDT')
                current = ticker['last']
            except:
                continue
            
            stop = pos['stop']
            tp = pos['tp']
            entry = pos['entry']
            side = pos['side']
            
            exit_info = None
            if side == 'long':
                if current <= stop:
                    exit_info = {'reason': 'stop', 'exit': stop}
                elif current >= tp:
                    exit_info = {'reason': 'tp', 'exit': tp}
            else:
                if current >= stop:
                    exit_info = {'reason': 'stop', 'exit': stop}
                elif current <= tp:
                    exit_info = {'reason': 'tp', 'exit': tp}
            
            if exit_info:
                print(f"  [{coin}] → 触发出场 {exit_info['reason']} @ {exit_info['exit']:.4f}")
                close_position(coin, pos, exit_info['reason'])
                del positions[coin]
                positions[coin] = {'position': 0}
    
    # 扫描信号
    total_pos = count_positions()
    print(f"\n[{datetime.now()}] 持仓: {total_pos}/{MAX_POSITIONS} | 今日交易: {state['trades_today']}/{MAX_TRADES_PER_DAY}")
    
    for coin, cfg in CONFIG.items():
        try:
            df_15m = get_15m_data(coin)
            df_1h = get_1h_data(coin)
            
            if df_15m is None or df_1h is None:
                continue
            
            sig = check_signal(coin, df_15m, df_1h, cfg)
            
            if sig:
                print(f"\n  [{coin}] ✅ 信号: {sig['side'].upper()}")
                print(f"    价格: {sig['price']:.4f}")
                print(f"    置信度: {sig['confidence']} | 详情: {sig['details']}")
                print(f"    杠杆: {sig['lev']}x | RR: {sig['rr']:.1f}")
                print(f"    止损: {sig['stop']:.4f} | 止盈: {sig['tp']:.4f}")
                
                if not DRY_RUN:
                    # 只在高置信度时发通知（正常信号静默）
                    if sig['confidence'] >= 85:
                        feishu_notify(
                            f"📊 信号 [{coin}] {sig['side'].upper()} {sig['confidence']}%\n"
                            f"止损: ${sig['stop']:.4f} | 止盈: ${sig['tp']:.4f}",
                            critical=True
                        )
                    
                    # 开仓
                    success = open_position(coin, sig, cfg)
                    if success:
                        positions[coin] = {
                            'position': 1,
                            'side': sig['side'],
                            'entry': sig['entry'],
                            'stop': sig['stop'],
                            'tp': sig['tp'],
                            'lev': sig['lev'],
                            'confidence': sig['confidence'],
                            'atr': sig['atr'],
                            'signal_time': datetime.now().isoformat(),
                        }
                        state['trades_today'] += 1
                        state['cooling_until'] = (datetime.now().replace(
                            hour=datetime.now().hour + COOLDOWN_HOURS % 24)).isoformat()
                        
                        # 开仓成功静默（信号通知已覆盖）
            else:
                print(f"  [{coin}] 无信号")
                
        except Exception as e:
            import traceback
            print(f"  [{coin}] [ERROR] {e}")
            traceback.print_exc()
            continue
        time.sleep(0.3)
    
    state['positions'] = positions
    return state

def main():
    lock = None
    try:
        print(f"\n[{datetime.now()}] === 多空趋势扫描 {'[DRY RUN]' if DRY_RUN else '[实盘]'} ===")
        print(f"品种: {list(CONFIG.keys())} | 最大持仓: {MAX_POSITIONS} | 日交易上限: {MAX_TRADES_PER_DAY}")
        
        # 网络健康检查
        net_ok, net_err = check_network_health()
        if not net_ok:
            print(f"[ERROR] 网络异常: {net_err}")
            send_network_alert(net_err)
            # 不退出，继续扫描（本地数据可能可用）
        else:
            print(f"[OK] 网络正常")
        
        lock = acquire_lock()
        print(f"[LOCK] 获取锁成功")
        
        state = load_state()
        state = run_scan(state)
        save_state(state)
        
        print(f"\n[{datetime.now()}] 扫描完成")
        
    finally:
        if lock:
            try:
                lock.release()
                print(f"[LOCK] 释放锁")
            except:
                pass

if __name__ == '__main__':
    import pandas as pd, numpy as np, ccxt, hmac, base64, hashlib
    main()
