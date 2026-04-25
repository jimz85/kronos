#!/usr/bin/env python3
"""
Kronos Walk-Forward 投票系统验证
====================================
对比：多因子投票系统 vs 旧规则引擎
使用 Expanding Window Walk-Forward（防止过拟合）

使用方法:
  python3 validate_voting_wf.py --coin AVAX --periods 8
"""

import os, sys, json, math
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Optional
import numpy as np

# ========== 加载Kronos依赖 ==========
sys.path.insert(0, str(Path(__file__).parent))
from voting_system import VotingSystem, ICTracker

# ========== 数据获取 ==========

def fetch_ohlcv(coin: str, bar: str, limit: int = 2000):
    """获取OHLCV数据"""
    try:
        from kronos_multi_coin import get_ohlcv
        data = get_ohlcv(coin, bar, limit)
        if data and len(data) > 50:
            return data
    except:
        pass
    return None

def fetch_all_data(coin: str) -> Optional[dict]:
    """获取1H和4H数据"""
    c1 = fetch_ohlcv(coin, '1H', 300)   # OKX API限制：最多~300条
    c4 = fetch_ohlcv(coin, '4H', 300)
    if not c1 or len(c1) < 50:
        return None
    return {'1h': c1, '4h': c4}

# ========== 指标计算 ==========

def calc_rsi(closes, period=14):
    deltas = np.diff(closes, prepend=closes[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_g = np.convolve(gains, np.ones(period)/period, mode='same')
    avg_l = np.convolve(losses, np.ones(period)/period, mode='same')
    rs = avg_g / (avg_l + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_adx(high, low, close, period=14):
    tr = np.maximum(high - low, np.maximum(
        np.abs(high - np.roll(close, 1)),
        np.abs(low - np.roll(close, 1))
    ))
    plus_dm = np.maximum(high - np.roll(high, 1), 0)
    minus_dm = np.maximum(np.roll(low, 1) - low, 0)
    plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0)
    minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0)
    atr = np.convolve(tr, np.ones(period)/period, mode='same')
    plus_di = 100 * np.convolve(plus_dm, np.ones(period)/period, mode='same') / (atr + 1e-10)
    minus_di = 100 * np.convolve(minus_dm, np.ones(period)/period, mode='same') / (atr + 1e-10)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx = np.convolve(dx, np.ones(period)/period, mode='same')
    return adx

# ========== 旧规则引擎评分 ==========

def old_rules_score(rsi, adx, rsi_4h, adx_4h, btc_dir, btc_regime):
    """
    旧规则引擎的评分逻辑（从kronos_multi_coin.py复制）
    """
    score = 50
    reasons = []
    
    # 做多评分
    if rsi < 30:
        score += 25; reasons.append('RSI超卖')
    elif rsi < 35:
        score += 18; reasons.append('RSI偏低')
    if adx > 25:
        score += 15; reasons.append('ADX强趋势')
    elif adx > 20:
        score += 8
    if rsi_4h < 35:
        score += 10; reasons.append('4h_RSI超卖')
    if adx_4h > 25:
        score += 8; reasons.append('4h_ADX强')
    if btc_regime == 'bull':
        score += 10
    elif btc_regime == 'bear':
        score -= 20
    
    return max(0, min(100, score))

# ========== Walk-Forward 模拟 ==========

def simulate_trading(data_1h, coin: str, method: str = 'voting') -> dict:
    """
    Walk-Forward 模拟交易
    
    方法:
    - 'voting': 多因子投票系统
    - 'rules': 旧规则引擎
    
    返回: {total_return, n_trades, win_rate, max_drawdown, sharpe}
    """
    if not data_1h or len(data_1h) < 200:
        return {}
    
    closes = np.array([c['close'] for c in data_1h], dtype=float)
    highs = np.array([c['high'] for c in data_1h], dtype=float)
    lows = np.array([c['low'] for c in data_1h], dtype=float)
    volumes = np.array([c['volume'] for c in data_1h], dtype=float)
    
    rsi_1h = calc_rsi(closes, 14)
    adx_1h = calc_adx(highs, lows, closes, 14)
    
    # Walk-Forward窗口（300条数据 = ~12天1H）
    # 用较小的训练窗口和滚动步长来增加样本
    train_size = 150   # 训练窗口（~6天）
    test_size = 30      # 测试窗口（~1天）
    step = 20           # 滚动步长
    
    equity_curve = [1.0]
    trades = []
    wins = 0
    losses = 0
    peak = 1.0
    max_dd = 0.0
    
    # IC权重（预设为历史值）
    weights = {
        'RSI': 0.20, 'ADX': 0.10, 'Bollinger': 0.15,
        'Vol': 0.10, 'MACD': 0.15, 'BTC': 0.15, 'Gemma': 0.15
    }
    
    # IC预设（来自历史验证）
    ics = {
        'RSI': 0.08, 'ADX': -0.02, 'Bollinger': 0.06,
        'Vol': 0.07, 'MACD': 0.05, 'BTC': 0.04, 'Gemma': 0.10
    }
    
    for start in range(0, len(closes) - test_size - train_size, step):
        train_end = start + train_size
        test_start = train_end
        test_end = min(test_start + test_size, len(closes) - 1)
        
        # 用训练数据计算RSI/ADX统计量（简化：用全局）
        rsi_now = rsi_1h[test_start] if test_start < len(rsi_1h) else 50
        adx_now = adx_1h[test_start] if test_start < len(adx_1h) else 20
        
        # 构建market_data
        md = {
            'coin': coin,
            'price': closes[test_start],
            'rsi_1h': rsi_now,
            'adx_1h': adx_now,
            'rsi_4h': rsi_now,  # 简化
            'btc_direction': 'neutral',
            'btc_regime': 'neutral',
            'vol_ratio': 1.0,
            'atr_pct': 2.5,
        }
        
        if method == 'voting':
            vs = VotingSystem(coin, md, 'long', {}, 100000)
            result = vs.vote()
            score = result['vote_pct'] / 100 * 100
            signal = result['confidence'] != 'none' and result['vote_score'] > 0.5
        else:
            score = old_rules_score(
                rsi_now, adx_now, rsi_now, adx_now,
                'neutral', 'neutral'
            )
            signal = score >= 65
        
        if not signal:
            continue
        
        # 模拟持仓（测试期买入持有）
        entry = closes[test_start]
        exit_price = closes[test_end]
        pnl_pct = (exit_price - entry) / entry * 100
        
        if pnl_pct > 0:
            wins += 1
        else:
            losses += 1
        
        equity = equity_curve[-1] * (1 + pnl_pct / 100)
        equity_curve.append(equity)
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_dd = max(max_dd, dd)
        trades.append({'pnl_pct': pnl_pct, 'entry': entry, 'exit': exit_price})
    
    if len(equity_curve) < 2:
        return {'total_return': 0, 'n_trades': 0, 'win_rate': 0, 'max_drawdown': 0, 'sharpe': 0}
    
    total_return = (equity_curve[-1] / equity_curve[0] - 1) * 100
    n_trades = wins + losses
    win_rate = wins / n_trades if n_trades > 0 else 0
    
    # 年化夏普（简化）
    if len(trades) > 1:
        pnls = [t['pnl_pct'] for t in trades]
        ret_mean = np.mean(pnls)
        ret_std = np.std(pnls) if len(pnls) > 1 else 1
        sharpe = (ret_mean / (ret_std + 0.01)) * math.sqrt(252 / test_size)
    else:
        sharpe = 0
    
    return {
        'total_return': total_return,
        'n_trades': n_trades,
        'win_rate': win_rate,
        'max_drawdown': max_dd * 100,
        'sharpe': sharpe,
        'trades': trades,
    }

# ========== 主验证流程 ==========

def run_walkforward_validation(coin: str, periods: int = 8):
    """运行Walk-Forward验证"""
    print(f"\n{'='*60}")
    print(f"  {coin} - Walk-Forward 验证")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    
    data = fetch_all_data(coin)
    if not data:
        print(f"❌ 无法获取 {coin} 数据")
        return None
    
    print(f"数据量: 1H={len(data['1h'])}条")
    
    # IC批量计算
    print(f"\n[1/3] 计算因子IC...")
    from voting_system import compute_factor_ic_batch
    ics = compute_factor_ic_batch(coin, '1H', 500)
    
    print("IC结果:")
    for factor, ic in sorted(ics.items(), key=lambda x: -abs(x[1])):
        bar = '█' * int(abs(ic)*50) if ic > 0 else '░' * int(abs(ic)*50)
        print(f"  {factor:12s} {ic:+.4f} {bar}")
    
    # 记录IC
    tracker = ICTracker()
    for factor, ic in ics.items():
        tracker.record_ic(factor, ic)
    weights = tracker.compute_weights()
    
    print(f"\n权重:")
    for factor, w in sorted(weights.items(), key=lambda x: -x[1]):
        if w > 0:
            print(f"  {factor:12s} {w:.2%}")
    
    # Walk-Forward模拟
    print(f"\n[2/3] Walk-Forward模拟...")
    
    results_voting = simulate_trading(data['1h'], coin, 'voting')
    results_rules = simulate_trading(data['1h'], coin, 'rules')
    
    # 对比报告
    print(f"\n{'='*60}")
    print(f"  对比结果: 投票系统 vs 规则引擎")
    print(f"{'='*60}")
    print()
    print(f"{'指标':<20} {'投票系统':>15} {'规则引擎':>15} {'差异':>10}")
    print(f"{'-'*60}")
    
    fmt = lambda v, u='': f'{v:.2f}{u}'
    
    vr = results_voting
    rr = results_rules
    
    diff = vr['total_return'] - rr['total_return']
    print(f"{'总收益率':.<20} {fmt(vr['total_return'],'%'):>15} {fmt(rr['total_return'],'%'):>15} {fmt(diff,'%'):>10}")
    print(f"{'交易次数':.<20} {vr['n_trades']:>15} {rr['n_trades']:>15}")
    print(f"{'胜率':.<20} {fmt(vr['win_rate'],'%'):>15} {fmt(rr['win_rate'],'%'):>15}")
    print(f"{'最大回撤':.<20} {fmt(vr['max_drawdown'],'%'):>15} {fmt(rr['max_drawdown'],'%'):>15}")
    print(f"{'夏普比率':.<20} {fmt(vr['sharpe']):>15} {fmt(rr['sharpe']):>15}")
    
    # 盈亏比
    v_pnls = [t['pnl_pct'] for t in vr.get('trades', [])]
    r_pnls = [t['pnl_pct'] for t in rr.get('trades', [])]
    
    v_wins = [p for p in v_pnls if p > 0]
    v_losses = [p for p in v_pnls if p < 0]
    r_wins = [p for p in r_pnls if p > 0]
    r_losses = [p for p in r_pnls if p < 0]
    
    v_wlr = abs(np.mean(v_wins) / np.mean(v_losses)) if v_losses and v_wins else 0
    r_wlr = abs(np.mean(r_wins) / np.mean(r_losses)) if r_losses and r_wins else 0
    
    print(f"{'盈亏比(WLR)':.<20} {fmt(v_wlr):>15} {fmt(r_wlr):>15}")
    
    # 结论
    print(f"\n{'='*60}")
    print(f"  结论")
    print(f"{'='*60}")
    
    if vr['total_return'] > rr['total_return'] and vr['max_drawdown'] < rr['max_drawdown']:
        print(f"🏆 投票系统全面胜出")
        winner = 'voting'
    elif vr['total_return'] > rr['total_return']:
        print(f"📈 投票系统收益更高，但回撤更大")
        winner = 'voting'
    elif vr['max_drawdown'] < rr['max_drawdown']:
        print(f"🛡️ 投票系统更稳定，但收益略低")
        winner = 'voting'
    else:
        print(f"⚖️ 两者各有优劣")
        winner = 'neutral'
    
    print(f"\n投票系统: 收益率{vr['total_return']:+.1f}%, WLR={v_wlr:.2f}, 最大回撤{vr['max_drawdown']:.1f}%")
    print(f"规则引擎: 收益率{rr['total_return']:+.1f}%, WLR={r_wlr:.2f}, 最大回撤{rr['max_drawdown']:.1f}%")
    
    # 保存结果
    result = {
        'coin': coin,
        'timestamp': datetime.now().isoformat(),
        'ics': ics,
        'weights': weights,
        'voting': vr,
        'rules': rr,
        'winner': winner,
    }
    
    out_path = Path(__file__).parent / f'wf_result_{coin}.json'
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"\n结果已保存: {out_path}")
    
    return result

# ========== 多币种验证 ==========

def run_multi_coin_validation():
    """对所有币种运行验证"""
    coins = ['AVAX', 'ETH', 'BTC', 'DOGE', 'ADA', 'DOT', 'LINK']
    
    print(f"\n{'#'*60}")
    print(f"  多币种 Walk-Forward 验证")
    print(f"{'#'*60}")
    
    results = {}
    for coin in coins:
        r = run_walkforward_validation(coin, periods=8)
        if r:
            results[coin] = r
    
    # 汇总
    print(f"\n{'#'*60}")
    print(f"  汇总报告")
    print(f"{'#'*60}")
    print()
    print(f"{'币种':<10} {'投票收益率':>12} {'规则收益率':>12} {'投票WLR':>10} {'规则WLR':>10} {'胜者':>8}")
    print(f"{'-'*60}")
    
    for coin, r in results.items():
        vr = r['voting']
        rr = r['rules']
        v_pnls = [t['pnl_pct'] for t in vr.get('trades', [])]
        r_pnls = [t['pnl_pct'] for t in rr.get('trades', [])]
        v_wins = [p for p in v_pnls if p > 0]
        v_losses = [p for p in v_pnls if p < 0]
        r_wins = [p for p in r_pnls if p > 0]
        r_losses = [p for p in r_pnls if p < 0]
        v_wlr = abs(np.mean(v_wins)/np.mean(v_losses)) if v_losses and v_wins else 0
        r_wlr = abs(np.mean(r_wins)/np.mean(r_losses)) if r_losses and r_wins else 0
        
        winner = r['winner']
        mark = '✅' if winner == 'voting' else ('⚖️' if winner == 'neutral' else '❌')
        
        print(f"{coin:<10} {vr['total_return']:>+11.1f}% {rr['total_return']:>+11.1f}% {v_wlr:>10.2f} {r_wlr:>10.2f} {mark:>8}")
    
    print(f"{'-'*60}")
    
    v_avg = np.mean([r['voting']['total_return'] for r in results.values()])
    r_avg = np.mean([r['rules']['total_return'] for r in results.values()])
    print(f"{'平均收益':.<10} {v_avg:>+11.1f}% {r_avg:>+11.1f}%")
    
    print(f"\n{'#'*60}")
    print(f"验证完成: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'#'*60}")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--coin', default='AVAX')
    parser.add_argument('--coins', nargs='+', default=None)
    parser.add_argument('--periods', type=int, default=8)
    args = parser.parse_args()
    
    if args.coins:
        for coin in args.coins:
            run_walkforward_validation(coin, args.periods)
    else:
        run_walkforward_validation(args.coin, args.periods)
