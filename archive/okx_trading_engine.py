#!/usr/bin/env python3
"""
OKX 模拟盘自动交易引擎
方向判断 + 杠杆开仓 + 止损止盈

用法:
  python3 okx_trading_engine.py          # 分析 + 推荐
  python3 okx_trading_engine.py --trade # 执行交易
"""
import requests, hashlib, hmac, base64, time, json, sys, math, os
import numpy as np
import pandas as pd
from datetime import datetime
from scipy import stats
import os
import pathlib
from dotenv import load_dotenv

# 加载 hermes 主目录的 .env
_HERMES_ENV = pathlib.Path.home() / '.hermes' / '.env'
load_dotenv(_HERMES_ENV, override=True)

# ==================== 配置 ====================
API_KEY = os.getenv('OKX_API_KEY', '')
SECRET_KEY = os.getenv('OKX_SECRET', '')
PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
SIMULATED = os.getenv('OKX_FLAG', '1')  # 0=实盘, 1=模拟

SYMBOL = 'BTC-USDT-SWAP'
TRADE_CCY = 'USDT'
MARGIN_MODE = 'isolated'  # 逐仓
LEVERAGE = 20              # 默认杠杆
POS_RISK_PCT = 0.05       # 每笔仓位占账户5%

# ==================== OKX API ====================
BASE = 'https://www.okx.com'

def sign(method, path, body=''):
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
    msg = ts + method + path + body
    mac = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256)
    return ts, base64.b64encode(mac.digest()).decode()

def api(method, path, body=''):
    ts, sig = sign(method, path, body)
    h = {
        'OK-ACCESS-KEY': API_KEY,
        'OK-ACCESS-SIGN': sig,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': SIMULATED
    }
    url = BASE + path
    r = requests.request(method, url, headers=h, timeout=15)
    return r.json()

# ==================== 数据获取 ====================
def get_candles(inst_id, bar='15m', limit=100):
    url = f'{BASE}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}'
    r = requests.get(url, timeout=15)
    data = r.json()
    if not data.get('data'):
        return []
    candles = []
    for c in reversed(data['data']):
        candles.append({
            'ts': int(c[0]),
            'open': float(c[1]),
            'high': float(c[2]),
            'low': float(c[3]),
            'close': float(c[4]),
            'vol': float(c[5])
        })
    return candles

def get_ticker(inst_id):
    url = f'{BASE}/api/v5/market/ticker?instId={inst_id}'
    r = requests.get(url, timeout=15)
    d = r.json()
    if d.get('data'):
        t = d['data'][0]
        return {
            'last': float(t['last']),
            'bid': float(t['bidPx']),
            'ask': float(t['askPx']),
            'high24h': float(t['high24h']),
            'low24h': float(t['low24h']),
            'vol24h': float(t['vol24h']),
            'open24h': float(t['open24h'])
        }
    return {}

def get_account():
    return api('GET', '/api/v5/account/balance')

def get_positions():
    return api('GET', '/api/v5/account/positions?instId=BTC-USDT-SWAP')

def get_instruments():
    r = requests.get(f'{BASE}/api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP', timeout=15)
    return r.json()

# ==================== 技术指标 ====================
def calc_rsi(closes, n=14):
    diff = np.diff(closes)
    gain = np.where(diff > 0, diff, 0)
    loss = np.where(diff < 0, -diff, 0)
    avg_gain = pd.Series(gain).ewm(alpha=1/n, min_periods=n).mean().values
    avg_loss = pd.Series(loss).ewm(alpha=1/n, min_periods=n).mean().values
    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return np.concatenate([[50]*n, rsi])

def calc_ma(closes, n):
    if len(closes) < n:
        return np.full(len(closes), np.nan)
    ma = np.convolve(closes, np.ones(n)/n, mode='valid')
    return np.concatenate([np.full(n-1, np.nan), ma])

def calc_bollinger(closes, n=20, k=2):
    ma = calc_ma(closes, n)
    std = np.array([closes[max(0,i-n+1):i+1].std() for i in range(len(closes))])
    upper = ma + k * std
    lower = ma - k * std
    return upper, ma, lower

def calc_atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return np.full(len(closes), np.nan)
    tr1 = highs[1:] - lows[1:]
    tr2 = np.abs(highs[1:] - closes[:-1])
    tr3 = np.abs(lows[1:] - closes[:-1])
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    tr_padded = np.concatenate([[0], tr])
    # EMA方式计算ATR
    atr = np.zeros(len(tr_padded))
    atr[n-1] = tr_padded[:n].mean()
    for i in range(n, len(tr_padded)):
        atr[i] = (atr[i-1] * (n-1) + tr_padded[i]) / n
    return atr

def analyze(candles):
    """对K线列表进行完整分析，返回信号"""
    closes = np.array([c['close'] for c in candles])
    highs = np.array([c['high'] for c in candles])
    lows = np.array([c['low'] for c in candles])

    # 指标
    rsi14 = calc_rsi(closes, 14)
    rsi7 = calc_rsi(closes, 7)
    ma20 = calc_ma(closes, 20)
    ma50 = calc_ma(closes, 50)
    ma200 = calc_ma(closes, 200) if len(closes) >= 200 else None
    bb_u, bb_m, bb_l = calc_bollinger(closes, 20, 2)
    atr14 = calc_atr(highs, lows, closes, 14)

    last = closes[-1]
    last_rsi = rsi14[-1]
    last_ma20 = ma20[-1]
    last_ma50 = ma50[-1]
    bb_width = (bb_u[-1] - bb_l[-1]) / bb_m[-1]

    # 趋势判断
    above_ma20 = last > last_ma20
    above_ma50 = last > last_ma50
    ma20_above_ma50 = last_ma20 > last_ma50

    # 动量
    ret_1h = (last - closes[-4]) / closes[-4] if len(closes) >= 4 else 0
    ret_4h = (last - closes[-16]) / closes[-16] if len(closes) >= 16 else 0

    # RSI 超买超卖
    rsi_overbought = last_rsi > 70
    rsi_oversold = last_rsi < 30
    rsi_extreme_oversold = last_rsi < 25

    # 布林带位置
    bb_upper_pct = (last - bb_l[-1]) / (bb_u[-1] - bb_l[-1] + 1e-10)

    # 综合评分 (-100 到 +100)
    score = 0
    factors = []

    # 1. 趋势 (40分)
    if above_ma20 and above_ma50:
        score += 20
        factors.append('↗ 多头均线排列')
    elif not above_ma20 and not above_ma50:
        score -= 20
        factors.append('↘ 空头均线排列')
    else:
        factors.append('→ 均线混乱')

    # 2. RSI (30分)
    if rsi_extreme_oversold:
        score += 25
        factors.append(f'⚡ RSI极度超卖({last_rsi:.1f}) → 反弹概率高')
    elif rsi_oversold:
        score += 15
        factors.append(f'📉 RSI超卖({last_rsi:.1f})')
    elif rsi_overbought:
        score -= 15
        factors.append(f'📈 RSI超买({last_rsi:.1f})')
    else:
        score += 5
        factors.append(f'RSI中性({last_rsi:.1f})')

    # 3. 动量 (20分)
    if ret_4h > 0.02:
        score += 10
        factors.append(f'↑ 4H动能充足(+{ret_4h*100:.2f}%)')
    elif ret_4h < -0.02:
        score -= 10
        factors.append(f'↓ 4H动能负({ret_4h*100:.2f}%)')

    # 4. 布林带 (10分)
    if bb_upper_pct < 0.2:
        score += 8
        factors.append(f'🔵 布林下轨附近({bb_upper_pct:.2f})')
    elif bb_upper_pct > 0.8:
        score -= 8
        factors.append(f'🔴 布林上轨附近({bb_upper_pct:.2f})')

    # 方向判定
    if score >= 25:
        direction = 'LONG'
        confidence = min(score / 50.0, 1.0)
    elif score <= -15:
        direction = 'SHORT'
        confidence = min(abs(score) / 50.0, 1.0)
    else:
        direction = 'NEUTRAL'
        confidence = 0.0

    # 止损止盈
    atr = atr14[-1]
    if direction == 'LONG':
        stop_loss = last * (1 - 1.5 * atr / last)
        take_profit = last * (1 + 2.0 * atr / last)
    elif direction == 'SHORT':
        stop_loss = last * (1 + 1.5 * atr / last)
        take_profit = last * (1 - 2.0 * atr / last)
    else:
        stop_loss = last * 0.99
        take_profit = last * 1.01

    leverage = max(10, min(30, int(LEVERAGE * confidence))) if confidence > 0.3 else 0

    return {
        'direction': direction,
        'confidence': confidence,
        'leverage': leverage,
        'score': score,
        'factors': factors,
        'price': last,
        'rsi': last_rsi,
        'ma20': last_ma20,
        'ma50': last_ma50,
        'atr': atr,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'ret_1h': ret_1h,
        'ret_4h': ret_4h,
        'bb_width': bb_width,
        'above_ma20': above_ma20,
        'above_ma50': above_ma50,
    }

# ==================== 交易执行 ====================
def place_order(inst_id, side, sz, px=None, sl=None, tp=None):
    """市价开仓"""
    body = {
        'instId': inst_id,
        'tdMode': MARGIN_MODE,
        'side': side,
        'ordType': 'market',
        'sz': str(sz),
        'posSide': 'long' if side == 'buy' else 'short',
    }
    if sl:
        body['slTriggerPx'] = str(sl)
        body['slOrdPx'] = str(sl * 0.998 if side == 'buy' else sl * 1.002)
    if tp:
        body['tpTriggerPx'] = str(tp)
        body['tpOrdPx'] = str(tp * 1.002 if side == 'buy' else tp * 0.998)

    body_json = json.dumps(body)
    resp = api('POST', '/api/v5/trade/order', body_json)
    return resp

def close_position(inst_id, pos_side):
    """市价平仓"""
    body = {
        'instId': inst_id,
        'tdMode': MARGIN_MODE,
        'side': 'sell' if pos_side == 'long' else 'buy',
        'ordType': 'market',
        'sz': '0',  # 全平
        'posSide': pos_side,
    }
    body_json = json.dumps(body)
    return api('POST', '/api/v5/trade/order', body_json)

def get_position(inst_id):
    """获取当前持仓"""
    resp = api('GET', f'/api/v5/account/positions?instId={inst_id}')
    positions = resp.get('data', [])
    if not positions:
        return None
    for p in positions:
        if float(p.get('pos', 0)) != 0:
            return p
    return None

def get_balance():
    """获取可用余额"""
    resp = get_account()
    details = resp.get('data', [{}])[0].get('details', [])
    for d in details:
        if d.get('ccy') == 'USDT':
            return float(d.get('availBal', 0))
    return 0.0

def calc_size(balance, price, leverage, risk_pct=POS_RISK_PCT):
    """计算开仓数量"""
    max_risk = balance * risk_pct
    size = int(max_risk * leverage / price)
    return max(1, size)

# ==================== 主程序 ====================
def main():
    trade_mode = '--trade' in sys.argv

    print(f'\n{"="*60}')
    print(f'  OKX 模拟盘交易引擎  {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    print(f'{"="*60}\n')

    # 获取数据
    print('📡 获取市场数据...')
    candles_15m = get_candles(SYMBOL, '15m', 200)
    candles_1h = get_candles(SYMBOL, '1H', 100)
    ticker = get_ticker(SYMBOL)

    if not candles_15m:
        print('❌ 无法获取K线数据')
        return

    print(f'  BTC价格: ${ticker["last"]:,.1f}')
    print(f'  24h高: ${ticker["high24h"]:,.1f}  低: ${ticker["low24h"]:,.1f}')
    print()

    # 分析
    sig_15m = analyze(candles_15m)
    sig_1h = analyze(candles_1h)

    print(f'【15分钟分析 - 决策用】')
    print(f'  >>> 信号: {sig_15m["direction"]}  信心: {sig_15m["confidence"]*100:.0f}%  评分: {sig_15m["score"]}')
    for f in sig_15m['factors']:
        print(f'    {f}')
    print(f'  建议杠杆: {sig_15m["leverage"]}x')

    if sig_15m["stop_loss"]:
        print(f'  止损: ${sig_15m["stop_loss"]:,.1f}  止盈: ${sig_15m["take_profit"]:,.1f}')

    print()
    print(f'【1小时分析】')
    print(f'  信号: {sig_1h["direction"]}  信心: {sig_1h["confidence"]*100:.0f}%  评分: {sig_1h["score"]}')
    for f in sig_1h['factors']:
        print(f'    {f}')

    # 持仓检查
    print()
    pos = get_position(SYMBOL)
    if pos:
        print(f'【当前持仓】')
        print(f'  方向: {pos.get("posSide","")}  数量: {pos.get("pos","")}  均价: {pos.get("avgPx","")}')
        sl = float(pos.get('slTriggerPx', 0))
        tp = float(pos.get('tpTriggerPx', 0))
        if sl: print(f'  止损: ${sl:,.1f}')
        if tp: print(f'  止盈: ${tp:,.1f}')
    else:
        print('【当前持仓】无')

    balance = get_balance()
    print(f'  可用余额: ${balance:,.2f}')

    # 交易执行
    if not trade_mode:
        print()
        print('▶ 使用 --trade 参数执行交易')
        print()
        return

    print()
    print(f'🔨 执行交易信号...')

    # 如果有持仓，先平掉
    if pos and float(pos.get('pos', 0)) != 0:
        print(f'  平掉现有{pos.get("posSide")}仓...')
        r = close_position(SYMBOL, pos.get('posSide'))
        if r.get('code') == '0':
            print(f'  ✅ 平仓成功')
        else:
            print(f'  ❌ 平仓失败: {r}')
        time.sleep(2)

    direction = sig_15m['direction']
    if direction == 'NEUTRAL':
        print('  ⏸ 信号中性，跳过交易')
        return

    side = 'buy' if direction == 'LONG' else 'sell'
    leverage = sig_15m['leverage']
    price = sig_15m['price']
    sl = sig_15m['stop_loss']
    tp = sig_15m['take_profit']

    if leverage < 10:
        print(f'  ⏸ 信心不足({sig_15m["confidence"]*100:.0f}%)，杠杆{leverage}x < 10，跳过')
        return

    # 设置杠杆
    lever_body = json.dumps({'instId': SYMBOL, 'lever': str(leverage), 'mgnMode': MARGIN_MODE})
    r = api('POST', '/api/v5/account/set-leverage', lever_body)
    if r.get('code') != '0':
        print(f'  ⚠️ 设置杠杆失败: {r.get("msg")}')

    # 计算数量
    size = calc_size(balance, price, leverage)
    print(f'  下单: {side.upper()} {size}张 @ ${price:,.1f}  杠杆{leverage}x')
    print(f'  止损: ${sl:,.1f}  止盈: ${tp:,.1f}')

    # 下单
    r = place_order(SYMBOL, side, size, sl=sl, tp=tp)
    if r.get('code') == '0':
        print(f'  ✅ 开仓成功！订单ID: {r["data"][0]["ordId"]}')
    else:
        print(f'  ❌ 开仓失败: {r.get("msg")} {r.get("data")}')

    # 打印账户信息
    time.sleep(2)
    pos_after = get_position(SYMBOL)
    if pos_after:
        print(f'\n  持仓确认: {pos_after.get("posSide")} {pos_after.get("pos")}张 @ ${pos_after.get("avgPx")}')

if __name__ == '__main__':
    main()
