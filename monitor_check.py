
import requests, json, os, hmac, hashlib, base64, re
from datetime import datetime
from pathlib import Path

API_KEY = os.getenv('OKX_API_KEY')
SECRET = os.getenv('OKX_SECRET')
PASSPHRASE = os.getenv('OKX_PASSPHRASE')

def _ts():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

def sign(ts, method, path, body=''):
    msg = ts + method + path + (body if body else '')
    m = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(m.digest()).decode()

def req(method, path, body=''):
    ts = _ts()
    sig = sign(ts, method, path, body)
    h = {
        'OK-ACCESS-KEY': API_KEY,
        'OK-ACCESS-SIGN': sig,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': os.getenv('OKX_FLAG', '1'),
    }
    url = 'https://www.okx.com' + path
    if method == 'GET':
        r = requests.get(url, headers=h, timeout=10)
    else:
        r = requests.post(url, headers=h, data=body, timeout=10)
    return r.json()

issues = []

# 1. 检查OKX API连通性
try:
    r = req('GET', '/api/v5/account/balance')
    if r.get('code') not in ('0', None):
        issues.append('OKX API错误: ' + r.get('code') + ' ' + r.get('msg'))
except Exception as e:
    issues.append('OKX连接失败: ' + str(e))

# 2. 检查持仓SL/TP完整性
try:
    pos_r = req('GET', '/api/v5/account/positions?instType=SWAP')
    algo_r = req('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP&ordType=oco&limit=100')
    algos = {}
    for o in algo_r.get('data', []):
        inst = o.get('instId', '')
        if '-USDT-SWAP' in inst:
            coin = inst.replace('-USDT-SWAP', '')
            algos[coin] = {'sl': o.get('slTriggerPx'), 'tp': o.get('tpTriggerPx')}
    
    active_pos = [p for p in pos_r.get('data', []) if float(p.get('pos', 0)) > 0]
    for p in active_pos:
        inst = p.get('instId', '')
        coin = inst.replace('-USDT-SWAP', '')
        sz = float(p.get('pos', 0))
        if sz > 0 and coin not in algos:
            issues.append(coin + ' 持仓无SL/TP保护!')
        upl = float(p.get('upl', 0))
        if upl < -300:
            issues.append(coin + ' 浮亏$' + str(upl) + '，接近强制止损')
except Exception as e:
    issues.append('持仓检查异常: ' + str(e))

# 3. 检查最新多币种扫描是否有gemma4推理失败
try:
    out_dir = Path.home() / '.hermes' / 'cron' / 'output' / '9971e0c09235'
    latest = sorted(out_dir.glob('*.md'))[-1] if out_dir.exists() else None
    if latest:
        content = latest.read_text()
        if 'LLM调用失败' in content or ('gemma4异常' in content and '解析失败' in content):
            issues.append('gemma4推理失败: ' + latest.name)
except:
    pass

# 4. 检查是否有真正被执行的操作
try:
    out_dir = Path.home() / '.hermes' / 'cron' / 'output' / '9971e0c09235'
    latest = sorted(out_dir.glob('*.md'))[-1] if out_dir.exists() else None
    if latest:
        content = latest.read_text()
        blocked = any(kw in content for kw in ['禁止开仓', '被风控否决', '风控检查', '保持现状'])
        final_hold = '最终决策: hold' in content or '保持现状' in content
        actual_actions = []
        for kw in ['平仓成功', '开仓成功', 'trailing_sl', 'take_profit']:
            if kw in content and not final_hold:
                actual_actions.append(kw)
        final_match = re.search(r'最终决策[:\s]+(\S+)', content)
        if final_match:
            final_decision = final_match.group(1).strip().rstrip('|')
            if final_decision not in ('hold',):
                if '平仓' in content and '禁止' not in content:
                    actual_actions.append('平仓')
        if actual_actions and not final_hold and not blocked:
            issues.append('实际交易执行: ' + ' / '.join(set(actual_actions)) + ' (' + latest.name + ')')
except Exception as e:
    pass

if issues:
    msg = '\n'.join(issues)
    print('ACTIVE_MONITORING_ALERT')
    print(msg)
else:
    print('MONITORING_OK')
