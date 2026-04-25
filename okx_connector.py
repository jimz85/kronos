#!/usr/bin/env python3
"""
OKX交易所连接器 v1.0
===================================
功能：
  - 获取实时价格（公开接口，无需认证）
  - 追踪组合盈亏
  - 信号记录（纸交易模式）
  - 未来可扩展：真实下单

使用方法：
  python okx_connector.py --signal BTC long 77135.4 0.30
"""

import requests
import json
import os
import sys
from datetime import datetime

# OKX公开API（无需认证）
BASE_URL = 'https://www.okx.com/api/v5'

CACHE_DIR = os.path.expanduser('~/.hermes/cron/output/')
os.makedirs(CACHE_DIR, exist_ok=True)
PORTFOLIO_FILE = os.path.join(CACHE_DIR, 'okx_portfolio.json')
TRADE_SIGNALS = os.path.join(CACHE_DIR, 'trade_signals.json')

def get_price(inst_id):
    """获取单个币种实时价格（公开接口）"""
    url = f'{BASE_URL}/market/ticker?instId={inst_id}'
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get('code') == '0':
            return float(data['data'][0]['last'])
    except:
        pass
    return None

def get_prices(coins):
    """批量获取多个币种价格"""
    prices = {}
    inst_map = {
        'BTC': 'BTC-USDT',
        'ETH': 'ETH-USDT',
        'ADA': 'ADA-USDT',
        'DOGE': 'DOGE-USDT',
        'AVAX': 'AVAX-USDT',
        'DOT': 'DOT-USDT',
        'SOL': 'SOL-USDT',
    }
    for coin in coins:
        inst_id = inst_map.get(coin)
        if inst_id:
            price = get_price(inst_id)
            if price:
                prices[coin] = price
    return prices

def log_signal(coin, direction, price, size, confidence, best_factor, ic_values):
    """记录交易信号（纸交易）"""
    signals = []
    if os.path.exists(TRADE_SIGNALS):
        with open(TRADE_SIGNALS) as f:
            signals = json.load(f)
    
    signal = {
        'time': datetime.now().isoformat(),
        'coin': coin,
        'direction': direction,
        'entry_price': price,
        'size_usd': size * price,
        'confidence': confidence,
        'best_factor': best_factor,
        'ic': ic_values,
        'status': 'OPEN',
        'result': None,
        'exit_time': None,
    }
    signals.append(signal)
    signals = signals[-200:]  # 保留最近200条
    
    with open(TRADE_SIGNALS, 'w') as f:
        json.dump(signals, f, indent=2)
    
    return signal

def close_signal(coin, exit_price):
    """平仓信号"""
    signals = []
    if os.path.exists(TRADE_SIGNALS):
        with open(TRADE_SIGNALS) as f:
            signals = json.load(f)
    
    for sig in reversed(signals):
        if sig['coin'] == coin and sig['status'] == 'OPEN':
            sig['status'] = 'CLOSED'
            sig['exit_price'] = exit_price
            sig['exit_time'] = datetime.now().isoformat()
            entry = sig['entry_price']
            direction = sig['direction']
            ret = (exit_price - entry) / entry if direction == 'long' else (entry - exit_price) / entry
            sig['result'] = ret * 100
            break
    
    with open(TRADE_SIGNALS, 'w') as f:
        json.dump(signals, f, indent=2)
    
    return signals

def portfolio_summary():
    """组合汇总"""
    signals = []
    if os.path.exists(TRADE_SIGNALS):
        with open(TRADE_SIGNALS) as f:
            signals = json.load(f)
    
    if not signals:
        return '暂无交易信号记录'
    
    open_signals = [s for s in signals if s['status'] == 'OPEN']
    closed = [s for s in signals if s['status'] == 'CLOSED']
    
    total_pnl = sum(s.get('result', 0) or 0 for s in closed)
    wins = [s for s in closed if (s.get('result') or 0) > 0]
    losses = [s for s in closed if (s.get('result') or 0) < 0]
    
    msg = f"""当前持仓: {len(open_signals)}笔
累计平仓: {len(closed)}笔
胜率: {len(wins)/len(closed)*100:.0f}%({len(wins)}W/{len(losses)}L)
累计收益率: {total_pnl:+.1f}%
"""
    return msg

def run_check(prices=None):
    """每小时运行：检查持仓 + 记录新信号"""
    if prices is None:
        prices = get_prices(['BTC', 'ETH', 'ADA', 'DOGE', 'AVAX', 'DOT', 'SOL'])
    
    print(f'[{datetime.now().strftime("%H:%M:%S")}] OKX检查: {prices}')
    print(portfolio_summary())
    return prices

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='检查持仓')
    parser.add_argument('--signal', nargs=6, metavar=('COIN', 'DIR', 'PRICE', 'SIZE', 'CONF', 'FACTOR'),
                        help='记录信号: coin direction price size confidence factor')
    parser.add_argument('--close', nargs=2, metavar=('COIN', 'EXIT_PRICE'), help='平仓: coin exit_price')
    parser.add_argument('--prices', action='store_true', help='获取实时价格')
    args = parser.parse_args()
    
    if args.prices:
        prices = run_check()
        for coin, price in prices.items():
            print(f'  {coin}: ${price:,.4f}')
    
    elif args.signal:
        coin, direction, price, size, conf, factor = args.signal
        sig = log_signal(coin, direction, float(price), float(size), float(conf), factor, {})
        print(f'已记录信号: {sig}')
    
    elif args.close:
        coin, exit_price = args.close
        sigs = close_signal(coin, float(exit_price))
        print(f'已平仓 {coin} @ ${exit_price}')
    
    elif args.check:
        run_check()
    
    else:
        print('OKX连接器用法:')
        print('  --prices        获取实时价格')
        print('  --check         检查持仓状态')
        print('  --signal BTC long 77000 0.3 80 rsi_inv  记录做多信号')
        print('  --close BTC 80000                        平仓BTC')
