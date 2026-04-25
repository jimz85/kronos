#!/usr/bin/env python3
"""
AVAX持仓主动管理器
每30分钟自动检查并调整SL
触发条件：浮盈≥3%/5%/8%/10%时主动收紧止损
推送飞书：有操作才推送，静默时无输出
"""
import os, sys, json
from dotenv import load_dotenv
load_dotenv('/Users/jimingzhang/.hermes/.env')

import yfinance as yf
import pandas as pd
import numpy as np
from okx.api import AlgoTrade, Market

# ── 参数 ──────────────────────────────────────────
POSITION = {'coin': 'AVAX', 'entry': 9.37, 'sl': 9.20, 'tp': 10.907, 'contracts': 168}
TRAILING_RULES = [
    (3,  9.20,  '浮盈≥3%: SL保本'),
    (5,  9.30,  '浮盈≥5%: SL锁定2%利润'),
    (8,  9.50,  '浮盈≥8%: SL锁定6%利润'),
    (10, 9.70,  '浮盈≥10%: SL锁定9%利润'),
]
# ──────────────────────────────────────────────────

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2:
        return 50.0
    trs, pdms, mdms = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
        pdm = max(highs[i]-highs[i-1], 0)
        mdm = max(lows[i-1]-lows[i], 0)
        pdms.append(pdm)
        mdms.append(mdm)
    if len(trs) < period:
        return 50.0
    atr = sum(trs[-period:]) / period
    pdi = sum(pdms[-period:]) / period / atr * 100 if atr > 0 else 0
    mdi = sum(mdms[-period:]) / period / atr * 100 if atr > 0 else 0
    dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    return dx

def push(msg):
    try:
        sys.path.insert(0, '/Users/jimingzhang/kronos')
        from kronos_pilot import push_feishu
        push_feishu(msg)
    except:
        pass

def get_current_price():
    try:
        mkt = Market()
        r = mkt.get_ticker(instId='AVAX-USDT-SWAP')
        return float(r['data'][0]['last'])
    except:
        return None

def get_sl_order():
    try:
        c = AlgoTrade(secret=os.environ['OKX_SECRET'], key=os.environ['OKX_API_KEY'],
                       passphrase=os.environ['OKX_PASSPHRASE'], flag='1')
        r = c.get_orders_algo_pending(ordType='conditional', instId='AVAX-USDT-SWAP')
        for o in r.get('data', []):
            if o.get('slTriggerPx'):
                return o.get('algoId'), float(o.get('slTriggerPx'))
        return None, None
    except:
        return None, None

def amend_sl(algo_id, new_sl):
    c = AlgoTrade(secret=os.environ['OKX_SECRET'], key=os.environ['OKX_API_KEY'],
                   passphrase=os.environ['OKX_PASSPHRASE'], flag='1')
    r = c.set_amend_algos(instId='AVAX-USDT-SWAP', algoId=algo_id,
                          newSlTriggerPx=str(round(new_sl, 4)))
    return r.get('code') == '0'

def get_market_data():
    df = yf.download('AVAX-USD', period='5d', interval='1h', progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    closes = df['close'].dropna().values
    highs = df['high'].dropna().values
    lows = df['low'].dropna().values
    return closes, highs, lows

def main():
    closes, highs, lows = get_market_data()
    current_price = closes[-1]
    entry = POSITION['entry']
    pnl_pct = (current_price - entry) / entry * 100

    algo_id, current_sl = get_sl_order()
    if algo_id is None:
        print('未找到AVAX SL订单')
        return

    rsi14 = calc_rsi(closes, 14)
    rsi7 = calc_rsi(closes, 7)
    adx = calc_adx(highs, lows, closes, 14)

    # Progressive SL tightening
    new_sl = None
    for threshold, sl_target, label in TRAILING_RULES:
        if pnl_pct >= threshold and sl_target > current_sl:
            new_sl = sl_target
            desc = label
            break

    if new_sl:
        ok = amend_sl(algo_id, new_sl)
        if ok:
            msg = (f'[AVAX主动SL收紧]\n'
                   f'{desc}\n'
                   f'浮盈: {pnl_pct:.2f}% | 现价: ${current_price:.4f}\n'
                   f'SL: ${current_sl:.2f} → ${new_sl:.2f}\n'
                   f'指标: RSI14={rsi14:.0f} ADX={adx:.0f}')
            push(msg)
            print(f'✅ SL {current_sl:.2f}→{new_sl:.2f} | {pnl_pct:+.2f}%')
        else:
            print(f'❌ SL调整失败')
    else:
        # No action needed - passive monitoring
        print(f'SL保持${current_sl:.2f} | 浮盈{pnl_pct:+.2f}% | RSI14={rsi14:.0f} ADX={adx:.0f}')

if __name__ == '__main__':
    main()
