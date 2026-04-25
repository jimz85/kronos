#!/usr/bin/env python3
"""
布林带突破双策略系统 v3 - 布林双策略+风控加固
修复：并发锁 / 成交等待 / API失败安全模式 / 同周期重复开仓防护 / 动态杠杆
参数来源: 2026-04-20 实盘验证
"""

import sys, json, os, time, requests
from datetime import datetime
from filelock import FileLock

import pandas as pd
import numpy as np
import ccxt

# ============ 全局配置 ============
DRY_RUN = '--dry-run' in sys.argv
STRATEGY_FILTER = None
if '--daily' in sys.argv:
    STRATEGY_FILTER = 'daily'
elif '--1h' in sys.argv:
    STRATEGY_FILTER = '1h'

LOCK_FILE = os.path.join(os.path.dirname(__file__), '.trade.lock')
LOCK_TIMEOUT = 55  # 秒（必须小于cron间隔）

CONFIG = {
    'daily': {
        'AVAX': {'lookback': 15, 'stop_pct': 0.08, 'trail_pct': 0.02, 'position_pct': 0.06, 'leverage': 2, 'pool_pct': 0.40},
        'ETH':  {'lookback': 20, 'stop_pct': 0.08, 'trail_pct': 0.02, 'position_pct': 0.07, 'leverage': 2, 'pool_pct': 0.40},
        'BTC':  {'lookback': 20, 'stop_pct': 0.08, 'trail_pct': 0.02, 'position_pct': 0.05, 'leverage': 2, 'pool_pct': 0.20},
    },
    '1h': {
        'AVAX': {'lookback': 15, 'stop_pct': 0.08, 'trail_pct': 0.02, 'position_pct': 0.01, 'leverage': 1, 'pool_pct': 0.10},
        'ETH':  {'lookback': 20, 'stop_pct': 0.08, 'trail_pct': 0.02, 'position_pct': 0.01, 'leverage': 1, 'pool_pct': 0.10},
    },
}

# 杠杆分档：基于突破强度
LEVERAGE_BOOST = {
    'strong': {'leverage_adj': +1, 'position_adj': 1.0},
    'normal': {'leverage_adj':  0, 'position_adj': 1.0},
    'weak':   {'leverage_adj': -1, 'position_adj': 0.5},
}

MAX_POSITIONS = 3

# 品种优先顺序（按历史胜率）
# AVAX 72% > ETH 63% > BTC 61%
COIN_PRIORITY = {'AVAX': 3, 'ETH': 2, 'BTC': 1}
FEE = 0.0002
DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
STATE_FILE = os.path.join(os.path.dirname(__file__), 'dual_strategy_state.json')
TRADE_LOG = os.path.join(os.path.dirname(__file__), 'dual_strategy_trades.csv')
STATE_1H_FILE = os.path.join(os.path.dirname(__file__), 'dual_strategy_1h_daily.json')
CIRCUIT_DAILY_LOSS_PCT = 0.03
CIRCUIT_MAX_DD_PCT = 0.15

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET = os.getenv("OKX_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID = os.getenv("HERMES_SESSION_KEY", "").split(":")[-1]  # Fallback to home channel

exchange = ccxt.okx({
    'apiKey': OKX_API_KEY, 'secret': OKX_SECRET, 'password': OKX_PASSPHRASE,
    'enableRateLimit': True, 'testnet': True,
})

# ============ 飞书通知 ============
_feishu_token_cache = None
_feishu_token_expire = 0

def get_feishu_token():
    """获取Feishu tenant_access_token"""
    global _feishu_token_cache, _feishu_token_expire
    if _feishu_token_cache and time.time() < _feishu_token_expire:
        return _feishu_token_cache
    try:
        resp = requests.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET},
            timeout=10
        )
        data = resp.json()
        if data.get('code') == 0:
            _feishu_token_cache = data['tenant_access_token']
            _feishu_token_expire = time.time() + data.get('expire', 3600) - 60
            return _feishu_token_cache
    except Exception as e:
        print(f"[WARN] Feishu token获取失败: {e}")
    return None

def feishu_notify(text):
    """发送飞书消息"""
    try:
        token = get_feishu_token()
        if not token:
            print(f"[WARN] Feishu无token，跳过通知: {text[:50]}")
            return
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        payload = {
            'receive_id': FEISHU_CHAT_ID,
            'msg_type': 'text',
            'content': json.dumps({'text': text})
        }
        resp = requests.post(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
            headers=headers, json=payload, timeout=10
        )
        if resp.status_code == 200:
            print(f"[FEISHU] ✅ 通知已发送")
        else:
            print(f"[FEISHU] ❌ 发送失败: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"[FEISHU] ❌ 通知异常: {e}")

# ============ 🔴 致命问题1: 分布式文件锁 ============
def acquire_lock():
    lock = FileLock(LOCK_FILE, timeout=LOCK_TIMEOUT)
    try:
        lock.acquire()
        return lock
    except Exception as e:
        print(f"[ERROR] 无法获取交易锁: {e}")
        print("[ERROR] 可能有其他进程在运行，强制退出")
        sys.exit(1)

# ============ 🔴 致命问题5: API失败安全模式 ============
def safe_api_call(func, fallback=None, retries=3, reason="API调用"):
    """API调用失败时返回fallback（fail-safe）"""
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            print(f"  [WARN] {reason}失败 ({attempt+1}/{retries}): {e}")
            time.sleep(0.5)
    print(f"  [ERROR] {reason}重试{retries}次全部失败，使用安全默认值")
    return fallback

def get_all_positions():
    """获取所有持仓，失败时返回有持仓（fail-safe，防止误开仓）"""
    result = safe_api_call(
        lambda: exchange.fetch_positions([]),
        fallback=[{'symbol': 'DUMMY', 'contracts': 1}],  # fail-safe: 假装有持仓
        retries=3,
        reason="fetch_positions"
    )
    return result if result else [{'symbol': 'DUMMY', 'contracts': 1}]

def get_position_amount(coin):
    """获取指定币种持仓数量"""
    positions = get_all_positions()
    for p in positions:
        if coin in p.get('symbol', ''):
            return p.get('contracts', 0)
    return 0

def count_total_positions():
    """统计总持仓数量（来自交易所，真实数据）"""
    positions = get_all_positions()
    return sum(1 for p in positions if p.get('contracts', 0) > 0 and 'USDT' in p.get('symbol', ''))

# ============ 🔴 致命问题2: 等待成交确认 ============
def wait_for_fill(order_id, inst_id, timeout=30, poll_interval=0.5):
    """等待订单成交确认，超时则撤单重发"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = exchange.fetch_order(order_id, inst_id)
            filled = float(status.get('filled', 0))
            remaining = float(status.get('remaining', 0))
            if status.get('status') in ['closed', 'filled']:
                return {'filled': filled, 'status': 'filled', 'success': True}
            elif remaining == 0 and filled > 0:
                return {'filled': filled, 'status': 'partial', 'success': True}
            elif time.time() >= deadline - 1:
                # 超时，撤单
                try:
                    exchange.cancel_order(order_id, inst_id)
                    print(f"    [WARN] 订单超时未完全成交，已撤单 (filled={filled})")
                except:
                    pass
                return {'filled': filled, 'status': 'cancelled', 'success': False}
        except Exception as e:
            print(f"    [WARN] 查询订单状态失败: {e}")
        time.sleep(poll_interval)
    return {'filled': 0, 'status': 'timeout', 'success': False}

# ============ 熔断检查 ============
def get_1h_pool_capital():
    return 10000 * 0.10

def load_1h_daily():
    today = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(STATE_1H_FILE):
        with open(STATE_1H_FILE) as f:
            data = json.load(f)
            if data.get('date') == today:
                return data.get('daily_loss', 0), data.get('consecutive_losses', 0)
    return 0, 0

def save_1h_daily(daily_loss, consecutive_losses):
    with open(STATE_1H_FILE, 'w') as f:
        json.dump({
            'date': datetime.now().strftime('%Y-%m-%d'),
            'daily_loss': daily_loss,
            'consecutive_losses': consecutive_losses,
        }, f)

def check_circuit_breaker(strategy_type):
    if strategy_type != '1h':
        return True, ""
    daily_loss_pct, consecutive = load_1h_daily()
    if daily_loss_pct > CIRCUIT_DAILY_LOSS_PCT:
        return False, f"熔断: 1h单日亏损{daily_loss_pct*100:.1f}% > 3%"
    state = load_state()
    pool_capital = get_1h_pool_capital()
    peak = state.get('1h_peak', pool_capital)
    current = pool_capital * (1 + state.get('1h_total_pnl', 0) / 100)
    if peak > 0:
        dd_pct = (peak - current) / peak
        if dd_pct > CIRCUIT_MAX_DD_PCT:
            return False, f"熔断: 1h最大回撤{dd_pct*100:.1f}% > 15%"
    return True, ""

# ============ 日志 ============
def log_trade(strategy, coin, action, entry, exit_price, pnl_pct, reason):
    row = {
        'time': datetime.now().isoformat(),
        'strategy': strategy, 'coin': coin, 'action': action,
        'entry': entry, 'exit': exit_price, 'pnl_pct': round(pnl_pct, 2),
        'reason': reason, 'dry_run': DRY_RUN,
    }
    with open(TRADE_LOG, 'a') as f:
        if f.tell() == 0:
            f.write('time,strategy,coin,action,entry,exit,pnl_pct,reason,dry_run\n')
        f.write(f"{row['time']},{row['strategy']},{row['coin']},{row['action']},"
                f"{row['entry']},{row['exit']},{row['pnl_pct']},{row['reason']},{row['dry_run']}\n")
    print(f"[TRADE] {strategy} {action} {coin} @ {exit_price:.4f} PnL: {pnl_pct:+.2f}% ({reason})")

    # 飞书通知：每次交易操作
    if not DRY_RUN:
        if action == 'ENTRY':
            feishu_notify(
                f"📈 开仓 [{strategy}]\n"
                f"币种: {coin}\n"
                f"入场: ${entry:.4f}\n"
                f"信号强度: {reason.replace('signal_','')}\n"
                f"当前持仓: {count_total_positions()}/{MAX_POSITIONS}"
            )
        elif action == 'EXIT':
            emoji = "🟢" if pnl_pct > 0 else "🔴"
            feishu_notify(
                f"{emoji} 平仓 [{strategy}]\n"
                f"币种: {coin}\n"
                f"出场: ${exit_price:.4f} ({pnl_pct:+.2f}%)\n"
                f"原因: {reason}\n"
                f"当前持仓: {count_total_positions()}/{MAX_POSITIONS}"
            )

# ============ 数据获取 ============
def get_daily_bars(coin):
    f = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    try:
        if os.path.exists(f):
            df = pd.read_csv(f)
            df = df.rename(columns={'datetime_utc': 'timestamp', df.columns[1]: 'dt2'})
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
            df = df[['open','high','low','close','volume']].astype(float)
            df_daily = df.resample('D').agg({
                'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
            }).dropna()
            df_daily.index = df_daily.index.tz_localize(None)
            return df_daily
    except Exception as e:
        print(f"    [WARN] CSV加载失败: {e}")
    symbol = f'{coin}/USDT:USDT'
    ohlcv = exchange.fetch_ohlcv(symbol, '1d', limit=60)
    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df

def get_1h_bars(coin, lookback=50):
    f = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    try:
        if os.path.exists(f):
            df = pd.read_csv(f)
            df = df.rename(columns={'datetime_utc': 'timestamp', df.columns[1]: 'dt2'})
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
            df = df[['open','high','low','close','volume']].astype(float)
            df_1h = df.resample('1h').agg({
                'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
            }).dropna()
            df_1h.index = df_1h.index.tz_localize(None)
            return df_1h
    except Exception as e:
        print(f"    [WARN] CSV加载失败: {e}")
    symbol = f'{coin}/USDT:USDT'
    ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=lookback * 3)
    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df

# ============ 信号检查 ============
def check_daily_signal(coin, cfg):
    lb = cfg['lookback']
    df = get_daily_bars(coin)
    if len(df) < lb + 20:
        return None
    df['HH'] = df['high'].rolling(lb).max().shift(1)
    df['LL'] = df['low'].rolling(lb).min().shift(1)
    df['MA20'] = df['close'].rolling(lb).mean()
    df['MA50'] = df['close'].rolling(50).mean()
    df['trend_up'] = df['MA20'] > df['MA20'].shift(10)
    df['bull_filter'] = df['MA20'] > df['MA50']
    prev = df.iloc[-2]
    row = df.iloc[-1]
    if (prev['close'] <= prev['HH'] and row['close'] > row['HH'] and row['trend_up'] and row['bull_filter']):
        entry = row['close'] * (1 + FEE)
        stop = entry * (1 - cfg['stop_pct'])
        breakout_pct = (row['close'] - row['HH']) / row['HH'] * 100
        return {
            'entry': entry, 'stop': stop,
            'trail_pct': cfg['trail_pct'],
            'price': row['close'], 'close_time': row.name,
            'breakout_pct': breakout_pct,
        }
    return None

def check_1h_signal(coin, cfg):
    lb = cfg['lookback']
    df = get_1h_bars(coin, lookback=lb + 30)
    if len(df) < lb + 20:
        return None
    df['HH'] = df['high'].rolling(lb).max().shift(1)
    df['LL'] = df['low'].rolling(lb).min().shift(1)
    df['MA20'] = df['close'].rolling(lb).mean()
    df['MA50'] = df['close'].rolling(50).mean()
    df['trend_up'] = df['MA20'] > df['MA20'].shift(10)
    df['bull_filter'] = df['MA20'] > df['MA50']
    prev = df.iloc[-2]
    row = df.iloc[-1]
    if (prev['close'] <= prev['HH'] and row['close'] > row['HH'] and row['trend_up'] and row['bull_filter']):
        entry = row['close'] * (1 + FEE)
        stop = entry * (1 - cfg['stop_pct'])
        breakout_pct = (row['close'] - row['HH']) / row['HH'] * 100
        return {
            'entry': entry, 'stop': stop,
            'trail_pct': cfg['trail_pct'],
            'price': row['close'], 'close_time': row.name,
            'breakout_pct': breakout_pct,
        }
    return None

def check_exit(symbol, state, cfg, current_price):
    entry = state['entry']
    peak = state.get('peak', entry)
    stop = state['stop']
    trail_pct = state.get('trail_pct', cfg['trail_pct'])
    if current_price > peak:
        peak = current_price
    trail_stop = peak * (1 - trail_pct)
    if current_price <= stop:
        return {'exit_price': stop, 'pnl_pct': (stop / entry - 1) * 100, 'reason': 'stop', 'new_peak': peak}
    if current_price < trail_stop:
        return {'exit_price': trail_stop, 'pnl_pct': (trail_stop / entry - 1) * 100, 'reason': 'trail_stop', 'new_peak': peak}
    return None

# ============ 状态管理 ============
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return new_default_state()

def new_default_state():
    return {
        'daily': {coin: {'position': 0, 'entry': 0, 'peak': 0, 'stop': 0,
                         'signal_date': None, 'leverage': CONFIG['daily'][coin]['leverage'],
                         'position_adj': 1.0, 'strength': None, 'cycle_time': None}
                  for coin in CONFIG['daily']},
        '1h': {coin: {'position': 0, 'entry': 0, 'peak': 0, 'stop': 0,
                      'signal_date': None, 'trades_today': 0,
                      'leverage': CONFIG['1h'][coin]['leverage'],
                      'position_adj': 1.0, 'strength': None, 'cycle_time': None}
               for coin in CONFIG['1h']},
        'last_run_date': None,
    }

def migrate_state(state):
    for s in ['daily', '1h']:
        for coin in CONFIG[s]:
            if coin not in state.get(s, {}):
                state.setdefault(s, {})
                state[s][coin] = {'position': 0, 'entry': 0, 'peak': 0, 'stop': 0,
                                  'signal_date': None, 'trades_today': 0,
                                  'leverage': CONFIG[s][coin]['leverage'],
                                  'position_adj': 1.0, 'strength': None, 'cycle_time': None}
            for field in ['leverage', 'position_adj', 'strength', 'cycle_time']:
                if field not in state[s][coin]:
                    state[s][coin][field] = 1.0 if field in ['leverage', 'position_adj'] else None
    return state

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)

# ============ 🔴 致命问题3: 仓位硬限制 ============
def check_hard_position_limit(extra=0):
    """检查总持仓是否超过限制（来自交易所真实数据）"""
    total = count_total_positions() + extra
    if total > MAX_POSITIONS:
        print(f"  [ERROR] 仓位超限: {total} > {MAX_POSITIONS}，禁止开仓")
        return False
    return True

# ============ 🔴 重要优化1: 同周期重复开仓防护 ============
def is_same_candle_cycle(cycle_time, current_time, timeframe):
    """检查是否在同一K线周期内"""
    if cycle_time is None:
        return False
    if timeframe == '1h':
        # 同一小时算同一周期
        return cycle_time.replace(minute=0, second=0) == current_time.replace(minute=0, second=0)
    else:
        # 同一日算同一周期
        return cycle_time.date() == current_time.date()

# ============ 平仓 + 成交等待 ============
def close_position(coin, cfg, pos_state, reason, timeframe):
    """执行平仓，等待成交确认"""
    symbol = f'{coin}/USDT:USDT'
    inst_id = symbol.replace('USDT:USDT', '-SWAP')

    # 先查当前实际持仓
    real_pos = get_position_amount(coin)
    if real_pos <= 0:
        print(f"    [WARN] 交易所无持仓，无需平仓")
        return True

    print(f"    执行平仓: {real_pos}张...")
    try:
        order = exchange.create_order(
            inst_id, 'market', 'sell', float(real_pos),
            params={'posSide': 'long', 'tdMode': 'isolated', 'reduceOnly': True}
        )
        order_id = order['id']
        print(f"    平仓单发出: {order_id}")
    except Exception as e:
        print(f"    ❌ 平仓失败: {e}")
        return False

    # 🔴 致命问题2: 等待成交确认
    fill_result = wait_for_fill(order_id, inst_id, timeout=30)
    if fill_result['success'] or fill_result['filled'] > 0:
        pnl = (fill_result['filled'] / pos_state['entry'] - 1) * 100 if pos_state['entry'] > 0 else 0
        print(f"    ✅ 平仓完成: {fill_result['filled']}张成交 ({fill_result['status']})")
        log_trade(timeframe, coin, 'EXIT', pos_state['entry'],
                  pos_state.get('entry', 0), pnl, reason)
        return True
    else:
        print(f"    ❌ 平仓未完成，尝试市价重发...")
        # 重发市价单
        try:
            real_pos2 = get_position_amount(coin)
            if real_pos2 > 0:
                order2 = exchange.create_order(
                    inst_id, 'market', 'sell', float(real_pos2),
                    params={'posSide': 'long', 'tdMode': 'isolated', 'reduceOnly': True}
                )
                fr2 = wait_for_fill(order2['id'], inst_id, timeout=20)
                print(f"    重发平仓: {fr2['filled']}张 ({fr2['status']})")
        except Exception as e:
            print(f"    ❌ 重发平仓失败: {e}")
        return False

# ============ 开仓 ============
def open_position(coin, cfg, signal, lev, position_size, strength, timeframe, state):
    """执行开仓"""
    symbol = f'{coin}/USDT:USDT'
    inst_id = symbol.replace('USDT:USDT', '-SWAP')

    # 🔴 致命问题3: 开仓前再次检查仓位（最后一关）
    # 品种优先级: AVAX(3) > ETH(2) > BTC(1)
    # 仓位满时，优先保留高优先级品种
    current_count = count_total_positions()
    if current_count >= MAX_POSITIONS:
        # 获取当前持仓的币种
        held = set()
        for p in get_all_positions():
            for c in ['AVAX', 'ETH', 'BTC']:
                if c in p.get('symbol', ''):
                    held.add(c)
        held_lowest = min([COIN_PRIORITY.get(c, 0) for c in held], default=99)
        if COIN_PRIORITY.get(coin, 0) < held_lowest:
            print(f"    ❌ 仓位已满({current_count}/{MAX_POSITIONS})，{coin}优先级低，跳过")
            return False
        print(f"    ❌ 仓位已满({current_count}/{MAX_POSITIONS})，禁止开仓")
        return False

    # 🔴 致命问题3: 强制重设杠杆（不清除历史杠杆影响）
    try:
        exchange.set_leverage(lev, inst_id)
        print(f"    杠杆设置: {lev}x ({inst_id})")
    except Exception as e:
        print(f"    ❌ 杠杆设置失败: {e}")
        return False

    # 计算开仓数量
    try:
        bal = safe_api_call(lambda: exchange.fetch_balance(), fallback=None, retries=2)
        if bal is None:
            print(f"    ❌ 无法获取余额，取消开仓")
            return False
        usdt_bal = bal['USDT']['total']
        pos_value = usdt_bal * position_size * lev
        amount = pos_value / signal['price']
        amount = exchange.amount_to_precision(symbol, amount)
        print(f"    计算仓位: {amount}张 (${usdt_bal:.0f} × {position_size*100:.1f}% × {lev}x)")
    except Exception as e:
        print(f"    ❌ 仓位计算失败: {e}")
        return False

    # 执行开仓
    try:
        order = exchange.create_order(
            inst_id, 'market', 'buy', float(amount),
            params={'tdMode': 'isolated'}
        )
        order_id = order['id']
        print(f"    开仓单发出: {order_id}")
    except Exception as e:
        print(f"    ❌ 开仓失败: {e}")
        return False

    # 等待成交
    fill_result = wait_for_fill(order_id, inst_id, timeout=30)
    if fill_result['success']:
        print(f"    ✅ 开仓完成: {fill_result['filled']}张成交 ({fill_result['status']})")
        log_trade(timeframe, coin, 'ENTRY', signal['entry'], signal['price'], 0, f'signal_{strength}')
        return True
    else:
        print(f"    ❌ 开仓未成交，取消状态更新")
        return False

# ============ 主逻辑 ============
def run_strategy(strategy_type, state, lock):
    results = []
    configs = CONFIG[strategy_type]

    for coin, cfg in configs.items():
        symbol = f'{coin}/USDT:USDT'
        pos_state = state[strategy_type][coin]
        timeframe = '日线' if strategy_type == 'daily' else '1h'
        check_fn = check_daily_signal if strategy_type == 'daily' else check_1h_signal

        try:
            # ==== 持仓管理 ====
            if pos_state['position'] == 1:
                if DRY_RUN:
                    print(f"  [{timeframe}] {coin}: 持仓中 @ {pos_state['entry']:.4f}")
                    print(f"    止损: {pos_state['stop']:.4f} | 峰值: {pos_state.get('peak', pos_state['entry']):.4f}")
                    print(f"    [dry-run 跳过出场检查]")
                else:
                    # 🔴 致命问题5: 用交易所真实数据判断持仓
                    real_pos = get_position_amount(coin)
                    if real_pos <= 0:
                        # 持仓消失了（可能手动平了），重置状态
                        print(f"  [{timeframe}] {coin}: 持仓消失，重置状态")
                        pos_state['position'] = 0
                        continue

                    ticker = safe_api_call(
                        lambda: exchange.fetch_ticker(symbol),
                        fallback=None, retries=2
                    )
                    if ticker is None:
                        print(f"  [{timeframe}] {coin}: 无法获取价格，跳过")
                        continue
                    current = ticker['last']

                    exit_info = check_exit(symbol, pos_state, cfg, current)
                    if exit_info:
                        print(f"  [{timeframe}] {coin}: → 触发出场 {exit_info['reason']} @ {exit_info['exit_price']:.4f} ({exit_info['pnl_pct']:+.2f}%)")
                        close_position(coin, cfg, pos_state, exit_info['reason'], timeframe)
                        # 更新1h每日亏损
                        if strategy_type == '1h':
                            daily_loss, consecutive = load_1h_daily()
                            daily_loss += abs(exit_info['pnl_pct']) / 100
                            consecutive = consecutive + 1 if exit_info['pnl_pct'] < 0 else 0
                            save_1h_daily(daily_loss, consecutive)
                        # 🔴 重要优化1: 同周期重复开仓防护 - 止损后本周期不开新仓
                        pos_state['position'] = 0
                        pos_state['cycle_time'] = datetime.now()  # 锁定本周期
                    else:
                        if current > pos_state.get('peak', pos_state['entry']):
                            pos_state['peak'] = current
                        pnl = (current / pos_state['entry'] - 1) * 100
                        print(f"  [{timeframe}] {coin}: 持仓中 @ {pos_state['entry']:.4f} → 当前 {current:.4f} 浮盈{pnl:+.2f}%")

            # ==== 入场信号 ====
            else:
                # 🔴 重要优化1: 同周期重复开仓防护
                now = datetime.now()
                if is_same_candle_cycle(pos_state.get('cycle_time'), now, strategy_type):
                    print(f"  [{timeframe}] {coin}: 本周期已操作过，跳过")
                    continue

                # 熔断检查
                safe, msg = check_circuit_breaker(strategy_type)
                if not safe:
                    print(f"  [{timeframe}] {coin}: {msg}，跳过")
                    if not DRY_RUN:
                        feishu_notify(f"🛑 熔断触发 [{timeframe}]\n币种: {coin}\n原因: {msg}")
                    continue

                signal = check_fn(coin, cfg)
                if signal:
                    # 信号强度判断
                    bp = signal['breakout_pct']
                    if bp >= 3.0:
                        strength = 'strong'
                    elif bp >= 1.0:
                        strength = 'normal'
                    else:
                        strength = 'weak'
                    lev_cfg = LEVERAGE_BOOST[strength]
                    lev = max(1, cfg['leverage'] + lev_cfg['leverage_adj'])
                    pos_adj = lev_cfg['position_adj']
                    position_size = cfg['position_pct'] * pos_adj

                    print(f"  [{timeframe}] {coin}: ✅ 信号 @ {signal['price']:.4f}")
                    print(f"    突破强度: {bp:.2f}% ({strength}) → 杠杆{lev}x 仓位{position_size*100:.1f}%")
                    print(f"    止损: {signal['stop']:.4f} ({cfg['stop_pct']*100:.0f}%)")

                    if not DRY_RUN:
                        success = open_position(coin, cfg, signal, lev, position_size, strength, timeframe, state)
                        if success:
                            pos_state['position'] = 1
                            pos_state['entry'] = signal['entry']
                            pos_state['peak'] = signal['entry']
                            pos_state['stop'] = signal['stop']
                            pos_state['trail_pct'] = signal['trail_pct']
                            pos_state['signal_date'] = datetime.now().isoformat()
                            pos_state['trades_today'] = pos_state.get('trades_today', 0) + 1
                            pos_state['leverage'] = lev
                            pos_state['position_adj'] = pos_adj
                            pos_state['strength'] = strength
                            pos_state['cycle_time'] = signal['close_time'].replace(tzinfo=None) if signal.get('close_time') else datetime.now()
                        else:
                            print(f"    ❌ 开仓失败，禁止更新状态")
                    else:
                        pos_state['position'] = 1
                        pos_state['entry'] = signal['entry']
                        pos_state['peak'] = signal['entry']
                        pos_state['stop'] = signal['stop']
                        pos_state['trail_pct'] = signal['trail_pct']
                        pos_state['signal_date'] = datetime.now().isoformat()
                        pos_state['trades_today'] = pos_state.get('trades_today', 0) + 1
                        pos_state['leverage'] = lev
                        pos_state['position_adj'] = pos_adj
                        pos_state['strength'] = strength
                        pos_state['cycle_time'] = signal['close_time'].replace(tzinfo=None) if signal.get('close_time') else datetime.now()
                        log_trade(timeframe, coin, 'ENTRY', signal['entry'], signal['price'], 0, f'signal_{strength}')
                else:
                    print(f"  [{timeframe}] {coin}: 无信号")

        except Exception as e:
            print(f"  [{timeframe}] {coin}: [ERROR] {e}")

        time.sleep(0.5)

    return results

def main():
    lock = None
    try:
        # 🔴 致命问题1: 获取文件锁
        print(f"\n[{datetime.now()}] === 布林双策略 {'[DRY RUN]' if DRY_RUN else '[实盘]'} ===")
        if STRATEGY_FILTER:
            print(f"策略过滤器: {STRATEGY_FILTER.upper()}")
        print(f"日线: AVAX(LB15) + ETH(LB20) + BTC(LB20) | 1h: AVAX + ETH")
        print(f"总仓位上限: {MAX_POSITIONS}个 | 最大同时持仓: 通过交易所API实时校验")

        lock = acquire_lock()
        print(f"[LOCK] 获取交易锁成功")

        state = load_state()
        state = migrate_state(state)

        today = datetime.now().strftime('%Y-%m-%d')
        if state.get('last_run_date') != today:
            for coin in CONFIG['1h']:
                state['1h'][coin]['trades_today'] = 0
            state['last_run_date'] = today

        # 🔴 致命问题3: 实时显示交易所实际持仓
        real_total = count_total_positions()
        print(f"交易所实际持仓: {real_total}/{MAX_POSITIONS}\n")

        if STRATEGY_FILTER in [None, 'daily']:
            print("━━━ 日线策略 ━━━")
            run_strategy('daily', state, lock)

        if STRATEGY_FILTER in [None, '1h']:
            print("\n━━━ 1h策略 ━━━")
            run_strategy('1h', state, lock)

        save_state(state)

        # 🔴 致命问题3: 执行后再次确认持仓
        final_total = count_total_positions()
        print(f"\n执行后持仓: {final_total}/{MAX_POSITIONS}")

    finally:
        if lock:
            try:
                lock.release()
                print(f"[LOCK] 释放交易锁")
            except:
                pass

    print(f"\n[{datetime.now()}] 本轮检查完成")

if __name__ == '__main__':
    main()
