#!/usr/bin/env python3
"""
Kronos Walk-Forward 参数优化框架
============================================================
目的：用历史数据自动找到最优参数，避免主观调参的过拟合

流程：
  1. 拉取历史K线数据（OKX公开API）
  2. 分割：训练窗口 + 测试窗口（滚动）
  3. 网格搜索最优参数（训练窗口）
  4. 样本外验证（测试窗口）
  5. 汇总：哪些参数在样本内外都稳定

指标：
  - 年化收益率 (Annual Return)
  - 夏普比率 (Sharpe Ratio)
  - 最大回撤 (Max Drawdown)
  - 盈亏比 (Win/Loss Ratio)
  - 胜率 (Win Rate)

参数空间：
  - RSI周期: [7, 10, 14, 21]
  - RSI超卖阈值: [25, 30, 35, 40]
  - RSI超买阈值: [60, 65, 70, 75]
  - ADX阈值: [15, 20, 25, 30]
  - SL比例: [0.015, 0.02, 0.025, 0.03]
  - TP比例: [0.05, 0.08, 0.10, 0.12]

运行：
  python3 walkforward_optimizer.py              # 默认AVAX 1h
  python3 walkforward_optimizer.py --coin ETH   # ETH
  python3 walkforward_optimizer.py --coin DOGE --timeframe 4h
"""

import os, sys, json, time, itertools
import numpy as np
import pandas as pd
import vectorbt as vbt
from datetime import datetime, timedelta
from pathlib import Path
RSI_PERIODS = [7, 10, 14, 21]
RSI_OVERSOLD = [25, 30, 35, 40]
RSI_OVERBOUGHT = [60, 65, 70, 75]
ADX_THRESHOLDS = [15, 20, 25, 30]
SL_PCTS = [0.015, 0.02, 0.025, 0.03]
TP_PCTS = [0.05, 0.08, 0.10, 0.12]

# Walk-Forward配置
WF_TRAIN_DAYS = 90      # 训练窗口：90天
WF_TEST_DAYS = 30       # 测试窗口：30天
WF_STEP_DAYS = 15       # 滚动步长：15天
MIN_TRADES = 10         # 最少交易次数（太少没统计意义）

COIN = 'AVAX'
TIMEFRAME = '1H'
DATA_DIR = Path.home() / '.hermes/cron/output'


# ========== 数据获取 ==========

def fetch_ohlcv_okx(coin, bar='1H', limit=1000):
    """从OKX公开API分页获取K线数据（最多获取max_batches批）"""
    try:
        import requests
        all_data = []
        after = None
        max_batches = limit // 300 + 2

        for batch in range(max_batches):
            if after:
                url = f'https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bar}&limit=300&after={after}'
            else:
                url = f'https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bar}&limit=300'

            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get('code') != '0' or not data.get('data'):
                break

            candles = data['data']
            if len(all_data) == 0:
                oldest_ts = candles[-1][0]

            all_data.extend(candles)

            if len(candles) < 300:
                break

            oldest_ts = candles[-1][0]
            after = oldest_ts

        if not all_data:
            return None

        rows = []
        for d in reversed(all_data):
            try:
                rows.append({
                    'ts': int(d[0]),
                    'open': float(d[4]),
                    'high': float(d[2]),
                    'low': float(d[3]),
                    'close': float(d[4]),
                    'volume': float(d[5]),
                })
            except:
                pass

        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['ts'], unit='ms')
        df = df.set_index('date')
        return df
    except Exception as e:
        print(f'数据获取失败: {e}')
        return None

def load_local_data(coin):
    """加载本地缓存数据"""
    cache_file = DATA_DIR / f'wf_data_{coin}.csv'
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=['date'], index_col='date')
        print(f'  本地缓存: {len(df)}条 {df.index[0].date()} ~ {df.index[-1].date()}')
        return df
    return None

def save_local_data(coin, df):
    """保存本地缓存"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = DATA_DIR / f'wf_data_{coin}.csv'
    df.to_csv(cache_file)
    print(f'  已缓存: {cache_file}')

# ========== 指标计算 ==========

def calc_rsi(close, period=14):
    """计算RSI"""
    deltas = close.diff()
    gains = deltas.where(deltas > 0, 0)
    losses = -deltas.where(deltas < 0, 0)
    if isinstance(close, pd.Series):
        return gains.rolling(period).mean() / (gains.rolling(period).mean() + losses.rolling(period).mean()) * 100
    avg_gain = np.convolve(gains, np.ones(period)/period, mode='valid')
    avg_loss = np.convolve(losses, np.ones(period)/period, mode='valid')
    rs = avg_gain / (avg_loss + 1e-10)
    return np.concatenate([np.full(period-1, 50), 100 - 100/(1+rs)])

def calc_adx(high, low, close, period=14):
    """计算ADX (Wilder平滑)"""
    high = np.asarray(high)
    low = np.asarray(low)
    close = np.asarray(close)
    n = len(close)
    tr = np.zeros(n-1)
    plus_dm = np.zeros(n-1)
    minus_dm = np.zeros(n-1)
    for i in range(1, n):
        tr[i-1] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
        up = high[i] - high[i-1]
        down = low[i-1] - low[i]
        plus_dm[i-1] = up if up > down and up > 0 else 0
        minus_dm[i-1] = down if down > up and down > 0 else 0
    if len(tr) < period:
        return np.full(n, 20)
    atr = np.zeros(n)
    plus_di = np.zeros(n)
    minus_di = np.zeros(n)
    atr[period] = np.mean(tr[:period])
    plus_di[period] = np.mean(plus_dm[:period])
    minus_di[period] = np.mean(minus_dm[:period])
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i-1]) / period
        plus_di[i] = (plus_di[i-1] * (period-1) + plus_dm[i-1]) / period
        minus_di[i] = (minus_di[i-1] * (period-1) + minus_dm[i-1]) / period
        if atr[i] > 0:
            plus_di[i] = (plus_di[i] / atr[i]) * 100
            minus_di[i] = (minus_di[i] / atr[i]) * 100
    dx = np.zeros(n)
    for i in range(period, n):
        if plus_di[i] + minus_di[i] > 0:
            dx[i] = abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i]) * 100
    adx = np.zeros(n)
    adx[period*2] = np.mean(dx[period:period*2])
    for i in range(period*2+1, n):
        adx[i] = (adx[i-1] * (period-1) + dx[i]) / period
    return np.nan_to_num(adx, nan=20)

# ========== 策略信号生成 ==========

def generate_signals(df, rsi_period, rsi_oversold, rsi_overbought, adx_thresh):
    """生成买卖信号（基于RSI+ADX）"""
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values

    rsi = calc_rsi(df['close'], rsi_period)
    adx = calc_adx(df['high'], df['low'], df['close'], 14)

    # 买入信号：RSI超卖 + ADX确认趋势
    buy = (rsi < rsi_oversold) & (adx > adx_thresh)
    # 卖出信号：RSI超买
    sell = (rsi > rsi_overbought)

    return buy.astype(int), sell.astype(int), rsi, adx

# ========== 向量化回测（单组参数） ==========

def backtest_params(df, params):
    """用vectorbt回测一组参数"""
    rsi_period, rsi_oversold, rsi_overbought, adx_thresh, sl_pct, tp_pct = params

    buy, sell, rsi, adx = generate_signals(df, rsi_period, rsi_oversold, rsi_overbought, adx_thresh)

    # 创建entries/exits矩阵
    entries = pd.Series(buy, index=df.index)
    exits = pd.Series(sell, index=df.index)

    try:
        pf = vbt.Portfolio.from_signals(
            close=df['close'],
            entries=entries,
            exits=exits,
            freq='4h',
            init_cash=10000,
            fees=0.0006,
            slippage=0.0005,
            size=100,
            size_type='percent',
            sl_stop=sl_pct,
            tp_stop=tp_pct,
        )
    except Exception as e:
        return None

    stats = pf.stats()
    if stats.get('Total Trades', 0) < MIN_TRADES:
        return None

    total_return = float(stats.get('Total Return [%]', 0))
    max_dd = float(stats.get('Max Drawdown [%]', 0))
    sharpe = float(stats.get('Sharpe Ratio', 0) or 0)
    profit_factor = float(stats.get('Profit Factor', 0) or 0)
    avg_winning = float(stats.get('Avg Winning Trade [%]', 0) or 0)

    return {
        'params': params,
        'total_trades': int(stats.get('Total Trades', 0)),
        'win_rate': float(stats.get('Win Rate [%]', 0)),
        'annual_return': total_return,  # total return for the period
        'max_drawdown': max_dd,
        'sharpe_ratio': sharpe,
        'profit_factor': profit_factor,
        'avg_trade': avg_winning,
        'final_equity': float(pf.value().iloc[-1]),
    }

# ========== Walk-Forward 核心 ==========

def walk_forward_optimize(df, coin):
    """
    Walk-Forward优化：
    将数据分割成多个训练/测试窗口，
    在训练窗口找最优参数，在测试窗口验证
    """
    total_days = (df.index[-1] - df.index[0]).days
    print(f'\n数据总天数: {total_days}天 ({df.index[0].date()} ~ {df.index[-1].date()})')
    print(f'训练窗口: {WF_TRAIN_DAYS}天 | 测试窗口: {WF_TEST_DAYS}天 | 滚动步长: {WF_STEP_DAYS}天')

    # 生成所有参数组合
    all_params = list(itertools.product(
        RSI_PERIODS, RSI_OVERSOLD, RSI_OVERBOUGHT,
        ADX_THRESHOLDS, SL_PCTS, TP_PCTS
    ))
    total_combinations = len(all_params)
    print(f'参数组合总数: {total_combinations}')

    # 滚动窗口
    windows = []
    end_date = df.index[-1]
    current = df.index[0] + timedelta(days=WF_TRAIN_DAYS)

    while current + timedelta(days=WF_TEST_DAYS) <= end_date:
        train_end = current
        test_end = min(current + timedelta(days=WF_TEST_DAYS), end_date)

        train_df = df[df.index < train_end]
        test_df = df[(df.index >= train_end) & (df.index < test_end)]

        if len(train_df) < 100 or len(test_df) < 50:
            current += timedelta(days=WF_STEP_DAYS)
            continue

        windows.append({
            'train_start': train_df.index[0],
            'train_end': train_end,
            'test_start': test_df.index[0],
            'test_end': test_end,
            'train_df': train_df,
            'test_df': test_df,
        })
        current += timedelta(days=WF_STEP_DAYS)

    print(f'滚动窗口数: {len(windows)}')

    if len(windows) == 0:
        print('数据不足，跳过Walk-Forward')
        return None

    all_results = []
    window_results = []

    for wi, w in enumerate(windows):
        print(f'\n--- 窗口 {wi+1}/{len(windows)} ---')
        print(f'  训练: {w["train_start"].date()} ~ {w["train_end"].date()} ({len(w["train_df"])}条)')
        print(f'  测试: {w["test_start"].date()} ~ {w["test_end"].date()} ({len(w["test_df"])}条)')

        # ===== 训练阶段：网格搜索 =====
        best_train = None
        best_score = -999

        for pi, p in enumerate(all_params):
            if pi % 500 == 0:
                print(f'  进度: {pi}/{total_combinations} ({pi/total_combinations*100:.0f}%)')
            result = backtest_params(w['train_df'], p)
            if result and result['total_trades'] >= MIN_TRADES:
                # 综合评分：年化收益 - 回撤惩罚
                score = result['annual_return'] * 0.6 - abs(result['max_drawdown']) * 0.4
                if score > best_score:
                    best_score = score
                    best_train = result

        if not best_train:
            print(f'  训练窗口无有效结果')
            continue

        print(f'  最优参数: RSI{p[0]} OS={p[1]} OB={p[2]} ADX>{p[3]} SL={p[4]*100:.1f}% TP={p[5]*100:.0f}%')
        print(f'  训练结果: 交易{best_train["total_trades"]}笔 胜率{best_train["win_rate"]:.1%} 年化{best_train["annual_return"]:.1%} 回撤{best_train["max_drawdown"]:.1%}')

        # ===== 测试阶段：验证最优参数 =====
        test_result = backtest_params(w['test_df'], best_train['params'])
        if test_result:
            print(f'  样本外: 交易{test_result["total_trades"]}笔 胜率{test_result["win_rate"]:.1%} 年化{test_result["annual_return"]:.1%} 回撤{test_result["max_drawdown"]:.1%}')
        else:
            print(f'  样本外: 无有效结果')
            test_result = {'total_trades': 0, 'win_rate': 0, 'annual_return': 0, 'max_drawdown': 0, 'sharpe_ratio': 0}

        window_results.append({
            'window': wi + 1,
            'train_period': f'{w["train_start"].date()} ~ {w["train_end"].date()}',
            'test_period': f'{w["test_start"].date()} ~ {w["test_end"].date()}',
            'best_params': best_train['params'],
            'train_trades': best_train['total_trades'],
            'train_return': best_train['annual_return'],
            'train_dd': best_train['max_drawdown'],
            'train_sharpe': best_train['sharpe_ratio'],
            'test_trades': test_result['total_trades'],
            'test_return': test_result['annual_return'],
            'test_dd': test_result['max_drawdown'],
            'test_sharpe': test_result['sharpe_ratio'],
        })

        all_results.append({
            'params': best_train['params'],
            'train': best_train,
            'test': test_result,
        })

    return window_results, all_results

# ========== 结果汇总 ==========

def summarize_results(window_results, coin):
    """汇总所有窗口结果，找出稳定最优参数"""
    if not window_results:
        print('无结果')
        return

    print(f'\n{"="*70}')
    print(f'Walk-Forward 完整报告: {coin}')
    print(f'{"="*70}')

    # 按参数分组统计
    param_returns = {}
    param_counts = {}
    for wr in window_results:
        p = wr['best_params']
        key = f'RSI{p[0]} OS={p[1]} OB={p[2]} ADX>{p[3]} SL={p[4]*100:.1f}% TP={p[5]*100:.0f}%'
        if key not in param_returns:
            param_returns[key] = []
            param_counts[key] = 0
        param_returns[key].append(wr['test_return'])
        param_counts[key] += 1

    print(f'\n参数出现频次（被选中次数）和样本外平均收益:')
    sorted_params = sorted(param_returns.items(), key=lambda x: np.mean(x[1]), reverse=True)
    for key, returns in sorted_params:
        mean_ret = np.mean(returns)
        std_ret = np.std(returns)
        count = param_counts[key]
        print(f'  {key}: {count}次 | 样本外平均年化 {mean_ret:+.1f}% ±{std_ret:.1f}%')

    # 全局统计
    test_returns = [wr['test_return'] for wr in window_results]
    test_dds = [wr['test_dd'] for wr in window_results]
    test_trades = [wr['test_trades'] for wr in window_results]

    print(f'\n全局样本外统计:')
    print(f'  平均年化收益: {np.mean(test_returns):+.1f}%')
    print(f'  收益标准差:   {np.std(test_returns):.1f}%')
    print(f'  最大回撤平均: {np.mean(test_dds):.1f}%')
    print(f'  平均交易次数: {np.mean(test_trades):.0f}笔/窗口')

    # 找出样本外稳定盈利的参数
    stable_params = [(k, np.mean(v)) for k, v in param_returns.items() if np.mean(v) > 0]
    stable_params.sort(key=lambda x: x[1], reverse=True)

    if stable_params:
        print(f'\n✅ 样本外稳定盈利的参数（按平均收益排序）:')
        for k, ret in stable_params[:5]:
            print(f'  {k}: 平均年化 {ret:+.1f}%')

        best_key = stable_params[0][0]
        print(f'\n🏆 推荐参数: {best_key}')
        print(f'   (在{param_counts[best_key]}个窗口中被选中，样本外平均年化{stable_params[0][1]:+.1f}%)')

    return stable_params

# ========== 主程序 ==========

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--coin', default='AVAX')
    parser.add_argument('--timeframe', default='1H')
    parser.add_argument('--days', type=int, default=730)  # 拉取2年数据
    parser.add_argument('--force', action='store_true', help='强制重新获取数据')
    args = parser.parse_args()

    COIN = args.coin
    TIMEFRAME = args.timeframe

    print(f'Kronos Walk-Forward 参数优化')
    print(f'币种: {COIN} | 周期: {TIMEFRAME} | 数据量: {args.days}天')

    # 加载数据
    df = load_local_data(COIN)
    if df is None or args.force:
        print(f'\n从OKX获取{args.days}天{TIMEFRAME}数据...')
        df = fetch_ohlcv_okx(COIN, TIMEFRAME, args.days // 24 + 100)
        if df is None:
            print('数据获取失败，退出')
            sys.exit(1)
        save_local_data(COIN, df)

    print(f'数据: {len(df)}条 {df.index[0]} ~ {df.index[-1]}')

    # Walk-Forward优化
    results = walk_forward_optimize(df, COIN)
    if results:
        window_results, all_results = results
        stable = summarize_results(window_results, COIN)

        # 保存结果
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        report_file = DATA_DIR / f'wf_report_{COIN}_{TIMEFRAME}.json'
        with open(report_file, 'w') as f:
            json.dump({
                'coin': COIN,
                'timeframe': TIMEFRAME,
                'windows': window_results,
                'stable_params': [(k, float(v)) for k, v in (stable or [])],
            }, f, indent=2, default=str)
        print(f'\n报告已保存: {report_file}')
