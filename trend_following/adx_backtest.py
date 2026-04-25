"""
ADX三阶段过滤 + 多空双向趋势跟踪
周期: 4H
方向: LONG + SHORT
规则:
  ADX < 20: 绝对空仓
  20 ≤ ADX < 40: 只做顺日线EMA50方向
  ADX ≥ 40: 停止开新仓，只持有
  ADX从40+跌破30: 清仓
"""
import vectorbt as vbt
import pandas as pd
import numpy as np
import json, os
from datetime import datetime

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OUT = '/Users/jimingzhang/kronos/trend_following/adx_results.json'
os.makedirs(os.path.dirname(OUT), exist_ok=True)

# ========== 工具函数 ==========

def calc_adx(high, low, close, n=14):
    """计算ADX, +DI, -DI"""
    tr1 = high - low
    tr2 = np.abs(high - close.shift())
    tr3 = np.abs(low - close.shift())
    tr = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
    
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=low.index)
    
    atr = tr.rolling(n).mean()
    plus_di = 100 * (plus_dm.rolling(n).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(n).mean() / atr)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx = dx.rolling(n).mean()
    return adx, plus_di, minus_di

def load_and_prep(coin):
    """加载数据并预处理"""
    df = pd.read_csv(f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv')
    df['timestamp'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    df = df.set_index('timestamp').sort_index()
    df = df[(df['close'] > 0)]
    
    # 4H聚合
    ohlc_4h = df[['open','high','low','close']].resample('4h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    })
    # 日线
    ohlc_1d = df[['open','high','low','close']].resample('1d').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    })
    
    return ohlc_4h, ohlc_1d

def adx_three_phase_strategy(close_4h, high_4h, low_4h, close_1d, 
                               adx_th_low=20, adx_th_high=40,
                               ema_fast=10, ema_mid=30, sl_atr=1.5):
    """
    ADX三阶段 + 多空双向趋势跟踪
    返回: entries_long, exits_long, entries_short, exits_short
    """
    close = close_4h
    high = high_4h
    low = low_4h
    
    # ADX
    adx, plus_di, minus_di = calc_adx(high, low, close, 14)
    adx_avg = adx.rolling(3).mean()  # ADX连续3根稳定才认定
    
    # 日线EMA50方向 (直接用日线数据算出trend，再resample到4H)
    ema50_1d = close_1d.ewm(span=50, adjust=False).mean()
    daily_trend_up = (close_1d > ema50_1d).astype(int)
    # Resample日线trend到4H (用最后一天的值填充)
    daily_trend_up_4h = daily_trend_up.resample('4h').last().ffill().bfill()
    daily_trend_up_4h = daily_trend_up_4h.reindex(close_4h.index).ffill().fillna(1)
    daily_trend_up_4h.index = close_4h.index
    
    # 4H EMA快慢线
    ema_fast_4h = close.ewm(span=ema_fast, adjust=False).mean()
    ema_mid_4h = close.ewm(span=ema_mid, adjust=False).mean()
    
    # ATR止损
    atr = ((high - low).rolling(14).mean()) / close
    sl_stop = (atr * sl_atr).replace(0, np.nan).ffill().bfill().fillna(0.001)
    
    # ===== 阶段一: ADX < adx_th_low → 绝对空仓 =====
    # entries和exits都设为0
    
    # ===== 阶段二: adx_th_low ≤ ADX < adx_th_high → 顺日线方向开仓 =====
    in_trend_zone = (adx_avg >= adx_th_low) & (adx_avg < adx_th_high)
    
    # 金叉做多 (only if 日线趋势向上)
    bull_cross = (ema_fast_4h > ema_mid_4h) & (ema_fast_4h.shift(1) <= ema_mid_4h.shift(1))
    entries_long = (in_trend_zone & daily_trend_up_4h & bull_cross).astype(int)
    exits_long = (ema_fast_4h < ema_mid_4h).astype(int)
    
    # 死叉做空 (only if 日线趋势向下)
    bear_cross = (ema_fast_4h < ema_mid_4h) & (ema_fast_4h.shift(1) >= ema_mid_4h.shift(1))
    entries_short = (in_trend_zone & ~daily_trend_up_4h & bear_cross).astype(int)
    exits_short = (ema_fast_4h > ema_mid_4h).astype(int)
    
    # ===== 阶段三: ADX ≥ adx_th_high → 不开新仓 =====
    # entries_long, entries_short 已经因为 in_trend_zone=False 而变成0
    # 已有持仓继续持有，直到被EMA死叉/金叉止损
    
    # ===== ADX从40+跌破30 → 强制清仓 =====
    adx_prev_above = adx_avg.shift(1) >= adx_th_high
    adx_now_below = adx_avg < 30
    emergency_exit_long = (adx_prev_above & adx_now_below & (ema_fast_4h > ema_mid_4h)).astype(int)
    emergency_exit_short = (adx_prev_above & adx_now_below & (ema_fast_4h < ema_mid_4h)).astype(int)
    
    exits_long = (exits_long | emergency_exit_long).astype(int)
    exits_short = (exits_short | emergency_exit_short).astype(int)
    
    return entries_long, exits_long, entries_short, exits_short, adx_avg, adx

def run_backtest(close, high, low, 
                 entries_long, exits_long,
                 entries_short, exits_short,
                 size=0.10):
    """多空双向回测"""
    atr = ((high - low).rolling(14).mean()) / close
    sl_atr_val = (atr * 1.5).replace(0, np.nan).ffill().bfill().fillna(0.001)
    
    # Long side
    pf_long = vbt.Portfolio.from_signals(
        close, entries_long, exits_long,
        fees=0.001, slippage=0.0005, size=size,
        sl_stop=sl_atr_val, direction='longonly'
    )
    
    # Short side
    pf_short = vbt.Portfolio.from_signals(
        close, entries_short, exits_short,
        fees=0.001, slippage=0.0005, size=size,
        sl_stop=sl_atr_val, direction='shortonly'
    )
    
    # 合并资金曲线
    value_long = pf_long.value()
    value_short = pf_short.value()
    
    # 总资金 = Long + Short (各50%仓位)
    # 或者简单加总
    return pf_long, pf_short

def calc_stats(pf, label=''):
    s = pf.stats()
    trades = s.get('Total Trades', 0)
    if trades < 5:
        return None
    wr = s.get('Win Rate [%]', 0) / 100
    aw = s.get('Avg Winning Trade [%]', 0) / 100
    al = abs(s.get('Avg Losing Trade [%]', 0) / 100)
    pf_ratio = aw / al if al > 1e-10 else 0
    dd = s.get('Max Drawdown [%]', 0) / 100
    sharpe = s.get('Sharpe Ratio', 0)
    ret = s.get('Total Return [%]', 0) / 100
    
    value = pf.value()
    returns = value.pct_change().dropna()
    n_days = len(value)
    annual_return = returns.mean() * 6 * 365  # 4H = 每天6根
    annual_vol = returns.std() * np.sqrt(6 * 365)
    sharpe_annual = annual_return / annual_vol if annual_vol > 1e-10 else 0
    
    return {
        'trades': trades, 'wr': wr, 'pf': pf_ratio,
        'dd': dd, 'sharpe': sharpe, 'sharpe_annual': sharpe_annual,
        'ret': ret, 'annual_return': annual_return, 'n_days': n_days,
    }

def year_stats(pf, close, year):
    """分年份统计"""
    yr_close = close[str(year)]
    if len(yr_close) < 100:
        return None
    
    yr_pf = pf[str(year)]
    if yr_pf is None or len(yr_pf.value()) < 2:
        return None
    
    s = yr_pf.stats()
    trades = s.get('Total Trades', 0)
    if trades < 3:
        return {'year': year, 'trades': 0, 'ret': 0, 'dd': 0, 'wr': 0, 'pf': 0}
    
    value = yr_pf.value()
    ret = (value.iloc[-1] / value.iloc[0] - 1) * 100
    dd = ((value - value.cummax()) / value.cummax()).min() * 100
    wr = s.get('Win Rate [%]', 0)
    aw = s.get('Avg Winning Trade [%]', 0)
    al = abs(s.get('Avg Losing Trade [%]', 0))
    pf_ratio = aw / al if al > 0 else 0
    
    return {'year': year, 'trades': trades, 'ret': ret, 'dd': dd, 'wr': wr, 'pf': pf_ratio}

# ========== 主回测 ==========
if __name__ == '__main__':
    coins = ['BTC', 'ETH', 'DOGE', 'BCH', 'ADA']
    
    # 测试不同ADX阈值组合
    adx_configs = [
        (18, 38),  # 保守
        (20, 40),  # 基准
        (22, 42),  # 激进
    ]
    
    all_results = []
    
    for coin in coins:
        print(f'\n加载 {coin}...')
        ohlc_4h, ohlc_1d = load_and_prep(coin)
        
        # 划分数据集
        train_4h = ohlc_4h.loc[:'2023-12-31']
        train_1d = ohlc_1d.loc[:'2023-12-31']
        val_4h = ohlc_4h.loc['2024-01-01':'2025-06-30']
        val_1d = ohlc_1d.loc['2024-01-01':'2025-06-30']
        test_4h = ohlc_4h.loc['2025-07-01':]
        test_1d = ohlc_1d.loc['2025-07-01':]
        
        for adx_low, adx_high in adx_configs:
            for ema_fast, ema_mid in [(8, 30), (10, 30), (10, 50), (12, 50)]:
                if ema_fast >= ema_mid:
                    continue
                
                for size in [0.05, 0.10]:  # 5%和10%仓位
                    # ===== 训练集 =====
                    close_train = train_4h['close']
                    high_train = train_4h['high']
                    low_train = train_4h['low']
                    close_1d_train = train_1d['close']
                    
                    eL, xL, eS, xS, adx_val, _ = adx_three_phase_strategy(
                        close_train, high_train, low_train, close_1d_train,
                        adx_low, adx_high, ema_fast, ema_mid
                    )
                    
                    atr_train = ((high_train - low_train).rolling(14).mean() / close_train).fillna(0.001)
                    sl_train = (atr_train * 1.5).replace(0, 0.001).fillna(0.001)
                    
                    pf_long_train = vbt.Portfolio.from_signals(
                        close_train, eL, xL,
                        fees=0.001, slippage=0.0005, size=size,
                        sl_stop=sl_train, direction='longonly'
                    )
                    pf_short_train = vbt.Portfolio.from_signals(
                        close_train, eS, xS,
                        fees=0.001, slippage=0.0005, size=size,
                        sl_stop=sl_train, direction='shortonly'
                    )
                    
                    stats_long = calc_stats(pf_long_train, 'LONG')
                    stats_short = calc_stats(pf_short_train, 'SHORT')
                    
                    # 合并资金曲线
                    val_total = pf_long_train.value() + pf_short_train.value()
                    ret_total = (val_total.iloc[-1] / val_total.iloc[0] - 1)
                    peak_total = val_total.cummax()
                    dd_total = ((val_total - peak_total) / peak_total).min()
                    returns_total = val_total.pct_change().dropna()
                    sharpe_total = (returns_total.mean() * 6 * 365) / (returns_total.std() * np.sqrt(6 * 365) + 1e-10)
                    
                    total_trades = (stats_long['trades'] if stats_long else 0) + (stats_short['trades'] if stats_short else 0)
                    
                    # 分年份
                    year_results = []
                    for yr in [2018, 2019, 2020, 2021, 2022, 2023]:
                        if str(yr) in close_train.index:
                            yr_ret_long = year_stats(pf_long_train, close_train, yr)
                            yr_ret_short = year_stats(pf_short_train, close_train, yr)
                            if yr_ret_long and yr_ret_short:
                                year_results.append({
                                    'year': yr,
                                    'ret_long': yr_ret_long.get('ret', 0),
                                    'ret_short': yr_ret_short.get('ret', 0),
                                    'ret_combined': yr_ret_long.get('ret', 0) * size + yr_ret_short.get('ret', 0) * size,
                                    'trades': yr_ret_long.get('trades', 0) + yr_ret_short.get('trades', 0),
                                })
                    
                    config_label = f"ADX({adx_low}/{adx_high}) EMA({ema_fast}/{ema_mid}) {size*100:.0f}%"
                    
                    r = {
                        'coin': coin,
                        'config': config_label,
                        'adx_low': adx_low,
                        'adx_high': adx_high,
                        'ema_fast': ema_fast,
                        'ema_mid': ema_mid,
                        'size': size,
                        'total_trades': total_trades,
                        'ret_train': ret_total,
                        'dd_train': dd_total,
                        'sharpe_train': sharpe_total,
                        'long_stats': stats_long,
                        'short_stats': stats_short,
                        'year_results': year_results,
                    }
                    all_results.append(r)
                    
                    passed = (abs(ret_total) < 2.0)  # 不设太高的过滤，看数据说话
                    
                    if total_trades > 10:
                        print(f"  {config_label}: 总交易{total_trades}次 训练集收益={ret_total*100:.1f}% DD={dd_total*100:.1f}% Sharpe={sharpe_total:.2f}")
    
    # 保存结果
    with open(OUT, 'w') as f:
        json.dump({'results': all_results}, f, default=str, indent=2)
    
    print(f'\n结果已保存到 {OUT}')
    
    # 找最优配置
    if all_results:
        sorted_res = sorted(all_results, key=lambda x: x['sharpe_train'], reverse=True)
        print(f'\n按Sharpe排序前10:')
        for r in sorted_res[:10]:
            print(f"  {r['coin']} {r['config']}: Sharpe={r['sharpe_train']:.2f} 训练集收益={r['ret_train']*100:.1f}% DD={r['dd_train']*100:.1f}% 交易数={r['total_trades']}")
        
        # 保存最优
        with open(OUT.replace('.json', '_top.json'), 'w') as f:
            json.dump({'top': sorted_res[:20]}, f, default=str, indent=2)
