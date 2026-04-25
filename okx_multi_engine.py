"""
BTC/BCH 趋势跟踪策略 - 实盘执行版
ADX>23纯多 | EMA(10/30)金叉 | ATR止损1.5x | 5%仓位
"""
import vbt
import pandas as pd
import numpy as np
import json, os, time, requests
from datetime import datetime

# ===== 配置 =====
DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
FEE_TIER = '5'  # Level 5: maker 0.02%, taker 0.05%

COINS = ['BTC', 'BCH']
SIZE_PCT = 0.05  # 5%总资金/币
ADX_TH = 23
EMA_FAST = 10
EMA_SLOW = 30
SL_ATR = 1.5

# ===== 指标计算 =====
def calc_adx(high, low, close, n=14):
    tr1 = high - low
    tr2 = np.abs(high - close.shift())
    tr3 = np.abs(low - close.shift())
    tr = pd.DataFrame({'tr1':tr1,'tr2':tr2,'tr3':tr3}).max(axis=1)
    up = high.diff()
    dn = -low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=low.index)
    atr = tr.rolling(n).mean()
    pdi = 100 * (pdm.rolling(n).mean() / atr)
    mdi = 100 * (mdm.rolling(n).mean() / atr)
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)
    return dx.rolling(n).mean()

def get_signals(ohlc_4h, ohlc_1d):
    """返回当前做多信号"""
    close = ohlc_4h['close']
    high = ohlc_4h['high']
    low = ohlc_4h['low']
    close_1d = ohlc_1d['close']
    
    adx = calc_adx(high, low, close, 14)
    adx_avg = adx.rolling(3).mean()
    
    ema20_1d = close_1d.ewm(span=20, adjust=False).mean()
    ema50_1d = close_1d.ewm(span=50, adjust=False).mean()
    daily_trend_up = (ema20_1d > ema50_1d)
    
    ema10 = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema30 = close.ewm(span=EMA_SLOW, adjust=False).mean()
    
    # 金叉
    bull_cross = (ema10 > ema30) & (ema10.shift(1) <= ema30.shift(1))
    
    # ATR止损
    atr_pct = ((high - low).rolling(14).mean()) / close
    sl_atr = float(atr_pct.iloc[-1] * SL_ATR) if not np.isnan(atr_pct.iloc[-1]) else 0.02
    
    # 信号
    can_long = (adx_avg.iloc[-1] > ADX_TH) and daily_trend_up.iloc[-1] and bull_cross.iloc[-1]
    
    return {
        'can_long': can_long,
        'adx': float(adx_avg.iloc[-1]),
        'ema10': float(ema10.iloc[-1]),
        'ema30': float(ema30.iloc[-1]),
        'close': float(close.iloc[-1]),
        'sl_pct': sl_atr,
        'daily_trend_up': bool(daily_trend_up.iloc[-1]),
        'bull_cross': bool(bull_cross.iloc[-1]),
    }

def check_positions():
    """检查当前持仓"""
    if not OKX_API_KEY:
        return {}
    # OKX持仓查询
    try:
        ts = str(int(time.time() * 1000))
        method = 'GET'
        path = '/api/v5/account/positions?instType=MARGIN'
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
        return resp.json()
    except:
        return {}

def place_order(coin, side, size_usd, stop_pct):
    """下单"""
    if not OKX_API_KEY:
        print(f'  [模拟] {side} {coin} ${size_usd:.0f} 止损{stop_pct:.3f}')
        return None
    
    try:
        ts = str(int(time.time() * 1000))
        method = 'POST'
        path = '/api/v5/trade/order'
        
        # 获取当前价格
        ticker_resp = requests.get(f'https://www.okx.com/api/v5/market/ticker?instId={coin}-USDT', timeout=5)
        price = float(ticker_resp.json()['data'][0]['last'])
        
        # 计算止损价格
        if side == 'buy':
            sl_price = price * (1 - stop_pct)
        else:
            sl_price = price * (1 + stop_pct)
        
        body = json.dumps({
            'instId': f'{coin}-USDT',
            'tdMode': 'isolated',
            'side': side,
            'posSide': 'long',
            'ordType': 'market',
            'sz': str(int(size_usd / price)),
            'slTriggerPx': str(sl_price),
            'slOrdPx': '-1',
        })
        
        sign = hmac.new(OKX_SECRET.encode(), f'{ts}{method}{path}{body}'.encode(), 'sha256').hexdigest()
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json',
        }
        resp = requests.post(f'https://www.okx.com{path}', headers=headers, data=body, timeout=10)
        result = resp.json()
        print(f'  OKX下单: {side} {coin} 结果: {result.get('msg', '成功')}')
        return result
    except Exception as e:
        print(f'  下单失败: {e}')
        return None

def run_scan():
    """每日扫描"""
    print(f"\n{'='*60}")
    print(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"策略: ADX>{ADX_TH}纯多 | EMA({EMA_FAST}/{EMA_SLOW}) | {SIZE_PCT*100:.0f}%仓位")
    print(f"{'='*60}")
    
    results = {}
    
    for coin in COINS:
        try:
            # 加载数据
            df = pd.read_csv(f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv')
            df['timestamp'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
            df = df.set_index('timestamp').sort_index()
            
            ohlc_4h = df[['open','high','low','close']].resample('4h').agg({
                'open':'first','high':'max','low':'min','close':'last'
            })
            ohlc_1d = df[['open','high','low','close']].resample('1d').agg({
                'open':'first','high':'max','low':'min','close':'last'
            })
            
            signals = get_signals(ohlc_4h, ohlc_1d)
            results[coin] = signals
            
            print(f"\n{coin}:")
            print(f"  价格: ${signals['close']:,.0f}")
            print(f"  ADX(4H): {signals['adx']:.1f} (阈值>{ADX_TH})")
            print(f"  EMA10: {signals['ema10']:.0f} EMA30: {signals['ema30']:.0f}")
            print(f"  日线趋势: {'多头' if signals['daily_trend_up'] else '空头'}")
            print(f"  金叉信号: {'是' if signals['bull_cross'] else '否'}")
            print(f"  做多信号: {'✅ 是' if signals['can_long'] else '❌ 否'}")
            print(f"  ATR止损: {signals['sl_pct']:.3f} ({signals['sl_pct']*100:.1f}%)")
            
        except Exception as e:
            print(f"\n{coin}: 数据加载失败 - {e}")
    
    return results

if __name__ == '__main__':
    import hmac
    
    # 如果有API密钥，执行真实交易
    if OKX_API_KEY:
        results = run_scan()
        for coin, sig in results.items():
            if sig['can_long']:
                # 获取账户余额
                print(f"\n  → 考虑开多{coin}")
                # place_order(coin, 'buy', 1000, sig['sl_pct'])  # 取消注释以执行
    else:
        # 只做信号扫描
        results = run_scan()
        print("\n[提示] 设置 OKX_API_KEY/OKX_SECRET/OKX_PASSPHRASE 环境变量以启用实盘交易")
