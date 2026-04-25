#!/usr/bin/env python3
"""
performance_tracker.py
交易绩效追踪 + 策略自我反馈系统

Automaton的Reflection Pipeline思路应用到交易：
- 每次策略运行 → 记录结果
- 每个结果 → 更新策略评分
- 评分下降 → 触发告警 + 策略重评估
- 持续低于预期 → 标记失效，停止策略

这不是事后诸葛，是实时的策略健康监控。
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass, asdict
from collections import defaultdict

DB_PATH = Path(__file__).parent / "performance.db"

@dataclass
class TradeRecord:
    coin: str
    strategy: str  # 'rsi' / 'bb' / 'market_sense'
    signal_price: float
    exit_price: Optional[float]
    exit_reason: Optional[str]  # 'stop' / 'rsisell' / 'hold_expire' / 'trailing' / 'end'
    entry_time: str
    exit_time: Optional[str]
    pnl_pct: Optional[float]  # 百分比盈亏
    pnl_abs: Optional[float]   # 绝对盈亏
    capital_used: float
    realized: bool  # 是否已平仓

@dataclass
class SignalRecord:
    coin: str
    strategy: str
    signal_type: str  # 'buy' / 'sell' / 'skip'
    price: float
    reason: str
    timestamp: str
    followed: bool  # 是否跟随了信号
    result: Optional[str]  # 'pending' / 'win' / 'loss' / 'skipped'

@dataclass
class StrategyMetrics:
    strategy: str
    coin: str
    total_signals: int
    signals_followed: int
    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    total_pnl_pct: float
    avg_pnl_pct: float
    max_win_pct: float
    max_loss_pct: float
    pf: float  # profit factor
    last_updated: str
    health_score: float  # 0-100,策略健康度

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            strategy TEXT NOT NULL,
            signal_price REAL NOT NULL,
            exit_price REAL,
            exit_reason TEXT,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            pnl_pct REAL,
            pnl_abs REAL,
            capital_used REAL NOT NULL,
            realized INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            strategy TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            price REAL NOT NULL,
            reason TEXT,
            timestamp TEXT NOT NULL,
            followed INTEGER NOT NULL DEFAULT 0,
            result TEXT DEFAULT 'pending'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS strategy_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            coin TEXT NOT NULL,
            total_signals INTEGER DEFAULT 0,
            signals_followed INTEGER DEFAULT 0,
            closed_trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            avg_win_pct REAL DEFAULT 0,
            avg_loss_pct REAL DEFAULT 0,
            total_pnl_pct REAL DEFAULT 0,
            avg_pnl_pct REAL DEFAULT 0,
            max_win_pct REAL DEFAULT 0,
            max_loss_pct REAL DEFAULT 0,
            pf REAL DEFAULT 0,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
            health_score REAL DEFAULT 100,
            UNIQUE(strategy, coin)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            coin TEXT,
            strategy TEXT,
            message TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            acknowledged INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 索引
    c.execute("CREATE INDEX IF NOT EXISTS idx_trades_coin ON trades(coin)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp)")
    conn.commit()
    return conn

# ─── 交易记录 ─────────────────────────────────────────────────
def record_trade(trade: TradeRecord) -> int:
    conn = init_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades (coin, strategy, signal_price, exit_price, exit_reason,
                          entry_time, exit_time, pnl_pct, pnl_abs, capital_used, realized)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade.coin, trade.strategy, trade.signal_price, trade.exit_price,
        trade.exit_reason, trade.entry_time, trade.exit_time,
        trade.pnl_pct, trade.pnl_abs, trade.capital_used, 1 if trade.realized else 0
    ))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    
    # 更新策略评分
    _update_strategy_metrics(trade.coin, trade.strategy)
    return trade_id

def _update_strategy_metrics(coin: str, strategy: str):
    """根据最近20笔交易更新策略评分"""
    conn = init_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT pnl_pct FROM trades
        WHERE coin=? AND strategy=? AND realized=1 AND pnl_pct IS NOT NULL
        ORDER BY created_at DESC LIMIT 20
    """, (coin, strategy))
    rows = c.fetchall()
    
    if len(rows) < 3:
        conn.close()
        return
    
    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    
    win_rate = len(wins) / len(pnls) * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / len(pnls)
    max_win = max(wins) if wins else 0
    max_loss = min(pnls) if pnls else 0
    pf = (avg_win * len(wins)) / (avg_loss * len(losses)) if losses and avg_loss > 0 else float('inf')
    
    # 健康度评分
    health = _calculate_health(win_rate, avg_pnl, pf, len(pnls))
    
    c.execute("""
        INSERT INTO strategy_metrics (strategy, coin, total_signals, signals_followed,
                                     closed_trades, wins, losses, win_rate, avg_win_pct,
                                     avg_loss_pct, total_pnl_pct, avg_pnl_pct, max_win_pct,
                                     max_loss_pct, pf, last_updated, health_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy, coin) DO UPDATE SET
            closed_trades=excluded.closed_trades,
            wins=excluded.wins, losses=excluded.losses,
            win_rate=excluded.win_rate, avg_win_pct=excluded.avg_win_pct,
            avg_loss_pct=excluded.avg_loss_pct, total_pnl_pct=excluded.total_pnl_pct,
            avg_pnl_pct=excluded.avg_pnl_pct, max_win_pct=excluded.max_win_pct,
            max_loss_pct=excluded.max_loss_pct, pf=excluded.pf,
            last_updated=excluded.last_updated, health_score=excluded.health_score
    """, (
        strategy, coin, len(pnls), len(pnls),
        len(pnls), len(wins), len(losses), win_rate,
        avg_win, avg_loss, total_pnl, avg_pnl,
        max_win, max_loss, pf,
        datetime.now().isoformat(), health
    ))
    conn.commit()
    
    # 检查是否需要告警
    _check_alerts(conn, coin, strategy, health, win_rate, avg_pnl, pf)
    
    conn.close()

def _calculate_health(win_rate: float, avg_pnl: float, pf: float, n_trades: int) -> float:
    """
    计算策略健康度 0-100
    因子：胜率、均笔收益、盈亏比、样本量
    """
    if n_trades < 3:
        return 75  # 样本不足，保守估计
    
    # 胜率得分 (0-40分)
    wr_score = min(40, win_rate * 0.40)
    
    # 均笔收益得分 (0-30分)
    # >2% 均笔 = 30分，>0% = 15分，<0% = 0分
    if avg_pnl > 2:
        pnl_score = 30
    elif avg_pnl > 1:
        pnl_score = 25
    elif avg_pnl > 0:
        pnl_score = 15
    else:
        pnl_score = 0
    
    # 盈亏比得分 (0-20分)
    # PF > 2 = 20分，> 1 = 10分，< 1 = 0分
    if pf > 2:
        pf_score = 20
    elif pf > 1.5:
        pf_score = 17
    elif pf > 1:
        pf_score = 10
    else:
        pf_score = 0
    
    # 样本量奖励 (0-10分)
    sample_score = min(10, n_trades * 0.5)
    
    health = wr_score + pnl_score + pf_score + sample_score
    
    # 连续亏损惩罚
    conn = init_db()
    c = conn.cursor()
    c.execute("""
        SELECT pnl_pct FROM trades ORDER BY created_at DESC LIMIT 5
    """)
    recent = [r[0] for r in c.fetchall() if r[0] is not None]
    if len(recent) >= 3 and all(p < 0 for p in recent):
        health = health * 0.5  # 连续3笔亏损，健康度减半
    conn.close()
    
    return round(health, 1)

def _check_alerts(conn, coin: str, strategy: str, health: float, 
                  win_rate: float, avg_pnl: float, pf: float):
    """检查是否需要触发告警"""
    c = conn.cursor()
    
    # 策略健康度告警
    if health < 30:
        _add_alert(conn, "strategy_critical", coin, strategy,
                   f"策略{strategy}@{coin}健康度跌至{health}%，建议立即停止", "critical")
    elif health < 50:
        _add_alert(conn, "strategy_warning", coin, strategy,
                   f"策略{strategy}@{coin}健康度下降至{health}%，密切监控", "warning")
    
    # 胜率异常
    if win_rate < 40 and health < 60:
        _add_alert(conn, "winrate_low", coin, strategy,
                   f"胜率{win_rate:.0f}%异常低，策略可能失效", "warning")
    
    # 盈亏比告警
    if pf < 0.8 and health < 50:
        _add_alert(conn, "pf_low", coin, strategy,
                   f"盈亏比{pf:.2f}<1，策略亏损大于盈利", "warning")
    
    # 均笔亏损
    if avg_pnl < -1:
        _add_alert(conn, "avgpnl_negative", coin, strategy,
                   f"均笔亏损{avg_pnl:.1f}%，考虑暂停策略", "warning")

def _add_alert(conn, alert_type: str, coin: Optional[str], strategy: Optional[str],
               message: str, severity: str):
    c = conn.cursor()
    # 检查是否已有未确认的同类告警（24h内）
    c.execute("""
        SELECT id FROM alerts
        WHERE alert_type=? AND acknowledged=0
        AND datetime(created_at) > datetime('now', '-24 hours')
        LIMIT 1
    """, (alert_type,))
    if c.fetchone():
        return  # 已有同类告警，跳过
    c.execute("""
        INSERT INTO alerts (alert_type, coin, strategy, message, severity)
        VALUES (?, ?, ?, ?, ?)
    """, (alert_type, coin, strategy, message, severity))

# ─── 信号记录 ─────────────────────────────────────────────────
def record_signal(signal: SignalRecord) -> int:
    conn = init_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO signals (coin, strategy, signal_type, price, reason, timestamp, followed, result)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal.coin, signal.strategy, signal.signal_type, signal.price,
        signal.reason, signal.timestamp, 1 if signal.followed else 0, signal.result
    ))
    sig_id = c.lastrowid
    conn.commit()
    conn.close()
    return sig_id

def mark_signal_result(signal_id: int, result: str):
    conn = init_db()
    c = conn.cursor()
    c.execute("UPDATE signals SET result=? WHERE id=?", (result, signal_id))
    conn.commit()
    conn.close()

# ─── 读取绩效 ─────────────────────────────────────────────────
def get_strategy_metrics(coin: Optional[str] = None, strategy: Optional[str] = None) -> list[StrategyMetrics]:
    conn = init_db()
    c = conn.cursor()
    
    query = "SELECT * FROM strategy_metrics WHERE 1=1"
    params = []
    if coin:
        query += " AND coin=?"
        params.append(coin)
    if strategy:
        query += " AND strategy=?"
        params.append(strategy)
    
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return []
    
    cols = [d[0] for d in c.description] if c.description else []
    return [StrategyMetrics(**dict(zip(cols, r))) for r in rows]

def get_recent_trades(coin: Optional[str] = None, limit: int = 20) -> list[TradeRecord]:
    conn = init_db()
    c = conn.cursor()
    
    query = "SELECT * FROM trades WHERE 1=1"
    params = []
    if coin:
        query += " AND coin=?"
        params.append(coin)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return []
    
    cols = [d[0] for d in c.description]
    records = []
    for r in rows:
        d = dict(zip(cols, r))
        d['realized'] = bool(d['realized'])
        del d['id']
        records.append(TradeRecord(**d))
    return records

def get_unacknowledged_alerts() -> list:
    conn = init_db()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM alerts
        WHERE acknowledged=0
        ORDER BY
            CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
            created_at DESC
    """)
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]

def acknowledge_alert(alert_id: int):
    conn = init_db()
    c = conn.cursor()
    c.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
    conn.commit()
    conn.close()

# ─── 策略决策 ─────────────────────────────────────────────────
def should_use_strategy(coin: str, strategy: str) -> tuple[bool, str]:
    """
    判断是否应该使用某策略
    返回 (可以使用, 原因)
    """
    metrics = get_strategy_metrics(coin=coin, strategy=strategy)
    if not metrics:
        return True, "新策略，尚无数据"
    
    m = metrics[0]
    
    if m.health_score < 30:
        return False, f"健康度{m.health_score}，严重警告"
    elif m.health_score < 50:
        return False, f"健康度{m.health_score}，不建议使用"
    elif m.closed_trades < 3:
        return True, f"样本不足({m.closed_trades}笔)，仅供参考"
    elif m.avg_pnl_pct < 0:
        return False, f"均笔亏损{m.avg_pnl_pct:.2f}%"
    elif m.win_rate < 40:
        return False, f"胜率{m.win_rate:.0f}%过低"
    else:
        return True, f"健康度{m.health_score:.0f}，胜率{m.win_rate:.0f}%，均笔{m.avg_pnl_pct:+.2f}%"

# ─── 报告生成 ─────────────────────────────────────────────────
def generate_performance_report() -> str:
    """生成绩效报告"""
    conn = init_db()
    c = conn.cursor()
    
    lines = []
    lines.append("=" * 60)
    lines.append("Kronos 策略绩效报告")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    
    # 未确认告警
    c.execute("SELECT * FROM alerts WHERE acknowledged=0 ORDER BY created_at DESC")
    alerts = c.fetchall()
    if alerts:
        lines.append(f"\n⚠️  未确认告警 ({len(alerts)}条):")
        for a in alerts[:5]:
            lines.append(f"  [{a[6]}] {a[3]}: {a[4]}")
    
    # 各策略评分
    c.execute("SELECT * FROM strategy_metrics ORDER BY health_score ASC")
    metrics_rows = c.fetchall()
    cols = [d[0] for d in c.description]
    
    lines.append(f"\n策略健康度:")
    lines.append(f"{'策略':<8} {'币种':<10} {'交易数':>6} {'胜率':>8} {'均笔':>8} {'PF':>6} {'健康度':>8}")
    lines.append("-" * 60)
    
    for r in metrics_rows:
        d = dict(zip(cols, r))
        status = "✅" if d['health_score'] >= 70 else ("⚠️" if d['health_score'] >= 50 else "🔴")
        lines.append(f"{status} {d['strategy']:<6} {d['coin']:<10} "
                    f"{d['closed_trades']:>6} {d['win_rate']:>7.0f}% {d['avg_pnl_pct']:>+7.2f}% "
                    f"{d['pf']:>6.2f} {d['health_score']:>7.1f}")
    
    # 最近交易
    c.execute("""
        SELECT coin, strategy, pnl_pct, exit_reason, entry_time, exit_time
        FROM trades WHERE realized=1 ORDER BY created_at DESC LIMIT 10
    """)
    recent = c.fetchall()
    
    lines.append(f"\n最近10笔已平仓交易:")
    lines.append(f"{'币种':<10} {'策略':<8} {'收益':>8} {'原因':<15} {'入场':<12} {'出场':<12}")
    lines.append("-" * 70)
    for r in recent:
        coin, strat, pnl, reason, entry, exit_t = r
        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "进行中"
        lines.append(f"{coin:<10} {strat:<8} {pnl_str:>8} {reason or '':<15} "
                    f"{entry[:10] if entry else '':<12} {exit_t[:10] if exit_t else '':<12}")
    
    conn.close()
    return "\n".join(lines)

# ─── 核心：Automaton风格的反思 ─────────────────────────────────
def reflection_cycle():
    """
    Automaton Reflection Pipeline 的核心逻辑：
    1. 收集最近的证据（交易结果）
    2. 计算当前策略健康度
    3. 决定是否需要更新SOUL.md
    4. 记录新的失效发现
    
    这个函数应该在每次重要事件后调用。
    """
    import json
    from pathlib import Path
    
    report = generate_performance_report()
    print(report)
    
    # 检查是否有告警
    alerts = get_unacknowledged_alerts()
    critical = [a for a in alerts if a['severity'] == 'critical']
    
    if critical:
        print("\n🚨 严重告警，需要立即处理!")
        for a in critical:
            print(f"  {a['message']}")
        return {
            'action_needed': True,
            'severity': 'critical',
            'alerts': critical
        }
    
    # 检查策略健康度
    metrics = get_strategy_metrics()
    low_health = [m for m in metrics if m.health_score < 50]
    
    if low_health:
        print(f"\n⚠️  {len(low_health)}个策略健康度低于50%:")
        for m in low_health:
            print(f"  {m.strategy}@{m.coin}: 健康度{m.health_score}")
        
        # 如果有策略持续低健康度，更新SOUL.md
        for m in low_health:
            if m.closed_trades >= 5 and m.health_score < 40:
                _log_strategy_failure(m)
        
        return {
            'action_needed': True,
            'severity': 'warning',
            'low_health_strategies': low_health
        }
    
    print("\n✅ 所有策略运行正常")
    return {
        'action_needed': False,
        'severity': 'ok'
    }

def _log_strategy_failure(metric: StrategyMetrics):
    """记录策略失效到SOUL.md"""
    soul_path = Path(__file__).parent / "SOUL.md"
    if not soul_path.exists():
        return
    
    with open(soul_path) as f:
        content = f.read()
    
    failure_entry = f"| {datetime.now().strftime('%Y-%m-%d')} | {metric.strategy}@{metric.coin} | 健康度{metric.health_score}，胜率{metric.win_rate:.0f}%，均笔{metric.avg_pnl_pct:+.2f}% | 评估中 |"
    
    if "失效记录" in content:
        if failure_entry in content:
            return  # 已记录
        
        # 追加到失效记录表
        new_content = content.replace(
            "| 日期 | 事件 | 结论 |",
            f"| 日期 | 事件 | 结论 |\n{failure_entry}"
        )
        with open(soul_path, 'w') as f:
            f.write(new_content)

# ─── 主入口 ─────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--alerts', action='store_true')
    parser.add_argument('--trade', action='store_true',
                        help='记录一笔交易 (需 --coin --strategy --entry --exit --pnl)')
    parser.add_argument('--coin')
    parser.add_argument('--strategy')
    parser.add_argument('--entry-price', type=float)
    parser.add_argument('--exit-price', type=float)
    parser.add_argument('--exit-reason')
    parser.add_argument('--entry-time')
    parser.add_argument('--exit-time')
    parser.add_argument('--pnl-pct', type=float)
    parser.add_argument('--capital', type=float)
    args = parser.parse_args()
    
    if args.report:
        print(generate_performance_report())
    elif args.alerts:
        alerts = get_unacknowledged_alerts()
        if not alerts:
            print("无未确认告警 ✅")
        else:
            print(f"未确认告警 ({len(alerts)}条):")
            for a in alerts:
                print(f"  [{a['severity']}] {a['created_at'][:19]}: {a['message']}")
    elif args.trade:
        trade = TradeRecord(
            coin=args.coin,
            strategy=args.strategy,
            signal_price=args.entry_price,
            exit_price=args.exit_price,
            exit_reason=args.exit_reason,
            entry_time=args.entry_time or datetime.now().isoformat(),
            exit_time=args.exit_time or datetime.now().isoformat(),
            pnl_pct=args.pnl_pct,
            pnl_abs=None,
            capital_used=args.capital or 10000,
            realized=True
        )
        record_trade(trade)
        print(f"交易已记录: {args.coin} {args.strategy} {args.pnl_pct:+.2f}%")
        reflection_cycle()
    else:
        reflection_cycle()
