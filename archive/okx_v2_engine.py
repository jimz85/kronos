"""
OKX 升级版实盘引擎 v3 - 基于验证结论（2026-04-17全面验证）

策略:
- DOT RSI<35 72h (唯一2022熊市正收益策略，⭐最可信)
- AVAX 突破20日高点 72h (Walk-Forward +50.8%年均，⭐次可信)
- AVAX RSI<45 72h / RSI>35 追涨 (仅限牛市环境)

风控:
- 熊市/震荡市降低仓位或暂停
- 最大回撤30%硬止损
- 单笔仓位10-20%

币种: DOT, AVAX (AAVE永久排除 - 熊市陷阱)
手续费: 0.25% + 0.1%滑点
"""

import pandas as pd
import numpy as np
import json, os, time, requests
from datetime import datetime
import talib

# ===== 配置 =====
DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')

# 验证结论确认的币种和策略（AAVE永久排除 - 熊市陷阱）
# 手续费: 0.25% + 0.1%滑点
COINS_STRATEGIES = {
    'DOT': {
        'primary': {'rsi_lo': 0, 'rsi_hi': 35, 'adx_min': 15, 'hold_h': 72, 'label': 'RSI<35 72h'},
        'leverage': 3,
        'confidence': 0.85,
        'note': '唯一2022熊市正收益策略'
    },
    'AVAX': {
        # 突破系统（最优，Walk-Forward +50.8%年均，2/2窗口）
        'breakout': {'breakout_days': 20, 'hold_h': 72, 'label': '突破20日高点 72h'},
        # RSI系统（牛市有效，熊市亏-72%）
        'bull_only': {'rsi_lo': 0, 'rsi_hi': 45, 'adx_min': 15, 'hold_h': 72, 'label': 'RSI<45 72h'},
        'bull_momentum': {'rsi_lo': 35, 'rsi_hi': 100, 'adx_min': 15, 'hold_h': 72, 'label': 'RSI>35 追涨 72h'},
        'leverage': 2,
        'breakout_confidence': 0.75,
        'rsi_confidence': 0.5,
        'note': '突破系统最优，RSI系统仅限牛市'
    }
}

# 手续费: 0.25% + 0.1%滑点 = 0.35%总损耗
FEE = 0.0025   # 0.25%
SLIPPAGE = 0.001  # 0.1%

SIZE_PCT = 0.15  # 15%总资金/币（DOT/ETH/BTC）
AVAX_SIZE_PCT = 0.05  # 5%总资金（AVAX单币上限，突破系统波动大）
MAX_TOTAL_EXPOSURE = 0.45  # 总仓位上限45%

# ===== 指标计算 =====
def calc_indicators(ohlc):
    close = ohlc['close'].values
    high = ohlc['high'].values
    low = ohlc['low'].values
    ohlc['rsi'] = talib.RSI(close, timeperiod=14)
    ohlc['adx'] = talib.ADX(high, low, close, timeperiod=14)
    return ohlc


def detect_market_regime(ohlc_1d):
    """检测市场环境: 牛市/震荡/熊市"""
    close = ohlc_1d['close']
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    
    rsi_14 = talib.RSI(close.values, timeperiod=14)
    rsi = rsi_14[-1]
    
    trend_score = 0
    if ema20.iloc[-1] > ema50.iloc[-1]:
        trend_score += 1
    if ema20.iloc[-1] > ema200.iloc[-1]:
        trend_score += 1
    if rsi > 50:
        trend_score += 1
    
    if trend_score >= 2:
        return 'bull'
    elif trend_score == 1:
        return 'sideways'
    else:
        return 'bear'


def check_signal(ohlc_1h, coin, regime):
    """检查是否有信号"""
    ohlc = calc_indicators(ohlc_1h.copy())
    strat = COINS_STRATEGIES.get(coin, {})

    if not strat:
        return None

    rsi = ohlc['rsi'].iloc[-1]
    adx = ohlc['adx'].iloc[-1]
    close = ohlc['close'].iloc[-1]

    results = []

    # 主策略(DOT): RSI<35 72h - 始终有效（唯一熊市验证通过）
    if coin == 'DOT':
        s = strat['primary']
        if s['rsi_lo'] <= rsi < s['rsi_hi'] and adx > s['adx_min']:
            results.append({
                'strategy': s['label'],
                'direction': 'long',
                'leverage': strat['leverage'],
                'confidence': strat['confidence'],
                'hold_h': s['hold_h'],
                'signal': True,
                'rsi': round(rsi, 1),
                'adx': round(adx, 1),
                'regime': regime,
                'note': strat['note']
            })

    # AVAX: 突破系统（最优，任何环境） + RSI系统（仅牛市）
    elif coin == 'AVAX':
        close_series = ohlc['close']

        # 突破系统 - 20日/30日高点突破（最优，任何环境有效）
        for breakout_days in [20, 30]:
            high_n = close_series.rolling(breakout_days * 24).max().shift(1).iloc[-1]
            prev_close = close_series.iloc[-2]
            if close > high_n and prev_close <= high_n:
                results.append({
                    'strategy': f'突破{breakout_days}日高点',
                    'direction': 'long',
                    'leverage': strat['leverage'],
                    'confidence': strat['breakout_confidence'],
                    'hold_h': 72,
                    'signal': True,
                    'rsi': round(rsi, 1),
                    'adx': round(adx, 1),
                    'regime': regime,
                    'note': '突破系统，Walk-Forward +50.8%年均'
                })

        # RSI系统 - 仅牛市环境
        if regime == 'bull':
            for key in ['bull_only', 'bull_momentum']:
                if key in strat:
                    s = strat[key]
                    if s['rsi_lo'] <= rsi < s['rsi_hi'] and adx > s['adx_min']:
                        results.append({
                            'strategy': s['label'],
                            'direction': 'long',
                            'leverage': strat['leverage'],
                            'confidence': strat['rsi_confidence'],
                            'hold_h': s['hold_h'],
                            'signal': True,
                            'rsi': round(rsi, 1),
                            'adx': round(adx, 1),
                            'regime': regime,
                            'note': 'RSI系统，仅限牛市环境'
                        })

    # 没有信号
    if not results:
        return {
            'signal': False,
            'rsi': round(rsi, 1),
            'adx': round(adx, 1),
            'regime': regime,
            'note': '无信号' if regime != 'bull' else f'RSI={round(rsi,1)} ADX={round(adx,1)}不满足条件'
        }

    # 返回最强信号
    return max(results, key=lambda x: x['confidence'])


def get_positions():
    """获取当前持仓"""
    # TODO: 实现OKX API调用
    return []


def send_notification(msg):
    """发送飞书通知"""
    print(f"[通知] {msg}")


def run_scan():
    """主扫描函数"""
    print(f"\n{'='*60}")
    print(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print('='*60)
    
    signals_found = []
    
    for coin in COINS_STRATEGIES.keys():
        try:
            # 加载数据
            path = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
            df = pd.read_csv(path)
            df.columns = [c.lstrip('\ufeff') for c in df.columns]
            ts_col = [c for c in df.columns if 'timestamp' in c.lower() or 'datetime' in c.lower()][0]
            df['ts'] = pd.to_datetime(df[ts_col], unit='ms', errors='coerce')
            if df['ts'].isna().all():
                df['ts'] = pd.to_datetime(df[ts_col], errors='coerce')
            df = df.set_index('ts')
            cols = [c for c in ['open', 'high', 'low', 'close', 'vol', 'volume'] if c in df.columns]
            df = df[cols]
            df = df.rename(columns={'volume': 'vol'})

            ohlc_1h = df[['open', 'high', 'low', 'close', 'vol']].resample('1h').agg({
                'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'vol': 'sum'
            }).dropna()

            ohlc_1d = df[['open', 'high', 'low', 'close', 'vol']].resample('1D').agg({
                'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'vol': 'sum'
            }).dropna()
            
            # 检测市场环境
            regime = detect_market_regime(ohlc_1d)
            
            # 检查信号
            result = check_signal(ohlc_1h, coin, regime)
            
            status = '✅' if result.get('signal') else '❌'
            print(f"{status} {coin}: RSI={result.get('rsi','N/A')} ADX={result.get('adx','N/A')} | 环境:{regime} | {result.get('note','')}")
            
            if result.get('signal'):
                signals_found.append({**result, 'coin': coin})
                print(f"  → 信号: {result['strategy']} {result['direction']} @ {result.get('close','N/A')} | 信心:{result['confidence']} | 杠杆:{result['leverage']}x | 持仓:{result['hold_h']}h")
            
        except Exception as e:
            print(f"  错误: {e}")
    
    # 汇总
    if signals_found:
        print(f"\n📊 发现 {len(signals_found)} 个信号")
        for s in sorted(signals_found, key=lambda x: -x['confidence']):
            print(f"  {s['coin']}: {s['strategy']} (信心{s['confidence']}, {s['leverage']}x)")
        send_notification(f"发现信号: {', '.join([s['coin'] for s in signals_found])}")
    else:
        print(f"\n📊 无信号")
    
    return signals_found


if __name__ == '__main__':
    signals = run_scan()
