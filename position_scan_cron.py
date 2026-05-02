#!/usr/bin/env python3
"""Position scan cron - check OKX connection, positions, and UPL"""
import sys, os, json, hmac, hashlib, base64, requests
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime

load_dotenv(Path.home() / '.hermes' / '.env', override=True)
OKX_API_KEY = os.getenv('OKX_API_KEY')
OKX_SECRET = os.getenv('OKX_SECRET')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE')
OKX_FLAG = os.getenv('OKX_FLAG', '1')

def _ts():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

def _req(method, path, body=''):
    ts = _ts()
    msg = ts + method + path + (body if body else '')
    sig = base64.b64encode(hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    h = {
        'OK-ACCESS-KEY': OKX_API_KEY,
        'OK-ACCESS-SIGN': sig,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': OKX_FLAG,
    }
    url = 'https://www.okx.com' + path
    r = requests.get(url, headers=h, timeout=10) if method=='GET' else requests.post(url, headers=h, data=body, timeout=10)
    return r.json()

# 1. Balance
result = _req('GET', '/api/v5/account/balance?ccy=USDT')
if result.get('code') != '0' or not result.get('data'):
    print(f"❌ OKX连接失败: {result.get('msg', 'unknown')}")
    sys.exit(1)

bal = result['data'][0]
total_eq = float(bal.get('eq', 0))
cash = float(bal.get('cashBal', 0))
upl_bal = float(bal.get('upl', 0))
print(f"OKX连接正常 | Equity=${total_eq:,.2f} | Cash=${cash:,.2f} | BalUPL=${upl_bal:,.2f}")

# 2. Positions
result2 = _req('GET', '/api/v5/account/positions')
positions = result2.get('data', [])
total_upl = 0
active_positions = []

for p in positions:
    inst = p.get('instId', '')
    side = p.get('posSide', '')
    sz = p.get('pos', '0')
    avgPx = p.get('avgPx', '0')
    markPx = p.get('markPx', '0')
    upl = float(p.get('upl', 0))
    notional = float(p.get('notionalUsd', 0) or 0)
    total_upl += upl
    coin = inst.split('-')[0]
    margin = float(p.get('margin', 0) or 0)
    liqPx = float(p.get('liqPx', 0) or 0)
    
    if float(sz or 0) > 0 or notional > 100:
        active_positions.append({
            'coin': coin, 'side': side, 'sz': float(sz or 0),
            'entry': float(avgPx), 'mark': float(markPx),
            'upl': upl, 'notional': notional, 'margin': margin,
            'liqPx': liqPx
        })

print(f"持仓数: {len(active_positions)}")
for p in active_positions:
    print(f"{p['coin']:6s} {p['side']:6s} sz={p['sz']:8.1f} entry=${p['entry']:<8.4f} mark=${p['mark']:<8.4f} UPL=${p['upl']:8.2f} margin=${p['margin']:<8.2f} liq=${p['liqPx']:<10.4f}")

print(f"总UPL: ${total_upl:,.2f}")
print(f"权益使用率: {(sum(p['margin'] for p in active_positions) / total_eq * 100):.1f}%")
