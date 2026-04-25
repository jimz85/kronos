#!/usr/bin/env python3
"""
每周交易复盘 - 布林双策略
分析过去一周的交易表现，生成报告并发送飞书
"""
import json
import os
import sys
from datetime import datetime, timedelta

TRADE_LOG = os.path.join(os.path.dirname(__file__), 'dual_strategy_trades.csv')
STATE_FILE = os.path.join(os.path.dirname(__file__), 'dual_strategy_state.json')
OUTPUT_FILE = os.path.expanduser('~/.hermes/kronos/weekly_review.txt')

def load_trades():
    """加载交易记录"""
    if not os.path.exists(TRADE_LOG):
        return []
    
    trades = []
    with open(TRADE_LOG) as f:
        header = f.readline()
        for line in f:
            parts = line.strip().split(',')
            if len(parts) >= 7:
                try:
                    trades.append({
                        'time': parts[0],
                        'strategy': parts[1],
                        'coin': parts[2],
                        'action': parts[3],
                        'entry': float(parts[4]) if parts[4] else 0,
                        'exit': float(parts[5]) if parts[5] else 0,
                        'pnl_pct': float(parts[6]) if parts[6] else 0,
                        'reason': parts[7] if len(parts) > 7 else '',
                    })
                except:
                    pass
    return trades

def analyze_week(trades, start_date, end_date):
    """分析指定时间段内的交易"""
    filtered = []
    for t in trades:
        try:
            t_time = datetime.fromisoformat(t['time'])
            if start_date <= t_time <= end_date:
                filtered.append(t)
        except:
            pass
    
    if not filtered:
        return None
    
    entries = [t for t in filtered if t['action'] == 'ENTRY']
    exits = [t for t in filtered if t['action'] == 'EXIT']
    
    # 按币种分组
    by_coin = {}
    for t in exits:
        coin = t['coin']
        if coin not in by_coin:
            by_coin[coin] = []
        by_coin[coin].append(t)
    
    # 统计
    pnls = [t['pnl_pct'] for t in exits]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    
    report = {
        'period': f"{start_date.strftime('%m-%d')} ~ {end_date.strftime('%m-%d')}",
        'total_trades': len(exits),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': len(wins) / len(pnls) * 100 if pnls else 0,
        'total_pnl': sum(pnls),
        'avg_win': sum(wins) / len(wins) if wins else 0,
        'avg_loss': sum(losses) / len(losses) if losses else 0,
        'max_win': max(wins) if wins else 0,
        'max_loss': min(losses) if losses else 0,
        'by_coin': {},
        'by_reason': {},
        'exits': exits,
    }
    
    # 币种分析
    for coin, coin_trades in by_coin.items():
        coin_pnls = [t['pnl_pct'] for t in coin_trades]
        report['by_coin'][coin] = {
            'trades': len(coin_trades),
            'total': sum(coin_pnls),
            'win_rate': len([p for p in coin_pnls if p > 0]) / len(coin_pnls) * 100,
        }
    
    # 原因分析
    for t in exits:
        reason = t['reason']
        if reason not in report['by_reason']:
            report['by_reason'][reason] = {'count': 0, 'total': 0}
        report['by_reason'][reason]['count'] += 1
        report['by_reason'][reason]['total'] += t['pnl_pct']
    
    return report

def generate_report(report, state):
    """生成文字报告"""
    if report is None:
        return f"""📊 布林双策略周报
{'-'*40}
📅 周期: {datetime.now().strftime('%Y-%m-%d')}
❌ 本周无交易记录"""

    lines = [
        f"📊 布林双策略周报",
        f"{'='*40}",
        f"📅 周期: {report['period']}",
        f"",
    ]
    
    # 核心数据
    emoji = "🟢" if report['total_pnl'] > 0 else "🔴"
    lines.append(f"{emoji} 总收益: {report['total_pnl']:+.2f}%")
    lines.append(f"   交易次数: {report['total_trades']}笔")
    lines.append(f"   胜率: {report['win_rate']:.0f}% ({report['wins']}胜/{report['losses']}负)")
    lines.append(f"   平均盈利: {report['avg_win']:+.2f}% | 平均亏损: {report['avg_loss']:+.2f}%")
    lines.append(f"   最大单笔盈利: {report['max_win']:+.2f}% | 最大单笔亏损: {report['max_loss']:+.2f}%")
    
    # 币种分布
    if report['by_coin']:
        lines.append(f"")
        lines.append(f"📦 币种分布:")
        for coin, data in report['by_coin'].items():
            lines.append(f"   {coin}: {data['trades']}笔 {data['total']:+.1f}% (胜率{data['win_rate']:.0f}%)")
    
    # 出场原因
    if report['by_reason']:
        lines.append(f"")
        lines.append(f"📋 出场原因:")
        for reason, data in sorted(report['by_reason'].items(), key=lambda x: -x[1]['count']):
            lines.append(f"   {reason}: {data['count']}笔 {data['total']:+.1f}%")
    
    # 当前持仓
    lines.append(f"")
    lines.append(f"💼 当前持仓:")
    active = []
    for strat in ['daily', '1h']:
        for coin, pos in state.get(strat, {}).items():
            if pos.get('position', 0) == 1:
                pnl = 0  # 无法计算实时浮盈
                active.append(f"   {coin}@{pos.get('entry', 0):.4f} ({strat})")
    if active:
        lines.extend(active)
    else:
        lines.append(f"   空仓")
    
    lines.append(f"")
    lines.append(f"⏰ 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    return '\n'.join(lines)

def main():
    trades = load_trades()
    
    # 分析过去7天
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    
    report = analyze_week(trades, start_date, end_date)
    
    # 加载状态
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    
    # 生成报告
    report_text = generate_report(report, state)
    print(report_text)
    
    # 保存
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, 'w') as f:
        f.write(report_text)
    print(f"\n报告已保存: {OUTPUT_FILE}")
    
    return report_text

if __name__ == '__main__':
    main()
