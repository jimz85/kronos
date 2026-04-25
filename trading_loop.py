#!/usr/bin/env python3
"""
trading_loop.py
自动化交易信号生成 + 绩效追踪循环

Automaton Agent Loop 思路：
- Think: 分析当前市场数据
- Act: 生成交易信号
- Observe: 记录信号结果
- Persist: 更新绩效追踪 + 策略健康度

每次运行都是一次完整的 Think→Act→Observe 循环。
"""
import yfinance as yf
import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).parent))
from performance_tracker import (
    record_trade, record_signal, reflection_cycle,
    should_use_strategy, init_db, TradeRecord, SignalRecord,
    generate_performance_report
)
from adaptive_trading_v3 import (
    get_strategy, RSIStrategy, BBStrategy, STRATEGY_CONFIG
)

# ─── 市场数据 ─────────────────────────────────────────────────
def fetch_market_data(coins, interval='1d', lookback='30d'):
    """获取多个币种的市场数据"""
    data = {}
    for coin in coins:
        try:
            t = yf.Ticker(coin)
            df = t.history(period=lookback, interval=interval)
            if not df.empty:
                df = df[['Open','High','Low','Close','Volume']].copy()
                df.columns = ['open','high','low','close','volume']
                df.index = df.index.tz_localize(None) if df.index.tz else df.index
                data[coin] = df
        except Exception as e:
            print(f"  数据获取失败 {coin}: {e}")
    return data

def safe_df(df):
    """安全处理yfinance返回的DataFrame"""
    if df is None: return None
    needed = {'Open','High','Low','Close','Volume'}
    cols = [c for c in df.columns if c in needed]
    if len(cols) < 5: return None
    df = df[cols].copy()
    df.columns = ['open','high','low','close','volume']
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    return df.dropna()

# ─── 指标计算 ─────────────────────────────────────────────────
def calc_rsi(p, period=14):
    d = np.diff(p, prepend=p[0])
    g = np.where(d > 0, d, 0)
    l = np.where(d < 0, -d, 0)
    ag = pd.Series(g).rolling(period).mean()
    al = pd.Series(l).rolling(period).mean()
    return 100 - (100 / (1 + ag / (al + 1e-10)))

def calc_atr(h, l, c, period=14):
    prev = np.roll(c, 1); prev[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    return pd.Series(tr).rolling(period).mean()

def calc_bb(p, period=20, std_mult=2.5):
    ma = pd.Series(p).rolling(period).mean()
    std = pd.Series(p).rolling(period).std()
    return ma, ma + std_mult * std, ma - std_mult * std

# ─── 信号生成 ─────────────────────────────────────────────────
def generate_signals(data: dict) -> list[SignalRecord]:
    """
    对所有币种生成交易信号
    返回信号列表
    """
    signals = []
    
    for coin, df in data.items():
        p = df['close'].values.astype(float)
        h = df['high'].values.astype(float)
        l = df['low'].values.astype(float)
        n = len(p)
        if n < 25: continue
        
        rsi = calc_rsi(p)
        atr = calc_atr(h, l, p)
        ma, upper, lower = calc_bb(p)
        
        r = float(rsi.iloc[-1])
        curr_p = float(p[-1])
        a = float(atr.iloc[-1])
        u = float(upper.iloc[-1])
        m = float(ma.iloc[-1])
        prev_p = float(p[-2])
        
        cfg = STRATEGY_CONFIG.get(coin, {'type': 'none'})
        strat_type = cfg.get('type', 'none')
        
        # 检查策略是否可用
        can_use, reason = should_use_strategy(coin, strat_type)
        
        signal_type = 'skip'
        reason_str = ''
        
        if strat_type == 'rsi':
            # RSI均值回归
            stop_pct = cfg.get('stop_pct', 0.03)
            if not can_use:
                signal_type = 'skip'
                reason_str = f'strategy_unavailable: {reason}'
            elif r < cfg.get('rsi_buy', 35):
                signal_type = 'buy'
                reason_str = f'RSI={r:.0f}<{cfg.get("rsi_buy",35)}, stop={stop_pct:.0%}'
            elif r > cfg.get('rsi_sell', 65):
                signal_type = 'sell'
                reason_str = f'RSI={r:.0f}>{cfg.get("rsi_sell",65)}'
            else:
                signal_type = 'skip'
                reason_str = f'RSI={r:.0f}中性'
        
        elif strat_type == 'bb':
            # 布林趋势
            if not can_use:
                signal_type = 'skip'
                reason_str = f'strategy_unavailable: {reason}'
            elif prev_p <= u and curr_p > u and r < cfg.get('rsi_exit', 75):
                signal_type = 'buy'
                reason_str = f'BB突破@${curr_p:.0f}, RSI={r:.0f}'
            else:
                signal_type = 'skip'
                reason_str = f'无BB信号(Rsi={r:.0f})'
        
        elif strat_type == 'none':
            signal_type = 'skip'
            reason_str = 'DOGE: no trade'
        
        # 记录信号
        sig = SignalRecord(
            coin=coin,
            strategy=strat_type,
            signal_type=signal_type,
            price=curr_p,
            reason=reason_str,
            timestamp=datetime.now().isoformat(),
            followed=(signal_type == 'buy'),
            result='pending'
        )
        signals.append(sig)
        record_signal(sig)
        
    return signals

# ─── 持仓检查 + 平仓判断 ─────────────────────────────────────────────────
def check_positions(data: dict, open_trades: list) -> list[dict]:
    """
    检查现有持仓是否应平仓
    返回需要平仓的列表
    """
    closes = []
    
    for trade in open_trades:
        coin = trade['coin']
        if coin not in data:
            continue
        
        df = data[coin]
        p = df['close'].values.astype(float)
        h = df['high'].values.astype(float)
        l = df['low'].values.astype(float)
        n = len(p)
        if n < 25: continue
        
        rsi = calc_rsi(p)
        atr = calc_atr(h, l, p)
        ma, upper, lower = calc_bb(p)
        
        r = float(rsi.iloc[-1])
        curr_p = float(p[-1])
        a = float(atr.iloc[-1])
        m = float(ma.iloc[-1])
        entry_p = trade['entry_price']
        entry_atr = trade.get('entry_atr', a)
        cfg = STRATEGY_CONFIG.get(coin, {'type': 'none'})
        strat_type = cfg.get('type', 'none')
        
        should_close = False
        close_reason = ''
        
        if strat_type == 'rsi':
            stop_pct = cfg.get('stop_pct', 0.03)
            stop = entry_p * (1 - stop_pct)
            hold_days = (datetime.now() - datetime.fromisoformat(trade['entry_time'])).days
            
            if curr_p <= stop:
                should_close = True
                close_reason = f'stop({stop_pct:.0%})'
            elif r > cfg.get('rsi_sell', 65):
                should_close = True
                close_reason = f'RSI={r:.0f}>sell'
            elif hold_days >= cfg.get('hold_max', 15):
                should_close = True
                close_reason = f'hold_expired({hold_days}d)'
        
        elif strat_type == 'bb':
            trailing_atr = cfg.get('trailing_atr', 2.0)
            stop_atr = cfg.get('stop_atr', 2.0)
            highest_since = max(trade.get('highest_price', entry_p), curr_p)
            trailing_stop = highest_since - trailing_atr * entry_atr
            stop = entry_p - stop_atr * entry_atr
            
            if curr_p <= stop:
                should_close = True
                close_reason = f'stop({stop_atr}×ATR)'
            elif curr_p <= trailing_stop:
                should_close = True
                close_reason = f'trailing({trailing_atr}×ATR)'
            elif curr_p < m:
                should_close = True
                close_reason = 'below_MA'
        
        if should_close:
            pnl_pct = (curr_p - entry_p) / entry_p * 100
            closes.append({
                'trade': trade,
                'exit_price': curr_p,
                'exit_reason': close_reason,
                'pnl_pct': pnl_pct,
                'exit_time': datetime.now().isoformat()
            })
    
    return closes

# ─── 主循环 ─────────────────────────────────────────────────
def run_cycle():
    """运行一次完整的交易循环"""
    print(f"\n{'='*60}")
    print(f"  Kronos 交易循环  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    coins = ['BTC-USD', 'ETH-USD', 'BNB-USD', 'SOL-USD']
    
    # ── Think: 获取市场数据 ──
    print("\n[1/4] 获取市场数据...")
    data = fetch_market_data(coins, interval='1d', lookback='30d')
    if not data:
        print("数据获取失败")
        return
    print(f"  成功获取 {len(data)} 个币种数据")
    
    # ── Observe: 检查现有持仓 ──
    print("\n[2/4] 检查持仓状态...")
    conn = init_db()
    c = conn.cursor()
    c.execute("""
        SELECT coin, strategy, signal_price, entry_time, capital_used
        FROM trades WHERE realized=0
    """)
    open_rows = c.fetchall()
    conn.close()
    
    open_trades = []
    for row in open_rows:
        open_trades.append({
            'coin': row[0], 'strategy': row[1], 'entry_price': row[2],
            'entry_time': row[3], 'capital_used': row[4],
            'highest_price': None, 'entry_atr': None
        })
    
    print(f"  持仓数: {len(open_trades)}")
    
    # ── 检查平仓 ──
    closes = check_positions(data, open_trades)
    if closes:
        print(f"\n  平仓信号 ({len(closes)}笔):")
        for cl in closes:
            t = cl['trade']
            print(f"    {t['coin']}: ${cl['exit_price']:.0f} ({cl['exit_reason']}, {cl['pnl_pct']:+.2f}%)")
            # 记录平仓交易
            trade = TradeRecord(
                coin=t['coin'],
                strategy=t['strategy'],
                signal_price=t['entry_price'],
                exit_price=cl['exit_price'],
                exit_reason=cl['exit_reason'],
                entry_time=t['entry_time'],
                exit_time=cl['exit_time'],
                pnl_pct=cl['pnl_pct'],
                pnl_abs=cl['pnl_pct'] / 100 * t['capital_used'],
                capital_used=t['capital_used'],
                realized=True
            )
            record_trade(trade)
            # 标记原合约为已平仓（通过exit_price更新）
            # 这里简单处理：直接更新数据库
            conn2 = init_db()
            c2 = conn2.cursor()
            c2.execute("""
                UPDATE trades SET exit_price=?, exit_reason=?, exit_time=?,
                pnl_pct=?, realized=1
                WHERE coin=? AND strategy=? AND realized=0
                AND exit_time IS NULL
            """, (cl['exit_price'], cl['exit_reason'], cl['exit_time'],
                  cl['pnl_pct'], t['coin'], t['strategy']))
            conn2.commit()
            conn2.close()
    
    # ── Act: 生成新信号 ──
    print("\n[3/4] 生成交易信号...")
    signals = generate_signals(data)
    
    buy_signals = [s for s in signals if s.signal_type == 'buy']
    if buy_signals:
        print(f"  买入信号 ({len(buy_signals)}个):")
        for s in buy_signals:
            print(f"    🟢 {s.coin}: {s.reason} @ ${s.price:.0f}")
    else:
        print("  无买入信号")
    
    sell_signals = [s for s in signals if s.signal_type == 'sell']
    if sell_signals:
        print(f"  卖出信号 ({len(sell_signals)}个):")
        for s in sell_signals:
            print(f"    🔴 {s.coin}: {s.reason}")
    
    # ── Persist: 反思循环 ──
    print("\n[4/4] 绩效反思...")
    reflection_cycle()
    
    return {
        'signals': signals,
        'buy_signals': buy_signals,
        'closes': closes,
        'open_trades': len(open_trades)
    }

# ─── 快速诊断 ─────────────────────────────────────────────────
def diagnose():
    """快速诊断当前所有策略状态"""
    print(f"\n{'='*60}")
    print(f"  Kronos 策略诊断  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    coins = ['BTC-USD', 'ETH-USD', 'BNB-USD', 'SOL-USD', 'DOGE-USD']
    data = fetch_market_data(coins, interval='1d', lookback='30d')
    
    print(f"\n{'币种':<12} {'策略':<8} {'RSI':>5} {'BB位置':>10} {'信号':>8} {'可用性':>20}")
    print("-" * 70)
    
    for coin in coins:
        if coin not in data:
            print(f"{coin:<12} 数据获取失败")
            continue
        
        df = data[coin]
        p = df['close'].values.astype(float)
        h = df['high'].values.astype(float)
        l = df['low'].values.astype(float)
        n = len(p)
        
        rsi = calc_rsi(p)
        atr = calc_atr(h, l, p)
        ma, upper, lower = calc_bb(p)
        
        r = float(rsi.iloc[-1])
        curr_p = float(p[-1])
        u = float(upper.iloc[-1])
        lwr = float(lower.iloc[-1])
        m = float(ma.iloc[-1])
        
        cfg = STRATEGY_CONFIG.get(coin, {'type': 'none'})
        strat = cfg.get('type', 'none')
        
        can_use, reason = should_use_strategy(coin, strat)
        
        # BB位置
        if curr_p > u:
            bb_pos = f"上轨({curr_p/u:.2f})"
        elif curr_p < lwr:
            bb_pos = f"下轨({curr_p/lwr:.2f})"
        else:
            bb_pos = f"中轨({curr_p/m:.2f})"
        
        # 当前信号
        if strat == 'rsi':
            rsi_buy = cfg.get('rsi_buy', 35)
            rsi_sell = cfg.get('rsi_sell', 65)
            if r < rsi_buy:
                sig = "买入"
            elif r > rsi_sell:
                sig = "卖出"
            else:
                sig = "观望"
        elif strat == 'bb':
            if curr_p > u:
                sig = "买入"
            elif curr_p < m:
                sig = "可能卖出"
            else:
                sig = "观望"
        else:
            sig = "不交易"
        
        status = "✅" if can_use else f"❌{reason[:18]}"
        print(f"{coin:<12} {strat:<8} {r:>5.0f} {bb_pos:>12} {sig:>8} {status}")
    
    # 绩效摘要
    print()
    report = generate_performance_report()
    # 只打印关键部分
    lines = report.split('\n')
    print('\n'.join(lines[:35]))

# ─── 主入口 ─────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Kronos 自动化交易循环')
    parser.add_argument('--diagnose', '-d', action='store_true',
                       help='快速诊断当前市场状态')
    parser.add_argument('--cycle', '-c', action='store_true',
                       help='运行完整交易循环')
    parser.add_argument('--loop', '-l', type=int, default=0,
                       help='持续运行，每N分钟一次')
    args = parser.parse_args()
    
    if args.diagnose:
        diagnose()
    elif args.loop > 0:
        import time
        print(f"启动持续监控模式，每 {args.loop} 分钟运行一次")
        while True:
            try:
                result = run_cycle()
                time.sleep(args.loop * 60)
            except KeyboardInterrupt:
                print("\n停止")
                break
            except Exception as e:
                print(f"错误: {e}")
                time.sleep(60)
    else:
        run_cycle()
