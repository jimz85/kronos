"""
多品种趋势跟踪扫描器 - 人类交易员式 (优化版)
================================================================
核心逻辑：像人一样交易，根据盘面判断调整仓位

优化：
- 数据缓存60秒，避免重复读CSV
- 只保留最近2000行（足够15min+1h指标计算）
- API超时10秒，避免挂死
- 扫描顺序优化：先检查持仓→再分析机会
"""
import pandas as pd
import numpy as np
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
import requests, hmac, hashlib, base64

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
LOG_DIR = os.path.expanduser('~/.hermes/cron/output')
def load_data(coin):
    """加载数据（不用缓存，每次重新读取）"""
    fpath = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    if not os.path.exists(fpath): return None
    
    # 用wc -l快速计算行数，只读最后2000行
    total_lines = int(os.popen(f'wc -l < "{fpath}"').read().strip()) - 1
    skip = max(0, total_lines - 2000)
    df = pd.read_csv(fpath, skiprows=range(1, skip+1) if skip > 0 else None)
    
    cols = df.columns.tolist()
    col_map = {}
    for c in cols:
        cn = c.split('.')[0]
        if cn == 'vol': cn = 'volume'
        if cn not in col_map: col_map[c] = cn
    df = df.rename(columns=col_map)
    
    # 找时间列
    dt_col = next((c for c in cols if c in ('datetime_utc','datetime','date') or 'datetime' in c.lower()), cols[0])
    vol_col = next((c for c in cols if 'vol' in c.lower()), None)
    
    result = pd.DataFrame()
    result['open'] = df['open'].values if 'open' in df.columns else 0
    result['high'] = df['high'].values if 'high' in df.columns else 0
    result['low'] = df['low'].values if 'low' in df.columns else 0
    result['close'] = df['close'].values if 'close' in df.columns else df.iloc[:,1].values
    result['volume'] = df[vol_col].values if vol_col and vol_col in df.columns else 0
    result['ts'] = pd.to_datetime(df[dt_col], errors='coerce').dt.tz_localize(None)
    result = result.dropna(subset=['ts'])
    result = result[result['close'] > 0]
    if len(result) < 200: return None
    return result.set_index('ts').sort_index()

# ===== 参数 =====
TF_ENTRY = '15min'
TF_TREND = '1h'
LEVERAGE_DEFAULT = 2
POSITION_PCT_DEFAULT = 0.01

COINS = ['BTC', 'ETH', 'BNB', 'DOGE', 'ADA', 'AVAX']

# ===== OKX API =====
def okx_api(method, path, body=''):
    API_KEY = os.getenv('OKX_API_KEY', '')
    SECRET = os.getenv('OKX_SECRET', '')
    PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
    if not API_KEY: return None
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
    msg = ts + method + path + body
    mac = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256)
    sig = base64.b64encode(mac.digest()).decode()
    h = {'OK-ACCESS-KEY': API_KEY, 'OK-ACCESS-SIGN': sig,
         'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': PASSPHRASE,
         'Content-Type': 'application/json', 'x-simulated-trading': '1'}
    url = 'https://www.okx.com' + path
    try:
        if body:
            r = requests.post(url, headers=h, data=body, timeout=10)
        else:
            r = requests.get(url, headers=h, timeout=10)
        return r
    except requests.exceptions.Timeout:
        return None
    except Exception:
        return None

def get_balance():
    resp = okx_api('GET', '/api/v5/account/balance?ccy=USDT')
    if resp and resp.status_code == 200:
        data = resp.json()
        if data.get('code') == '0':
            return float(data['data'][0]['totalEq'])
    return None

def get_positions():
    resp = okx_api('GET', '/api/v5/account/positions')
    if resp and resp.json().get('code') == '0':
        return [p for p in resp.json().get('data', []) if float(p.get('pos', 0)) != 0]
    return []

def get_current_prices(coins):
    """批量获取当前价格"""
    prices = {}
    for coin in coins:
        try:
            r = requests.get(f'https://www.okx.com/api/v5/market/ticker?instId={coin}-USDT-SWAP', timeout=5)
            prices[coin] = float(r.json()['data'][0]['last'])
        except:
            prices[coin] = None
    return prices

def calc_rsi(close, n=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=n, adjust=False).mean()
    avg_loss = loss.ewm(span=n, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_atr(high, low, close, n=14):
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def calc_adx(ohlc, n=14):
    h, l, c = ohlc['high'], ohlc['low'], ohlc['close']
    tr1, tr2, tr3 = h-l, (h-c.shift()).abs(), (l-c.shift()).abs()
    tr = pd.concat([tr1,tr2,tr3], axis=1).max(axis=1)
    atr = tr.rolling(n).mean()
    up = h.diff(); dn = -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    plus_di = 100 * (plus_dm.rolling(n).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(n).mean() / atr)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    return dx.rolling(n).mean()

Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

def resample(df, rule):
    return df[['open','high','low','close','volume']].resample(rule).agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum'
    }).dropna()

def analyze_coin(coin, positions, curr_price, hyper_oversold=False):
    """分析单个币种，返回信号
    hyper_oversold: 系统性超卖模式（BTC RSI<40 且 ≥3币种 RSI<40）
    """
    df = load_data(coin)
    if df is None or len(df) < 200: return None
    
    ohlc_entry = resample(df, TF_ENTRY)
    ohlc_trend = resample(df, TF_TREND)
    
    if len(ohlc_entry) < 50 or len(ohlc_trend) < 50: return None
    
    # 趋势
    c_trend = ohlc_trend['close']
    ema20_trend = c_trend.ewm(span=20, adjust=False).mean()
    ema50_trend = c_trend.ewm(span=50, adjust=False).mean()
    adx_trend = calc_adx(ohlc_trend)
    
    # 入场指标
    c = ohlc_entry['close']
    h = ohlc_entry['high']
    l = ohlc_entry['low']
    v = ohlc_entry['volume']
    
    rsi = calc_rsi(c, 14)
    atr = calc_atr(h, l, c, 14)
    atr_pct = atr / c
    atr_ma = atr_pct.rolling(20).mean()
    atr_ratio = atr_pct / (atr_ma + 1e-10)
    vol_ma = v.rolling(20).mean()
    vol_ratio = v / (vol_ma + 1e-10)
    
    rsi_curr = float(rsi.iloc[-1])
    rsi_prev = float(rsi.iloc[-2])
    rsi_ma_curr = float(rsi.rolling(5).mean().iloc[-1])
    rsi_ma_prev = float(rsi.rolling(5).mean().shift(1).iloc[-1])
    atr_ratio_curr = float(atr_ratio.iloc[-1])
    vol_ratio_curr = float(vol_ratio.iloc[-1])
    adx_curr = float(adx_trend.iloc[-1])
    price = float(c.iloc[-1])
    atr_pct_curr = float(atr_pct.iloc[-1])
    
    is_trend_up = bool(ema20_trend.iloc[-1] > ema50_trend.iloc[-1])
    is_trend_down = bool(ema20_trend.iloc[-1] < ema50_trend.iloc[-1])
    vol_blocked = atr_ratio_curr > 3.0 or atr_ratio_curr < 0.3
    
    # 持仓信息
    pos_side = None
    pos_contracts = 0
    for p in positions:
        if p.get('instId', '').startswith(coin):
            pos_side = p.get('posSide', '')
            pos_contracts = float(p.get('pos', 0))
    
    has_long = pos_side == 'long' and pos_contracts > 0
    has_short = pos_side == 'short' and pos_contracts > 0
    
    result = {
        'coin': coin, 'price': price, 'curr_price': curr_price,
        'rsi': rsi_curr, 'adx': adx_curr, 'atr_ratio': atr_ratio_curr,
        'vol_ratio': vol_ratio_curr, 'atr_pct': atr_pct_curr,
        'trend': 'up' if is_trend_up else ('down' if is_trend_down else 'neutral'),
        'vol_blocked': vol_blocked,
        'has_long': has_long, 'has_short': has_short,
        'pos_contracts': pos_contracts, 'pos_side': pos_side,
        'hyper_oversold': hyper_oversold,
    }
    
    # 动态止损止盈
    if atr_pct_curr < 0.0005:
        sl_pct = 0.005
    elif atr_pct_curr > 0.002:
        sl_pct = 0.02
    else:
        sl_pct = 0.01
    tp_pct = sl_pct * 3
    
    result['sl_pct'] = sl_pct
    result['tp_pct'] = tp_pct
    result['rr'] = round(tp_pct / sl_pct, 1)
    
    # ===== 入场评估 =====
    can_enter = False
    entry_lev = LEVERAGE_DEFAULT
    entry_pct = POSITION_PCT_DEFAULT
    entry_reason = ''
    entry_dir = None
    is_hyper_signal = False  # 标记是否为超卖专用信号
    
    rsi_bounce = (rsi_curr >= rsi_prev) and (rsi_prev < rsi_ma_prev)
    rsi_extreme = rsi_curr < 25
    rsi_oversold = rsi_curr < 35
    
    if not vol_blocked and result['rr'] >= 2.0:
        # 大机会
        if (rsi_extreme or rsi_curr < 28) and is_trend_up and adx_curr > 30 and vol_ratio_curr > 0.8:
            can_enter = True
            entry_pct = 0.02
            entry_lev = 3
            entry_dir = 'long'
            entry_reason = f'🟢 大机会: RSI={rsi_curr:.1f}<28 + ADX={adx_curr:.0f}>30 + 量比={vol_ratio_curr:.1f}'
        # 标准机会
        elif rsi_oversold and is_trend_up and rsi_bounce:
            can_enter = True
            entry_pct = 0.01
            entry_lev = 2
            entry_dir = 'long'
            entry_reason = f'🟡 标准: RSI={rsi_curr:.1f}<35反弹 + 趋势多头 + ADX={adx_curr:.0f}'
        # 试探
        elif rsi_curr < 40 and is_trend_up and adx_curr > 20:
            can_enter = True
            entry_pct = 0.005
            entry_lev = 1
            entry_dir = 'long'
            entry_reason = f'🔵 试探: RSI={rsi_curr:.1f}<40 + 趋势多头'
        # 做空
        elif is_trend_down and rsi_curr > 65 and adx_curr > 25:
            can_enter = True
            entry_pct = 0.01
            entry_lev = 2
            entry_dir = 'short'
            entry_reason = f'🟠 做空: RSI={rsi_curr:.1f}>65超买 + 趋势空头 + ADX={adx_curr:.0f}'
    
    # ===== 系统性超卖加仓规则（临时例外） =====
    # 仅当：超卖环境 AND 无持仓 AND 非BTC/ETH/BNB AND (DOGE需RSI<35)
    NO_ADD_COINS = {'BTC', 'ETH', 'BNB'}
    if hyper_oversold and not can_enter:
        if coin not in NO_ADD_COINS and not has_long and not has_short:
            # DOGE: 必须 RSI<35 才能做空
            if coin == 'DOGE' and is_trend_down and rsi_curr < 35 and adx_curr > 20:
                can_enter = True
                entry_pct = 0.0025  # 原仓位1/4
                entry_lev = 1
                entry_dir = 'short'
                entry_reason = f'🚨 超卖加仓-DOGE: RSI={rsi_curr:.1f}<35 + 趋势空头'
                is_hyper_signal = True
            # 一般币种: RSI<40 + 趋势UP
            elif coin != 'DOGE' and is_trend_up and rsi_curr < 40 and adx_curr > 15:
                can_enter = True
                entry_pct = 0.005  # 原仓位1/2
                entry_lev = 1
                entry_dir = 'long'
                entry_reason = f'🚨 超卖加仓: RSI={rsi_curr:.1f}<40 + 趋势多头'
                is_hyper_signal = True
    
    result['can_enter'] = can_enter
    result['entry_pct'] = entry_pct
    result['entry_lev'] = entry_lev
    result['entry_reason'] = entry_reason
    result['entry_dir'] = entry_dir
    result['is_hyper_signal'] = is_hyper_signal
    # ===== 补单 =====
    can_add = False
    add_reason = ''
    if has_long and is_trend_up and adx_curr > 35 and rsi_curr < 40 and rsi_curr > rsi_prev:
        can_add = True
        add_reason = f'补多: ADX={adx_curr:.0f}>35 + RSI={rsi_curr:.1f}回调'
    elif has_short and is_trend_down and adx_curr > 35 and rsi_curr > 60:
        can_add = True
        add_reason = f'补空: ADX={adx_curr:.0f}>35 + RSI={rsi_curr:.1f}反弹'
    
    result['can_add'] = can_add
    result['add_reason'] = add_reason
    
    # ===== 平仓 =====
    should_exit = False
    exit_reason = ''
    if has_long:
        if rsi_curr < 18:
            should_exit = True
            exit_reason = f'平多: RSI={rsi_curr:.1f}<18极端'
        elif adx_curr < 15:
            should_exit = True
            exit_reason = f'平多: ADX={adx_curr:.0f}<15趋势衰竭'
    elif has_short:
        if rsi_curr > 85:
            should_exit = True
            exit_reason = f'平空: RSI={rsi_curr:.1f}>85极端'
        elif adx_curr < 15:
            should_exit = True
            exit_reason = f'平空: ADX={adx_curr:.0f}<15趋势衰竭'
    
    result['should_exit'] = should_exit
    result['exit_reason'] = exit_reason
    
    return result

def execute_trade(coin, side, size_contracts, lev, is_add=False):
    """执行交易"""
    pos_side = 'long' if side == 'buy' else 'short'
    
    open_body = json.dumps({
        'instId': f'{coin}-USDT-SWAP', 'tdMode': 'isolated',
        'side': side, 'ordType': 'market',
        'sz': str(int(size_contracts)), 'lever': str(lev),
        'posSide': pos_side,
    })
    resp = okx_api('POST', '/api/v5/trade/order', open_body)
    if not resp or resp.json().get('code') != '0':
        err_data = resp.json() if resp else {}
        print(f"  ❌ {'补单' if is_add else '开仓'}失败: {err_data}")
        return None, err_data  # 返回错误详情供重试判断
    
    ord_id = resp.json()['data'][0]['ordId']
    print(f"  ✅ {'补单' if is_add else '开仓'}成功! ID: {ord_id}")
    
    import time; time.sleep(1)
    
    # 获取成交均价
    fill_resp = okx_api('GET', f'/api/v5/trade/fills?ordId={ord_id}&instId={coin}-USDT-SWAP')
    avg_price = None
    if fill_resp and fill_resp.json().get('code') == '0' and fill_resp.json().get('data'):
        avg_price = float(fill_resp.json()['data'][0]['fillPx'])
        print(f"  成交: ${avg_price:,.4f}")
    
    if avg_price:
        curr_price = get_current_prices([coin])[coin]
        if side == 'buy':
            sl_p = round(curr_price * 0.99, 4)
            tp_p = round(curr_price * 1.03, 4)
            close_side = 'sell'
        else:
            sl_p = round(curr_price * 1.01, 4)
            tp_p = round(curr_price * 0.97, 4)
            close_side = 'buy'
        
        algo_body = json.dumps({
            'instId': f'{coin}-USDT-SWAP', 'tdMode': 'isolated',
            'side': close_side, 'ordType': 'conditional',
            'sz': str(int(size_contracts)), 'posSide': pos_side,
            'slTriggerPx': str(sl_p), 'slOrdPx': '-1', 'slTriggerPxType': 'last',
            'tpTriggerPx': str(tp_p), 'tpOrdPx': '-1', 'tpTriggerPxType': 'last',
        })
        algo_resp = okx_api('POST', '/api/v5/trade/order-algo', algo_body)
        if algo_resp and algo_resp.json().get('code') == '0':
            print(f"  ✅ 止损${sl_p} 止盈${tp_p}")
        else:
            print(f"  ⚠️ 止损止盈失败: {algo_resp.json() if algo_resp else 'None'}")
    
    return True, ord_id

def close_position(coin, pos_side, size_contracts):
    side = 'sell' if pos_side == 'long' else 'buy'
    resp = okx_api('POST', '/api/v5/trade/order', json.dumps({
        'instId': f'{coin}-USDT-SWAP', 'tdMode': 'isolated',
        'side': side, 'ordType': 'market',
        'sz': str(int(size_contracts)), 'posSide': pos_side,
    }))
    if resp and resp.json().get('code') == '0':
        print(f"  ✅ 平仓成功! ID: {resp.json()['data'][0]['ordId']}")
        return True
    print(f"  ❌ 平仓失败: {resp.json() if resp else 'None'}")
    return False

def feishu_notify(message):
    try:
        import requests as _req
        app_id = os.getenv('FEISHU_APP_ID', '')
        app_secret = os.getenv('FEISHU_APP_SECRET', '')
        if not app_id: return
        token_resp = _req.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': app_id, 'app_secret': app_secret}, timeout=5
        )
        if token_resp.json().get('code') != 0: return
        token = token_resp.json()['tenant_access_token']
        _req.post(
            'https://open.feishu.cn/open-apis/im/v1/messages',
            params={'receive_id_type': 'chat_id'},
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'receive_id': 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0',
                  'msg_type': 'text', 'content': json.dumps({'text': message})}, timeout=5
        )
    except: pass

def log_trade(trade_data):
    log_file = Path(LOG_DIR) / 'trades_log.json'
    logs = []
    if log_file.exists():
        try: logs = json.loads(log_file.read_text())
        except: pass
    logs.append({**trade_data, 'logged_at': datetime.now(timezone.utc).isoformat()})
    log_file.write_text(json.dumps(logs[-200:], indent=2))

def scan_and_report(balance, positions, prices):
    """扫描并生成报告"""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    print(f"\n{'='*60}")
    print(f"多品种趋势扫描 | {now} UTC | 余额: ${balance:,.2f}")
    print(f"{'='*60}")
    
    # ===== 第一步：快速RSI扫描，检测系统性超卖 =====
    rsi_map = {}  # {coin: rsi_value}
    for coin in COINS:
        try:
            df = load_data(coin)
            if df is None or len(df) < 200: continue
            ohlc_entry = resample(df, TF_ENTRY)
            if len(ohlc_entry) < 50: continue
            c = ohlc_entry['close']
            rsi = calc_rsi(c, 14)
            rsi_map[coin] = float(rsi.iloc[-1])
        except:
            pass
    
    btc_rsi = rsi_map.get('BTC', 99)
    oversold_coins = sum(1 for c, r in rsi_map.items() if r < 40)
    hyper_oversold = (btc_rsi < 40 and oversold_coins >= 3)
    
    if hyper_oversold:
        print(f"\n🚨 系统性超卖环境 | BTC RSI={btc_rsi:.1f} | {oversold_coins}个币种RSI<40")
        print(f"   【临时例外规则生效】允许最多2个新币种入场（仓位1/2，止损1.5%）")
    
    # ===== 第二步：完整分析 =====
    results = []
    for coin in COINS:
        try:
            r = analyze_coin(coin, positions, prices.get(coin), hyper_oversold=hyper_oversold)
            if r: results.append(r)
        except Exception as e:
            print(f"\n❌ {coin}: 失败 - {e}")
    
    def score(r):
        if r['should_exit']: return 20
        if r['can_add']: return 10
        if not r['can_enter']: return 0
        return r['entry_pct'] * r['entry_lev']
    
    results.sort(key=score, reverse=True)
    
    print()
    for r in results:
        trend_icon = {'up': '📈', 'down': '📉', 'neutral': '⚖️'}.get(r['trend'], '⚖️')
        price_str = f'${r["price"]:,.4f}' if r['price'] < 1 else f'${r["price"]:,.0f}'
        
        print(f"{trend_icon} {r['coin']}: {price_str}")
        print(f"  趋势:{r['trend'].upper():8s} | RSI={r['rsi']:.1f} | ADX={r['adx']:.1f} | ATR={r['atr_ratio']:.2f}x | Vol={r['vol_ratio']:.2f}x | RR={r['rr']}:1")
        
        if r['has_long'] or r['has_short']:
            pos_dir = '多' if r['has_long'] else '空'
            print(f"  📌 持仓: {r['pos_contracts']:.0f}张{pos_dir}")
        
        if r['should_exit']:
            print(f"  🚨 平仓: {r['exit_reason']}")
        elif r['can_add']:
            print(f"  🟡 补单: {r['add_reason']}")
        elif r['can_enter']:
            print(f"  ✅ 入场: {r['entry_reason']}")
            print(f"     仓位:{r['entry_pct']*100:.1f}% × {r['entry_lev']}x")
        else:
            reason = []
            if r['vol_blocked']: reason.append(f'波动率{r["atr_ratio"]:.1f}x异常')
            elif r['rr'] < 2.0: reason.append(f'赔率{r["rr"]:.1f}<2.0')
            else: reason.append('无信号')
            print(f"  ❌ {reason[0]}")
    
    return results

def main():
    import time
    
    LIVE_MODE = '--live' in sys.argv
    
    balance = get_balance()
    if not balance:
        print("❌ 无法获取余额")
        return
    
    print(f"余额: ${balance:,.2f}")
    
    positions = get_positions()
    if positions:
        print(f"\n持仓:")
        for p in positions:
            inst = p.get('instId','').replace('-USDT-SWAP','')
            upl = float(p.get('upl', 0))
            print(f"  {inst}: {p['pos']}张 {p['posSide']} @ ${p.get('avgPx','?')} | 浮亏:${upl:.2f}")
    
    # 批量获取价格
    prices = get_current_prices(COINS)
    results = scan_and_report(balance, positions, prices)
    
    if not LIVE_MODE:
        print("\n(使用 --live 开启实盘)")
        return
    
    # ===== 执行 =====
    # scan_and_report已经返回了results，这里直接用
    if not results: return
    
    new_entry_count_global = {'count': 0}  # 超卖模式新币种计数
    bal = get_balance()
    if not bal: return
    positions = get_positions()  # 重新获取最新持仓
    
    for r in results:
        coin = r['coin']
        
        # 平仓
        if r['should_exit']:
            print(f"\n🚨 平仓: {coin} {r['exit_reason']}")
            pos = next((p for p in positions if p.get('instId','').startswith(coin)), None)
            if pos:
                ok = close_position(coin, pos['posSide'], pos['pos'])
                if ok:
                    feishu_notify(f"🚨 自动平仓\n\n📌 {coin}\n📌 {r['exit_reason']}\n\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            continue
        
        # 补单
        if r['can_add']:
            pos = next((p for p in positions if p.get('instId','').startswith(coin)), None)
            if pos and float(pos.get('pos', 0)) > 0:
                print(f"\n🟡 补单: {coin} - {r['add_reason']}")
                size = max(int(bal * r['entry_pct'] / r['price']), 1)
                side = 'buy' if pos['posSide'] == 'long' else 'sell'
                ok, _ = execute_trade(coin, side, size, r['entry_lev'], is_add=True)
                if ok:
                    feishu_notify(f"🟡 补单\n\n📌 {coin}\n📌 {r['add_reason']}\n📌 张数: {size}\n\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            continue
        
        # 入场
        if r['can_enter']:
            pos = next((p for p in positions if p.get('instId','').startswith(coin)), None)
            if pos and float(pos.get('pos', 0)) > 0:
                print(f"\n⚠️ {coin}已有持仓，跳过")
                continue
            
            print(f"\n✅ 入场: {coin}")
            print(f"   {r['entry_reason']}")
            print(f"   仓位: {r['entry_pct']*100:.1f}% = ${bal*r['entry_pct']:.2f}")
            print(f"   杠杆: {r['entry_lev']}x")
            
            size = max(int(bal * r['entry_pct'] / r['price']), 1)
            side = 'buy' if r['entry_dir'] == 'long' else 'sell'
            
            ok, err = execute_trade(coin, side, size, r['entry_lev'])
            if ok:
                feishu_notify(
                    f"✅ 自动开仓\n\n📌 {coin}\n📌 方向: {'做多' if side=='buy' else '做空'}\n"
                    f"📌 仓位: {r['entry_pct']*100:.1f}% | 杠杆: {r['entry_lev']}x\n📌 张数: {size}\n"
                    f"📌 理由: {r['entry_reason']}\n\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
                )
                log_trade({
                    'coin': coin, 'direction': side,
                    'entry_price': r['price'],
                    'size': size, 'lev': r['entry_lev'],
                    'balance': bal,
                })
                if hyper_oversold:
                    new_entry_count = new_entry_count_global.get('count', 0) + 1
                    new_entry_count_global['count'] = new_entry_count
                    if new_entry_count >= 2:
                        print(f"\n🚨 超卖模式已达2个新币种上限，停止开仓")
                        break
                else:
                    break  # 非超卖模式，一次只做一个
            elif err and err.get('code') == '1' and err.get('data', [{}])[0].get('sCode') == '54031':
                # ADA等超卖币种被拒，尝试更小仓位
                small_size = max(int(bal * r['entry_pct'] * 0.5 / r['price']), 1)
                print(f"\n🟡 超卖币种被拒，尝试更小仓位: {small_size}张")
                ok2, _ = execute_trade(coin, side, small_size, r['entry_lev'])
                if ok2:
                    new_entry_count = new_entry_count_global.get('count', 0) + 1
                    new_entry_count_global['count'] = new_entry_count
                    if new_entry_count >= 2:
                        print(f"\n🚨 超卖模式已达2个新币种上限，停止开仓")
                        break

    return results

if __name__ == '__main__':
    main()
