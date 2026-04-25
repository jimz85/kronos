#!/usr/bin/env python3
"""
类人盘感Agent - Phase 1 实盘扫描器
Cron Job: 每3分钟运行一次
波动率状态过滤 + RSI均值回归 + 飞书通知
"""
import pandas as pd
import numpy as np
import json, os, sys, time, hmac, hashlib, requests
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
LOG_DIR = os.path.expanduser('~/.hermes/cron/output')
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

# ===== 策略参数 =====
TF_RULE = '15min'
RSI_LONG_TH = 40
RSI_SHORT_TH = 60
LEVERAGE = 3
POSITION_PCT = 0.01       # 总资金1%入场
RISK_PCT = 0.01          # 单笔风险1%
TP_MULT = 2.0             # 1:2赔率

# 波动率过滤
ATR_UPPER = 2.5   # ATR > 2.5x均值 → 禁止开仓（太妖）
ATR_LOWER = 0.5   # ATR < 0.5x均值 → 禁止开仓（太闷）

# ===== OKX API =====
OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
HOME_CHAT_ID = 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0'

def calc_rsi(close, n=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=n, adjust=False).mean()
    avg_loss = loss.ewm(span=n, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def load_data(coin='BTC'):
    """加载并预处理数据"""
    df = pd.read_csv(f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv')
    cols = df.columns.tolist()
    
    # 统一列名
    col_map = {}
    for c in cols:
        cn = c.split('.')[0]
        if cn == 'vol' and 'volume' not in col_map:
            col_map[c] = 'volume'
        elif cn not in col_map:
            col_map[c] = cn
    
    df = df.rename(columns=col_map)
    
    # 处理datetime列
    if 'datetime_utc' in df.columns:
        dt_col = 'datetime_utc'
    elif 'datetime' in df.columns:
        dt_col = 'datetime'
    elif 'timestamp' in df.columns:
        dt_col = 'timestamp'
    else:
        dt_col = cols[0]
    
    # 确保有必要的列
    needed = ['open', 'high', 'low', 'close', 'volume']
    for col in needed:
        if col not in df.columns:
            df[col] = df[needed[0]]  # fallback
    
    df['ts'] = pd.to_datetime(df[dt_col]).dt.tz_localize(None)
    df = df.set_index('ts').sort_index()
    df = df[df['close'] > 0]
    return df

def get_atr_filter(ohlc):
    """波动率状态过滤"""
    h = ohlc['high']
    l = ohlc['low']
    c = ohlc['close']
    atr = ((h - l).rolling(14).mean()).fillna(c * 0.01)
    atr_ma = atr.rolling(20).mean().iloc[-1]
    atr_curr = atr.iloc[-1]
    ratio = atr_curr / (atr_ma + 1e-10)
    return ratio, ATR_UPPER, ATR_LOWER

def scan_coin(coin='BTC'):
    """扫描单个币种"""
    df = load_data(coin)
    ohlc = df[['open', 'high', 'low', 'close', 'volume']].resample(TF_RULE).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()
    
    if len(ohlc) < 50:
        return None
    
    c = ohlc['close']
    h = ohlc['high']
    l = ohlc['low']
    v = ohlc['volume']
    
    # 指标
    rsi = calc_rsi(c, 14)
    atr = ((h - l).rolling(14).mean()).fillna(c * 0.01)
    atr_pct = atr / c
    
    # 波动率过滤
    atr_ratio, atr_upper, atr_lower = get_atr_filter(ohlc)
    vol_blocked = atr_ratio > atr_upper or atr_ratio < atr_lower
    
    # RSI信号
    rsi_curr = float(rsi.iloc[-1])
    rsi_prev = float(rsi.iloc[-2])
    rsi_ma = rsi.rolling(5).mean()
    rsi_ma_curr = float(rsi_ma.iloc[-1])
    rsi_ma_prev = float(rsi_ma.iloc[-2])
    
    # ATR当前值
    atr_val = float(atr_pct.iloc[-1])
    sl_pct = max(min(atr_val * 2.0, 0.08), 0.01)  # 止损2xATR
    tp_pct = sl_pct * TP_MULT
    
    # 当前价格
    price = float(c.iloc[-1])
    ts = str(c.index[-1])
    
    # 订单流简化评分
    recent_vols = v.iloc[-10:].values
    avg_vol = np.mean(recent_vols)
    vol_score = recent_vols[-1] / (avg_vol + 1e-10)  # 放量>1,缩量<1
    
    # 买入量估算
    buy_ratio = 0.52
    buy_vol = float(v.iloc[-1]) * buy_ratio
    sell_vol = float(v.iloc[-1]) * (1 - buy_ratio)
    
    order_flow = (buy_vol - sell_vol) / (float(v.iloc[-1]) + 1e-10)  # 正=偏多
    
    results = {}
    
    # ===== 做多信号 =====
    can_long = (
        not vol_blocked and
        rsi_curr < RSI_LONG_TH and
        rsi_prev <= rsi_ma_prev  # 刚从低位起来
    )
    
    if can_long:
        # 仓位计算
        position_size = POSITION_PCT  # 总资金1%
        contracts = int(position_size * 10000 / price)
        if contracts < 1:
            contracts = 1  # 至少1张
        
        sl_price = price * (1 - sl_pct)
        tp_price = price * (1 + tp_pct)
        
        results['long'] = {
            'signal': True,
            'price': price,
            'rsi': rsi_curr,
            'atr_ratio': atr_ratio,
            'order_flow': order_flow,
            'sl_price': round(sl_price, 2),
            'tp_price': round(tp_price, 2),
            'sl_pct': round(sl_pct * 100, 2),
            'tp_pct': round(tp_pct * 100, 2),
            'contracts': contracts,
            'leverage': LEVERAGE,
            'ts': ts,
        }
    else:
        reason = []
        if vol_blocked:
            regime = '过激' if atr_ratio > atr_upper else '过低'
            reason.append(f'波动率{atr_ratio:.1f}x均值{regime}')
        if rsi_curr >= RSI_LONG_TH:
            reason.append(f'RSI={rsi_curr:.1f}>={RSI_LONG_TH}')
        if rsi_prev > rsi_ma_prev:
            reason.append('RSI未超卖')

        results['long'] = {
            'signal': False,
            'reason': ' | '.join(reason) if reason else '条件未满足',
            'rsi': rsi_curr,
            'atr_ratio': atr_ratio,
            'price': price,
            'ts': ts,
        }
    
    # ===== 做空信号（仅观察，不建议开仓）=====
    can_short = (
        not vol_blocked and
        rsi_curr > RSI_SHORT_TH and
        rsi_prev >= rsi_ma_prev
    )
    results['short'] = {
        'signal': can_short,
        'rsi': rsi_curr,
        'atr_ratio': atr_ratio,
        'price': price,
        'ts': ts,
    }
    
    return results

def get_okx_balance():
    """获取OKX USDT余额"""
    if not OKX_API_KEY:
        return None
    try:
        ts = str(int(time.time() * 1000))
        method = 'GET'
        path = '/api/v5/account/balance?ccy=USDT'
        body = ''
        sign = hmac.new(OKX_SECRET.encode(), f'{ts}{method}{path}{body}'.encode(), 'sha256').hexdigest()
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
        }
        resp = requests.get(f'https://www.okx.com{path}', headers=headers, timeout=10)
        data = resp.json()
        if data.get('code') == '0':
            for info in data['data'][0]['balances']:
                if info['ccy'] == 'USDT':
                    return float(info['availBal'])
        return None
    except:
        return None

def place_order(coin, side, size_contracts, lev=3, sl_price=None, tp_price=None):
    """下单"""
    if not OKX_API_KEY:
        return None
    try:
        ts = str(int(time.time() * 1000))
        method = 'POST'
        path = '/api/v5/trade/order'
        
        body_dict = {
            'instId': f'{coin}-USDT-SWAP',
            'tdMode': 'isolated',
            'side': side,
            'ordType': 'market',
            'sz': str(int(size_contracts)),
            'lever': str(lev),
        }
        if sl_price:
            body_dict['slTriggerPx'] = str(round(sl_price, 2))
            body_dict['slOrdPx'] = '-1'
        if tp_price:
            body_dict['tpTriggerPx'] = str(round(tp_price, 2))
            body_dict['tpOrdPx'] = '-1'
        
        body = json.dumps(body_dict)
        sign = hmac.new(OKX_SECRET.encode(), f'{ts}{method}{path}{body}'.encode(), 'sha256').hexdigest()
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
        }
        resp = requests.post(f'https://www.okx.com{path}', headers=headers, data=body, timeout=10)
        return resp.json()
    except Exception as e:
        return {'code': '1', 'msg': str(e)}

def feishu_notify(message):
    """发送飞书通知"""
    try:
        import os
        app_id = os.getenv('FEISHU_APP_ID', '')
        app_secret = os.getenv('FEISHU_APP_SECRET', '')
        if not app_id:
            return
        
        # 获取tenant_access_token
        token_resp = requests.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': app_id, 'app_secret': app_secret},
            timeout=10
        )
        token_data = token_resp.json()
        if token_data.get('code') != 0:
            return
        tenant_token = token_data['tenant_access_token']
        
        # 发送消息
        msg_resp = requests.post(
            'https://open.feishu.cn/open-apis/im/v1/messages',
            params={'receive_id_type': 'chat_id'},
            headers={
                'Authorization': f'Bearer {tenant_token}',
                'Content-Type': 'application/json',
            },
            json={
                'receive_id': HOME_CHAT_ID,
                'msg_type': 'text',
                'content': json.dumps({'text': message}),
            },
            timeout=10
        )
    except:
        pass

def log_trade(trade_data):
    """记录交易到本地文件"""
    log_file = Path(LOG_DIR) / 'trades_log.json'
    logs = []
    if log_file.exists():
        try:
            logs = json.loads(log_file.read_text())
        except:
            logs = []
    logs.append({**trade_data, 'logged_at': datetime.now(timezone.utc).isoformat()})
    log_file.write_text(json.dumps(logs[-100:], indent=2))  # 保留最近100条

def run_scan():
    """执行扫描"""
    now = datetime.now(timezone.utc)
    ts_str = now.strftime('%Y-%m-%d %H:%M')
    
    print(f"\n{'='*60}")
    print(f"盘感扫描 | {ts_str} UTC")
    print(f"{'='*60}")
    
    all_results = {}
    vol_names = {True: '❌ 禁止开仓', False: '✅ 正常'}
    
    for coin in ['BTC', 'ETH']:
        try:
            result = scan_coin(coin)
            all_results[coin] = result
            
            r = result['long']
            vol_blocked = r['atr_ratio'] > ATR_UPPER or r['atr_ratio'] < ATR_LOWER
            
            print(f"\n{coin}:")
            print(f"  价格: ${r['price']:,.0f} | RSI={r['rsi']:.1f}")
            print(f"  ATR状态: {r['atr_ratio']:.2f}x均值 | {vol_names[vol_blocked]}")
            
            if r['signal']:
                print(f"  ✅ 做多信号!")
                print(f"    建议张数: {r['contracts']}张 | {r['leverage']}x杠杆")
                print(f"    止损: ${r['sl_price']:,.0f} ({r['sl_pct']}%)")
                print(f"    止盈: ${r['tp_price']:,.0f} ({r['tp_pct']}%)")
            else:
                print(f"  ❌ 无信号: {r['reason']}")
            
            # 记录
            log_trade({
                'coin': coin,
                'ts': ts_str,
                'signal': r['signal'],
                'price': r['price'],
                'rsi': r['rsi'],
                'atr_ratio': r['atr_ratio'],
                'order_flow': r.get('order_flow', 0),
            })
            
        except Exception as e:
            print(f"  {coin}: 扫描失败 - {e}")
    
    # 检测到信号时飞书通知
    signals = []
    for coin, res in all_results.items():
        if res and res['long']['signal']:
            r = res['long']
            msg = (
                f"📈 {coin} 做多信号\n"
                f"价格: ${r['price']:,.0f}\n"
                f"RSI: {r['rsi']:.1f} | ATR: {r['atr_ratio']:.2f}x\n"
                f"止损: ${r['sl_price']:,.0f} ({r['sl_pct']}%)\n"
                f"止盈: ${r['tp_price']:,.0f} ({r['tp_pct']}%)\n"
                f"张数: {r['contracts']}张 | {r['leverage']}x"
            )
            signals.append(msg)
    
    if signals:
        feishu_notify('\n\n'.join(signals))
    
    return all_results

if __name__ == '__main__':
    run_scan()
