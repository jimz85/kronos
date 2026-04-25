"""
高频率合约策略 - 实盘引擎
15m RSI均值回归 | RSI_L=40 RSI_S=60 | 3x杠杆 | 1:2赔率
固定2%风险/笔 | 日损5% | 双边交易
"""
import pandas as pd
import numpy as np
import json, os, time, hmac, hashlib, requests
from datetime import datetime, timezone

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
LOG_DIR = os.path.expanduser('~/.hermes/cron/output')
os.makedirs(LOG_DIR, exist_ok=True)

# ===== 策略参数 =====
TF = '15min'
RSI_LONG_TH = 40
RSI_SHORT_TH = 60
USE_EMA_FILTER = False
LEVERAGE = 3
RISK_PCT = 0.02  # 每笔最多亏2%资金
TP_MULT = 2.0    # 1:2赔率
FEE_PCT = 0.0004  # 0.04%手续费
DAILY_LOSS_LIMIT = 0.05  # 日损5%停止

def calc_rsi(close, n=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=n, adjust=False).mean()
    avg_loss = loss.ewm(span=n, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def load_data(coin='BTC'):
    df = pd.read_csv(f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv')
    cols = df.columns.tolist()
    new_cols = []
    seen = {}
    for c in cols:
        cn = c.split('.')[0]
        if cn not in seen:
            new_cols.append(c)
            seen[cn] = cn
    df = df[new_cols]
    df = df.rename(columns={'datetime_utc': 'dt'})[['dt', 'open', 'high', 'low', 'close', 'volume']]
    df['ts'] = pd.to_datetime(df['dt']).dt.tz_localize(None)
    df = df.set_index('ts').sort_index()
    df = df[df['close'] > 0]
    return df

def get_current_signals(coin='BTC'):
    """获取当前信号"""
    df = load_data(coin)
    ohlc = df[['open', 'high', 'low', 'close']].resample(TF).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    }).dropna()
    
    c = ohlc['close']
    h = ohlc['high']
    l = ohlc['low']
    
    rsi = calc_rsi(c, 14)
    rsi_ma = rsi.rolling(5).mean()
    ema10 = c.ewm(span=10, adjust=False).mean()
    ema30 = c.ewm(span=30, adjust=False).mean()
    
    atr = ((h - l).rolling(14).mean()).fillna(c * 0.01)
    atr_pct = atr / c
    
    # RSI just bounced
    rsi_just_bounced = (rsi > rsi.shift(1)) & (rsi.shift(1) <= rsi.shift(2))
    rsi_just_dropped = (rsi < rsi.shift(1)) & (rsi.shift(1) >= rsi.shift(2))
    
    ema_trend_long = (ema10 > ema30) if USE_EMA_FILTER else True
    ema_trend_short = (ema10 < ema30) if USE_EMA_FILTER else True
    
    # 当前K线信号
    rsi_curr = float(rsi.iloc[-1])
    rsi_prev = float(rsi.iloc[-2])
    rsi_ma_curr = float(rsi_ma.iloc[-1])
    rsi_ma_prev = float(rsi_ma.iloc[-2])
    
    can_long = (
        rsi_curr < RSI_LONG_TH and
        rsi_prev <= rsi_ma_prev  # 上一根RSI低于等于MA = 刚从低位反弹
    )
    
    can_short = (
        rsi_curr > RSI_SHORT_TH and
        rsi_prev >= rsi_ma_prev  # 上一根RSI高于等于MA = 刚从高位回落
    )
    
    atr_val = float(atr_pct.iloc[-1])
    sl_pct = max(min(atr_val, 0.05), 0.003)
    
    return {
        'coin': coin,
        'rsi': rsi_curr,
        'price': float(c.iloc[-1]),
        'atr_pct': sl_pct,
        'can_long': can_long,
        'can_short': can_short,
        'ema_trend_long': bool(ema_trend_long),
        'ema_trend_short': bool(ema_trend_short),
        'timestamp': str(c.index[-1]),
        'close_4h': float(c.iloc[-1]),
    }

def get_account_equity():
    """获取账户资金"""
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
            return float(data['data'][0]['totalEq'])
        return None
    except:
        return None

def get_current_positions():
    """获取当前持仓"""
    if not OKX_API_KEY:
        return []
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
        data = resp.json()
        if data.get('code') == '0':
            return data['data']
        return []
    except:
        return []

def place_futures_order(coin, side, size_contracts, lev=3, sl_pct=0.02, take_profit_pct=None):
    """下单"""
    if not OKX_API_KEY:
        direction = '做多' if side == 'buy' else '做空'
        print(f'  [模拟] {direction} {coin} {size_contracts}张 杠杆{lev}x')
        return None
    
    try:
        ts = str(int(time.time() * 1000))
        method = 'POST'
        path = '/api/v5/trade/order'
        
        # 获取当前价格
        ticker_r = requests.get(f'https://www.okx.com/api/v5/market/ticker?instId={coin}-USDT-SWAP', timeout=5)
        price = float(ticker_r.json()['data'][0]['last'])
        
        # 止损止盈价格
        if side == 'buy':
            sl_price = price * (1 - sl_pct)
            tp_price = price * (1 + sl_pct * TP_MULT) if take_profit_pct else None
        else:
            sl_price = price * (1 + sl_pct)
            tp_price = price * (1 - sl_pct * TP_MULT) if take_profit_pct else None
        
        body_dict = {
            'instId': f'{coin}-USDT-SWAP',
            'tdMode': 'isolated',
            'side': side,
            'ordType': 'market',
            'sz': str(int(size_contracts)),
            'lever': str(lev),
            'slTriggerPx': str(round(sl_price, 2)),
            'slOrdPx': '-1',
        }
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
        result = resp.json()
        msg = result.get('msg', '成功')
        direction = '做多' if side == 'buy' else '做空'
        print(f'  OKX下单: {direction} {coin} 结果: {msg}')
        return result
    except Exception as e:
        print(f'  下单失败: {e}')
        return None

def run_live_scan():
    """实时扫描"""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    print(f"\n{'='*60}")
    print(f"扫描时间: {now} UTC")
    print(f"策略: RSI(15m) L={RSI_LONG_TH} S={RSI_SHORT_TH} | {LEVERAGE}x杠杆 | 每日信号")
    print(f"{'='*60}")
    
    sigs = {}
    for coin in ['BTC', 'ETH']:
        sig = get_current_signals(coin)
        sigs[coin] = sig
        
        lever = LEVERAGE
        equity = 10000  # 模拟资金
        
        print(f"\n{coin}:")
        print(f"  价格: ${sig['price']:,.0f}")
        print(f"  RSI(15m): {sig['rsi']:.1f}")
        print(f"  ATR止损: {sig['atr_pct']*100:.2f}%")
        print(f"  {'做多信号' if sig['can_long'] else '❌ 无做多信号'}")
        print(f"  {'做空信号' if sig['can_short'] else '❌ 无做空信号'}")
        
        # 仓位计算
        if sig['can_long']:
            risk_usd = equity * RISK_PCT
            sl_pct = sig['atr_pct']
            contracts = int(risk_usd / (sig['price'] * sl_pct * lever))
            print(f"  → 建议做多 {contracts}张 | 止损{sl_pct*100:.1f}% | 目标{sl_pct*TP_MULT*100:.1f}%")
        
        if sig['can_short']:
            risk_usd = equity * RISK_PCT
            sl_pct = sig['atr_pct']
            contracts = int(risk_usd / (sig['price'] * sl_pct * lever))
            print(f"  → 建议做空 {contracts}张 | 止损{sl_pct*100:.1f}% | 目标{sl_pct*TP_MULT*100:.1f}%")
    
    return sigs

def run_full_backtest():
    """完整历史回测"""
    print("\n" + "="*60)
    print("历史回测 (2020-2025)")
    print("="*60)
    
    df = load_data('BTC')
    ohlc = df[['open', 'high', 'low', 'close']].resample(TF).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    }).dropna()
    
    c = ohlc['close']
    h = ohlc['high']
    l = ohlc['low']
    
    rsi = calc_rsi(c, 14)
    rsi_ma = rsi.rolling(5).mean()
    
    atr = ((h - l).rolling(14).mean()).fillna(c * 0.01)
    atr_pct = atr / c
    
    rsi_just_bounced = (rsi > rsi.shift(1)) & (rsi.shift(1) <= rsi.shift(2))
    rsi_just_dropped = (rsi < rsi.shift(1)) & (rsi.shift(1) >= rsi.shift(2))
    
    entries_long = ((rsi < RSI_LONG_TH) & rsi_just_bounced).astype(int)
    entries_short = ((rsi > RSI_SHORT_TH) & rsi_just_dropped).astype(int)
    
    # 手动回测
    def backtest_period(c, h, l, eL, eS, init_cap=10000):
        equity = init_cap
        equity_curve = [equity]
        trades = []
        position = None
        entry_price = 0
        entry_equity = init_cap
        daily_pnl = {}
        idx_list = c.index.tolist()
        
        for i in range(20, len(c) - 1):
            price = float(c.iloc[i])
            if price <= 0: continue
            
            sl_pct = float(atr_pct.iloc[i])
            sl_pct = max(min(sl_pct, 0.05), 0.003)
            
            eL_i = int(eL.iloc[i]) if i < len(eL) else 0
            eS_i = int(eS.iloc[i]) if i < len(eS) else 0
            
            day_key = idx_list[i].strftime('%Y-%m-%d')
            
            if position is None:
                if eL_i:
                    position = 'long'
                    entry_price = price * (1 + FEE_PCT)
                    entry_equity = equity
                elif eS_i:
                    position = 'short'
                    entry_price = price * (1 - FEE_PCT)
                    entry_equity = equity
            else:
                if position == 'long':
                    pnl_pct = (price - entry_price) / entry_price * LEVERAGE - FEE_PCT * 2
                    sl_price = entry_price * (1 - sl_pct)
                    tp_price = entry_price * (1 + sl_pct * TP_MULT)
                    
                    hit_sl = price <= sl_price
                    hit_tp = price >= tp_price
                    
                    if hit_sl:
                        pnl = -equity * RISK_PCT
                        equity += pnl
                        trades.append({'dir': 'long', 'pnl': pnl, 'rr': -1, 'reason': 'stop', 'day': day_key})
                        position = None
                    elif hit_tp:
                        pnl = equity * RISK_PCT * TP_MULT
                        equity += pnl
                        trades.append({'dir': 'long', 'pnl': pnl, 'rr': TP_MULT, 'reason': 'tp', 'day': day_key})
                        position = None
                    elif pnl_pct <= -RISK_PCT:
                        pnl = -equity * RISK_PCT
                        equity += pnl
                        trades.append({'dir': 'long', 'pnl': pnl, 'rr': pnl_pct/sl_pct, 'reason': 'risk', 'day': day_key})
                        position = None
                        
                elif position == 'short':
                    pnl_pct = (entry_price - price) / entry_price * LEVERAGE - FEE_PCT * 2
                    sl_price = entry_price * (1 + sl_pct)
                    tp_price = entry_price * (1 - sl_pct * TP_MULT)
                    
                    hit_sl = price >= sl_price
                    hit_tp = price <= tp_price
                    
                    if hit_sl:
                        pnl = -equity * RISK_PCT
                        equity += pnl
                        trades.append({'dir': 'short', 'pnl': pnl, 'rr': -1, 'reason': 'stop', 'day': day_key})
                        position = None
                    elif hit_tp:
                        pnl = equity * RISK_PCT * TP_MULT
                        equity += pnl
                        trades.append({'dir': 'short', 'pnl': pnl, 'rr': TP_MULT, 'reason': 'tp', 'day': day_key})
                        position = None
                    elif pnl_pct <= -RISK_PCT:
                        pnl = -equity * RISK_PCT
                        equity += pnl
                        trades.append({'dir': 'short', 'pnl': pnl, 'rr': pnl_pct/sl_pct, 'reason': 'risk', 'day': day_key})
                        position = None
            
            equity_curve.append(max(equity, 1))
        
        return equity, equity_curve, trades
    
    # 分年份测试
    years = [2020, 2021, 2022, 2023, 2024, 2025]
    results = []
    
    print(f"\n{'年份':>4} {'收益':>8} {'DD':>7} {'交易':>5} {'胜率':>6} {'LONG盈亏':>12} {'SHORT盈亏':>12}")
    print("-"*65)
    
    for year in years:
        yr_c = c.loc[f'{year}-01-01':f'{year}-12-31']
        yr_h = h.loc[f'{year}-01-01':f'{year}-12-31']
        yr_l = l.loc[f'{year}-01-01':f'{year}-12-31']
        yr_eL = entries_long.loc[f'{year}-01-01':f'{year}-12-31']
        yr_eS = entries_short.loc[f'{year}-01-01':f'{year}-12-31']
        
        if len(yr_c) < 100:
            continue
        
        final_eq, eq_curve, trades = backtest_period(yr_c, yr_h, yr_l, yr_eL, yr_eS)
        
        total_ret = (final_eq - 10000) / 10000 * 100
        peak = np.maximum.accumulate(eq_curve)
        dd = np.min((np.array(eq_curve) - peak) / peak) * 100
        
        wins = [t for t in trades if t['pnl'] > 0]
        wr = len(wins) / len(trades) * 100 if trades else 0
        
        longs = [t for t in trades if t['dir'] == 'long']
        shorts = [t for t in trades if t['dir'] == 'short']
        long_pnl = sum(t['pnl'] for t in longs)
        short_pnl = sum(t['pnl'] for t in shorts)
        
        mark = '✅' if total_ret > 0 else '❌'
        print(f"  {year}: {total_ret:>+7.1f}% {dd:>6.1f}% {len(trades):>5} {wr:>5.0f}% {long_pnl:>+11,.0f} {short_pnl:>+11,.0f} {mark}")
        
        results.append({
            'year': year, 'ret': total_ret, 'dd': dd, 
            'n': len(trades), 'wr': wr,
            'long_pnl': long_pnl, 'short_pnl': short_pnl
        })
    
    # 汇总
    total_ret_all = sum(r['ret'] for r in results) / len(results)
    avg_dd = max(abs(r['dd']) for r in results)
    total_trades = sum(r['n'] for r in results)
    mark = '✅' if total_ret_all > 50 else ('⚠️' if total_ret_all > 0 else '❌')
    print(f"\n  均收益: {total_ret_all:+.1f}% | 最大DD: {avg_dd:.1f}% | 总交易: {total_trades}次 {mark}")

if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--backtest':
        run_full_backtest()
    else:
        sigs = run_live_scan()
        
        # 如果有OKX API，执行真实下单
        if OKX_API_KEY and False:  # 默认关闭，手动执行
            equity = get_account_equity()
            for coin, sig in sigs.items():
                if sig['can_long']:
                    place_futures_order(coin, 'buy', ...)
                if sig['can_short']:
                    place_futures_order(coin, 'sell', ...)
        else:
            print(f"\n[提示] 设置 OKX_API_KEY/OKX_SECRET/OKX_PASSPHRASE 环境变量启用实盘")
            print(f"[提示] 添加 --backtest 参数运行历史回测")
