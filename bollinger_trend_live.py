#!/usr/bin/env python3
"""
布林趋势策略 - 实盘执行版
AVAX: LB=15, SP=8%, TP=2%
ETH:  LB=20, SP=8%, TP=2%

用法: python3 bollinger_trend_live.py [--dry-run]
"""

import sys
import json
import os
import time
import argparse
from datetime import datetime

import pandas as pd
import numpy as np
import ccxt

# ============ 配置 ============
DRY_RUN = '--dry-run' in sys.argv

CONFIG = {
    'AVAX/USDT:USDT': {
        'lookback': 15,
        'stop_pct': 0.08,
        'trail_pct': 0.02,
        'position_pct': 0.05,   # 5%总资金
        'leverage': 2,
    },
    'ETH/USDT:USDT': {
        'lookback': 20,
        'stop_pct': 0.08,
        'trail_pct': 0.02,
        'position_pct': 0.08,   # 8%总资金
        'leverage': 2,
    },
}

STATE_FILE = os.path.join(os.path.dirname(__file__), 'bollinger_state.json')
TRADE_LOG = os.path.join(os.path.dirname(__file__), 'bollinger_trades.csv')
POS_FILE = os.path.join(os.path.dirname(__file__), 'bollinger_positions.json')

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

FEE = 0.0002

# ============ 交易所 ============
if OKX_API_KEY:
    exchange = ccxt.okx({
        'apiKey': OKX_API_KEY,
        'secret': OKX_SECRET,
        'password': OKX_PASSPHRASE,
        'enableRateLimit': True,
        'testnet': True,
    })
else:
    exchange = ccxt.okx({'enableRateLimit': True, 'testnet': True})
    print("[WARN] 无API密钥，仅模拟运行")

# ============ 工具函数 ============
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {s: {'position': 0, 'entry': 0, 'peak': 0, 'stop': 0, 'signal_date': None}
            for s in CONFIG}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)

def log_trade(symbol, action, entry, exit_price, pnl_pct, reason):
    row = {
        'time': datetime.now().isoformat(),
        'symbol': symbol,
        'action': action,
        'entry': entry,
        'exit': exit_price,
        'pnl_pct': round(pnl_pct, 2),
        'reason': reason,
        'dry_run': DRY_RUN,
    }
    with open(TRADE_LOG, 'a') as f:
        if f.tell() == 0:
            f.write('time,symbol,action,entry,exit,pnl_pct,reason,dry_run\n')
        f.write(f"{row['time']},{row['symbol']},{row['action']},{row['entry']},{row['exit']},{row['pnl_pct']},{row['reason']},{row['dry_run']}\n")
    print(f"[TRADE] {action} {symbol} @ {exit_price:.4f} PnL: {pnl_pct:+.2f}% ({reason})")

def get_daily_bars(symbol, days=60):
    """获取日线数据（优先本地CSV，失败则API）"""
    coin = symbol.split('/')[0]
    data_dir = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
    f = f'{data_dir}/{coin}_USDT_5m_from_20180101.csv'
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
    # 回退到API
    ohlcv = exchange.fetch_ohlcv(symbol, '1d', limit=days)
    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df

def check_signal(symbol, cfg):
    """检查是否触发入场信号"""
    try:
        df = get_daily_bars(symbol, days=cfg['lookback'] + 20)
        lb = cfg['lookback']
        
        df['HH'] = df['high'].rolling(lb).max().shift(1)
        df['LL'] = df['low'].rolling(lb).min().shift(1)
        df['MA20'] = df['close'].rolling(lb).mean()
        df['MA50'] = df['close'].rolling(50).mean()
        df['trend_up'] = df['MA20'] > df['MA20'].shift(10)
        df['bull_filter'] = df['MA20'] > df['MA50']
        
        row = df.iloc[-1]
        prev = df.iloc[-2]
        
        # 入场信号：今日收盘突破20日高点 + 趋势向上
        if (prev['close'] <= prev['HH'] and 
            row['close'] > row['HH'] and 
            row['trend_up'] and row['bull_filter']):
            entry = row['close'] * (1 + FEE)
            stop = entry * (1 - cfg['stop_pct'])
            return {'action': 'LONG', 'entry': entry, 'stop': stop, 
                    'trail_pct': cfg['trail_pct'], 'price': row['close']}
        
        return None
    except Exception as e:
        print(f"[ERROR] check_signal {symbol}: {e}")
        return None

def check_exit(symbol, state, current_price):
    """检查是否触发出场"""
    cfg = CONFIG[symbol]
    entry = state['entry']
    peak = state['peak']
    stop = state['stop']
    trail_pct = state['trail_pct']
    
    low_today = current_price * 0.998  # 保守估计日内低点
    trail_stop = peak * (1 - trail_pct)
    
    # 止损
    if low_today <= stop:
        return {'action': 'EXIT', 'exit_price': stop, 
                'pnl_pct': (stop / entry - 1) * 100, 'reason': 'stop'}
    
    # 追踪止损
    if current_price < trail_stop:
        return {'action': 'EXIT', 'exit_price': trail_stop,
                'pnl_pct': (trail_stop / entry - 1) * 100, 'reason': 'trail_stop'}
    
    # 低点止损（跌破20日低点）
    # 需要重新计算LL
    try:
        df = get_daily_bars(symbol, days=cfg['lookback'] + 2)
        lb = cfg['lookback']
        df['LL'] = df['low'].rolling(lb).min().shift(1)
        ll_today = df['LL'].iloc[-1]
        if current_price < ll_today:
            return {'action': 'EXIT', 'exit_price': current_price,
                    'pnl_pct': (current_price / entry - 1) * 100, 'reason': 'low_exit'}
    except:
        pass
    
    return None

# ============ 主循环 ============
def run():
    print(f"[{datetime.now()}] 布林趋势策略启动 {'[DRY RUN]' if DRY_RUN else ''}")
    state = load_state()
    
    for symbol, cfg in CONFIG.items():
        coin = symbol.split('/')[0]
        print(f"\n=== {coin} ===")
        
        try:
            # 获取当前持仓（dry-run时跳过API调用）
            has_pos = False
            if not DRY_RUN and OKX_API_KEY:
                try:
                    pos = exchange.fetch_positions([symbol])
                    has_pos = any(p['contracts'] > 0 for p in pos)
                except Exception as e:
                    print(f"  [WARN] 获取持仓失败: {e}")
            else:
                has_pos = state[symbol]['position'] == 1
            
            if has_pos or state[symbol]['position'] == 1:
                # 管理现有持仓
                if DRY_RUN:
                    print(f"  持仓中（dry-run展示）: 入场 {state[symbol]['entry']:.4f}")
                    print(f"  止损: {state[symbol]['stop']:.4f} 峰值: {state[symbol]['peak']:.4f}")
                    print(f"  [dry-run 跳过出场检查]")
                else:
                    ticker = exchange.fetch_ticker(symbol)
                    current = ticker['last']
                    print(f"  持仓中: 入场 {state[symbol]['entry']:.4f} 当前 {current:.4f}")
                    print(f"  止损: {state[symbol]['stop']:.4f} 峰值: {state[symbol]['peak']:.4f}")
                    
                    exit_info = check_exit(symbol, state[symbol], current)
                    if exit_info:
                        print(f"  → 触发出场: {exit_info['reason']}")
                        log_trade(symbol, 'EXIT', state[symbol]['entry'], 
                                 exit_info['exit_price'], exit_info['pnl_pct'], exit_info['reason'])
                        if not DRY_RUN and OKX_API_KEY:
                            exchange.cancel_all_orders(symbol)
                            exchange.create_market_order(symbol, 'sell', 
                                exchange.amount_to_precision(symbol, 1))
                        state[symbol] = {'position': 0, 'entry': 0, 'peak': 0, 
                                        'stop': 0, 'signal_date': None}
                        save_state(state)
                    else:
                        if current > state[symbol]['peak']:
                            state[symbol]['peak'] = current
                            save_state(state)
                        pnl = (current / state[symbol]['entry'] - 1) * 100
                        print(f"  当前浮盈: {pnl:+.2f}%")
            else:
                # 检查入场信号
                signal = check_signal(symbol, cfg)
                if signal:
                    print(f"  ✅ 入场信号: {signal['action']} @ {signal['price']:.4f}")
                    print(f"     止损: {signal['stop']:.4f} ({cfg['stop_pct']*100:.0f}%)")
                    
                    if not DRY_RUN and OKX_API_KEY:
                        # 设置杠杆
                        exchange.set_leverage(cfg['leverage'], symbol)
                        # 市价开多
                        exchange.create_market_order(symbol, 'buy', 
                            exchange.amount_to_precision(symbol, 1))
                    
                    state[symbol] = {
                        'position': 1,
                        'entry': signal['entry'],
                        'peak': signal['entry'],
                        'stop': signal['stop'],
                        'trail_pct': signal['trail_pct'],
                        'signal_date': datetime.now().isoformat(),
                    }
                    save_state(state)
                    log_trade(symbol, 'ENTRY', signal['entry'], signal['price'], 0, 'signal')
                else:
                    print(f"  无信号，继续观察")
        
        except Exception as e:
            print(f"  [ERROR] {e}")
        
        time.sleep(1)
    
    print(f"\n[{datetime.now()}] 本轮检查完成")

if __name__ == "__main__":
    run()
