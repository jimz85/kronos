#!/usr/bin/env python3
"""
futures_backtest_engine.py
合约回测引擎 - 支持做多，做空、杠杆
"""
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

def calc_rsi(prices, period=14):
    prices = np.asarray(prices).flatten()
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = pd.Series(gains).rolling(period).mean()
    avg_loss = pd.Series(losses).rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_bollinger(prices, period=20, std_mult=2.0):
    prices = np.asarray(prices).flatten()
    ma = pd.Series(prices).rolling(period).mean()
    std = pd.Series(prices).rolling(period).std()
    return ma, ma + std_mult * std, ma - std_mult * std

def calc_atr(high, low, close, period=14):
    high = np.asarray(high).flatten()
    low = np.asarray(low).flatten()
    close = np.asarray(close).flatten()
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(period).mean()

def run_strategy(symbol, strategy_type, params, direction="both", leverage=1):
    periods = [
        ("2024-01-01", "2025-01-01", "牛市"),
        ("2025-01-01", "2026-01-01", "熊市"),
        ("2024-01-01", "2026-04-01", "全周期"),
    ]
    all_results = {}
    for start, end, period_name in periods:
        df = yf.download(symbol, start=start, end=end, progress=False)
        if df.empty:
            all_results[period_name] = {"error": "数据加载失败"}
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df = df.loc[:, df.columns.get_level_values(0)]
        p = np.asarray(df["Close"].values).flatten()
        h = np.asarray(df["High"].values).flatten()
        l = np.asarray(df["Low"].values).flatten()
        rsi = calc_rsi(p)
        trades_long = []
        trades_short = []
        
        if strategy_type == "RSI":
            rsi_buy = params.get("rsi_buy", 30)
            rsi_sell = params.get("rsi_sell", 70)
            stop_pct = params.get("stop_pct", 0.04)
            hold_max = params.get("hold_max", 20)
            
            pos_long = None
            for i in range(20, len(p) - 1):
                if pos_long is None and float(rsi.iloc[i]) < rsi_buy:
                    pos_long = i
                elif pos_long is not None:
                    if p[i] <= p[pos_long] * (1 - stop_pct):
                        ret = (p[i] - p[pos_long]) / p[pos_long]
                        trades_long.append(ret * leverage)
                        pos_long = None
                    elif float(rsi.iloc[i]) > rsi_sell or i - pos_long >= hold_max:
                        ret = (p[i] - p[pos_long]) / p[pos_long]
                        trades_long.append(ret * leverage)
                        pos_long = None
            
            pos_short = None
            for i in range(20, len(p) - 1):
                if pos_short is None and float(rsi.iloc[i]) > rsi_sell:
                    pos_short = i
                elif pos_short is not None:
                    if p[i] >= p[pos_short] * (1 + stop_pct):
                        ret = (p[pos_short] - p[i]) / p[pos_short]
                        trades_short.append(ret * leverage)
                        pos_short = None
                    elif float(rsi.iloc[i]) < rsi_buy or i - pos_short >= hold_max:
                        ret = (p[pos_short] - p[i]) / p[pos_short]
                        trades_short.append(ret * leverage)
                        pos_short = None
        
        elif strategy_type == "BB":
            bb_period = int(params.get("bb_period", 20))
            bb_std = params.get("bb_std", 2.0)
            stop_atr = params.get("stop_atr", 1.5)
            ma, bb_upper, bb_lower = calc_bollinger(p, bb_period, bb_std)
            atr = calc_atr(h, l, p)
            
            pos_long = None
            for i in range(bb_period, len(p) - 1):
                if pos_long is None and p[i] > float(bb_upper.iloc[i]):
                    pos_long = i
                elif pos_long is not None:
                    stop = p[pos_long] - stop_atr * float(atr.iloc[pos_long])
                    if p[i] <= stop:
                        ret = (p[i] - p[pos_long]) / p[pos_long]
                        trades_long.append(ret * leverage)
                        pos_long = None
                    elif p[i] < float(ma.iloc[i]):
                        ret = (p[i] - p[pos_long]) / p[pos_long]
                        trades_long.append(ret * leverage)
                        pos_long = None
            
            pos_short = None
            for i in range(bb_period, len(p) - 1):
                if pos_short is None and p[i] < float(bb_lower.iloc[i]):
                    pos_short = i
                elif pos_short is not None:
                    stop = p[pos_short] + stop_atr * float(atr.iloc[pos_short])
                    if p[i] >= stop:
                        ret = (p[pos_short] - p[i]) / p[pos_short]
                        trades_short.append(ret * leverage)
                        pos_short = None
                    elif p[i] > float(ma.iloc[i]):
                        ret = (p[pos_short] - p[i]) / p[pos_short]
                        trades_short.append(ret * leverage)
                        pos_short = None
        
        if direction == "long":
            trades = trades_long
        elif direction == "short":
            trades = trades_short
        else:
            trades = trades_long + trades_short
        
        if not trades:
            all_results[period_name] = {"trades": 0, "win_rate": 0, "profit_factor": 0, "cagr": 0, "max_drawdown": 0, "eliminated": True}
            continue
        
        wins = [t for t in trades if t > 0]
        losses = [t for t in trades if t < 0]
        win_rate = len(wins) / len(trades)
        pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999
        
        equity = [1.0]
        for t in trades:
            equity.append(equity[-1] * (1 + t))
        equity = np.array(equity)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = abs(dd.min())
        total_ret = equity[-1] - 1
        days = len(df)
        cagr = (equity[-1]) ** (365.0 / days) - 1
        
        eliminated = win_rate < 0.45 or pf < 1.2 or max_dd > 0.50
        
        all_results[period_name] = {
            "trades": len(trades),
            "win_rate": win_rate,
            "profit_factor": pf,
            "cagr": cagr,
            "max_drawdown": max_dd,
            "total_return": total_ret,
            "long_trades": len(trades_long),
            "short_trades": len(trades_short),
            "long_pnl": sum(trades_long),
            "short_pnl": sum(trades_short),
            "eliminated": eliminated
        }
    
    return all_results

def print_results(symbol, strategy, direction, leverage, results):
    dir_str = {"both": "双向", "long": "做多", "short": "做空"}.get(direction, direction)
    print(f"\n{'='*60}")
    print(f"  {symbol} {strategy} | {dir_str} | {leverage}x杠杆")
    print(f"{'='*60}")
    for period, r in results.items():
        if "error" in r:
            print(f"  {period}: {r['error']}")
            continue
        status = "❌" if r.get("eliminated") else "✅"
        print(f"  {period}: {status}")
        print(f"    总交易: {r['trades']} (多:{r['long_trades']} 空:{r['short_trades']})")
        print(f"    胜率: {r['win_rate']:.1%} | PF: {r['profit_factor']:.2f}")
        print(f"    总收益: {r['total_return']:.1%} | 年化: {r['cagr']:.1%}")
        print(f"    最大DD: {r['max_drawdown']:.1%}")
        if r.get("long_trades", 0) + r.get("short_trades", 0) > 0:
            print(f"    多头: {r['long_pnl']:+.1%} | 空头: {r['short_pnl']:+.1%}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python3 futures_backtest_engine.py <symbol> <strategy> [options]")
        sys.exit(1)
    
    symbol = sys.argv[1]
    strategy = sys.argv[2]
    direction = "both"
    leverage = 1
    params = {}
    
    for i in range(3, len(sys.argv)):
        if sys.argv[i] == "--direction" and i+1 < len(sys.argv):
            direction = sys.argv[i+1]; i += 1
        elif sys.argv[i] == "--leverage" and i+1 < len(sys.argv):
            leverage = float(sys.argv[i+1]); i += 1
        elif sys.argv[i].startswith("--"):
            key = sys.argv[i][2:]
            try:
                val = float(sys.argv[i+1])
                params[key] = val
                i += 1
            except:
                pass
    
    results = run_strategy(symbol, strategy, params, direction, leverage)
    print_results(symbol, strategy, direction, leverage, results)
