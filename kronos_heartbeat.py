#!/usr/bin/env python3
"""
Kronos策略自我复盘心跳
- 每小时自动分析持仓状态 + 交易历史
- 输出结构化报告推送到飞书
- 异常情况实时告警
"""
import sys, json, os
from pathlib import Path
from datetime import datetime, timezone
import zoneinfo

# 加载.env
from dotenv import load_dotenv
load_dotenv(Path.home() / '.hermes' / '.env', override=True)

# 导入kronos模块
sys.path.insert(0, str(Path(__file__).parent))
from kronos_pilot import push_feishu
from real_monitor import (
    get_account_balance, get_real_positions, get_real_sl_tp_orders,
    START_BALANCE, TIER_THRESHOLDS, get_survival_tier,
    get_position_multiplier, can_open_new_position,
    TREASURY_BASE, load_treasury_state, format_treasury_report,
    get_dynamic_treasury_limits,
)

MAX_HOLD_HOURS = 72

# ============ 连续亏损熔断器 ============

def _get_circuit_path():
    return Path.home() / '.hermes' / 'cron' / 'output' / 'kronos_circuit.json'


def load_circuit_state():
    """加载熔断状态"""
    path = _get_circuit_path()
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {
            'consecutive_losses': 0,     # 当前连续亏损次数
            'last_outcome': None,        # 'win'/'loss'/None
            'is_tripped': False,         # 熔断是否被触发
            'trip_reason': '',
            'trip_time': '',
            'losses_log': [],            # 最近10笔结果
        }


def save_circuit_state(state):
    """保存熔断状态"""
    path = _get_circuit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(state, f, indent=2)


FAILURE_REASONS = {'insufficient_balance', 'balance_insufficient',
                   'timestamp_error', 'timeout_sync', 'system_error', 'open_failed'}

def record_trade_outcome(coin, pnl, close_reason=''):
    """
    记录一笔交易的盈亏结果
    用于更新连续亏损计数

    关键原则：只记录「真实交易亏损」
    - 余额不足/时间戳错误等系统失败 → 不计入熔断亏损计数
      （这些由黑名单处理，不反映交易能力）
    - 真实亏损（TP/SL触发，手动平仓亏钱）→ 计入熔断计数
    """
    state = load_circuit_state()
    now = datetime.now(zoneinfo.ZoneInfo('Asia/Shanghai')).isoformat()

    # ── 区分真实亏损 vs 系统失败 ──────────────────────────────────
    is_failure = close_reason in FAILURE_REASONS
    if is_failure:
        # 系统失败：不计入熔断亏损（黑名单已处理）
        outcome = 'failure'
    elif pnl is not None and pnl > 0:
        outcome = 'win'
    elif pnl is not None and pnl < 0:
        # 真实亏损 → 计入熔断计数
        outcome = 'loss'
    else:
        # pnl is None or pnl == 0（Break-even，开仓时无已实现盈亏）
        # 不计入熔断计数，避免 break-even 触发熔断
        outcome = 'neutral'

    # ── 更新连续计数（仅真实盈亏计入，neutral不改变状态）───────────────
    if outcome == 'neutral':
        # 中立结果：不更新计数也不更新 last_outcome，保持原有状态
        return state
    if outcome == state['last_outcome']:
        if outcome == 'loss':
            state['consecutive_losses'] += 1
        elif outcome == 'win':
            state['consecutive_losses'] = 0
    else:
        if outcome == 'loss':
            state['consecutive_losses'] = 1
        elif outcome == 'win':
            state['consecutive_losses'] = 0

    state['last_outcome'] = outcome

    # ── 防止同一笔交易重复记录（同一秒内同一币种同一结果）────────────────
    dup_key = (coin, now[:19], outcome)
    if state['losses_log'] and (state['losses_log'][-1].get('_dup_key') == dup_key):
        # 重复记录，跳过（只保留第一条）
        save_circuit_state(state)
        return state

    # ── 记录日志（所有结果都记录，用于审计）─────────────────────
    state['losses_log'].append({
        'coin': coin, 'pnl': round(pnl, 4) if pnl is not None else 0.0,
        'outcome': outcome, 'time': now, 'reason': close_reason,
        '_dup_key': dup_key,
    })
    if len(state['losses_log']) > 10:
        state['losses_log'] = state['losses_log'][-10:]

    # ── 检查是否触发熔断（连续3次真实交易亏损）─────────────────
    if state['consecutive_losses'] >= 3:
        state['is_tripped'] = True
        state['trip_reason'] = '连续%d笔交易亏损' % state['consecutive_losses']
        state['trip_time'] = now

    save_circuit_state(state)
    return state


def check_circuit_breaker():
    """
    检查熔断状态
    返回: (is_tripped, reason, state)
    """
    state = load_circuit_state()
    if state['is_tripped']:
        return True, state['trip_reason'], state
    return False, '', state


def reset_circuit_breaker():
    """重置熔断（赢一笔后自动调用或人工调用）"""
    state = load_circuit_state()
    if not state['is_tripped']:
        return state
    state['is_tripped'] = False
    state['consecutive_losses'] = 0
    state['trip_reason'] = ''
    save_circuit_state(state)
    return state


def sync_circuit_from_positions():
    """
    将真实持仓的浮盈状态同步到熔断器。
    
    逻辑：
    - 有持仓且总浮盈 > 0 → 视为"盈利"，重置熔断
    - 有持仓且总浮亏 > $500 或浮亏 > 20% → 视为"亏损"，保持/触发熔断
    - 无持仓且熔断已解除 → 不做操作
    
    调用时机：guardian每次运行前，确保熔断器反映真实账户状态。
    """
    from real_monitor import get_real_positions
    state = load_circuit_state()
    
    positions, err = get_real_positions()
    if err or not positions:
        # 无持仓，不做实时同步（依赖 journal 的交易结果）
        return state
    
    total_upl = sum(float(p.get('unrealized_pnl', 0)) for p in positions.values())
    
    now = datetime.now(zoneinfo.ZoneInfo('Asia/Shanghai')).isoformat()
    
    if total_upl > 0:
        # 有浮盈 → 重置熔断（视为盈利）
        if state['is_tripped']:
            print(f"[熔断重置] 实时持仓浮盈${total_upl:.2f}，重置熔断器")
            state['is_tripped'] = False
            state['consecutive_losses'] = 0
            state['last_outcome'] = 'win'
            state['trip_reason'] = ''
            state['losses_log'].append({
                'coin': 'REALTIME',
                'pnl': round(total_upl, 2),
                'outcome': 'win',
                'time': now,
                'reason': 'unrealized_profit_reset',
            })
            save_circuit_state(state)
    elif total_upl < -500:
        # 浮亏超过$500 → 保持/强化熔断
        if not state['is_tripped']:
            print(f"[熔断触发] 实时持仓浮亏${total_upl:.2f}，触发熔断")
            state['is_tripped'] = True
            state['consecutive_losses'] = max(state['consecutive_losses'], 1)
            state['trip_reason'] = f'实时持仓浮亏${total_upl:.2f}'
            state['trip_time'] = now
            save_circuit_state(state)
    
    return state


def get_pnl_from_fills(coin):
    """
    从OKX成交记录计算某笔持仓的已实现盈亏
    返回: float or None

    关键：只对已确认平仓的持仓返回PnL。
    如果OKX仍有持仓（有未匹配买入），返回None。
    """
    try:
        from real_monitor import _req, get_real_positions

        # ── Step 1: 确认持仓已平 ──────────────────────────────
        # 如果OKX显示仍有持仓，说明有未匹配买入，不能计算已实现PnL
        pos, _ = get_real_positions()
        if coin in pos and pos[coin]['size'] > 0:
            return None  # 持仓仍开，不返回PnL

        # ── Step 2: 获取成交记录 ──────────────────────────────
        r = _req('GET', '/api/v5/trade/fills?instId=%s-USDT-SWAP&limit=50' % coin)
        if r.get('code') != '0':
            return None
        fills = r.get('data', [])
        if not fills:
            return None

        # 解析成交
        buys = []  # (sz, px, ts)
        sells = []
        for f in fills:
            sz = float(f.get('fillSz', 0))
            px = float(f.get('fillPx', 0))
            ts = int(f.get('ts', 0))
            if f.get('side') == 'buy':
                buys.append((sz, px, ts))
            else:
                sells.append((sz, px, ts))

        # 按时间排序
        buys.sort(key=lambda x: x[2])
        sells.sort(key=lambda x: x[2])

        # 配对计算（先进先出）
        remaining_buys = list(buys)
        realized_pnl = 0.0

        for sell_sz, sell_px, _ in sells:
            remaining = sell_sz
            while remaining > 0 and remaining_buys:
                buy_sz, buy_px, _ = remaining_buys[0]
                match = min(buy_sz, remaining)
                realized_pnl += (sell_px - buy_px) * match
                remaining -= match
                if match >= buy_sz:
                    remaining_buys.pop(0)
                else:
                    remaining_buys[0] = (buy_sz - match, buy_px, remaining_buys[0][2])

        return round(realized_pnl, 4) if realized_pnl != 0 else None
    except Exception as e:
        return None


def update_trade_pnl_from_fills(paper_path=None):
    """
    从OKX fills更新paper_trades中的已实现PnL
    同时更新熔断器记录
    """
    if paper_path is None:
        paper_path = Path.home() / '.hermes' / 'cron' / 'output' / 'paper_trades.json'
    try:
        with open(paper_path) as f:
            trades = json.load(f)
    except:
        return []

    updated = []
    for t in trades:
        if t.get('status') in ('CLOSED', 'FAILED'):
            if t.get('pnl') in (None, '?', ''):
                coin = t.get('coin')
                pnl = get_pnl_from_fills(coin)
                if pnl is not None:
                    t['pnl'] = pnl
                    close_reason = t.get('close_reason', '')
                    # 同步更新熔断器（仅真实盈亏计入）
                    record_trade_outcome(coin, pnl, close_reason)
                    updated.append('%s: pnl=$%+.4f' % (coin, pnl))

    if updated:
        with open(paper_path, 'w') as f:
            json.dump(trades, f, indent=2)

    return updated


def format_circuit_report(state):
    """生成熔断状态报告"""
    lines = ['━━━ 熔断器 ━━━']
    if state['is_tripped']:
        lines.append('  ⛔ 已触发: %s' % state['trip_reason'])
        lines.append('  触发时间: %s' % state.get('trip_time', '?')[:19])
        lines.append('  → 需要1笔盈利才能解锁')
    else:
        cl = state['consecutive_losses']
        lines.append('  %s 连续亏损: %d次 / 3次触发熔断' % (
            '⚠️' if cl >= 2 else '✅', cl))
        last = state.get('last_outcome', '无')
        lines.append('  上次结果: %s' % (last or '无交易'))

    if state.get('losses_log'):
        recent = state['losses_log'][-5:]
        lines.append('  最近交易:')
        for l in recent:
            icon = {'win': '✅', 'loss': '❌', 'failure': '⚠️'}.get(l['outcome'], '❓')
            lines.append('    %s %s %s%+.4f (%s)' % (
                icon, l['coin'], l.get('reason', ''), l['pnl'], l['outcome']))

    return '\n'.join(lines)


def load_paper_trades():
    """加载交易记录"""
    path = Path.home() / '.hermes' / 'cron' / 'output' / 'paper_trades.json'
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []


def compute_trade_stats(trades):
    """从交易历史计算统计数据"""
    closed = [t for t in trades if t.get('status') == 'CLOSED']
    failed = [t for t in trades if t.get('status') == 'FAILED']
    open_pos = [t for t in trades if t.get('status') == 'OPEN']

    # 分离有pnl和没有pnl的
    with_pnl = [t for t in closed if t.get('pnl') not in (None, '?', '')]
    without_pnl = [t for t in closed if t.get('pnl') in (None, '?', '')]

    total_pnl = sum(float(t['pnl']) for t in with_pnl)
    wins = [t for t in with_pnl if float(t['pnl']) > 0]
    losses = [t for t in with_pnl if float(t['pnl']) <= 0]
    win_rate = len(wins) / len(with_pnl) * 100 if with_pnl else 0
    avg_win = sum(float(t['pnl']) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(float(t['pnl']) for t in losses) / len(losses) if losses else 0

    return {
        'total': len(trades),
        'closed': len(closed),
        'failed': len(failed),
        'open': len(open_pos),
        'with_pnl': len(with_pnl),
        'without_pnl': len(without_pnl),
        'total_pnl': total_pnl,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'wrr': abs(avg_win / avg_loss) if avg_loss != 0 else 0,
        'closed_list': closed,
        'open_list': open_pos,
    }


def get_current_price(coin):
    """获取当前价格"""
    try:
        import ccxt
        c = ccxt.okx({'enableRateLimit': True})
        ticker = c.fetch_ticker('%s-USDT' % coin)
        return ticker['last']
    except:
        return None


def analyze_open_positions(open_trades, real_pos, real_orders):
    """分析当前开仓持仓"""
    now_cst = datetime.now(zoneinfo.ZoneInfo('Asia/Shanghai'))
    result = []

    for t in open_trades:
        coin = t['coin']
        pos = real_pos.get(coin, {})

        # 从真实持仓获取最新数据
        if pos and pos.get('size', 0) > 0:
            entry = pos['entry']
            side = pos['side']
            size = pos['size']
            pnl = pos['unrealized_pnl']
        else:
            # 纸仓有但OKX没有（可能已平）
            entry = t.get('entry_price', 0)
            side = t.get('direction', 'LONG')
            size = 0
            pnl = 0

        if size <= 0:
            continue

        # 开仓时间
        open_time_str = t.get('open_time', '')
        if open_time_str:
            try:
                ot = datetime.fromisoformat(open_time_str.replace('Z', '+00:00'))
                if ot.tzinfo is None:
                    ot = ot.replace(tzinfo=zoneinfo.ZoneInfo('Asia/Shanghai'))
                hours_elapsed = (now_cst - ot).total_seconds() / 3600
            except:
                hours_elapsed = 0
        else:
            hours_elapsed = 0

        # SL/TP距离
        sl_price = None
        tp_price = None
        current = get_current_price(coin)
        if coin in real_orders:
            sl_price = float(real_orders[coin].get('sl', {}).get('price', 0) or 0)
            tp_price = float(real_orders[coin].get('tp', {}).get('price', 0) or 0)

        if side.lower() == 'buy' or side == 'LONG':
            dist_to_sl = (current - sl_price) / current * 100 if current and sl_price else None
            dist_to_tp = (tp_price - current) / current * 100 if current and tp_price else None
            dist_to_entry = (current - entry) / entry * 100 if current and entry else 0
        else:
            dist_to_sl = (sl_price - current) / current * 100 if current and sl_price else None
            dist_to_tp = (current - tp_price) / current * 100 if current and tp_price else None
            dist_to_entry = (entry - current) / entry * 100 if current and entry else 0

        # 强平距离（3x杠杆）
        if current and entry:
            liq = entry * (1 - 1/3/3)  # 3x lev
            liq_buffer = (current - liq) / current * 100
        else:
            liq_buffer = None

        result.append({
            'coin': coin,
            'entry': entry,
            'current': current,
            'side': side,
            'size': size,
            'pnl': pnl,
            'dist_to_entry_pct': dist_to_entry,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'dist_to_sl_pct': dist_to_sl,
            'dist_to_tp_pct': dist_to_tp,
            'hours_elapsed': hours_elapsed,
            'hours_remaining': MAX_HOLD_HOURS - hours_elapsed,
            'liq_buffer_pct': liq_buffer,
            'timeout_pct': hours_elapsed / MAX_HOLD_HOURS * 100 if hours_elapsed else 0,
        })

    return result


def check_anomalies(positions, equity, tier, treasury_state):
    """检查异常情况"""
    anomalies = []
    warnings = []

    for pos in positions:
        coin = pos['coin']
        hours_pct = pos['timeout_pct']
        dist_sl = pos['dist_to_sl_pct']
        dist_tp = pos['dist_to_tp_pct']
        pnl = pos['pnl']
        liq_buf = pos['liq_buffer_pct']

        # 1. 持仓超时警告
        if hours_pct >= 100:
            anomalies.append('⛔ %s 持仓超时(%.1fh)！立即检查' % (coin, pos['hours_elapsed']))
        elif hours_pct >= 80:
            warnings.append('🟡 %s 持仓接近超时: %.1f小时/%d小时' % (coin, pos['hours_elapsed'], MAX_HOLD_HOURS))

        # 2. SL过近（<2%）
        if dist_sl is not None and dist_sl < 2.0:
            anomalies.append('🔴 %s SL仅距现价%.1f%%，极度危险！' % (coin, dist_sl))
        elif dist_sl is not None and dist_sl < 4.0:
            warnings.append('🟡 %s SL距现价%.1f%%，建议关注' % (coin, dist_sl))

        # 3. TP过远（>30%），持仓时间已经较长
        if dist_tp is not None and dist_tp > 30 and hours_pct > 50:
            warnings.append('🟡 %s TP距现价%.0f%%，可能持仓时间过长' % (coin, dist_tp))

        # 4. 强平距离过近
        if liq_buf is not None and liq_buf < 3.0:
            anomalies.append('🔴 %s 距强平仅%.1f%%，立即关注！' % (coin, liq_buf))
        elif liq_buf is not None and liq_buf < 5.0:
            warnings.append('🟡 %s 距强平仅%.1f%%' % (coin, liq_buf))

        # 5. 深亏检测
        if equity > 0 and pnl < 0:
            loss_pct = abs(pnl) / equity * 100
            if loss_pct >= 10:
                anomalies.append('🔴 %s 浮亏$%+.2f(占总权益%.0f%%)' % (coin, pnl, loss_pct))
            elif loss_pct >= 5:
                warnings.append('🟡 %s 浮亏$%+.2f(%.0f%%)' % (coin, pnl, loss_pct))

    # 6. 层级警告
    if tier in ('low_compute', 'critical'):
        anomalies.append('⚠️ 系统层级: %s — 交易受限' % tier.upper())

    # 7. 财务政策警告（使用动态限制）
    dyn = get_dynamic_treasury_limits(equity)
    hourly_snap = treasury_state.get('hourly_snapshot_equity')
    if hourly_snap:
        hourly_loss = hourly_snap - equity
        if hourly_loss >= dyn['hourly_limit'] * TREASURY_BASE['warning_ratio']:
            warnings.append('🟡 小时亏损$%.2f (限制$%.2f)' % (hourly_loss, dyn['hourly_limit']))

    return anomalies, warnings


def format_heartbeat_report(positions, stats, tier, equity, treasury_state):
    """生成心跳报告"""
    lines = []

    # === Header ===
    now = datetime.now(zoneinfo.ZoneInfo('Asia/Shanghai'))
    lines.append('━━━ Kronos策略复盘 ━━━')
    lines.append('%s' % now.strftime('%m-%d %H:%M'))
    lines.append('')

    # === 账户概览 ===
    pct = equity / START_BALANCE * 100
    lines.append('【账户】权益$%.2f (%.0f%%) | %s层' % (
        equity, pct, tier.upper()))

    # 层级描述
    desc_map = dict([(t, d) for t, (_, d, _) in TIER_THRESHOLDS.items()])
    lines.append('  %s' % desc_map.get(tier, '?'))

    if positions:
        total_pnl = sum(p['pnl'] for p in positions)
        lines.append('  持仓%d个 浮盈$%+.2f' % (len(positions), total_pnl))
    else:
        lines.append('  无持仓')
    lines.append('')

    # === 持仓明细 ===
    if positions:
        lines.append('【持仓明细】')
        for p in positions:
            coin = p['coin']
            entry = p['entry']
            current = p['current']
            side = p['side']
            pnl = p['pnl']

            # 状态行
            if pnl > 0:
                pnl_str = '+$%.2f 📈' % pnl
            elif pnl < 0:
                pnl_str = '$%.2f 📉' % pnl
            else:
                pnl_str = '$0.00'

            lines.append('  %s %s 均价$%s 现价$%s %s' % (
                coin, side[:4], '%.4f' % entry if entry else '?',
                '%.4f' % current if current else '?', pnl_str))

            # 距入场
            if p['dist_to_entry_pct'] is not None:
                arrow = '↑' if p['dist_to_entry_pct'] > 0 else '↓'
                lines.append('  %s入场%+.1f%% | SL%.1f%% | TP+%.1f%%' % (
                    arrow, p['dist_to_entry_pct'],
                    p['dist_to_sl_pct'] if p['dist_to_sl_pct'] else 0,
                    p['dist_to_tp_pct'] if p['dist_to_tp_pct'] else 0))

            # 持仓时长
            hr = p['hours_remaining']
            flag = ''
            if p['timeout_pct'] >= 100:
                flag = ' ⛔超时'
            elif p['timeout_pct'] >= 80:
                flag = ' 🟡超时警告'
            lines.append('  ⏱ %.1f小时/%d小时 (剩%.1fh)%s' % (
                p['hours_elapsed'], MAX_HOLD_HOURS, hr if hr > 0 else 0, flag))

            # 强平缓冲
            if p['liq_buffer_pct'] is not None:
                buf_flag = '🔴' if p['liq_buffer_pct'] < 3 else '🟡' if p['liq_buffer_pct'] < 5 else ''
                lines.append('  %s距强平%.1f%%' % (buf_flag, p['liq_buffer_pct']))
            lines.append('')

    # === 交易统计 ===
    if stats['total'] > 0:
        lines.append('【交易统计】共%d笔 | 开%d | 亏%d | 失败%d' % (
            stats['total'], stats['open'], stats['closed'], stats['failed']))
        if stats['with_pnl'] > 0:
            lines.append('  胜率%d%% | WLR%.2f | 总盈亏$%+.2f' % (
                stats['win_rate'], stats['wrr'], stats['total_pnl']))
            if stats['wins'] > 0:
                lines.append('  平均盈$%.2f | 平均亏$%.2f' % (
                    stats['avg_win'], stats['avg_loss']))
        if stats['without_pnl'] > 0:
            lines.append('  ⚠️ %d笔缺少pnl数据' % stats['without_pnl'])
        lines.append('')

    # === 财务政策（使用动态限制）===
    dyn = get_dynamic_treasury_limits(equity)
    hourly_snap = treasury_state.get('hourly_snapshot_equity')
    daily_snap = treasury_state.get('daily_snapshot_equity')
    if hourly_snap and daily_snap:
        hourly_loss = hourly_snap - equity
        daily_loss = daily_snap - equity
        h_pct = min(100, hourly_loss / dyn['hourly_limit'] * 100) if dyn['hourly_limit'] > 0 else 0
        d_pct = min(100, daily_loss / dyn['daily_limit'] * 100) if dyn['daily_limit'] > 0 else 0
        h_bar = '█' * int(max(0, h_pct)/10) + '░' * (10 - int(max(0, h_pct)/10))
        d_bar = '█' * int(max(0, d_pct)/10) + '░' * (10 - int(max(0, d_pct)/10))
        h_flag = '🚫' if hourly_loss >= dyn['hourly_limit'] else '🟡' if hourly_loss >= dyn['hourly_limit']*0.8 else '✅'
        d_flag = '🚫' if daily_loss >= dyn['daily_limit'] else '🟡' if daily_loss >= dyn['daily_limit']*0.8 else '✅'
        lines.append('【财务政策(动态)】')
        lines.append('  %s 小时亏损 $%.2f/$%.2f [%s] %.0f%%' % (h_flag, abs(hourly_loss), dyn['hourly_limit'], h_bar, abs(h_pct)))
        lines.append('  %s 今日亏损 $%.2f/$%.2f [%s] %.0f%%' % (d_flag, abs(daily_loss), dyn['daily_limit'], d_bar, abs(d_pct)))

    return '\n'.join(lines)


def run_heartbeat():
    """运行心跳报告"""
    now = datetime.now(zoneinfo.ZoneInfo('Asia/Shanghai'))
    hour = now.hour

    # 0. 先从OKX fills修复历史PnL（每次心跳都检查）
    updated_pnl = update_trade_pnl_from_fills()
    if updated_pnl:
        print('PnL修复:', ', '.join(updated_pnl))

    # 权益和层级
    bal = get_account_balance()
    equity = bal.get('totalEq', 0)
    tier, tier_state = get_survival_tier(equity)
    treasury_state = load_treasury_state()

    # 真实持仓 + SL/TP
    real_pos, _ = get_real_positions()
    orders, _ = get_real_sl_tp_orders()

    # 熔断检查
    circuit_tripped, circuit_reason, circuit_state = check_circuit_breaker()

    # 交易历史（已更新PnL）
    trades = load_paper_trades()
    stats = compute_trade_stats(trades)
    open_trades = [t for t in trades if t.get('status') == 'OPEN']
    positions = analyze_open_positions(open_trades, real_pos or {}, orders or {})

    # 异常检测
    anomalies, warnings = check_anomalies(positions, equity, tier, treasury_state)

    # 熔断触发 → 加入异常
    if circuit_tripped:
        anomalies.append('⛔ 熔断已触发: %s' % circuit_reason)
        anomalies.append('→ 禁止开新仓，需1笔盈利才能解锁')

    # 生成报告
    report = format_heartbeat_report(positions, stats, tier, equity, treasury_state)

    # 熔断报告附加
    circuit_report = format_circuit_report(circuit_state)
    report += '\n\n' + circuit_report

    # ===== Journal统计附加（每6小时推送时）=====
    if hour % 6 == 0:
        try:
            from kronos_journal import load_journal, compute_stats as j_stats
            journal = load_journal()
            j = j_stats(journal)
            if j['total_trades'] > 0:
                pnl_icon = '🟢' if j['total_pnl'] >= 0 else '🔴'
                journal_section = [
                    '',
                    '【日志统计】',
                    '  总交易 %d笔 | 已平 %d | 持仓 %d | 异常 %d' % (
                        j['total_trades'], j['closed_trades'], j['open_trades'],
                        j['balance_fails'] + j['system_fails']),
                    '  %s总盈亏 %s%.4f | 胜率 %.1f%% | WLR %s' % (
                        pnl_icon, pnl_icon, j['total_pnl'],
                        j['win_rate'] * 100, str(j['wlr'])),
                    '  均持仓 %.1fh | 最大回撤 $%.4f' % (
                        j['avg_hold_hours'], j['max_drawdown']),
                ]
                if j['coin_stats']:
                    coin_lines = []
                    for coin, cs in sorted(j['coin_stats'].items()):
                        cp = '🟢' if cs['total_pnl'] >= 0 else '🔴'
                        wr = cs['win_rate']
                        wr_str = '%.0f%%' % (wr * 100) if isinstance(wr, float) else str(wr)
                        coin_lines.append(
                            '  %s %s %s%.4f (%s胜率%s)' % (
                                coin, cp, cp, cs['total_pnl'],
                                cs['wins'], wr_str))
                    if coin_lines:
                        journal_section.append('  各币:')
                        journal_section.extend(coin_lines)
                report += '\n'.join(journal_section)
        except Exception as e:
            print('Journal stats error: %s' % e)

    # === 推送决策 ===
    if anomalies:
        # 有异常：立即推送
        push_feishu('🚨 Kronos告警\n\n' + '\n'.join(anomalies))
        print('推送异常告警: %d条' % len(anomalies))

    # 每6小时推送完整报告（0,6,12,18时）
    if hour % 6 == 0 and not anomalies:
        push_feishu(report)
        print('推送完整报告 (hour=%d)' % hour)
    elif not anomalies:
        # 非完整报告时间但有警告
        if warnings:
            push_feishu('⚠️ Kronos提醒\n\n' + '\n'.join(warnings))
            print('推送警告 (hour=%d): %d条' % (hour, len(warnings)))
        else:
            print('静默 (hour=%d, 正常)' % hour)

    return report, anomalies, warnings


if __name__ == '__main__':
    report, anomalies, warnings = run_heartbeat()
    print()
    print(report)
    if anomalies:
        print('\n异常:', anomalies)
    if warnings:
        print('\n警告:', warnings)
