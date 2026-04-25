"""
RSI+ADX 自动多空策略回测
========================
参数（来自2026-04-17最终报告）：
  - RSI > 70 → 做空
  - RSI < 30 → 做多
  - ADX > 15 → 趋势确认
  - 杠杆: 3x
  - 仓位: 10%
  - 止损: 5%
  - 止盈: 10%

数据源: /Users/jimingzhang/Desktop/crypto_data_Pre5m/
"""

import pandas as pd
import numpy as np
from datetime import datetime
import json

# ── 策略参数 ──────────────────────────────────────────────────────
RSI_OB   = 70       # RSI超买阈值（做空）
RSI_OS   = 30       # RSI超卖阈值（做多）
ADX_MIN  = 15       # ADX趋势确认阈值
LEVERAGE = 3        # 杠杆
POS_PCT  = 0.10     # 仓位 10%
SL_PCT   = 0.05     # 止损 5%
TP_PCT   = 0.10     # 止盈 10%
FEE      = 0.002    # 手续费 0.2%

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
COINS    = ['DOGE', 'AVAX', 'DOT', 'ADA', 'BNB', 'BTC', 'ETH', 'SOL']

# ── 指标计算 ──────────────────────────────────────────────────────

def calc_rsi(close, period=14):
    """计算RSI"""
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.inf)
    rsi = 100 - (100 / (1 + rs))
    return rsi.values

def calc_adx(high, low, close, period=14):
    """计算ADX"""
    high = pd.Series(high)
    low  = pd.Series(low)
    close = pd.Series(close)
    
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

# ── 加载数据 ──────────────────────────────────────────────────────

def load_coin_data(coin):
    """加载币种数据（5min）"""
    path = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    df = pd.read_csv(path)
    
    # 不同文件格式不同，兼容处理
    if 'vol' in df.columns:
        vol_col = 'vol'
    elif 'volume' in df.columns:
        vol_col = 'volume'
    else:
        vol_col = None
    
    # 时间戳处理
    if 'timestamp' in df.columns:
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_localize(None)
    elif 'datetime_utc' in df.columns:
        df['datetime'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    
    df = df.set_index('datetime').sort_index()
    
    # 选择需要的列
    cols = ['open', 'high', 'low', 'close']
    if vol_col:
        cols.append(vol_col)
    df = df[cols]
    
    if vol_col == 'vol':
        df = df.rename(columns={'vol': 'volume'})
    
    return df

def resample_to_1min(df):
    """5min转1min（简单复制或重采样）"""
    # 5min数据直接用，保持原始粒度
    return df

# ── 回测引擎 ──────────────────────────────────────────────────────

def backtest(df, coin_name):
    """
    RSI+ADX 策略回测
    
    多头信号: RSI < 30 AND ADX > 15
    空头信号: RSI > 70 AND ADX > 15
    
    持仓逻辑:
    - 开多后：止损5% OR 止盈10%
    - 开空后：止损5% OR 止盈10%
    - 仓位: 10% * 3x = 30% 账户资本
    """
    
    close = df['close'].values
    high  = df['high'].values
    low   = df['low'].values
    
    n = len(df)
    
    # 计算指标
    rsi   = calc_rsi(close)
    adx, _, _ = calc_adx(high, low, close)
    
    # 生成信号
    long_signal  = (rsi < RSI_OS) & (adx > ADX_MIN)
    short_signal = (rsi > RSI_OB) & (adx > ADX_MIN)
    
    # 初始化
    initial_capital = 10000  # 假设10000 USDT
    capital = initial_capital
    position = 0  # 0=无持仓, 1=多头, -1=空头
    entry_price = 0
    entry_idx = 0
    
    trades = []
    equity_curve = [capital]
    dates = [df.index[0]]
    
    for i in range(20, n - 1):  # 至少20根K线预热
        current_date = df.index[i]
        
        # ── 无持仓 → 检查信号 ──
        if position == 0:
            # 做多信号
            if long_signal[i]:
                position = 1
                entry_price = close[i + 1]  # 下一根开盘入场
                entry_idx = i + 1
            # 做空信号
            elif short_signal[i]:
                position = -1
                entry_price = close[i + 1]
                entry_idx = i + 1
                
        # ── 持仓中 → 检查止损止盈 ──
        elif position != 0:
            exit_price = None
            exit_reason = None
            ret_pct = 0
            
            if position == 1:  # 持有多头
                pnl_pct = (close[i] - entry_price) / entry_price
                
                # 止损
                if pnl_pct <= -SL_PCT:
                    exit_price = entry_price * (1 - SL_PCT)
                    exit_reason = 'SL'
                    ret_pct = -SL_PCT
                # 止盈
                elif pnl_pct >= TP_PCT:
                    exit_price = entry_price * (1 + TP_PCT)
                    exit_reason = 'TP'
                    ret_pct = TP_PCT
                    
            elif position == -1:  # 持有空头
                pnl_pct = (entry_price - close[i]) / entry_price
                
                # 止损
                if pnl_pct <= -SL_PCT:
                    exit_price = entry_price * (1 + SL_PCT)
                    exit_reason = 'SL'
                    ret_pct = -SL_PCT
                # 止盈
                elif pnl_pct >= TP_PCT:
                    exit_price = entry_price * (1 - TP_PCT)
                    exit_reason = 'TP'
                    ret_pct = TP_PCT
            
            # 执行出场
            if exit_price is not None:
                # 实际执行价格（手续费）
                net_ret = ret_pct - FEE
                position_value = capital * POS_PCT
                pnl = position_value * LEVERAGE * net_ret
                capital += pnl
                
                trades.append({
                    'date': current_date,
                    'coin': coin_name,
                    'direction': 'LONG' if position == 1 else 'SHORT',
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'return_pct': net_ret,
                    'pnl': pnl,
                    'reason': exit_reason,
                    'capital': capital,
                    'rsi_entry': rsi[entry_idx],
                    'adx_entry': adx[entry_idx],
                })
                
                position = 0
                entry_price = 0
            
        # 记录权益
        equity_curve.append(capital)
        dates.append(current_date)
    
    # ── 结果统计 ──
    if not trades:
        return None
    
    df_trades = pd.DataFrame(trades)
    
    total_return = (capital - initial_capital) / initial_capital * 100
    n_trades = len(trades)
    win_trades = df_trades[df_trades['pnl'] > 0]
    lose_trades = df_trades[df_trades['pnl'] <= 0]
    win_rate = len(win_trades) / n_trades * 100 if n_trades > 0 else 0
    
    # 最大回撤
    equity = np.array(equity_curve)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak * 100
    max_dd = abs(drawdown.min())
    
    # 年化
    start_date = df.index[20]
    end_date = df.index[-1]
    years = (end_date - start_date).days / 365.25
    annual_return = ((capital / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0
    
    # 每日交易
    trading_days = (end_date - start_date).days
    daily_trades = n_trades / trading_days if trading_days > 0 else 0
    
    result = {
        'coin': coin_name,
        'start_date': str(start_date)[:10],
        'end_date': str(end_date)[:10],
        'years': round(years, 2),
        'initial_capital': initial_capital,
        'final_capital': round(capital, 2),
        'total_return_pct': round(total_return, 2),
        'annual_return_pct': round(annual_return, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'n_trades': n_trades,
        'win_rate_pct': round(win_rate, 2),
        'daily_trades': round(daily_trades, 2),
        'avg_pnl': round(df_trades['pnl'].mean(), 2),
        'n_wins': len(win_trades),
        'n_loses': len(lose_trades),
    }
    
    return result, df_trades

# ── 主程序 ────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("RSI+ADX 自动多空策略 - 实时数据回测")
    print("=" * 70)
    print(f"\n策略参数:")
    print(f"  RSI超卖/超买: {RSI_OS}/{RSI_OB}")
    print(f"  ADX确认: > {ADX_MIN}")
    print(f"  杠杆: {LEVERAGE}x")
    print(f"  仓位: {POS_PCT*100}%")
    print(f"  止损/止盈: {SL_PCT*100}% / {TP_PCT*100}%")
    print(f"  手续费: {FEE*100}%")
    print("-" * 70)
    
    all_results = []
    all_trades = []
    
    for coin in COINS:
        print(f"\n📊 正在回测 {coin}...", end=" ")
        
        try:
            df = load_coin_data(coin)
            print(f"加载 {len(df)} 行数据 ({str(df.index[0])[:10]} ~ {str(df.index[-1])[:10]})")
            
            result = backtest(df, coin)
            
            if result is None:
                print(f"  ⚠️  {coin}: 无交易信号")
                continue
            
            res, trades = result
            all_results.append(res)
            all_trades.append(trades)
            
            print(f"  ✅ {coin}:")
            print(f"     总收益: {res['total_return_pct']:+.2f}%")
            print(f"     年化: {res['annual_return_pct']:+.2f}%")
            print(f"     最大DD: {res['max_drawdown_pct']:.2f}%")
            print(f"     交易次数: {res['n_trades']}")
            print(f"     胜率: {res['win_rate_pct']:.2f}%")
            print(f"     日均交易: {res['daily_trades']:.2f}笔/天")
            
        except Exception as e:
            import traceback
            print(f"  ❌ {coin}: 错误 - {e}")
            traceback.print_exc()
            continue
    # ── 汇总 ──
    if all_results:
        print("\n" + "=" * 70)
        print("📈 汇总结果")
        print("=" * 70)
        
        df_summary = pd.DataFrame(all_results)
        df_summary = df_summary.sort_values('total_return_pct', ascending=False)
        
        print(f"\n{'币种':<8} {'总收益':>10} {'年化':>10} {'最大DD':>10} {'交易数':>8} {'胜率':>8} {'日均':>8}")
        print("-" * 70)
        
        for _, row in df_summary.iterrows():
            print(f"{row['coin']:<8} {row['total_return_pct']:>+10.2f}% {row['annual_return_pct']:>+10.2f}% {row['max_drawdown_pct']:>10.2f}% {row['n_trades']:>8} {row['win_rate_pct']:>8.2f}% {row['daily_trades']:>8.2f}")
        
        print("-" * 70)
        
        # 多币种等权组合
        avg_return = df_summary['total_return_pct'].mean()
        avg_dd = df_summary['max_drawdown_pct'].mean()
        avg_daily = df_summary['daily_trades'].mean()
        
        print(f"\n5币种等权组合（排除BTC/ETH）:")
        top5 = df_summary[df_summary['coin'].isin(['DOGE', 'AVAX', 'DOT', 'ADA', 'BNB'])]
        if len(top5) > 0:
            print(f"  平均总收益: {top5['total_return_pct'].mean():+.2f}%")
            print(f"  平均年化: {top5['annual_return_pct'].mean():+.2f}%")
            print(f"  平均最大DD: {top5['max_drawdown_pct'].mean():.2f}%")
            print(f"  平均日均交易: {top5['daily_trades'].mean():.2f}笔/天")
        
        # 保存结果
        output_path = '~/kronos/rsi_adx_backtest_result.json'
        df_summary.to_json(output_path, orient='records', indent=2)
        print(f"\n✅ 结果已保存到: {output_path}")
        
    else:
        print("\n❌ 无有效回测结果")

if __name__ == '__main__':
    main()
