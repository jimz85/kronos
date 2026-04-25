#!/usr/bin/env python3
"""
Kronos 共享工具函数
=================
所有脚本共享的 OKX API 封装、PnL 计算等工具函数
避免代码重复和循环导入
"""
import os, json, hmac, base64, hashlib, time, requests
from datetime import datetime
from pathlib import Path

# OKX API 配置
BASE_URL = 'https://www.okx.com'

def _sign(ts, method, path, body=''):
    """OKX API 签名"""
    msg = ts + method + path + body
    mac = hmac.new(os.environ.get('OKX_SECRET','').encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_req(method, path, body='', api_key=None, secret=None, passphrase=None):
    """
    通用的 OKX API 请求
    返回 parsed JSON 或 {'error': ...}
    """
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (int(time.time() * 1000) % 1000)
    key = api_key or os.environ.get('OKX_API_KEY', '')
    secret_key = secret or os.environ.get('OKX_SECRET', '')
    phrase = passphrase or os.environ.get('OKX_PASSPHRASE', '')
    flag = os.environ.get('OKX_FLAG', '1')  # default simulation

    headers = {
        'OK-ACCESS-KEY': key,
        'OK-ACCESS-SIGN': _sign(ts, method, path, body),
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': phrase,
        'Content-Type': 'application/json',
        'x-simulated-trading': '1' if flag == '1' else '0',
    }
    try:
        r = requests.request(method, BASE_URL + path, headers=headers, data=body, timeout=10)
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def get_pnl_from_fills(coin):
    """
    从OKX成交记录计算某币的已实现盈亏（仅适用于已平仓持仓）
    返回: float（美元盈亏）或 None（持仓仍开或无数据）
    """
    # Step 1: 确认该币已平仓
    pos_data = okx_req('GET', f'/api/v5/account/positions?instId={coin}-USDT-SWAP')
    try:
        positions = pos_data.get('data', [])
        for p in positions:
            if float(p.get('pos', 0)) != 0:
                return None  # 持仓仍开，不计算PnL
    except:
        pass

    # Step 2: 获取成交记录
    fills_data = okx_req('GET', f'/api/v5/trade/fills?instId={coin}-USDT-SWAP&limit=50')
    try:
        if fills_data.get('code') != '0':
            return None
        fills = fills_data.get('data', [])
        if not fills:
            return None
    except:
        return None

    buys, sells = [], []
    for f in fills:
        sz = float(f.get('fillSz', 0))
        px = float(f.get('fillPx', 0))
        ts = int(f.get('ts', 0))
        if f.get('side') == 'buy':
            buys.append((sz, px, ts))
        else:
            sells.append((sz, px, ts))

    buys.sort(key=lambda x: x[2])
    sells.sort(key=lambda x: x[2])

    # FIFO 配对
    remaining_buys = list(buys)
    realized_pnl = 0.0
    for sell_sz, sell_px, _ in sells:
        remaining = sell_sz
        while remaining > 0 and remaining_buys:
            buy_sz, buy_px, _ = remaining_buys[0]
            match = min(buy_sz, remaining)
            realized_pnl += (sell_px - buy_px) * match
            remaining -= match
            if match >= buy_sz:
                remaining_buys.pop(0)
            else:
                remaining_buys[0] = (buy_sz - match, buy_px, remaining_buys[0][2])

    return round(realized_pnl, 4) if realized_pnl != 0 else None


def calculate_trade_pnl(trade, exit_price):
    """
    根据入场价、出场价、方向、杠杆计算交易盈亏
    trade: dict，含 direction, entry_price, contracts, leverage
    exit_price: float
    返回: (result_pct, pnl)
    """
    entry = trade.get('entry_price', 0)
    direction = trade.get('direction', 'LONG')
    contracts = trade.get('contracts', 0)
    lev = trade.get('leverage', 3)

    if direction == 'LONG':
        ret = (exit_price - entry) / entry
    else:
        ret = (entry - exit_price) / entry

    ret_with_lev = ret * lev
    result_pct = round(ret_with_lev * 100, 2)
    pnl = round(ret_with_lev * contracts, 4)
    return result_pct, pnl


def get_account_balance():
    """获取OKX账户权益"""
    data = okx_req('GET', '/api/v5/account/balance')
    try:
        if data.get('code') == '0' and data.get('data'):
            return {'totalEq': float(data['data'][0].get('totalEq', 0))}
    except:
        pass
    return {'totalEq': 0}


def get_funding_rate(coin: str) -> dict:
    """
    获取某币种当前资金费率（OKX公开接口，无需认证）
    返回: {'rate': float(百分比), 'next_time': str, 'direction': 'long'/'short'}
    资金费率 > 0.01% (0.0001) = 多头付钱给空头 = 偏多
    资金费率 < -0.01% = 空头付钱给多头 = 偏空
    """
    try:
        r = requests.get(
            f'https://www.okx.com/api/v5/public/funding-rate?instId={coin}-USDT-SWAP',
            timeout=8
        )
        d = r.json()
        if d.get('code') != '0' or not d.get('data'):
            return {}
        item = d['data'][0]
        rate = float(item.get('fundingRate', 0))
        next_ts = int(item.get('nextFundingTime', 0))
        next_time = datetime.fromtimestamp(next_ts / 1000).strftime('%m-%d %H:%M') if next_ts else ''
        direction = 'long_pays' if rate > 0 else 'short_pays'
        return {
            'rate': rate * 100,  # 转百分比
            'rate_raw': rate,
            'next_time': next_time,
            'direction': direction,
            'coin': coin,
        }
    except:
        return {}


def get_open_interest(coin: str) -> dict:
    """
    获取某币种持仓量（Open Interest），OKX公开接口
    返回: {'oi_usd': float, 'oi_change_24h': float}
    OI上升 = 多空双方都在加仓 = 趋势可能延续
    OI下降 = 多空双方都在减仓 = 趋势可能反转
    """
    try:
        r = requests.get(
            f'https://www.okx.com/api/v5/public/open-interest?instId={coin}-USDT-SWAP',
            timeout=8
        )
        d = r.json()
        if d.get('code') != '0' or not d.get('data'):
            return {}
        item = d['data'][0]
        oi = float(item.get('oi', 0))
        # oiUsd is in USD terms, else compute from contracts
        oi_usd = float(item.get('oiUsd', oi * 100))  # approximate
        return {
            'oi': oi,
            'oi_usd': oi_usd,
            'coin': coin,
        }
    except:
        return {}


def get_multi_funding_and_oi(coins: list) -> dict:
    """
    批量获取多个币种的资金费率和OI
    用于扫描时一次性获取，避免逐个请求延迟
    """
    result = {}
    for coin in coins:
        fr = get_funding_rate(coin)
        oi = get_open_interest(coin)
        result[coin] = {**fr, **oi}
    return result


def load_paper_log():
    """加载 paper_trades.json"""
    path = Path.home() / '.hermes' / 'cron' / 'output' / 'paper_trades.json'
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []


def save_paper_log(trades):
    """保存 paper_trades.json（现已使用原子写入）"""
    path = Path.home() / '.hermes' / 'cron' / 'output' / 'paper_trades.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, trades[-500:])


# ═══════════════════════════════════════════════════════════
#  原子写入工具（防断电损坏）
# ═══════════════════════════════════════════════════════════
import tempfile, os, shutil

def atomic_write_json(path: Path, data, indent: int = 2) -> None:
    """
    原子级 JSON 文件写入（多进程安全版）。

    实现原理：
        1. tempfile.mkstemp() 生成唯一临时文件（PID+随机数后缀）
           → 多进程/多线程并发写入不会互相覆盖各自的 .tmp 文件
        2. 写入内容后 os.fsync() 确保内容落盘
        3. os.replace() 将临时文件 Rename 覆盖目标文件

    关键性质：
        - os.replace() 是跨平台原子操作（Linux/macOS/Windows 均保证原子性）
        - 即使在 os.replace() 执行瞬间断电，原文件要么是旧版本，
          要么是完整新版本，绝不会出现截断/损坏的 JSON
        - 唯一临时文件名保证多进程并发安全

    参数：
        path: 目标文件路径（Path 对象或 str）
        data: 可序列化的 Python 对象
        indent: json.dump indent 参数（默认 2）
    """
    path = Path(path)
    # 使用唯一临时文件名（避免多进程竞争同一 .tmp 文件）
    fd, tmp_path = tempfile.mkstemp(suffix='.json.tmp', prefix='atomic_', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except Exception:
        # 失败时清理临时文件，避免残留
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def atomic_write_text(path: Path, content: str) -> None:
    """
    原子级纯文本文件写入（用于非 JSON 格式的状态文件）。

    参数：
        path: 目标文件路径
        content: 字符串内容
    """
    path = Path(path)
    tmp = path.with_suffix('.tmp')

    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())

    os.replace(str(tmp), str(path))
