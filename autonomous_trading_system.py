#!/usr/bin/env python3
"""
Kronos 自动驾驶交易系统 v1.0
===================================
目标：每天自动分析IC因子状态，生成交易信号，自主执行（或人工确认后执行）

架构：
  1. IC Monitor (每日09:00) → 判断因子有效性
  2. Signal Engine (每小时) → 基于最优因子生成交易信号
  3. Risk Manager → 仓位/止损/熔断
  4. Execution Layer → OKX/Binance API下单
  5. Performance Tracker → 记录每日PnL，评估IC驱动效果

核心逻辑：
  - 每天计算所有币种的RSI/Vol/ADX因子IC
  - IC > 0.05 → 因子有效；IC > 0.10 → 强有效
  - 有效因子生成交易信号，仓位 = 置信度% × 最大仓位%
  - 每小时检查持仓，触发止损/止盈则自动平仓
  - 每日推送飞书报告

文件路径：
  ~/kronos/autonomous_trading_system.py
  ~/kronos/ic_history_cache.json  (本地IC历史缓存)
  ~/kronos/trade_log.json          (交易记录)
"""

import yfinance as yf
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from datetime import datetime, timedelta
import json
import os
import time
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 配置
# ============================================================
COINS = ['BTC', 'ETH', 'ADA', 'DOGE', 'AVAX', 'DOT', 'SOL']
COIN_TICKERS = {
    'BTC': 'BTC-USD', 'ETH': 'ETH-USD', 'ADA': 'ADA-USD',
    'DOGE': 'DOGE-USD', 'AVAX': 'AVAX-USD', 'DOT': 'DOT-USD', 'SOL': 'SOL-USD'
}

# 交易参数
MAX_POSITION = 0.30      # 最大单币仓位
MAX_PORTFOLIO = 0.90     # 最大总仓位
STOP_LOSS = 0.03         # 止损3%
TAKE_PROFIT = 0.20       # 止盈20%
LEVERAGE = 3             # 3x杠杆

# IC参数
IC_THRESHOLD = 0.05
IC_STRONG = 0.10
IC_DECAY_ALERT = 0.015
IC_WINDOW = 60

# 文件路径
CACHE_DIR = os.path.expanduser('~/.hermes/cron/output/')
os.makedirs(CACHE_DIR, exist_ok=True)
IC_CACHE = os.path.join(CACHE_DIR, 'ic_history_cache.json')
TRADE_LOG = os.path.join(CACHE_DIR, 'trade_log.json')
PORTFOLIO_FILE = os.path.join(CACHE_DIR, 'portfolio.json')
PERFORMANCE_FILE = os.path.join(CACHE_DIR, 'performance.json')

# ============================================================
# 数据源
# ============================================================
def fetch_cc_data_multi_page(symbol, pages=5):
    """
    从CryptoCompare获取多页历史数据（使用toTs分页）
    每页2000条1h数据 ≈ 83天
    pages=5 → ~416天历史
    """
    import requests
    API_KEY = '9b11d0c9-5a20-4f1e-82c8-0af6f308e2d0'
    all_data = []
    current_ts = int(datetime.now().timestamp())
    
    for page in range(pages):
        url = f'https://min-api.cryptocompare.com/data/v2/histohour?fsym={symbol}&tsym=USDT&limit=2000&aggregate=1&toTs={current_ts}'
        try:
            r = requests.get(url, headers={'Authorization': f'Apikey {API_KEY}'}, timeout=15)
            d = r.json()
            if d.get('Response') != 'Success':
                break
            rows = d['Data']['Data']
            if not rows:
                break
            # Find the earliest timestamp to continue from
            earliest_ts = rows[-1]['time']
            all_data.extend(rows)
            current_ts = earliest_ts - 1  # go before earliest
            time.sleep(0.2)
        except Exception as e:
            print(f'  {symbol} page {page} error: {e}')
            break
    
    if not all_data:
        return None
    
    df = pd.DataFrame(all_data)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df.rename(columns={'volumefrom': 'vol'})
    df = df.sort_values('time').reset_index(drop=True)
    return df

def fetch_yf_data(ticker, period='2y', interval='1d'):
    """yfinance日线数据"""
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        if hasattr(df.index, 'tz') and df.index.tz:
            df.index = df.index.tz_localize(None)
        return df
    except:
        return None

# ============================================================
# 技术指标
# ============================================================
def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def calc_adx(h, lo, c, p=14):
    pdm = h.diff(); mdm = -lo.diff()
    pdm[pdm < 0] = 0; mdm[mdm < 0] = 0
    tr = np.maximum(h - lo, np.maximum(abs(h - c.shift(1)), abs(lo - c.shift(1))))
    atr = tr.rolling(p).mean()
    pdi = 100 * (pdm.rolling(p).mean() / atr)
    mdi = 100 * (mdm.rolling(p).mean() / atr)
    dx = 100 * abs(pdi - mdi) / (pdi + mdi)
    return dx.rolling(p).mean()

def compute_all_factors(df):
    """计算所有因子"""
    f = pd.DataFrame(index=df.index)
    f['close'] = df['close']
    f['high'] = df.get('high', df['close'])
    f['low'] = df.get('low', df['close'])
    f['volume'] = df.get('volume', df.get('vol', 0))
    f['rsi'] = calc_rsi(f['close'])
    f['rsi_inv'] = 100 - f['rsi']
    f['adx'] = calc_adx(f['high'], f['low'], f['close'])
    f['vol_ma5'] = f['volume'].rolling(5).mean()
    f['vol_ma20'] = f['volume'].rolling(20).mean()
    f['vol_ratio'] = f['vol_ma5'] / f['vol_ma20'].replace(0, np.nan)
    f['trend_ma20'] = f['close'] / f['close'].rolling(20).mean() - 1
    f['ret'] = f['close'].pct_change()
    f['ret_next'] = f['close'].pct_change().shift(-1)
    return f

# ============================================================
# IC计算
# ============================================================
def compute_ic(factors_df, ret_series, window=IC_WINDOW):
    """计算各因子IC（Pearson快速版）"""
    results = {}
    for fac in ['rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']:
        valid = factors_df[fac].notna() & ret_series.notna()
        fac_arr = factors_df.loc[valid, fac].values[-window:]
        ret_arr = ret_series.loc[valid].values[-window:]
        if len(fac_arr) >= window and np.std(fac_arr) > 1e-10 and np.std(ret_arr) > 1e-10:
            ic = np.corrcoef(fac_arr, ret_arr)[0, 1]
            results[fac] = ic if not np.isnan(ic) else 0
        else:
            results[fac] = 0
    return results

def compute_ic_with_decay(factors_df, ret_series, window=IC_WINDOW):
    """计算IC + 7天衰减率"""
    ic_now = compute_ic(factors_df, ret_series, window)
    
    # 7天前的IC
    ic_7d = compute_ic(factors_df.iloc[:-7], ret_series.iloc[:-7], window) if len(factors_df) > 10 else ic_now
    
    decay = {}
    for fac in ic_now:
        now = ic_now[fac]
        then = ic_7d.get(fac, now)
        decay[fac] = (now - then) / 7 if (now and then) else 0
    
    return ic_now, decay

# ============================================================
# 信号生成
# ============================================================
def generate_signal(coin, factors_df, ret_series, ic_data, decay_data):
    """
    基于IC驱动的自适应信号
    返回: (direction, confidence, position_size, best_factor)
    """
    if not ic_data:
        return None
    
    # 找最优因子
    fac_scores = {}
    for fac, ic_val in ic_data.items():
        if ic_val is None or abs(ic_val) < 0.001:
            continue
        decay_penalty = abs(decay_data.get(fac, 0)) * 5
        if ic_val > 0:
            score = max(0, ic_val - decay_penalty)
        else:
            score = max(0, ic_val + decay_penalty * 0.5)
        fac_scores[fac] = score
    
    valid = {k: v for k, v in fac_scores.items() if v > IC_THRESHOLD}
    if not valid:
        return {
            'coin': coin,
            'direction': 'WATCH',
            'confidence': 0,
            'position_size': 0,
            'best_factor': None,
            'ic': ic_data,
            'decay': decay_data,
            'signal_text': f'{coin}: ⚠️ 所有因子无效，建议观望',
        }
    
    best_fac = max(valid, key=valid.get)
    best_score = valid[best_fac]
    best_ic = ic_data.get(best_fac, 0)
    best_decay = decay_data.get(best_fac, 0)
    
    # 方向判断
    if best_fac == 'rsi_inv':
        direction = 'LONG' if best_ic > 0 else 'SHORT'
    elif best_fac == 'vol_ratio':
        direction = 'LONG'
    elif best_fac == 'adx':
        direction = 'TREND_LONG' if best_ic > 0 else 'TREND_SHORT'
    else:
        direction = 'LONG' if best_ic > 0 else 'SHORT'
    
    # 置信度
    confidence = min(100, int(abs(best_score) / IC_STRONG * 100))
    if abs(best_decay) > IC_DECAY_ALERT:
        confidence = max(10, confidence - 30)
    
    # 仓位
    pos_size = confidence / 100 * MAX_POSITION
    
    signal_texts = {
        'LONG': f'🟢 做多 {coin} | {best_fac}(IC={best_ic:.3f}) | 置信{confidence}% | 仓位{pos_size*100:.0f}%',
        'SHORT': f'🔴 做空 {coin} | {best_fac}(IC={best_ic:.3f}) | 置信{confidence}% | 仓位{pos_size*100:.0f}%',
        'TREND_LONG': f'📈 趋势做多 {coin} | {best_fac}(IC={best_ic:.3f}) | 置信{confidence}%',
        'TREND_SHORT': f'📉 趋势做空 {coin} | {best_fac}(IC={best_ic:.3f}) | 置信{confidence}%',
        'WATCH': f'⚠️ {coin} 观望 | 原因: 所有因子无效',
    }
    
    return {
        'coin': coin,
        'direction': direction,
        'confidence': confidence,
        'position_size': pos_size,
        'best_factor': best_fac,
        'ic': ic_data,
        'decay': decay_data,
        'signal_text': signal_texts.get(direction, signal_texts['WATCH']),
    }

# ============================================================
# 组合管理
# ============================================================
def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {}

def save_portfolio(portfolio):
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(portfolio, f, indent=2)

def update_portfolio(signals, prices):
    """根据信号更新组合"""
    portfolio = load_portfolio()
    
    # 初始化结构
    if 'positions' not in portfolio:
        portfolio['positions'] = {}
    if 'cash' not in portfolio:
        portfolio['cash'] = 100000
    if 'total_value' not in portfolio:
        portfolio['total_value'] = 100000
    
    # 计算当前总资产
    total = portfolio['cash']
    for coin, pos in portfolio['positions'].items():
        if coin in prices:
            price = prices[coin]
            pos_value = pos['size'] * price
            pos['pnl'] = (price - pos['entry_price']) / pos['entry_price'] * pos['direction']
            pos['current_value'] = pos_value
            total += pos_value
    
    portfolio['total_value'] = total
    
    # 评估新信号
    new_positions = []
    for sig in signals:
        if sig is None or sig['confidence'] < 50 or sig['direction'] == 'WATCH':
            continue
        if sig['coin'] in portfolio['positions']:
            continue  # 已有仓位跳过
        
        direction_map = {'LONG': 1, 'SHORT': -1, 'TREND_LONG': 1, 'TREND_SHORT': -1}
        direction = direction_map.get(sig['direction'], 0)
        if direction == 0:
            continue
        
        coin = sig['coin']
        if coin not in prices:
            continue
        
        price = prices[coin]
        pos_value = total * sig['position_size']
        
        if pos_value > portfolio['cash'] * 0.5:
            pos_value = portfolio['cash'] * 0.5  # 不超过现金的50%开新仓
        
        if pos_value < 100:
            continue  # 太小跳过
        
        new_pos = {
            'coin': coin,
            'direction': direction,
            'entry_price': price,
            'size': pos_value / price,
            'entry_value': pos_value,
            'stop_loss': price * (1 - STOP_LOSS / abs(direction)) if direction > 0 else price * (1 + STOP_LOSS / abs(direction)),
            'take_profit': price * (1 + TAKE_PROFIT / abs(direction)) if direction > 0 else price * (1 - TAKE_PROFIT / abs(direction)),
            'confidence': sig['confidence'],
            'best_factor': sig['best_factor'],
            'entry_time': datetime.now().isoformat(),
            'pnl': 0,
            'current_value': pos_value,
        }
        
        portfolio['positions'][coin] = new_pos
        portfolio['cash'] -= pos_value
        new_positions.append(new_pos)
    
    # 检查止损/止盈
    closed = []
    for coin, pos in list(portfolio['positions'].items()):
        if coin not in prices:
            continue
        price = prices[coin]
        direction = pos['direction']
        
        # 止损
        if direction > 0 and price <= pos['stop_loss']:
            closed.append((coin, 'SL', price))
            portfolio['cash'] += pos['size'] * price
            del portfolio['positions'][coin]
            continue
        if direction < 0 and price >= pos['stop_loss']:
            closed.append((coin, 'SL', price))
            portfolio['cash'] += pos['size'] * price
            del portfolio['positions'][coin]
            continue
        
        # 止盈
        if direction > 0 and price >= pos['take_profit']:
            closed.append((coin, 'TP', price))
            portfolio['cash'] += pos['size'] * price
            del portfolio['positions'][coin]
            continue
        if direction < 0 and price <= pos['take_profit']:
            closed.append((coin, 'TP', price))
            portfolio['cash'] += pos['size'] * price
            del portfolio['positions'][coin]
            continue
    
    # 更新当前价值
    total = portfolio['cash']
    for coin, pos in portfolio['positions'].items():
        if coin in prices:
            price = prices[coin]
            pos['current_value'] = pos['size'] * price
            pos['pnl'] = (price - pos['entry_price']) / pos['entry_price'] * pos['direction'] * 100
            total += pos['current_value']
    
    portfolio['total_value'] = total
    
    save_portfolio(portfolio)
    return portfolio, new_positions, closed

# ============================================================
# 性能跟踪
# ============================================================
def log_trade(trade_record):
    logs = []
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            logs = json.load(f)
    logs.append(trade_record)
    logs = logs[-500:]  # 保留最近500条
    with open(TRADE_LOG, 'w') as f:
        json.dump(logs, f, indent=2)

def compute_performance():
    perf = {'daily': {}, 'total': {}}
    
    # 读取交易日志
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            logs = json.load(f)
        
        # 按日统计
        daily_pnl = {}
        for t in logs:
            if 'exit_time' in t:
                day = t['exit_time'][:10]
                daily_pnl[day] = daily_pnl.get(day, 0) + t.get('pnl', 0)
        
        perf['daily'] = daily_pnl
        
        # 总计
        total_pnl = sum(t.get('pnl', 0) for t in logs if 'pnl' in t)
        total_trades = len([t for t in logs if 'exit_time' in t])
        wins = len([t for t in logs if 'exit_time' in t and t.get('pnl', 0) > 0])
        perf['total'] = {
            'total_pnl': total_pnl,
            'total_trades': total_trades,
            'wins': wins,
            'losses': total_trades - wins,
            'win_rate': wins / total_trades if total_trades > 0 else 0,
        }
    
    with open(PERFORMANCE_FILE, 'w') as f:
        json.dump(perf, f, indent=2)
    return perf

# ============================================================
# 飞书推送
# ============================================================
def push_feishu(message, silent=False):
    try:
        import requests
        app_id = os.getenv('FEISHU_APP_ID', '')
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')
        
        token_url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
        token_resp = requests.post(token_url, json={'app_id': app_id, 'app_secret': app_secret}, timeout=10)
        token_data = token_resp.json()
        if token_data.get('code') != 0:
            return False
        
        token = token_data.get('tenant_access_token')
        msg_url = 'https://open.feishu.cn/open-apis/im/v1/messages'
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        
        chat_id = 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0'
        content = json.dumps({'text': message[:4000]})
        payload = {'receive_id': chat_id, 'msg_type': 'text', 'content': content}
        params = {'receive_id_type': 'chat_id'}
        
        resp = requests.post(msg_url, headers=headers, json=payload, params=params, timeout=10)
        result = resp.json()
        return result.get('code') == 0
    except:
        return False

# ============================================================
# 主分析引擎
# ============================================================
def run_daily_analysis():
    """每日IC分析 + 信号生成"""
    print(f'[{datetime.now().strftime("%H:%M:%S")}] 开始每日分析...')
    
    all_signals = []
    prices = {}
    
    for coin in COINS:
        ticker = COIN_TICKERS.get(coin, f'{coin}-USD')
        
        # 优先用yfinance（2年日线数据）
        df = fetch_yf_data(ticker, period='2y', interval='1d')
        
        if df is None or len(df) < IC_WINDOW + 10:
            print(f'  {coin}: 数据不足，跳过')
            continue
        
        f = compute_all_factors(df)
        ic_data, decay_data = compute_ic_with_decay(f[['rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']], f['ret_next'])
        
        sig = generate_signal(coin, f, f['ret_next'], ic_data, decay_data)
        if sig:
            all_signals.append(sig)
            prices[coin] = float(f.iloc[-1]['close'])
            sig_str = f"{sig['signal_text']}"
            print(f'  {sig_str}')
    
    return all_signals, prices

def run_full_report():
    """完整日报：IC分析 + 组合状态 + 性能"""
    signals, prices = run_daily_analysis()
    
    portfolio = load_portfolio()
    perf = compute_performance()
    
    # 生成报告
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    report = f"""# 🚀 Kronos 自动驾驶日报 {now}

---

## 📊 IC因子信号

"""
    for sig in signals:
        if sig:
            emoji = '✅' if sig['confidence'] >= 80 else '🟡' if sig['confidence'] >= 50 else '⚠️'
            decay_warn = ' ⚠️因子衰减!' if sig['decay'].get(sig['best_factor'], 0) < -IC_DECAY_ALERT else ''
            report += f"{emoji} {sig['signal_text']}{decay_warn}\n"
    
    # 持仓状态
    report += f"""
---

## 💼 当前组合

**总资产**: ${portfolio.get('total_value', 100000):,.0f}
**现金**: ${portfolio.get('cash', 100000):,.0f}
**持仓数**: {len(portfolio.get('positions', {}))}

"""
    for coin, pos in portfolio.get('positions', {}).items():
        direction_emoji = '🟢' if pos['direction'] > 0 else '🔴'
        pnl_color = '🟢' if pos.get('pnl', 0) > 0 else '🔴'
        report += f"{direction_emoji} {coin}: 入场${pos['entry_price']:.4f} 当前${pos.get('current_value', 0)/pos['size']:.4f} {pnl_color}{pos.get('pnl', 0):+.1f}%\n"
    
    if not portfolio.get('positions'):
        report += "暂无持仓\n"
    
    # 性能
    total_pnl = perf.get('total', {}).get('total_pnl', 0)
    total_trades = perf.get('total', {}).get('total_trades', 0)
    win_rate = perf.get('total', {}).get('win_rate', 0)
    report += f"""
---

## 📈 累计性能

**总PnL**: {'🟢+' if total_pnl > 0 else '🔴'}${total_pnl:,.0f}
**总交易数**: {total_trades}
**胜率**: {win_rate*100:.0f}% ({perf.get('total',{}).get('wins',0)}W/{perf.get('total',{}).get('losses',0)}L)

"""
    
    # 今日建议操作
    actionable = [s for s in signals if s and s['confidence'] >= 70 and s['direction'] != 'WATCH']
    if actionable:
        report += f"""
---

## 🎯 今日可执行信号（置信度≥70%）

"""
        for sig in actionable:
            report += f"- {sig['signal_text']}\n"
    else:
        report += """
---

## 🎯 今日操作建议

⚠️ 无高置信度信号，建议观望或持币不动。
"""
    
    print(report)
    
    # 飞书推送
    push_feishu(report)
    
    return signals, portfolio

# ============================================================
# 入口
# ============================================================
if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--full':
        # 完整日报
        run_full_report()
    else:
        # 快速IC分析
        run_daily_analysis()
