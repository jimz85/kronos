#!/usr/bin/env python3
"""
MiniMax-M2.7每小时战略审查 v3.0
====================================================
定位：战略层（小时级）→ 为战术层（3分钟）提供上下文

职责：
1. 分析市场环境（Regime / 因子健康 / 方向）
2. 输出 factor_context.json（供 kronos_multi_coin.py 读取）
3. 紧急干预时输出 emergency_stop.json
4. 安全兜底：亏损>5%直接平仓（独立于上下文）

★ 核心原则：MiniMax 做战略判断，不做执行决策
★ 执行决策由 kronos_multi_coin.py（gemma4）基于上下文做出

运行：
  python3 gemma4_hourly_review.py
"""

import os, sys, json, time
import subprocess
from datetime import datetime
from pathlib import Path
from kronos_utils import atomic_write_json  # 原子写入（防断电损坏）

# ========== 加载环境变量 ==========
for line in open(os.path.expanduser('~/.hermes/.env')):
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ[k.strip()] = v.strip()

# ========== 交易参数 ==========
SL_DIST_PCT = 0.02       # SL距现价2%
TP_DIST_PCT = 0.10       # TP距现价10%
MAX_LOSS_PCT = 0.05      # 最大亏损5%强制止损

# API Keys（从环境变量读取）
OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET_KEY', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
MINIMAX_API_KEY = os.getenv('MINIMAX_API_KEY', '')
MINIMAX_BASE_URL = os.getenv('MINIMAX_BASE_URL', 'https://api.minimaxi.com/v1')

# ========== 上下文Schema导入 ==========
sys.path.insert(0, str(Path(__file__).parent))
try:
    from context_schema import (
        FactorContext, FactorStatus, PositionContext, EmergencyStop,
        CTX_FILE, EMERGENCY_FILE,
        load_context_with_validation, write_emergency_stop, clear_emergency_stop,
        append_audit,
    )
    CTX_PATH = str(Path(__file__).parent / CTX_FILE)
    EMERGENCY_PATH = str(Path(__file__).parent / EMERGENCY_FILE)
    AUDIT_PATH = str(Path(__file__).parent / "audit_log.jsonl")
except ImportError:
    # 降级：context_schema.py 不存在时使用内联版本
    CTX_PATH = "factor_context.json"
    EMERGENCY_PATH = "emergency_stop.json"
    AUDIT_PATH = "audit_log.jsonl"

# ========== OKX API ==========

def _ts():
    global _okx_ts_offset
    try:
        if _okx_ts_offset is None:
            import requests
            r = requests.get('https://www.okx.com/api/v5/public/time', timeout=5)
            if r.status_code == 200:
                okx_ms = int(r.json()['data'][0]['ts'])
                local_ms = int(time.time() * 1000)
                _okx_ts_offset = okx_ms - local_ms
    except:
        pass
    from datetime import datetime
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

_okx_ts_offset = None

def _req_get(url):
    try:
        import requests
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        return r.json()
    except:
        return None

def _req(method, path, body=''):
    import requests, hmac, hashlib, base64
    ts = _ts()
    msg = f'{ts}{method}{path}{body}'
    sign = base64.b64encode(hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    headers = {
        'OK-ACCESS-KEY': OKX_API_KEY,
        'OK-ACCESS-SIGN': sign,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': os.getenv('OKX_FLAG', '1'),
    }
    url = 'https://www.okx.com' + path
    r = requests.get(url, headers=headers, timeout=10) if method == 'GET' else requests.post(url, headers=headers, data=body, timeout=10)
    return r.json()

# ========== 市场数据 ==========

def get_ohlcv(coin, bar='1H', limit=72):
    try:
        import requests
        instId = f'{coin}-USDT-SWAP'
        after = int(time.time() * 1000) - (limit * 3600 * 1000)
        r = requests.get(f'https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}&after={after}', timeout=10)
        data = r.json()
        if not data.get('data'):
            return []
        candles = []
        for d in data['data']:
            try:
                candles.append({'ts': int(d[0]), 'close': float(d[4]), 'high': float(d[2]), 'low': float(d[3]), 'volume': float(d[5])})
            except:
                pass
        return list(reversed(candles))
    except:
        return []

def calc_rsi(candles, period=14):
    if len(candles) < period + 1:
        return None
    closes = [c['close'] for c in candles]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_adx(candles, period=14):
    """
    正确的ADX计算 - Wilder平滑
    """
    if len(candles) < period * 2:
        return None
    
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    closes = [c['close'] for c in candles]
    
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        trs.append(tr)
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    
    if len(trs) < period:
        return None
    
    atr = sum(trs[:period]) / period
    plus_di_sum = sum(plus_dm[:period]) / period
    minus_di_sum = sum(minus_dm[:period]) / period
    
    dx_values = []
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        plus_di_sum = (plus_di_sum * (period - 1) + plus_dm[i]) / period
        minus_di_sum = (minus_di_sum * (period - 1) + minus_dm[i]) / period
        
        if atr > 0:
            plus_di = (plus_di_sum / atr) * 100
            minus_di = (minus_di_sum / atr) * 100
            if plus_di + minus_di > 0:
                dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
                dx_values.append(dx)
    
    if len(dx_values) < period:
        return None
    
    adx = sum(dx_values[-period:]) / period
    return adx

def get_current_price(coin):
    try:
        r = _req_get(f'https://www.okx.com/api/v5/market/ticker?instId={coin}-USDT-SWAP')
        if r and r.get('data'):
            return float(r['data'][0]['last'])
    except:
        pass
    return None

def get_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]['high'] - candles[i]['low'],
            abs(candles[i]['high'] - candles[i-1]['close']),
            abs(candles[i]['low'] - candles[i-1]['close'])
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

def get_multi_tf_data(coin):
    """获取多周期数据"""
    price = get_current_price(coin)
    h1 = get_ohlcv(coin, '1H', 72)
    h4 = get_ohlcv(coin, '4H', 168)
    d1 = get_ohlcv(coin, '1D', 90)
    
    atr = get_atr(h1) if h1 else None
    
    def vol_profile(candles):
        if len(candles) < 5:
            return 'low'
        vols = [c['volume'] for c in candles[-20:]]
        avg_vol = sum(vols) / len(vols)
        recent_vol = sum([c['volume'] for c in candles[-3:]]) / 3
        return 'high' if recent_vol > avg_vol * 1.3 else 'normal'
    
    return {
        'coin': coin,
        'price': price,
        'rsi_1h': round(calc_rsi(h1), 1) if h1 else None,
        'rsi_4h': round(calc_rsi(h4), 1) if h4 else None,
        'rsi_1d': round(calc_rsi(d1), 1) if d1 else None,
        'adx_1h': round(calc_adx(h1), 1) if h1 else None,
        'adx_4h': round(calc_adx(h4), 1) if h4 else None,
        'volume': vol_profile(h1),
        'trend_1h': 'up' if h1 and len(h1) > 2 and h1[-1]['close'] > h1[0]['close'] else 'down',
        'trend_4h': 'up' if h4 and len(h4) > 2 and h4[-1]['close'] > h4[0]['close'] else 'down',
        'atr': atr,
        'atr_pct': (atr / price * 100) if atr and price else None,
    }

# ========== 市场情绪与链上数据收集 ==========

def get_news(coins=['BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'AVAX'], max_per_coin=3):
    """从RSS抓取新闻，按币种过滤"""
    news_by_coin = {c: [] for c in coins}
    rss_feeds = [
        ('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/'),
        ('CoinTelegraph', 'https://cointelegraph.com/rss'),
    ]
    for source, url in rss_feeds:
        try:
            import requests
            r = requests.get(url, timeout=8)
            from xml.etree import ElementTree as ET
            root = ET.fromstring(r.text)
            for item in root.findall('.//item')[:15]:
                title = item.findtext('title') or ''
                link = item.findtext('link') or ''
                for coin in coins:
                    if coin in title.upper():
                        for c in coins:
                            if c in title.upper():
                                news_by_coin[c].append(f"[{source}] {title[:100]}")
                                break
        except:
            pass
    # 合并
    result = []
    for coin in coins:
        items = news_by_coin[coin][:max_per_coin]
        if items:
            result.append(f"【{coin}】" + ' | '.join(items))
    return '\n'.join(result) if result else '无重大新闻'

def get_funding_rates(coins=['BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'AVAX', 'BNB']):
    """获取各币种资金费率（OKX）"""
    try:
        rates = []
        for coin in coins:
            try:
                r = _req_get(f'https://www.okx.com/api/v5/market/ticker?instId={coin}-USDT-SWAP')
                if r and r.get('data'):
                    # OKX funding rate从instType=futures获取
                    pass
            except:
                pass
        # 备用：从CoinGecko获取币种USD价格变化推断情绪
        return _get_funding_from_coingecko(coins)
    except:
        return '资金费率数据不可用'

def _get_funding_from_coingecko(coins):
    """从CoinGecko free API获取相对情绪数据"""
    try:
        ids = {'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana',
               'DOGE': 'dogecoin', 'ADA': 'cardano', 'AVAX': 'avalanche-2', 'BNB': 'binancecoin'}
        ids_filter = [ids[c] for c in coins if c in ids]
        import requests
        r = requests.get(
            f'https://api.coingecko.com/api/v3/simple/price',
            params={'ids': ','.join(ids_filter), 'vs_currencies': 'usd',
                    'include_24hr_change': 'true', 'include_24hr_vol': 'true'},
            timeout=10
        )
        if r.status_code != 200:
            return '价格数据不可用'
        data = r.json()
        lines = []
        for coin in coins:
            gid = ids.get(coin)
            if gid and gid in data:
                d = data[gid]
                change_24h = d.get('usd_24h_change', 0)
                vol_24h = d.get('usd_24h_vol', 0)
                emoji = '🔴' if change_24h < -3 else ('🟢' if change_24h > 3 else '⚪')
                lines.append(f"{emoji} {coin}: {change_24h:+.2f}% (24h)")
        return '\n'.join(lines) if lines else '价格数据不可用'
    except Exception as e:
        return f'价格数据获取失败: {str(e)[:50]}'

def get_onchain_sentiment():
    """获取链上情绪指标 - 从本地缓存或实时抓取"""
    # 优先读market_sentiment.json（kronos_pilot.py写的）
    cache = Path.home() / '.hermes/cron/output/market_sentiment.json'
    if cache.exists():
        try:
            import json
            d = json.loads(cache.read_text())
            updated = d.get('updated', 'unknown')
            data = d.get('data', {})
            funding = data.get('l1_funding', {})
            news = data.get('news_alert', [])
            lines = [f"更新时间: {updated}"]
            if funding:
                fr_parts = []
                for coin, rate in list(funding.items())[:5]:
                    fr_parts.append(f"{coin}: {rate:+.4f}%")
                lines.append('资金费率: ' + ' | '.join(fr_parts))
            if news:
                lines.append('新闻: ' + ' | '.join([f"{a.get('coin')}@{a.get('source')}" for a in news[:3]]))
            return '\n'.join(lines)
        except:
            pass
    return '链上数据不可用'

def get_whale_indicator():
    """简单的鲸鱼活动代理指标 - 从交易所余额变化推断"""
    try:
        import requests
        # Binance比特币交易所余额（公开数据）
        r = requests.get('https://api.coingecko.com/api/v3/coins/bitcoin/tickers?exchange_ids=binance', timeout=10)
        # 简化：获取BTC交易所余额变化代理
        # CoinGecko提供bi该数据
        r = requests.get(
            'https://api.coingecko.com/api/v3/coins/bitcoin',
            params={'localization': 'false', 'tickers': 'false',
                    'market_data': 'true', 'community_data': 'false', 'developer_data': 'false'},
            timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            mkt = d.get('market_data', {})
            # 交易所流入/流出没有直接API，用持仓地址数作为代理
            # 不阻塞，直接返回
        return ' Whale数据需专业API，暂不可用'
    except:
        return ' Whale数据获取失败'

# ========== BTC趋势判断 ==========

def get_btc_regime():
    """判断BTC中长期趋势：bull/bear/neutral"""
    c4 = get_ohlcv('BTC', '4H', 200)
    if not c4 or len(c4) < 100:
        return 'neutral'
    btc_rsi_4h = calc_rsi(c4)
    btc_adx_4h = calc_adx(c4)
    btc_ma200 = sum(c['close'] for c in c4[-200:]) / 200
    btc_price = c4[-1]['close']
    if btc_price > btc_ma200 and btc_rsi_4h < 75:
        return 'bull'
    if btc_price < btc_ma200 and btc_rsi_4h > 25:
        return 'bear'
    return 'neutral'

# ========== 持仓操作 ==========

def get_positions():
    try:
        result = _req('GET', '/api/v5/account/positions')
        positions = {}
        for p in result.get('data', []):
            pos = float(p.get('pos', 0))
            if pos > 0:
                coin = p.get('instId', '').split('-')[0]
                positions[coin] = {
                    'instId': p.get('instId'),
                    'pos': pos,
                    'avgPx': float(p.get('avgPx', 0)),
                    'side': p.get('posSide', ''),
                    'upl': float(p.get('upl', 0)),
                    'uplRatio': float(p.get('uplRatio', 0)),
                    'lever': p.get('lever', ''),
                }
        return positions
    except:
        return {}

def get_pending_algos(instId):
    try:
        result = _req('GET', f'/api/v5/trade/orders-algo-pending?instId={instId}&ordType=conditional')
        algos = {}
        for o in result.get('data', []):
            if o.get('slTriggerPx'):
                algos['sl'] = {'algoId': o.get('algoId'), 'price': float(o.get('slTriggerPx')), 'sz': o.get('sz')}
            if o.get('tpTriggerPx'):
                algos['tp'] = {'algoId': o.get('algoId'), 'price': float(o.get('tpTriggerPx')), 'sz': o.get('sz')}
        return algos
    except:
        return {}

def get_account_equity():
    """获取账户USDT权益（账户总权益，含持仓浮盈亏）"""
    try:
        result = _req('GET', '/api/v5/account/balance?ccy=USDT')
        if result.get('code') == '0':
            data = result.get('data', [{}])[0]
            for d in data.get('details', []):
                if d.get('ccy') == 'USDT':
                    eq = float(d.get('eq', 0))
                    if eq > 0:
                        return eq
            total = float(data.get('totalEq', 0))
            if total > 0:
                return total
    except:
        pass
    return None  # 失败时返回None，不返回假值

def close_position(instId, side, sz):
    close_side = 'sell' if side == 'long' else 'buy'
    close_pos_side = 'long' if side == 'short' else 'short'
    body = json.dumps({'instId': instId, 'tdMode': 'isolated', 'side': close_side, 'ordType': 'market', 'sz': str(int(sz)), 'posSide': close_pos_side})
    result = _req('POST', '/api/v5/trade/order', body)
    ok = result.get('code') == '0'
    print(f"  {'✅' if ok else '❌'} 平仓: {result.get('msg', '')}")
    return ok

def amend_sl(instId, algoId, newSlPrice):
    body = json.dumps({'instId': instId, 'algoId': algoId, 'newSlTriggerPx': str(newSlPrice)})
    result = _req('POST', '/api/v5/trade/amend-algos', body)
    ok = result.get('code') == '0'
    print(f"  {'✅' if ok else '❌'} SL收紧→${newSlPrice}: {result.get('msg', '')}")
    return ok

# ========== MiniMax API ==========

def call_minimax(prompt, model='MiniMax-M2.7', timeout=90):
    """调用MiniMax API"""
    import requests
    headers = {
        'Authorization': f'Bearer {MINIMAX_API_KEY}',
        'Content-Type': 'application/json',
    }
    data = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.3,
        'max_tokens': 2500,
    }
    try:
        r = requests.post(f'{MINIMAX_BASE_URL}/text/chatcompletion_v2', headers=headers, json=data, timeout=timeout)
        result = r.json()
        if 'choices' in result:
            return result['choices'][0]['message']['content']
        return f"API错误: {result.get('base_error', result.get('msg', 'unknown'))}"
    except Exception as e:
        return f"MiniMax API错误: {str(e)}"

def gemma4_review(coin_data, positions, btc_data, news='', price_data='', onchain='', equity=None):
    """MiniMax-M2.7战略审查（生成结构化上下文）
    
    equity: 账户USDT总权益（美元）
    """
    
    # 构建持仓摘要（含持仓市值和占比）
    pos_summary = ""
    total_pos_value = 0.0
    for coin, pos in positions.items():
        price = coin_data.get(coin, {}).get('price')
        pnl = 0
        if price and pos['avgPx'] > 0:
            pnl = (price - pos['avgPx']) / pos['avgPx'] * 100 if pos['side'] == 'long' else (pos['avgPx'] - price) / pos['avgPx'] * 100
            pos_value = pos['pos'] * price
            total_pos_value += pos_value
            pos_summary += f"\n{coin}: {pos['side']} {pos['pos']}张 @{pos['avgPx']} | 浮盈亏{pnl:.2f}% | 市值≈${pos_value:.0f}"
        else:
            pos_summary += f"\n{coin}: {pos['side']} {pos['pos']}张 @{pos['avgPx']} | 浮盈亏{pnl:.2f}%"
        algos = get_pending_algos(pos['instId'])
        if algos:
            sl = algos.get('sl', {}).get('price', '?')
            tp = algos.get('tp', {}).get('price', '?')
            pos_summary += f" | SL=${sl} TP=${tp}"
    
    # 账户权益摘要
    equity_summary = f"${equity:,.0f}" if equity else "未知"
    if equity and total_pos_value > 0:
        exposure_pct = total_pos_value / equity * 100
        pos_summary += f"\n总持仓市值: ${total_pos_value:,.0f} | 总暴露: {exposure_pct:.1f}% 账户权益"
    
    # 构建市场摘要
    market_summary = ""
    for coin, d in coin_data.items():
        summary = f"\n{coin}: ${d['price']}"
        if d.get('rsi_1h'):
            summary += f" | RSI_1h={d['rsi_1h']}"
        if d.get('rsi_4h'):
            summary += f" RSI_4h={d['rsi_4h']}"
        if d.get('adx_1h'):
            summary += f" | ADX={d['adx_1h']}"
        summary += f" | vol={d['volume']} trend={d['trend_1h']}"
        market_summary += summary
    
    btc_regime = get_btc_regime()
    regime_desc = {'bull': '牛市（倾向做多）', 'bear': '熊市（倾向做空）', 'neutral': '震荡（多空均可）'}.get(btc_regime, '震荡')

    prompt = f"""你是顶级加密货币对冲基金经理，有15年交易经验。你的职责是做战略判断，不是执行下单。

【账户权益】
账户总USDT权益: {equity_summary}
{'（下单前必须检查：单个仓位不得超过账户5%，总暴露不得超过账户15%）' if equity else ''}

【当前持仓】
{pos_summary if pos_summary else "无持仓"}

【各币种市场数据】{market_summary}

【BTC中长期趋势】{regime_desc}

【市场情绪与链上数据】
📰 重大新闻:
{news if news else '无'}

💹 各币种24h涨跌:
{price_data if price_data else '不可用'}

⛓ 链上/资金费数据:
{onchain if onchain else '不可用'}

【交易参数】
- SL距现价应至少2%
- TP目标8-12%
- 盈亏比应>2:1
- 多空方向根据{regime_desc}决定
- 单仓位最大不得超过账户{equity_summary}的5%（约${f"{equity*0.05:,.0f}" if equity else "?"}）
- 总暴露不得超过账户{equity_summary}的15%（约${f"{equity*0.15:,.0f}" if equity else "?"}）

【你的任务】战略分析 + 生成结构化上下文

第一步：战略分析
分析维度：
1. 各持仓盈亏情况，SL距离是否合理
2. 亏损的是否该止损（注意：止损由3分钟层执行，你只做判断）
3. RSI在哪个周期出现极值
4. 【关键】整体风险敞口：检查单仓是否>5%账户，总暴露是否>15%账户
5. 当前方向（做多/做空）是否与市场环境匹配
6. 【关键】分析新闻情绪：是否有重大利空/利多？币价走势与新闻情绪是否一致？
7. 【关键】分析24h涨跌与资金费率：资金费率偏多还是偏空？币种间是否分化？
8. 【关键】综合所有数据，判断因子有效性，决定哪些因子权重应该提高/降低
9. 【关键】Oversized检测：任何单币种仓位>5%账户或总暴露>15%账户，必须在emergency_level中标记

第二步：生成结构化上下文（严格按JSON格式输出）

输出格式（必须包含以下JSON块）：
```json
{{
  "market_regime": "bull/bear/neutral/volatile",
  "regime_confidence": 0.0-1.0,
  "primary_direction": "long/short/both/none",
  "direction_confidence": 0.0-1.0,
  "overall_confidence": 0.0-1.0,
  "factor_status": {{
    "vol_ratio": {{"status": "active/inactive/degraded", "ic": 0.0-1.0, "note": ""}},
    "rsi": {{"status": "active/inactive/degraded", "ic": 0.0-1.0, "note": ""}},
    "adx": {{"status": "active/inactive/degraded", "ic": 0.0-1.0, "note": ""}},
    "sentiment": {{"status": "bullish/bearish/neutral", "confidence": 0.0-1.0, "news_signal": "利好/利空/中性", "note": ""}},
    "flow": {{"status": "inflow/outflow/neutral", "funding_bias": "long/short/neutral", "note": ""}}
  }},
  "ic_weights_adjustment": {{"raise": ["RSI"], "lower": ["ADX"], "reason": "新闻+资金费率+24h涨跌综合判断"}},
  "forbidden_actions": ["short_btc", "long_aave"],
  "strategic_hint": "自然语言建议，给3分钟层参考",
  "emergency_level": "none/watch/elevated/high/ultra",
  "emergency_reason": ""
}}
```

第三步：自然语言战略报告
```json
持仓审查:
[对每个持仓的分析，格式：币种 | 方向 | 建议 | 原因]
指令类型：持有 / 建议平仓 / 收紧止损 / 追踪止损

风险评估:
[整体风险等级：低/中/高/极大]
[风险说明]

战略建议:
[未来1-4小时的交易思路，注明多空方向]
```
"""
    print("MiniMax-M2.7战略分析中...")
    try:
        response = call_minimax(prompt, model='MiniMax-M2.7', timeout=90)
        return response
    except Exception as e:
        return f"MiniMax错误: {str(e)}"


def parse_structured_output(review_text: str) -> dict:
    """从MiniMax输出中提取结构化JSON"""
    try:
        # 尝试提取 ```json ... ``` 块
        import re
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', review_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
    except:
        pass
    return {}

# ========== 主流程 ==========

def build_factor_context(review_text: str, coin_data: dict, positions: dict, btc_regime: str) -> FactorContext:
    """从MiniMax输出和实际数据构建 FactorContext"""
    structured = parse_structured_output(review_text)
    
    # 构建因子状态
    factor_status = {}
    if structured.get('factor_status'):
        for fname, fdata in structured['factor_status'].items():
            if isinstance(fdata, dict):
                factor_status[fname] = FactorStatus(
                    status=fdata.get('status', 'unknown'),
                    ic=fdata.get('ic', 0.0),
                    confidence=fdata.get('confidence', 0.5),
                    note=fdata.get('note', ''),
                )
    
    # 构建持仓上下文
    pos_contexts = []
    for coin, pos in positions.items():
        price = coin_data.get(coin, {}).get('price', 0)
        pnl = 0.0
        if price and pos['avgPx'] > 0:
            pnl = (price - pos['avgPx']) / pos['avgPx'] * 100 if pos['side'] == 'long' else (pos['avgPx'] - price) / pos['avgPx'] * 100
        algos = get_pending_algos(pos['instId'])
        sl_dist = 0.0
        if algos.get('sl') and price:
            sl_dist = abs(price - algos['sl']['price']) / price * 100
        pos_contexts.append(PositionContext(
            coin=coin,
            direction=pos['side'],
            pnl_pct=pnl,
            sl_distance_pct=sl_dist,
            status='critical' if pnl < -5 else 'warning' if pnl < -2 else 'healthy',
        ))
    
    # 计算总暴露
    total_exposure = sum(abs(pos['pos']) for pos in positions.values()) / 10  # 简化估算
    
    ctx = FactorContext(
        generated_at=datetime.now().isoformat(),
        generated_by='MiniMax-M2.7',
        market_regime=structured.get('market_regime', btc_regime),
        regime_confidence=structured.get('regime_confidence', 0.5),
        btc_trend=btc_regime,
        overall_confidence=structured.get('overall_confidence', 0.5),
        factor_status={k: v for k, v in factor_status.items()},
        primary_direction=structured.get('primary_direction', 'both'),
        direction_confidence=structured.get('direction_confidence', 0.5),
        forbidden_actions=structured.get('forbidden_actions', []),
        current_positions=pos_contexts,
        total_exposure_pct=min(total_exposure, 30.0),
        max_total_leverage=1.5,
        strategic_hint=structured.get('strategic_hint', ''),
        confidence=structured.get('overall_confidence', 0.5),
        emergency_stop=EmergencyStop(
            level=structured.get('emergency_level', 'none'),
            reason=structured.get('emergency_reason', ''),
        ),
    )
    return ctx


# ========== 主流程 ==========

def hourly_review():
    coins = ['AVAX', 'ETH', 'BTC', 'SOL', 'DOGE', 'ADA', 'DOT', 'LINK']
    
    print(f"\n{'='*60}")
    print(f"MiniMax-M2.7战略审查 v3.0 | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    
    # 1. 采集数据
    print("\n[1/5] 采集市场数据...")
    coin_data = {}
    for coin in coins:
        d = get_multi_tf_data(coin)
        coin_data[coin] = d
        print(f"  {coin}: ${d['price']} | RSI_1h={d['rsi_1h']} | ADX={d['adx_1h']} | vol={d['volume']}")
    
    btc_data = coin_data.get('BTC', {})
    btc_regime = get_btc_regime()
    
    # 2. 持仓情况
    print("\n[2/5] 读取持仓...")
    positions = get_positions()
    if positions:
        for coin, pos in positions.items():
            price = coin_data.get(coin, {}).get('price', 0)
            pnl = (price - pos['avgPx']) / pos['avgPx'] * 100 if price and pos['side'] == 'long' else (pos['avgPx'] - price) / pos['avgPx'] * 100 if price else 0
            algos = get_pending_algos(pos['instId'])
            sl = algos.get('sl', {}).get('price', '?') if algos else '?'
            tp = algos.get('tp', {}).get('price', '?') if algos else '?'
            sl_dist = 0
            if sl != '?' and price:
                sl_dist = (price - sl) / price * 100 if pos['side'] == 'long' else (sl - price) / price * 100
            print(f"  {coin}: {pos['side']} {pos['pos']}张 @{pos['avgPx']} | PnL={pnl:.2f}% | SL=${sl}({sl_dist:.1f}%) TP=${tp}")
    else:
        print("  无持仓")
    
    # 3. 采集市场情绪数据
    print("\n[3/x] 采集市场情绪与链上数据...")
    try:
        news = get_news(coins)
        print(f"  📰 新闻: {news[:200] if news else '无'}")
    except Exception as e:
        news = f'新闻获取失败: {e}'
        print(f"  📰 新闻获取失败: {e}")
    try:
        price_data = _get_funding_from_coingecko(coins)
        print(f"  💹 价格/情绪: {price_data[:200] if price_data else '无'}")
    except Exception as e:
        price_data = f'价格数据获取失败: {e}'
    try:
        onchain = get_onchain_sentiment()
        print(f"  ⛓ 链上数据: {onchain[:200] if onchain else '无'}")
    except Exception as e:
        onchain = f'链上数据获取失败: {e}'
        print(f"  ⛓ 链上数据获取失败: {e}")

    # 4. 获取账户权益
    print("\n[3.5/5] 获取账户权益...")
    equity = get_account_equity()
    if equity:
        print(f"  账户USDT权益: ${equity:,.0f}")
    else:
        print(f"  ⚠️ 账户权益获取失败，prompt中将标记为'未知'")

    # 4. MiniMax战略分析
    print("\n[4/5] MiniMax-M2.7战略分析...")
    review = gemma4_review(coin_data, positions, btc_data, news=news, price_data=price_data, onchain=onchain, equity=equity)
    print(f"\n{review[:2000]}...")  # 限制打印长度
    
    # 4. 构建并保存 factor_context.json（核心新增功能）
    print("\n[4/5] 生成 factor_context.json...")
    try:
        ctx = build_factor_context(review, coin_data, positions, btc_regime)
        ctx.save(CTX_PATH)
        print(f"  ✅ 已写入: {CTX_PATH}")
        
        # 如果有紧急级别，同步写入 emergency_stop.json
        if ctx.emergency_stop.level != 'none':
            es = ctx.emergency_stop
            es.affected_coins = [p.coin for p in ctx.current_positions if p.status in ('warning', 'critical')]
            es.until_ts = int((datetime.now().timestamp() + 3600) * 1000)  # 1小时后过期
            if es.action == 'none':
                es.action = {'watch': 'pause_new', 'elevated': 'pause_new', 'high': 'close_affected', 'ultra': 'close_all'}.get(es.level, 'pause_new')
            write_emergency_stop(es, EMERGENCY_PATH)
            print(f"  🚨 紧急干预写入: {EMERGENCY_PATH} (level={es.level})")
        else:
            # 无紧急情况，清除旧的 emergency_stop
            clear_emergency_stop(EMERGENCY_PATH)
    except Exception as e:
        print(f"  ⚠️ 写入上下文失败: {e}，使用默认上下文")
        default_ctx = FactorContext.get_default()
        default_ctx.save(CTX_PATH)
    
    # 4b. 保存IC权重调整（MiniMax基于新闻/情绪/链上数据给出权重建议）
    try:
        structured = parse_structured_output(review)
        adj = structured.get('ic_weights_adjustment', {})
        if adj:
            adj_path = Path.home() / '.hermes/kronos_ic_weights_adjustment.json'
            adj_data = {
                'updated': datetime.now().isoformat(),
                'source': 'MiniMax战略审查',
                'adjustment': adj,
                'raw_review_snippet': review[:500],
            }
            atomic_write_json(adj_path, adj_data, indent=2)
            print(f"  📊 IC权重调整已写入: {adj_path}")
            print(f"     提高: {adj.get('raise', [])} | 降低: {adj.get('lower', [])} | 原因: {adj.get('reason', '')}")
        else:
            # 清除旧的调整
            adj_path = Path.home() / '.hermes/kronos_ic_weights_adjustment.json'
            if adj_path.exists():
                adj_path.unlink()
    except Exception as e:
        print(f"  ⚠️ IC权重调整保存失败: {e}")

    # 5. 安全兜底：亏损>5%直接平仓（独立于上下文的安全网）
    print("\n[5/5] 安全兜底检查（>5%强制止损）...")
    executed = []
    for coin, pos in positions.items():
        price = coin_data.get(coin, {}).get('price', 0)
        if price and pos['avgPx'] > 0:
            pnl = (price - pos['avgPx']) / pos['avgPx'] * 100 if pos['side'] == 'long' else (pos['avgPx'] - price) / pos['avgPx'] * 100
            if pnl < -5:
                print(f"  🚨 紧急止损: {coin} 亏损{pnl:.2f}%")
                ok = close_position(pos['instId'], pos['side'], pos['pos'])
                executed.append(f"{coin}紧急止损: {'成功' if ok else '失败'}")
    
    if executed:
        print("  已执行安全操作:")
        for e in executed:
            print(f"    • {e}")
    else:
        print("  无需安全操作")
    
    return review, executed, ctx if 'ctx' in dir() else None

if __name__ == '__main__':
    try:
        review_text, executed, ctx = hourly_review()
    except Exception as e:
        review_text = ''
        executed = [f'❌ 脚本崩溃: {str(e)[:100]}']
        ctx = None

    # ========== 飞书通知策略 ==========
    # 只在有实际操作、紧急情况或错误时才推飞书
    # 静默内容：正常审查结果 → 本地文件，AI处理
    try:
        from kronos_pilot import push_feishu

        now = datetime.now().strftime('%H:%M')
        push_lines = []

        if executed:
            push_lines.append(f"🤖 Kronos战略审查 | {now}")
            push_lines.append(f"执行了 {len(executed)} 项操作：")
            for e in executed:
                push_lines.append(f"  • {e}")

        # watch 级别 → gemma4 分钟层自动处理，不推飞书
        # elevated 及以上 → 推飞书（有实质风险需要人工知晓）
        if ctx and ctx.emergency_stop.level not in ('none', 'watch'):
            push_lines.append(f"🚨 紧急级别: {ctx.emergency_stop.level}")
            push_lines.append(f"原因: {ctx.emergency_stop.reason}")

        if push_lines:
            push_feishu('\n'.join(push_lines))
    except:
        pass
