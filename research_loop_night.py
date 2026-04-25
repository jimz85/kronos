"""
通宵量化研究循环 v2 (2026-04-18) - 基于Kronos skill真实验证
=============================================================
核心原则：止损是陷阱，让利润奔跑，趋势跟踪不止损
关键发现：
  - ADX>15, RSI<35 做多，72h持仓，无止损
  - BCH: RSI<45, ADX>15, 48h持仓
  - DOT: RSI<35, ADX>15, 72h持仓  ← 唯一通过2022熊市的策略
验证标准：Walk-Forward 100%通过 + 2022熊市正收益
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import os
import time
import itertools

# ── 路径配置 ──────────────────────────────────────────────────────
DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OUTPUT_DIR = os.path.expanduser('~/kronos/research_night')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 核心参数网格（基于skill验证结果）─────────────────────────────────
PARAM_GRID = {
    'rsi_oversold': [30, 35, 40, 45],   # 做多RSI阈值
    'rsi_overbought': [60, 65, 70],       # 做空RSI阈值（严格3倍）
    'adx_min': [15, 18, 20],             # ADX>15 是门槛
    'leverage': [1, 2, 3],               # 实际杠杆（非保证金杠杆）
    'pos_pct': [0.15, 0.20],             # 仓位比例
    'holding_bars': [48, 72, 96],         # 持仓K线数（5min=4h/6h/8h）
    'ma_filter': [True],                 # 必须开启MA多头过滤
    'cooldown_bars': [24, 36],           # 2h/3h冷却（12根5minK线/h）
}

# 测试币种（按优先级）
COINS_PRIORITY = ['DOT', 'DOGE', 'AVAX', 'BCH', 'ADA', 'BNB', 'BTC', 'ETH']

# ── 指标计算 ───────────────────────────────────────────────────────

def calc_rsi(close, period=14):
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.inf)
    rsi = 100 - (100 / (1 + rs))
    return rsi.values

def calc_adx(high, low, close, period=14):
    high, low, close = pd.Series(high), pd.Series(low), pd.Series(close)
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.rolling(window=period).mean()
    return adx.values, plus_di.values, minus_di.values

def calc_ma_trend(df_1h):
    """计算1h MA多头排列"""
    close_1h = df_1h['close']
    df_1h['ma20'] = close_1h.rolling(20).mean()
    df_1h['ma50'] = close_1h.rolling(50).mean()
    df_1h['ma200'] = close_1h.rolling(200).mean()
    df_1h['major_up'] = (df_1h['ma20'] > df_1h['ma50']) & (df_1h['ma50'] > df_1h['ma200'])
    df_1h['major_down'] = (df_1h['ma20'] < df_1h['ma50']) & (df_1h['ma50'] < df_1h['ma200'])
    return df_1h

# ── 数据加载 ────────────────────────────────────────────────────────

def load_coin_data(coin, n_rows=None):
    """加载币种数据，自动兼容不同格式"""
    path = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    df = pd.read_csv(path)
    
    if 'timestamp' in df.columns:
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_localize(None)
    elif 'datetime_utc' in df.columns:
        df['datetime'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    
    df = df.set_index('datetime').sort_index()
    
    cols = ['open', 'high', 'low', 'close']
    if 'vol' in df.columns:
        cols.append('vol')
    elif 'volume' in df.columns:
        cols.append('volume')
    df = df[cols]
    
    if n_rows:
        df = df.tail(n_rows)
    
    return df

# ── 回测引擎（skill正版：不做止损，让利润奔跑）──────────────────────

def backtest(params, df, coin_name, name=''):
    """
    核心改进（基于skill 2026-04-16发现）：
    - 不止损：趋势跟踪止损是负优化
    - 动态持仓时间：48-72h后强制平仓
    - MA趋势过滤：只在major_up时做多
    - 冷却机制：防止信号重复
    """
    RSI_OS = params['rsi_oversold']
    RSI_OB = params['rsi_overbought']
    ADX_MIN = params['adx_min']
    LEVERAGE = params['leverage']
    POS_PCT = params['pos_pct']
    HOLDING_BARS = params['holding_bars']
    MA_FILTER = params.get('ma_filter', True)
    COOLDOWN_BARS = params.get('cooldown_bars', 24)
    FEE = 0.0004  # taker fee 0.04%

    close = df['close'].values
    high  = df['high'].values
    low   = df['low'].values
    n = len(df)
    if n < 500:
        return None

    # 计算指标
    rsi = calc_rsi(close)
    adx, plus_di, minus_di = calc_adx(high, low, close)

    # 预热：RSI(14) + MA(200) + lag(12) + 1 = 214
    WARMUP = 220

    # ── 信号生成（带冷却机制）────────────────────────────
    long_signal  = np.zeros(n, dtype=bool)
    short_signal = np.zeros(n, dtype=bool)

    last_exit_long = -9999
    last_exit_short = -9999

    for i in range(WARMUP, n - 1):
        hold_bars_long  = i - last_exit_long
        hold_bars_short = i - last_exit_short

        # MA趋势过滤（做多前提）
        ma_up = True
        if MA_FILTER and i >= 200:
            ma20 = np.mean(close[i-20:i])
            ma50 = np.mean(close[i-50:i])
            ma200 = np.mean(close[i-200:i])
            ma_up = (ma20 > ma50) and (ma50 > ma200)

        # 做多信号：RSI超卖 + ADX强度 + MA多头 + 冷却
        if (rsi[i] < RSI_OS and 
            adx[i] > ADX_MIN and 
            plus_di[i] > minus_di[i] and
            hold_bars_long >= COOLDOWN_BARS and
            ma_up):
            long_signal[i] = True
            last_exit_long = i  # 会在出场时更新

        # 做空信号：深度超买 + ADX强度 + 空头排列 + 冷却
        if (rsi[i] > RSI_OB and 
            adx[i] > ADX_MIN and 
            minus_di[i] > plus_di[i] + 5 and  # 空头明显领先
            hold_bars_short >= COOLDOWN_BARS):
            short_signal[i] = True

    # ── 持仓模拟（无止损，只有时限出场）────────────────
    initial_capital = 10000.0
    capital = initial_capital
    position = 0
    entry_price = 0.0
    entry_idx = 0
    last_exit_idx = -9999

    trades = []
    equity_curve = [capital]

    for i in range(WARMUP, n - 1):
        # 冷却
        if i - last_exit_idx < COOLDOWN_BARS:
            pass

        # ── 无持仓 → 开仓 ──
        if position == 0:
            if long_signal[i]:
                position = 1
                entry_price = close[i + 1]
                entry_idx = i + 1
            elif short_signal[i]:
                position = -1
                entry_price = close[i + 1]
                entry_idx = i + 1

        # ── 持仓中 ──
        elif position != 0:
            hold_bars = i - entry_idx
            exit_price = None
            exit_reason = None

            if position == 1:
                pnl_pct = (close[i] - entry_price) / entry_price
                # 持仓超时 → 强制平仓
                if hold_bars >= HOLDING_BARS:
                    exit_price = close[i]
                    exit_reason = 'TO'
            elif position == -1:
                pnl_pct = (entry_price - close[i]) / entry_price
                if hold_bars >= HOLDING_BARS:
                    exit_price = close[i]
                    exit_reason = 'TO'

            if exit_price is not None:
                net_ret = pnl_pct - FEE
                position_value = capital * POS_PCT
                pnl = position_value * LEVERAGE * net_ret
                capital += pnl

                trades.append({
                    'direction': 'LONG' if position == 1 else 'SHORT',
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'return_pct': net_ret * 100,
                    'pnl': pnl,
                    'reason': exit_reason,
                    'hold_bars': hold_bars,
                    'rsi_entry': rsi[entry_idx],
                    'adx_entry': adx[entry_idx],
                })

                last_exit_idx = i
                if position == 1:
                    last_exit_long = i
                else:
                    last_exit_short = i
                position = 0
                entry_price = 0.0

        equity_curve.append(capital)

    if not trades:
        return None

    df_trades = pd.DataFrame(trades)
    total_return = (capital - initial_capital) / initial_capital
    n_trades = len(trades)
    win_trades = df_trades[df_trades['pnl'] > 0]
    lose_trades = df_trades[df_trades['pnl'] <= 0]
    win_rate = len(win_trades) / n_trades if n_trades > 0 else 0

    avg_win = win_trades['pnl'].mean() if len(win_trades) > 0 else 0
    avg_loss = abs(lose_trades['pnl'].mean()) if len(lose_trades) > 0 else 1
    wl_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    # 最大回撤
    equity = np.array(equity_curve)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_dd = abs(drawdown.min())

    # 年化
    start_date = df.index[WARMUP]
    end_date = df.index[-1]
    years = (end_date - start_date).days / 365.25
    annual_return = ((capital / initial_capital) ** (1 / years) - 1) if years > 0 else 0

    # 日均交易
    trading_days = max((end_date - start_date).days, 1)
    daily_trades = n_trades / trading_days

    return {
        'coin': coin_name,
        'name': name,
        'params': params,
        'total_return_pct': total_return * 100,
        'annual_return_pct': annual_return * 100,
        'max_drawdown_pct': max_dd * 100,
        'n_trades': n_trades,
        'win_rate_pct': win_rate * 100,
        'wl_ratio': wl_ratio,
        'daily_trades': daily_trades,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'capital': capital,
        'start_date': str(start_date)[:10],
        'end_date': str(end_date)[:10],
        'years': round(years, 2),
    }, df_trades


def walk_forward_validate(params, df_full, coin, n_splits=4):
    """Walk-Forward 4层验证"""
    n = len(df_full)
    if n < 3000:
        return None

    split_size = n // (n_splits + 1)
    passed = 0
    returns = []

    for i in range(n_splits):
        train_end = split_size * (i + 1)
        train_start = max(0, train_end - split_size * 2)
        df_train = df_full.iloc[train_start:train_end]
        test_start = train_end
        test_end = min(test_start + split_size, n)
        df_test = df_full.iloc[test_start:test_end]

        if len(df_train) < 1500 or len(df_test) < 500:
            continue

        result_train, _ = backtest(params, df_train, coin, name=f'train_{i}')
        result_test, _ = backtest(params, df_test, coin, name=f'test_{i}')

        if result_train and result_test:
            if result_train['total_return_pct'] > 0 and result_test['total_return_pct'] > 0:
                passed += 1
            returns.append(result_test['total_return_pct'])

    pass_rate = passed / n_splits if n_splits > 0 else 0
    avg_return = np.mean(returns) if returns else 0

    return {
        'pass_rate': pass_rate,
        'avg_test_return': avg_return,
        'n_splits': n_splits,
        'passed_splits': passed,
    }


def bear_market_2022(params, df_full, coin):
    """2022熊市验证"""
    try:
        df_2022 = df_full['2022-01-01':'2022-12-31']
        if len(df_2022) < 1000:
            return None
        result, _ = backtest(params, df_2022, coin, name='bear_2022')
        return result
    except:
        return None


# ── 主研究循环 ──────────────────────────────────────────────────────

def generate_param_combinations():
    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    count = 0
    for combo in itertools.product(*values):
        yield dict(zip(keys, combo))
        count += 1


def run_research_round(round_num, coins, round_name=''):
    """跑一轮研究"""
    print(f"\n{'='*70}")
    print(f"🔬 第{round_num}轮 {round_name} | 币种: {coins}")
    print(f"{'='*70}")

    results_all = []
    param_list = list(generate_param_combinations())
    total_combos = len(param_list)
    print(f"参数组合数: {total_combos}")

    start_time = time.time()

    for coin in coins:
        print(f"\n📊 {coin}...", end=" ", flush=True)

        df = load_coin_data(coin)
        print(f"({len(df)}行 {str(df.index[0])[:10]}~{str(df.index[-1])[:10]})", end=" ", flush=True)

        combo_idx = 0
        for params in param_list:
            combo_idx += 1

            if combo_idx % 1000 == 0:
                elapsed = time.time() - start_time
                print(f"\n  ⏱️ {coin} {combo_idx}/{total_combos} ({elapsed:.0f}s)", end="", flush=True)

            result, _ = backtest(params, df, coin)

            if result is None:
                continue

            # 基础过滤：正收益 + 盈亏比>1.5 + 日均>=0.5笔
            if not (result['total_return_pct'] > 0 and
                    result['wl_ratio'] > 1.5 and
                    result['daily_trades'] >= 0.5):
                continue

            # Walk-Forward 全量验证（不在抽样！）
            wf = walk_forward_validate(params, df, coin, n_splits=4)
            if wf:
                result['wf_pass_rate'] = wf['pass_rate']
                result['wf_avg_return'] = wf['avg_test_return']
                result['wf_passed_splits'] = wf['passed_splits']

            # 2022熊市验证
            bear = bear_market_2022(params, df, coin)
            if bear:
                result['bear_2022_return'] = bear['total_return_pct']
                result['bear_2022_wl_ratio'] = bear.get('wl_ratio', 0)

            results_all.append(result)

            # 每200个组合输出进度
            if combo_idx % 200 == 0:
                print(".", end="", flush=True)

    elapsed = time.time() - start_time
    print(f"\n\n⏱️ 本轮耗时: {elapsed:.0f}s, 达标结果: {len(results_all)}")

    if not results_all:
        print("⚠️ 无达标结果")
        return None

    df_results = pd.DataFrame(results_all)

    # 排序：先WF通过率，再WL比率
    df_results = df_results.sort_values(
        ['wf_pass_rate', 'wl_ratio', 'bear_2022_return'],
        ascending=[False, False, False]
    )

    print(f"\n{'币种':<6} {'总收益':>9} {'年化':>9} {'DD':>7} {'WLR':>6} {'胜率':>7} {'日均':>6} {'WF通过':>9} {'2022':>8}")
    print("-" * 75)

    for _, row in df_results.head(20).iterrows():
        wf_str = f"{row.get('wf_pass_rate', 0):.0%}({row.get('wf_passed_splits', 0)}/4)" if pd.notna(row.get('wf_pass_rate')) else '-'
        bear_str = f"{row['bear_2022_return']:+.0f}%" if pd.notna(row.get('bear_2022_return')) else '-'
        print(f"{row['coin']:<6} {row['total_return_pct']:>+8.1f}% {row['annual_return_pct']:>+8.1f}% {row['max_drawdown_pct']:>6.1f}% {row['wl_ratio']:>6.2f} {row['win_rate_pct']:>6.1f}% {row['daily_trades']:>6.2f} {wf_str:>9} {bear_str:>8}")

    return df_results


def main():
    print("=" * 70)
    print("🌙 通宵量化研究循环 v2 | 2026-04-18")
    print("目标: 盈亏比>2.0, Walk-Forward 100%通过, 2022熊市正收益")
    print("核心: 无止损 + MA趋势过滤 + 72h持仓")
    print("=" * 70)

    end_time = datetime(2026, 4, 18, 9, 0)
    now = datetime.now()
    remaining = (end_time - now).total_seconds() / 3600
    print(f"\n剩余时间: {remaining:.1f}小时")

    round_num = 0
    all_top_results = []

    while datetime.now() < end_time:
        round_num += 1

        # Round 1: DOT优先（唯一通过2022熊市的策略）
        if round_num == 1:
            results = run_research_round(round_num, ['DOT', 'DOGE', 'AVAX'], 'Round1-主流币')

        # Round 2: BCH + ADA
        elif round_num == 2:
            results = run_research_round(round_num, ['BCH', 'ADA', 'BNB'], 'Round2-中型币')

        # Round 3: BTC + ETH
        elif round_num == 3:
            results = run_research_round(round_num, ['BTC', 'ETH'], 'Round3-主流币')

        # Round 4+: 最优参数深度细粒度扫描
        elif round_num >= 4 and results is not None and len(results) > 0:
            top_params = results.head(3)['params'].tolist()
            print(f"\n🔍 Round {round_num}: 深度扫描Top3参数")

            # 细粒度网格（基于top参数微调）
            fine_grid = {
                'rsi_oversold': [33, 35, 37],
                'rsi_overbought': [62, 65, 68],
                'adx_min': [14, 15, 16],
                'leverage': [1, 2],
                'pos_pct': [0.18, 0.20, 0.22],
                'holding_bars': [60, 72, 84],
                'ma_filter': [True],
                'cooldown_bars': [24, 30],
            }

            global PARAM_GRID
            PARAM_GRID = fine_grid

            results = run_research_round(round_num, ['DOT', 'BCH', 'AVAX'], f'Round{round_num}-深度扫描')
            PARAM_GRID = {
                'rsi_oversold': [30, 35, 40, 45],
                'rsi_overbought': [60, 65, 70],
                'adx_min': [15, 18, 20],
                'leverage': [1, 2, 3],
                'pos_pct': [0.15, 0.20],
                'holding_bars': [48, 72, 96],
                'ma_filter': [True],
                'cooldown_bars': [24, 36],
            }

        # 保存每轮结果
        if results is not None and len(results) > 0:
            timestamp = datetime.now().strftime("%H%M%S")
            output_path = f'{OUTPUT_DIR}/round_{round_num}_{timestamp}.json'
            results.to_json(output_path, orient='records', indent=2)
            print(f"\n✅ 结果已保存: {output_path}")

            # 保存Top3
            for idx in range(min(3, len(results))):
                top = results.iloc[idx]
                all_top_results.append({
                    'round': round_num,
                    'coin': top['coin'],
                    'total_return_pct': float(top['total_return_pct']),
                    'wl_ratio': float(top['wl_ratio']),
                    'win_rate_pct': float(top['win_rate_pct']),
                    'annual_return_pct': float(top['annual_return_pct']),
                    'max_drawdown_pct': float(top['max_drawdown_pct']),
                    'daily_trades': float(top['daily_trades']),
                    'wf_pass_rate': float(top.get('wf_pass_rate', 0)),
                    'bear_2022_return': float(top.get('bear_2022_return', 0)),
                    'params': dict(top['params']),
                })

            top = results.iloc[0]
            print(f"\n🏆 当前最优: {top['coin']} WLR={top['wl_ratio']:.2f} "
                  f"WF={top.get('wf_pass_rate', 0):.0%} "
                  f"2022={top.get('bear_2022_return', 0):+.0f}%")

        time.sleep(3)

    # ── 最终汇总 ──
    if all_top_results:
        print("\n" + "=" * 70)
        print("🏆 最终最优策略汇总")
        print("=" * 70)

        df_final = pd.DataFrame(all_top_results)
        df_final = df_final.sort_values(['wf_pass_rate', 'wl_ratio', 'bear_2022_return'],
                                         ascending=[False, False, False])

        for _, row in df_final.iterrows():
            print(f"\n🥇 {row['coin']} (Round {row['round']})")
            print(f"   收益: {row['total_return_pct']:+.1f}% | 年化: {row['annual_return_pct']:+.1f}%")
            print(f"   盈亏比: {row['wl_ratio']:.2f} | 胜率: {row['win_rate_pct']:.1f}%")
            print(f"   最大DD: {row['max_drawdown_pct']:.1f}% | 日均: {row['daily_trades']:.2f}笔")
            print(f"   WF通过率: {row['wf_pass_rate']:.0%} | 2022熊市: {row['bear_2022_return']:+.0f}%")
            print(f"   参数: RSI<{row['params']['rsi_oversold']}/>{row['params']['rsi_overbought']} "
                  f"ADX>{row['params']['adx_min']}")
            print(f"   持仓: {row['params']['holding_bars']}根 | 杠杆: {row['params']['leverage']}x "
                  f"| 仓位: {row['params']['pos_pct']*100:.0f}%")

        # 保存最终结果
        final_path = f'{OUTPUT_DIR}/FINAL_RESULT.json'
        df_final.to_json(final_path, orient='records', indent=2)
        print(f"\n✅ 最终结果已保存: {final_path}")

    print("\n🎉 研究完成!")


if __name__ == '__main__':
    main()
