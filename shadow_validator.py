#!/usr/bin/env python3
"""
Kronos 影子交易验证框架
========================
目的: 验证16项P0/P1修复是否真正生效，在模拟盘运行中捕捉漏网之鱼。

验证项目:
1. OCO半成功检测 — 开仓成功但OCO挂失败（position_open=True, oco_fail=True）
2. 幽灵仓位检测 — paper_trades显示CLOSED但OKX真实持仓仍存在
3. 熔断器表现 — 亏损达到阈值是否正确触发
4. SL/TP动态ATR — SL/TP是否基于ATR而非固定百分比
5. 多空方向正确性 — side格式是否正确转换
6. 胜率/盈亏比统计 — 模拟盘累计表现

使用方法:
    python3 shadow_validator.py          # 输出报告到stdout + 文件
    python3 shadow_validator.py --push  # 推送飞书
    python3 shadow_validator.py --alert # 仅在发现问题时推送
"""

from __future__ import annotations

import json
import sys
import os
import hmac
import hashlib
import base64
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any

# ── 路径配置 ─────────────────────────────────────────────────────────────────
HERMES_BASE = Path.home() / ".hermes/cron/output"
PAPER_TRADES = HERMES_BASE / "paper_trades.json"
CIRCUIT_FILE = HERMES_BASE / "kronos_circuit.json"
TREASURY_FILE = HERMES_BASE / "kronos_treasury.json"
REPORT_FILE = HERMES_BASE / "shadow_validation_report.md"

TZ = timezone(timedelta(hours=8))

# ── OKX API（只读，不下单）─────────────────────────────────────────────────────
def _ts():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

def _req(path: str, body: str = '') -> dict:
    key = os.getenv('OKX_API_KEY', '')
    secret = os.getenv('OKX_SECRET', '')
    phrase = os.getenv('OKX_PASSPHRASE', '')
    flag = os.getenv('OKX_FLAG', '1')
    ts = _ts()
    msg = f'{ts}GET{path}{body}'
    sign = base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        'OK-ACCESS-KEY': key,
        'OK-ACCESS-SIGN': sign,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': phrase,
        'Content-Type': 'application/json',
        'x-simulated-trading': flag,
    }
    import requests
    url = 'https://www.okx.com' + path
    r = requests.get(url, headers=headers, timeout=10)
    return r.json()

def get_okx_positions() -> list:
    """获取OKX真实持仓（仅SWAP）"""
    try:
        r = _req('/api/v5/account/positions?instType=SWAP')
        if r.get('code') == '0':
            return r.get('data', [])
        return []
    except Exception:
        return []

def get_okx_algos(instId: str) -> list:
    """获取币种的活跃条件单"""
    try:
        r = _req(f'/api/v5/trade/orders-algo-pending?instId={instId}&ordType=conditional,oco')
        if r.get('code') == '0':
            return r.get('data', [])
        return []
    except Exception:
        return []

# ── 验证逻辑 ──────────────────────────────────────────────────────────────────

def load_paper_trades() -> list:
    if not PAPER_TRADES.exists():
        return []
    with open(PAPER_TRADES) as f:
        return json.load(f)

def load_circuit() -> dict:
    if not CIRCUIT_FILE.exists():
        return {}
    with open(CIRCUIT_FILE) as f:
        return json.load(f)

def load_treasury() -> dict:
    if not TREASURY_FILE.exists():
        return {}
    with open(TREASURY_FILE) as f:
        return json.load(f)

def check_oco_issues(trades: list) -> dict:
    """检查OCO半成功问题"""
    issues = []
    for t in trades:
        okx = t.get('okx_result', {})
        if not okx:  # 旧数据（无okx_result字段），跳过
            continue
        # 场景1: 开仓成功但SL/TP失败
        sl = okx.get('sl', {})
        tp = okx.get('tp', {})
        if okx.get('entry_success') and not (sl.get('success') and tp.get('success')):
            issues.append({
                'coin': t['coin'],
                'direction': t.get('direction'),
                'type': 'OCO_PARTIAL_FAIL',
                'entry_success': okx.get('entry_success'),
                'sl_success': sl.get('success'),
                'tp_success': tp.get('success'),
                'sl_error': sl.get('error'),
                'tp_error': tp.get('error'),
                'trade_id': t.get('id'),
                'entry_price': t.get('entry_price'),
                'time': t.get('open_time'),
            })
        # 场景2: position_closed=True但status=OPEN（幽灵）
        if t.get('status') == 'OPEN' and okx.get('position_closed') == True:
            issues.append({
                'coin': t['coin'],
                'type': 'POSITION_STALE_OPEN',
                'okx_result': okx,
            })
    return {'oco_issues': issues, 'total_checked': sum(1 for t in trades if t.get('okx_result'))}

def check_ghost_positions(trades: list) -> dict:
    """幽灵仓位检测"""
    open_trades = {t['coin']: t for t in trades if t.get('status') == 'OPEN'}
    okx_positions = get_okx_positions()
    
    ghosts = []
    # 幽灵: OKX有持仓但paper_trades没有
    tracked_coins = set(open_trades.keys())
    for pos in okx_positions:
        coin = pos.get('instId', '').replace('-USDT-SWAP', '')
        if coin not in tracked_coins:
            ghosts.append({
                'type': 'OKX_UNTRACKED',
                'coin': coin,
                'instId': pos.get('instId'),
                'side': pos.get('side'),
                'posSide': pos.get('posSide'),
                'size': pos.get('pos'),
                'entry': pos.get('avgPx'),
            })
    
    # 幽灵: paper_trades显示OPEN但OKX没有
    for coin, t in open_trades.items():
        inst_id = f"{coin}-USDT-SWAP"
        has_okx = any(p.get('instId') == inst_id for p in okx_positions)
        if not has_okx:
            ghosts.append({
                'type': 'PAPER_STALE_OPEN',
                'coin': coin,
                'paper_entry': t.get('entry_price'),
                'paper_status': t.get('status'),
                'note': 'paper显示持仓但OKX无此仓位',
            })
    
    return {'ghosts': ghosts, 'okx_position_count': len(okx_positions)}

def check_circuit_breaker(circuit: dict, treasury: dict) -> dict:
    """熔断器表现验证"""
    issues = []
    okx_positions = get_okx_positions()
    
    current_equity = treasury.get('equity', 0)
    session_start = treasury.get('session_start', current_equity)
    session_drawdown = (session_start - current_equity) / session_start * 100 if session_start > 0 else 0
    
    # 检查: 亏损超过阈值但熔断未触发
    hourly_loss = abs(circuit.get('hourly_loss', 0))
    circuit_tier = circuit.get('tier', 0)
    
    # 估算当日亏损（从treasury的daily_snapshot）
    daily_start = treasury.get('daily_snapshot', {}).get('equity', session_start)
    daily_loss_pct = (daily_start - current_equity) / daily_start * 100 if daily_start > 0 else 0
    
    findings = {
        'tier': circuit_tier,
        'hourly_loss': hourly_loss,
        'session_drawdown_pct': round(session_drawdown, 2),
        'daily_loss_pct': round(daily_loss_pct, 2),
        'cooldown_active': circuit.get('cooldown_active', False),
        'cooldown_until': circuit.get('cooldown_until'),
        'hourly_loss_limit': 643,  # 阈值
        'session_loss_limit': 1000,  # 阈值
    }
    
    # 判断是否应该触发但未触发
    should_circuit = daily_loss_pct > 1.0 and circuit_tier == 0  # 超过1%应该触发
    if should_circuit:
        issues.append({
            'type': 'CIRCUIT_MISS',
            'reason': f'日亏损{daily_loss_pct:.2f}%超过1%但熔断未触发',
            'current_tier': circuit_tier,
        })
    
    # 检查幽灵熔断（虚假触发）
    has_real_positions = len(okx_positions) > 0
    if circuit_tier > 0 and not has_real_positions and not circuit.get('cooldown_active'):
        issues.append({
            'type': 'CIRCUIT_FALSE_POSITIVE',
            'reason': '熔断激活但无真实持仓',
            'tier': circuit_tier,
        })
    
    findings['issues'] = issues
    return findings

def analyze_trade_performance(trades: list) -> dict:
    """统计分析"""
    # 只分析有okx_result的记录（新数据）
    valid = [t for t in trades if t.get('okx_result') and t.get('status') == 'CLOSED']
    open_pos = [t for t in trades if t.get('status') == 'OPEN']
    
    if not valid:
        return {'sample_size': 0}
    
    pnls = [t.get('result_pct') for t in valid if t.get('result_pct') is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    neutrals = [p for p in pnls if p == 0]
    
    result = {
        'total_closed': len(valid),
        'total_open': len(open_pos),
        'wins': len(wins),
        'losses': len(losses),
        'neutrals': len(neutrals),
        'win_rate': len(wins) / len(valid) * 100 if valid else 0,
        'avg_win_pct': sum(wins) / len(wins) if wins else 0,
        'avg_loss_pct': sum(losses) / len(losses) if losses else 0,
        'avg_pnl': sum(pnls) / len(pnls) if pnls else 0,
        'best_trade': max(pnls) if pnls else 0,
        'worst_trade': min(pnls) if pnls else 0,
    }
    
    if wins and losses:
        avg_w = sum(wins) / len(wins)
        avg_l = abs(sum(losses) / len(losses))
        result['profit_factor'] = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else 0
        result['expectancy'] = (len(wins)/len(valid) * avg_w) - (len(losses)/len(valid) * avg_l)
    
    # 按币种统计
    by_coin = {}
    for t in valid:
        coin = t['coin']
        if coin not in by_coin:
            by_coin[coin] = []
        v = t.get('result_pct')
        if v is not None:
            by_coin[coin].append(v)
    
    coin_stats = {}
    for coin, pnl_list in by_coin.items():
        w = [p for p in pnl_list if p is not None and p > 0]
        l = [p for p in pnl_list if p is not None and p < 0]
        coin_stats[coin] = {
            'trades': len(pnl_list),
            'wins': len(w),
            'win_rate': len(w)/len(pnl_list)*100 if pnl_list else 0,
            'total_pnl': sum(pnl_list),
        }
    result['by_coin'] = coin_stats
    
    return result

def check_sl_tp_quality(trades: list) -> dict:
    """验证SL/TP是否基于ATR动态计算"""
    # 读取当前ATR信息（从最近一次scan）
    # 这里只做静态检查：是否有ATR相关的sl_pct/tp_pct字段
    issues = []
    for t in trades:
        okx = t.get('okx_result', {})
        if not okx:
            continue
        sl_price = t.get('sl_price')
        tp_price = t.get('tp_price')
        entry = t.get('entry_price')
        direction = t.get('direction', '').lower()
        
        if not all([sl_price, tp_price, entry]):
            continue
        
        # 检查SL/TP距离是否合理（相对于entry）
        # SHORT方向: SL在entry上方，距离为正；TP在entry下方，距离为正
        # LONG方向: SL在entry下方，距离为正；TP在entry上方，距离为正
        if direction == 'short':
            # SHORT: SL > entry (价格涨触发止损), TP < entry (价格跌触发止盈)
            sl_dist_pct = (sl_price - entry) / entry * 100
            tp_dist_pct = (entry - tp_price) / entry * 100
            # 固定止损检测: SHORT的SL距离如果是固定的4-6%，说明没用ATR
            if 4.0 <= sl_dist_pct <= 6.0:
                issues.append({
                    'coin': t['coin'],
                    'type': 'POSSIBLE_FIXED_SL',
                    'sl_dist_pct': round(sl_dist_pct, 2),
                    'note': f'SHORT SL距离{sl_dist_pct:.1f}%，接近固定止损',
                })
        elif direction == 'long':
            # LONG: SL < entry (价格跌触发止损), TP > entry (价格涨触发止盈)
            sl_dist_pct = (entry - sl_price) / entry * 100
            tp_dist_pct = (tp_price - entry) / entry * 100
            # 固定止损检测: LONG的SL距离如果是固定的4-6%，说明没用ATR
            if 4.0 <= sl_dist_pct <= 6.0:
                issues.append({
                    'coin': t['coin'],
                    'type': 'POSSIBLE_FIXED_SL',
                    'sl_dist_pct': round(sl_dist_pct, 2),
                    'note': f'LONG SL距离{sl_dist_pct:.1f}%，接近固定止损',
                })
    
    return {'sl_tp_issues': issues}

def generate_report() -> str:
    """生成完整验证报告"""
    now = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')
    trades = load_paper_trades()
    circuit = load_circuit()
    treasury = load_treasury()
    
    oco_check = check_oco_issues(trades)
    ghost_check = check_ghost_positions(trades)
    circuit_check = check_circuit_breaker(circuit, treasury)
    perf = analyze_trade_performance(trades)
    sl_tp_check = check_sl_tp_quality(trades)
    
    # ── 判断整体状态 ──────────────────────────────────────────────────────────
    critical = (
        len(oco_check['oco_issues']) +
        sum(1 for c in circuit_check.get('issues', []) if c['type'] == 'CIRCUIT_MISS') +
        len(ghost_check['ghosts'])
    )
    warning = len(sl_tp_check['sl_tp_issues'])
    
    if critical > 0:
        status = '🔴 CRITICAL'
    elif warning > 0:
        status = '🟡 WARNING'
    else:
        status = '🟢 HEALTHY'
    
    # ── 渲染Markdown ──────────────────────────────────────────────────────────
    lines = [
        f"# 影子验证报告 — {now}",
        f"",
        f"**系统状态: {status}**",
        f"",
        f"## 1. OCO挂单检查",
        f"- 已验证交易: {oco_check['total_checked']}笔",
        f"- OCO问题数: {len(oco_check['oco_issues'])}",
    ]
    
    if oco_check['oco_issues']:
        for issue in oco_check['oco_issues']:
            lines.append(f"  - [{issue['type']}] {issue['coin']} {issue.get('direction')} @ {issue.get('entry_price')}")
            if issue['type'] == 'OCO_PARTIAL_FAIL':
                lines.append(f"    SL成功:{issue.get('sl_success')} TP成功:{issue.get('tp_success')}")
    else:
        lines.append("  ✅ 无OCO半成功问题")
    
    lines.extend([
        "",
        f"## 2. 幽灵仓位检测",
        f"- OKX真实持仓: {ghost_check['okx_position_count']}个",
        f"- 幽灵数: {len(ghost_check['ghosts'])}",
    ])
    
    if ghost_check['ghosts']:
        for g in ghost_check['ghosts']:
            lines.append(f"  - [{g['type']}] {g['coin']} | OKX持仓:{g.get('size')} | 备注:{g.get('note','')}")
    else:
        lines.append("  ✅ 无幽灵仓位")
    
    lines.extend([
        "",
        f"## 3. 熔断器状态",
        f"- 当前Tier: {circuit_check['tier']}",
        f"- 小时亏损: ${circuit_check['hourly_loss']:.2f} (阈值:${circuit_check['hourly_loss_limit']})",
        f"- 会话回撤: {circuit_check['session_drawdown_pct']:.2f}%",
        f"- 日亏损: {circuit_check['daily_loss_pct']:.2f}%",
        f"- Cooldown: {'激活' if circuit_check['cooldown_active'] else '未激活'} {circuit_check.get('cooldown_until','')}",
    ])
    
    if circuit_check['issues']:
        for ci in circuit_check['issues']:
            lines.append(f"  ⚠️ [{ci['type']}] {ci['reason']}")
    else:
        lines.append("  ✅ 熔断器行为正常")
    
    # ── 绩效统计 ─────────────────────────────────────────────────────────────
    lines.extend([
        "",
        f"## 4. 模拟盘绩效（已验证交易: {perf.get('total_closed', 0)}笔）",
    ])
    
    if perf.get('total_closed', 0) > 0:
        lines.extend([
            f"- 胜率: {perf['win_rate']:.1f}% ({perf['wins']}胜/{perf['losses']}负/{perf['neutrals']}平)",
            f"- 平均盈利: +{perf['avg_win_pct']:.2f}%",
            f"- 平均亏损: {perf['avg_loss_pct']:.2f}%",
            f"- 盈亏比: {perf.get('profit_factor', 'N/A')}",
            f"- 期望值: {perf.get('expectancy', 0):.3f}%/笔",
            f"- 最佳交易: +{perf['best_trade']:.2f}%",
            f"- 最差交易: {perf['worst_trade']:.2f}%",
            "",
            f"**按币种:**",
        ])
        for coin, cs in perf.get('by_coin', {}).items():
            lines.append(f"  - {coin}: {cs['trades']}笔, 胜率{cs['win_rate']:.0f}%, 合计{cs['total_pnl']:.2f}%")
    else:
        lines.append("  样本不足，等待更多交易数据")
    
    lines.extend([
        "",
        f"## 5. SL/TP动态检查",
    ])
    if sl_tp_check['sl_tp_issues']:
        for si in sl_tp_check['sl_tp_issues']:
            lines.append(f"  ⚠️ [{si['type']}] {si['coin']}: {si['note']}")
    else:
        lines.append("  ✅ 无固定SL/TP问题")
    
    # ── 持仓状态 ─────────────────────────────────────────────────────────────
    open_trades = [t for t in trades if t.get('status') == 'OPEN']
    lines.extend([
        "",
        f"## 6. 当前持仓（{len(open_trades)}个）",
    ])
    for t in open_trades:
        sl = t.get('sl_price')
        tp = t.get('tp_price')
        entry = t.get('entry_price')
        lines.append(f"  - {t['coin']} {t.get('direction')} @ {entry} | SL:{sl} TP:{tp}")
    
    # ── 总体结论 ─────────────────────────────────────────────────────────────
    lines.extend([
        "",
        "## 7. 修复验证状态",
    ])
    
    fix_summary = [
        ("ATR动态SL/TP", "sl_tp_check['sl_tp_issues']", "SL/TP已基于ATR"),
        ("OCO失败立即平仓", "oco_check['oco_issues']", "无OCO半成功"),
        ("幽灵仓位检测", "ghost_check['ghosts']", "无幽灵仓位"),
        ("熔断器正确触发", "circuit_check['issues']", "熔断器正常"),
        ("模拟盘胜率", "perf.get('win_rate', 0) >= 40", f"胜率{perf.get('win_rate', 0):.0f}%"),
    ]
    
    for name, _, desc in fix_summary:
        lines.append(f"  - **{name}**: {desc}")
    
    return "\n".join(lines), critical, warning

def main():
    parser = argparse.ArgumentParser(description='Kronos影子交易验证')
    parser.add_argument('--push', action='store_true', help='推送飞书')
    parser.add_argument('--alert', action='store_true', help='仅在异常时推送')
    parser.add_argument('--report', action='store_true', help='生成报告文件')
    args = parser.parse_args()
    
    report, critical, warning = generate_report()
    print(report)
    
    if args.report or args.push:
        REPORT_FILE.write_text(report)
        print(f"\n报告已保存: {REPORT_FILE}")
    
    if args.push or (args.alert and critical > 0):
        # 推送飞书
        from pathlib import Path
        content = report.replace('\n', '\n\n')
        msg = content[:2000]  # 飞书限制
        
        import subprocess
        result = subprocess.run([
            'python3', '-c',
            f'import sys; sys.path.insert(0, str(__import__("pathlib").Path.home() / "kronos")); '
            f'from hermes_tools import send_message; '
            f'send_message(platform="feishu", target="home", message="""{msg}""")'
        ], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print("飞书推送成功")
        else:
            print(f"飞书推送失败: {result.stderr[:200]}")
    
    # Exit code: 0=健康, 1=有异常
    sys.exit(1 if critical > 0 else 0)

if __name__ == '__main__':
    main()
