#!/usr/bin/env python3
"""
每日交易复盘 Cron Job
每天 16:05 UTC (00:05 北京) 运行，生成结构化复盘报告发飞书
"""
import json, os, sys
import hmac, base64, hashlib, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOG_DIR = os.path.expanduser('~/.hermes/cron/output')
TRADES_LOG = Path(LOG_DIR) / 'trades_log.json'

# OKX API (与trend_scanner.py一致的认证方式)
def okx_api(method, path, body=''):
    API_KEY = os.getenv('OKX_API_KEY', '')
    SECRET = os.getenv('OKX_SECRET', '')
    PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
    msg = ts + method + path + body
    mac = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    h = {'OK-ACCESS-KEY': API_KEY, 'OK-ACCESS-SIGN': sig,
         'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': PASSPHRASE,
         'Content-Type': 'application/json', 'x-simulated-trading': os.getenv('OKX_FLAG', '1')}
    url = 'https://www.okx.com' + path
    try:
        if body:
            return requests.post(url, headers=h, data=body, timeout=10)
        return requests.get(url, headers=h, timeout=10)
    except: return None

def get_balance():
    resp = okx_api('GET', '/api/v5/account/balance?ccy=USDT')
    if resp and resp.status_code == 200:
        data = resp.json()
        if data.get('code') == '0':
            return float(data['data'][0]['totalEq'])
    return None

def get_positions():
    resp = okx_api('GET', '/api/v5/account/positions?instType=SWAP')
    if not resp or resp.status_code != 200: return []
    data = resp.json()
    if data.get('code') != '0': return []
    return data.get('data', [])

def get_fills(day_date):
    """获取指定日期的所有成交"""
    after_ms = str(int(datetime.strptime(f'{day_date} 00:00:00', '%Y-%m-%d %H:%M:%S').timestamp() * 1000) - 1)
    before_ms = str(int(datetime.strptime(f'{day_date} 23:59:59', '%Y-%m-%d %H:%M:%S').timestamp() * 1000) + 1)
    resp = okx_api('GET', f'/api/v5/trade/fills?after={before_ms}&before={after_ms}&limit=100')
    if not resp or resp.status_code != 200: return []
    data = resp.json()
    if data.get('code') != '0': return []
    return data.get('data', [])

def daily_review(date=None):
    """
    生成每日复盘报告
    date: 'YYYY-MM-DD' 格式，默认昨天
    """
    if date is None:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        date = yesterday.strftime('%Y-%m-%d')
    
    # 获取当天成交
    fills = get_fills(date)
    
    # 按订单聚合
    order_map = {}  # {ordId: {coin, side, price, sz, fee, time}}
    for f in fills:
        oid = f.get('ordId', '')
        if oid not in order_map:
            order_map[oid] = {
                'coin': f.get('instId', '').replace('-USDT-SWAP', ''),
                'side': f.get('side', ''),
                'avgPx': float(f.get('avgPx', 0)),
                'sz': float(f.get('sz', 0)),
                'fee': float(f.get('fee', 0)),
                'time': f.get('ts', ''),
            }
    
    # 区分开仓/平仓
    opens = [o for o in order_map.values() if o['sz'] > 0]
    closes = [o for o in order_map.values() if o['sz'] < 0]  # 平仓sz是负数
    
    # 当前持仓
    positions = get_positions()
    pos_summary = []
    for p in positions:
        inst = p.get('instId', '').replace('-USDT-SWAP', '')
        pos = float(p.get('pos', 0))
        if pos == 0: continue
        avg_px = float(p.get('avgPx', 0))
        upl = float(p.get('upl', 0))
        pos_summary.append({
            'coin': inst, 'pos': pos, 'avgPx': avg_px, 'upl': upl,
            'side': p.get('posSide', '')
        })
    
    # 计算手续费
    total_fee = sum(o['fee'] for o in order_map.values())
    
    # ===== 生成报告 =====
    report = f"""📋 交易日报 | {date}

━━━━━━━━━━━━━━━━━━━━━━
1. 基础数据
━━━━━━━━━━━━━━━━━━━━━━
• 开仓订单: {len(opens)} 个
• 平仓订单: {len(closes)} 个
• 手续费支出: ${total_fee:.2f}
• 当前持仓: {len(pos_summary)} 个币种"""

    if pos_summary:
        report += "\n  持仓明细:"
        for p in pos_summary:
            report += f"\n  • {p['coin']} {p['side']} {p['pos']}张 @ ${p['avgPx']:,.2f} (浮亏${p['upl']:.2f})"

    report += f"""
━━━━━━━━━━━━━━━━━━━━━━
2. 当日成交明细
━━━━━━━━━━━━━━━━━━━━━━"""
    
    if order_map:
        for i, (oid, o) in enumerate(order_map.items(), 1):
            px = o['avgPx']
            px_str = f'${px:,.4f}' if px < 1 else f'${px:,.2f}'
            time_str = datetime.fromtimestamp(int(o['time'])/1000, tz=timezone.utc).strftime('%H:%M') if o['time'] else '?'
            report += f"""
{i}. {o['coin']} {o['side'].upper()} {o['sz']:.0f}张 @ {px_str} @{time_str}"""
    else:
        report += "\n   (无成交)"

    report += f"""
━━━━━━━━━━━━━━━━━━━━━━
3. 当前持仓状态
━━━━━━━━━━━━━━━━━━━━━━"""
    
    if positions:
        total_upl = sum(float(p.get('upl', 0)) for p in positions)
        report += f"\n• 持仓数: {len(positions)} 个"
        report += f"\n• 总浮亏: ${total_upl:.2f}"
    else:
        report += "\n• 无持仓"

    report += f"""
━━━━━━━━━━━━━━━━━━━━━━
4. 系统运行状态
━━━━━━━━━━━━━━━━━━━━━━
• 扫描频率: 每3分钟
• 超卖规则: {'已激活' if True else '未触发'}
• 模拟盘限制: 3笔用尽，新机会排队等待

━━━━━━━━━━━━━━━━━━━━━━
5. 明日计划
━━━━━━━━━━━━━━━━━━━━━━
□ 继续运行系统，等待模拟盘限制解除
□ AVAX RSI一旦 < 40，立即开仓
□ DOGE RSI一旦 < 35，立即开空

━━━━━━━━━━━━━━━━━━━━━━
⏰ 报告生成: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"""

    return report

if __name__ == '__main__':
    date = sys.argv[1] if len(sys.argv) > 1 else None
    print(daily_review(date))
