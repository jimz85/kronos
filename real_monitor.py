#!/usr/bin/env python3
"""
真实持仓监控系统
- 每分钟查询OKX真实持仓
- 对比paper_trades.json，发现差异立即告警
- 为每个持仓设置正确的SL/TP条件单（独立接口）
"""
import os, sys, json, time, hmac, hashlib, base64, requests, logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger('real_monitor')
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

# 导入kronos_pilot的推送函数和kronos_utils的PnL计算
sys.path.insert(0, str(Path(__file__).parent))
from kronos_pilot import push_feishu
from kronos_utils import get_pnl_from_fills, atomic_write_json
# Lazy import to avoid circular dependency
# from kronos_active_judgment import close_position as close_pos_action

# 加载.env
from dotenv import load_dotenv
load_dotenv(Path.home() / '.hermes' / '.env', override=True)

OKX_API_KEY = os.getenv('OKX_API_KEY')
OKX_SECRET = os.getenv('OKX_SECRET')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE')

# OKX需要ISO8601格式时间戳（真实账户不能用Unix毫秒）
def _ts():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

def _req(method, path, body=''):
    """带正确时间戳的OKX API请求"""
    ts = _ts()
    msg = ts + method + path + (body if body else '')
    sig = base64.b64encode(hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    h = {
        'OK-ACCESS-KEY': OKX_API_KEY,
        'OK-ACCESS-SIGN': sig,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': os.getenv('OKX_FLAG', '1'),
    }
    url = 'https://www.okx.com' + path
    if method == 'GET':
        r = requests.get(url, headers=h, timeout=10)
    else:
        r = requests.post(url, headers=h, data=body, timeout=10)
    result = r.json()
    if result.get('code') not in ('0', None):
        logger.error('    API错误: %s %s' % (result.get('code'), result.get('msg', '')))
    return result


# ============ 动态财务政策系统 v2.0 ============
# 防止单日/单笔/单小时崩溃式亏损
# 核心改进：根据账户余额、持仓、时间、市场环境自动调整限制

# 基础比例参数（占总权益百分比）
TREASURY_BASE = {
    'hourly_loss_pct':    0.02,   # 每小时最大允许亏损2%账户
    'daily_loss_pct':     0.05,   # 每天最大允许亏损5%账户
    'per_trade_pct':      0.01,   # 单笔最大1%账户
    'reserve_pct':        0.20,   # 最低保留20%账户（已从10%上调，P1 Fix）
    'warning_ratio':      0.80,   # 80%阈值告警
}

# 市场波动调整（ADX越高越收紧）
ADX_THRESHOLDS = [
    (15, 1.00),   # ADX<15: 正常
    (25, 0.80),   # ADX 15-25: 收紧20%
    (35, 0.60),   # ADX 25-35: 收紧40%
    (50, 0.40),   # ADX 35-50: 收紧60%
    (999, 0.25),  # ADX>50: 收紧75%（极端波动）
]

# 时间段调整（美股时段波动更大）
TIME_MULTIPLIERS = {
    'Asia':   1.00,   # 亚洲盘（22:00-8:00 UTC）：正常
    'Europe': 0.90,   # 欧洲盘（8:00-14:00 UTC）：略紧
    'US_AM':  0.70,   # 美股早盘（14:00-17:00 UTC）：收紧
    'US_PM':  0.60,   # 美股尾盘（17:00-22:00 UTC）：最紧
}

def _get_time_bucket():
    """获取当前时间段"""
    from datetime import datetime
    h = datetime.now().hour
    if 22 <= h or h < 8:
        return 'Asia'
    elif 8 <= h < 14:
        return 'Europe'
    elif 14 <= h < 17:
        return 'US_AM'
    else:
        return 'US_PM'

def get_consecutive_loss_multiplier(state):
    """
    连续亏损收紧
    连亏越多，收得越紧
    """
    consecutive = state.get('consecutive_loss_hours', 0)
    if consecutive <= 1:
        return 1.00   # 无或1次：正常
    elif consecutive == 2:
        return 0.85
    elif consecutive == 3:
        return 0.70
    elif consecutive == 4:
        return 0.50
    else:
        return 0.30   # 5次及以上：极度收紧

def get_dynamic_treasury_limits(equity, positions=None, market_data=None):
    """
    动态计算财务限制
    根据：账户余额、持仓规模、市场波动、时间段、连续亏损次数

    positions: {'AVAX': {'pos': 169, 'side': 'long', 'avgPx': 9.37, 'upl': -13.69}}
    market_data: {'AVAX': {'adx': 17.8, 'btc_direction': 'neutral'}, ...}
    """
    if equity is None or equity <= 0:
        return {
            'hourly_limit': 0,
            'daily_limit': 0,
            'per_trade_limit': 0,
            'reserve_balance': 0,
            'hourly_pct': 2.0,
            'daily_pct': 5.0,
            'multipliers': {'pos': 1.0, 'vol': 1.0, 'time': 1.0, 'loss': 1.0},
            'time_bucket': _get_time_bucket(),
            'combined_factor': 1.0,
            'equity': equity if equity else 0,
            'reason': '无有效权益',
        }

    # 1. 基础限制
    hourly_base = equity * TREASURY_BASE['hourly_loss_pct']
    daily_base = equity * TREASURY_BASE['daily_loss_pct']
    per_trade_base = equity * TREASURY_BASE['per_trade_pct']
    reserve_base = equity * TREASURY_BASE['reserve_pct']

    # 2. 持仓规模因子（持仓越大，整体限制越紧）
    pos_factor = 1.0
    if positions:
        total_exposure = sum(p['pos'] * p.get('avgPx', 0) for p in positions.values() if p.get('avgPx', 0) > 0)
        exposure_ratio = total_exposure / equity if equity > 0 else 0
        # 持仓占比>50%时开始收紧
        if exposure_ratio > 0.5:
            pos_factor = 0.8
        elif exposure_ratio > 0.8:
            pos_factor = 0.6
        elif exposure_ratio > 1.0:
            pos_factor = 0.4

    # 3. 市场波动因子（取持仓币种中最高的ADX）
    vol_factor = 1.0
    max_adx = 15
    if market_data:
        adx_values = [d.get('adx_1h', 15) or 15 for d in market_data.values() if d.get('adx_1h')]
        if adx_values:
            max_adx = max(adx_values)
            for threshold, factor in ADX_THRESHOLDS:
                if max_adx < threshold:
                    vol_factor = factor
                    break

    # 4. 时间段因子
    time_bucket = _get_time_bucket()
    time_factor = TIME_MULTIPLIERS.get(time_bucket, 1.0)

    # 5. 连续亏损因子
    state = load_treasury_state()
    loss_factor = get_consecutive_loss_multiplier(state)

    # 6. 综合乘数
    combined_factor = pos_factor * vol_factor * time_factor * loss_factor

    # 7. 最终限制
    hourly_limit = hourly_base * combined_factor
    daily_limit = daily_base * combined_factor
    per_trade_limit = per_trade_base * combined_factor * pos_factor  # 单笔单独乘以持仓因子
    reserve_balance = reserve_base

    return {
        'hourly_limit': round(hourly_limit, 2),
        'daily_limit': round(daily_limit, 2),
        'per_trade_limit': round(per_trade_limit, 2),
        'reserve_balance': round(reserve_balance, 2),
        'hourly_pct': round(TREASURY_BASE['hourly_loss_pct'] * combined_factor * 100, 2),
        'daily_pct': round(TREASURY_BASE['daily_loss_pct'] * combined_factor * 100, 2),
        'multipliers': {
            'pos': round(pos_factor, 2),
            'vol': round(vol_factor, 2),
            'time': round(time_factor, 2),
            'loss': round(loss_factor, 2),
        },
        'time_bucket': time_bucket,
        'combined_factor': round(combined_factor, 3),
        'equity': round(equity, 2),
        'reason': f'持仓{int(exposure_ratio*100) if positions else 0}% × ADX{int(max_adx) if market_data else 15} × {time_bucket} × 连亏{state.get("consecutive_loss_hours",0)}次',
    }


def _get_treasury_path():
    return Path.home() / '.hermes' / 'cron' / 'output' / 'kronos_treasury.json'


def load_treasury_state():
    """加载财务政策状态"""
    path = _get_treasury_path()
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {
            'hourly_snapshot_equity': None,
            'hourly_snapshot_time': None,
            'daily_snapshot_equity': None,
            'daily_snapshot_time': None,
            'session_start_equity': None,
            'session_start_time': None,
            'last_check_time': None,
            'consecutive_loss_hours': 0,   # 连续亏损小时数
        }


def save_treasury_state(state):
    """保存财务政策状态（原子写入，防断电损坏）"""
    path = _get_treasury_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state, indent=2)


def on_trade_result(pnl, trade_direction=None):
    """
    交易平仓后调用此函数，根据盈亏更新连续亏损计数。

    修复（2026-04-23）：
    - 盈利时（pnl > 0）：重置consecutive_loss_hours = 0
    - 亏损时（pnl < 0）：增加consecutive_loss_hours计数
    - 持平（pnl = 0）：不改变计数

    用法：在平仓逻辑（止损/止盈/手动平）处调用
    示例：on_trade_result(pnl=realized_pnl, trade_direction='long')
    """
    state = load_treasury_state()
    if pnl > 0:
        state['consecutive_loss_hours'] = 0
        state['last_profitable_trade'] = _ts()
        changed = True
    elif pnl < 0:
        state['consecutive_loss_hours'] = state.get('consecutive_loss_hours', 0) + 1
        changed = True
    else:
        changed = False
    if changed:
        save_treasury_state(state)
    return state.get('consecutive_loss_hours', 0)


def update_treasury_snapshots(equity, positions=None, market_data=None):
    """
    更新财务快照 + 动态限制计算
    同时追踪连续亏损次数

    修复（2026-04-23）：
    - daily_snapshot：在UTC午夜重置（真正的日内起点）
    - hourly_snapshot：每小时重置（用于计算上一小时的盈亏）
    - 新增 hourly_snapshot_prev：保存上一个小时的快照权益（用于滑动窗口计算）
    - consecutive_losses：只在实际盈利时重置，不在跨天/跨小时自动清零
    - equity：保存当前equity供check_treasury使用（P0-1修复）
    """
    now = datetime.now()
    state = load_treasury_state()

    changed = False

    # ── Guard: 不接受0或None作为equity（API失败时跳过快照更新） ──
    if not equity or equity <= 0:
        return state.get('consecutive_loss_hours', 0)

    # ── Guard: hourly_snapshot_equity=0 且 equity>0 → 说明快照因API失败被污染，
    #           等下次小时变化再修复，不要现在写入0值
    # ── P1 Fix: 同时处理 prev=0 的情况（prev=0 + equity>0 = 下次小时计算出巨额幽灵亏损）
    stale_hourly = (state.get('hourly_snapshot_equity') or 0) <= 0 and equity > 0
    stale_prev = (state.get('hourly_snapshot_prev') or 0) <= 0 and equity > 0

    # ── P0-1 Fix: 保存当前equity ─────────────────────────────
    if state.get('equity') != equity:
        state['equity'] = equity
        changed = True

    # daily窗口：在UTC午夜重置（真正的日内起点）
    daily_time = state.get('daily_snapshot_time')
    if daily_time:
        last_daily = datetime.fromisoformat(daily_time)
        if now.date() != last_daily.date():
            state['daily_snapshot_equity'] = equity
            state['daily_snapshot_time'] = now.isoformat()
            changed = True
    else:
        state['daily_snapshot_equity'] = equity
        state['daily_snapshot_time'] = now.isoformat()
        changed = True

    # hourly窗口：每小时重置（用于追踪"上一小时"的起始权益）
    hourly_time = state.get('hourly_snapshot_time')
    if hourly_time:
        last_hourly = datetime.fromisoformat(hourly_time)
        # P1 Fix: 检测到脏数据（API失败时hourly被写成0）→立即重新初始化，防止下次小时变化时计算出巨额幽灵亏损
        if stale_hourly or stale_prev or (now.hour != last_hourly.hour or now.date() != last_hourly.date()):
            # P1 Fix: 只有在快照有效时才更新prev_equity；stale数据无法可靠计算亏损
            old_snap = state.get('hourly_snapshot_equity')
            if old_snap and old_snap > 0:
                state['hourly_snapshot_prev'] = old_snap
            # else: 保持prev不变（stale数据无法用于亏损计算）
            if equity > 0:  # P0-5: 防止equity=0污染快照
                state['hourly_snapshot_equity'] = equity
            state['hourly_snapshot_time'] = now.isoformat()
            changed = True
    else:
        # 首次初始化
        if equity > 0:  # P0-5: 防止equity=0污染快照
            state['hourly_snapshot_equity'] = equity
        state['hourly_snapshot_prev'] = equity
        state['hourly_snapshot_time'] = now.isoformat()
        changed = True

    # ── P0-3 Fix: 如果prev仍未设置（系统启动时在同一小时内走完了if分支），
    #              立即补充初始化prev=当前equity
    if state.get('hourly_snapshot_prev') is None:
        state['hourly_snapshot_prev'] = equity
        changed = True

    # Session起始权益（用于per-trade计算）
    if state.get('session_start_equity') is None:
        state['session_start_equity'] = equity
        state['session_start_time'] = now.isoformat()

    # ========== 连续亏损追踪 ==========
    # 修复（2026-04-23）：
    # - 不再在小时/天边界自动重置consecutive_loss_hours
    # - 只在有实际盈利交易时重置（由on_trade_result调用）
    # - 在小时边界用hourly_snapshot_prev比较（滑动窗口），避免重启抹掉历史
    # 注意：这个计数反映的是"最近N小时内是否有盈利"，不是"连续N笔交易亏损"
    hourly_time = state.get('hourly_snapshot_time')
    if hourly_time:
        last_hourly = datetime.fromisoformat(hourly_time)
        if now.hour != last_hourly.hour or now.date() != last_hourly.date():
            # 新小时开始：检查这一个小时的盈亏（与hourly_snapshot_prev比较）
            prev_equity = state.get('hourly_snapshot_prev')
            if prev_equity and equity < prev_equity:
                # 亏损了，增加计数
                state['consecutive_loss_hours'] = state.get('consecutive_loss_hours', 0) + 1
            # 注意：盈利或持平不重置——只在真正有盈利交易时重置
    # 每天开始时重置daily快照（不重置consecutive_loss_hours）
    daily_time = state.get('daily_snapshot_time')
    if daily_time:
        last_daily = datetime.fromisoformat(daily_time)
        if now.date() != last_daily.date():
            pass  # 不再自动重置consecutive_loss_hours
    state['last_check_time'] = now.isoformat()

    if changed or state.get('hourly_snapshot_equity') is None:
        save_treasury_state(state)

    return state


def check_treasury_limits(equity, proposed_entry_price=None, proposed_sl_price=None, proposed_size=1, positions=None, market_data=None):
    """
    检查财务政策限制（动态版本）
    返回 (is_allowed, reason, warnings)

    is_allowed: True = 可以开仓, False = 被限制
    reason: 限制原因或允许原因
    warnings: 告警列表（接近阈值）

    positions: {'AVAX': {'pos': 169, 'side': 'long', 'avgPx': 9.37}}
    market_data: {'AVAX': {'adx_1h': 17.8, 'btc_direction': 'neutral'}}
    """
    # 尝试自动获取持仓和市场数据（如果没传）
    if positions is None or market_data is None:
        try:
            from .kronos_utils import get_positions, get_market_data
            if positions is None:
                positions = get_positions()
            if market_data is None:
                market_data = {coin: get_market_data(coin) for coin in positions.keys()}
        except:
            pass

    # 获取动态限制
    dyn = get_dynamic_treasury_limits(equity, positions, market_data)
    state = load_treasury_state()
    warnings = []

    # 1. ReserveBalance检查（硬限制）
    if equity < dyn['reserve_balance']:
        return False, (
            '🚫 权益$%.2f < 保留金$%.2f\n'
            '系统禁止开新仓，保留资金\n'
            '调整因子: %s' % (
                equity, dyn['reserve_balance'], dyn['reason']
            )
        ), []

    # 2. Hourly Loss Limit检查（已禁用，允许自由交易）
    # hourly_snap = state.get('hourly_snapshot_equity')
    # if hourly_snap is not None:
    #     hourly_loss = hourly_snap - equity  # 正=亏损，负=盈利
    #     hourly_limit = dyn['hourly_limit']
    #     if hourly_loss >= hourly_limit:
    #         return False, (
    #             '🚫 小时亏损$%.2f >= 限制$%.2f\n'
    #             '限制说明: 每小时亏%s的%.0f%% = $%.2f\n'
    #             '等待下一个小时窗口重置 | 因子: %s' % (
    #                 hourly_loss, hourly_limit,
    #                 equity, dyn['hourly_pct'],
    #                 hourly_limit, dyn['reason']
    #             )
    #         ), []
    #     elif hourly_loss >= hourly_limit * TREASURY_BASE['warning_ratio']:
    #         warnings.append(
    #             '🟡 小时亏损$%.2f (限制$%.2f的%.0f%%) | %s' % (
    #                 hourly_loss, hourly_limit, hourly_loss / hourly_limit * 100,
    #                 dyn['reason']
    #             )
    #         )

    # 3. Daily Loss Limit检查（仅当快照是今天UTC设置时生效）
    daily_snap = state.get('daily_snapshot_equity')
    daily_time_str = state.get('daily_snapshot_time')
    daily_limit = dyn['daily_limit']
    # 判断快照是否今天UTC设置的
    is_daily_snap_today = False
    if daily_time_str:
        try:
            snap_dt = datetime.fromisoformat(daily_time_str)
            is_daily_snap_today = snap_dt.date() == datetime.now().date()
        except:
            pass
    if daily_snap is not None and is_daily_snap_today:
        daily_loss = daily_snap - equity  # 正=亏损，负=盈利
        if daily_loss >= daily_limit:
            return False, (
                '🚫 今日亏损$%.2f >= 限制$%.2f\n'
                '限制说明: 每天亏%s的%.0f%%(×%.2f时段因子) = $%.2f\n'
                '等待UTC 00:00重置 | 因子: %s' % (
                    daily_loss, daily_limit,
                    equity, TREASURY_BASE['daily_loss_pct']*100,
                    dyn.get('multipliers',{}).get('time',1.0),
                    daily_limit, dyn['reason']
                )
            ), []
        elif daily_loss >= daily_limit * TREASURY_BASE['warning_ratio']:
            warnings.append(
                '🟡 今日亏损$%.2f (限制$%.2f的%.0f%%) | %s' % (
                    daily_loss, daily_limit, daily_loss / daily_limit * 100,
                    dyn['reason']
                )
            )
    elif daily_snap is not None and not is_daily_snap_today:
        # 快照是昨天或更早的，跳过日亏损熔断（避免历史亏损阻止今天交易）
        warnings.append('🟡 日亏损快照是昨天，跳过日熔断检查')

    # 4. Per-Trade Loss Limit检查（理论最大亏损）
    if proposed_entry_price and proposed_sl_price and proposed_size:
        try:
            loss_per_trade = abs(float(proposed_entry_price) - float(proposed_sl_price)) * float(proposed_size)
            pt_limit = dyn['per_trade_limit']
            if loss_per_trade > pt_limit:
                return False, (
                    '🚫 单笔理论亏损$%.2f > 限制$%.2f\n'
                    '减少仓位或扩大止损距离 | 因子: %s' % (
                        loss_per_trade, pt_limit, dyn['reason']
                    )
                ), []
            elif loss_per_trade >= pt_limit * TREASURY_BASE['warning_ratio']:
                warnings.append(
                    '🟡 单笔理论亏损$%.2f (限制$%.2f的%.0f%%)' % (
                        loss_per_trade, pt_limit,
                        loss_per_trade / pt_limit * 100
                    )
                )
        except (ValueError, TypeError):
            pass

    # 5. 综合判断
    if warnings:
        reason = '\n'.join(warnings)
    else:
        reason = '✅ 财务检查通过 | %s' % dyn['reason']

    return True, reason, warnings


def format_treasury_report(equity, positions=None, market_data=None):
    """生成财务政策状态报告（动态版本）"""
    state = load_treasury_state()
    now = datetime.now()

    # 获取动态限制
    dyn = get_dynamic_treasury_limits(equity, positions, market_data)

    lines = ['━━━ 动态财务政策 v2.0 ━━━']
    lines.append('  账户权益: $%s' % dyn['equity'])

    # 调整因子说明
    m = dyn['multipliers']
    lines.append('  调整因子: 持仓×%.0f | ADX×%.0f | %s时段×%.0f | 连亏%d次×%.0f' % (
        m['pos'] * 100, m['vol'] * 100, dyn['time_bucket'], m['time'] * 100,
        state.get('consecutive_loss_hours', 0), m['loss'] * 100
    ))
    lines.append('  综合乘数: %.1f%% | 实际限额: 每小时%.1f%% 每天%.1f%%' % (
        dyn['combined_factor'] * 100, dyn['hourly_pct'], dyn['daily_pct']
    ))

    # Hourly
    hourly_snap = state.get('hourly_snapshot_equity')
    if hourly_snap is not None:
        hourly_loss = hourly_snap - equity  # 正=亏损，负=盈利
        h_limit = dyn['hourly_limit']
        pct = min(100, hourly_loss / h_limit * 100) if h_limit > 0 else 0
        bar = '█' * int(max(0, pct) / 10) + '░' * (10 - int(max(0, pct) / 10))
        if hourly_loss >= h_limit:
            status = '🚫'
        elif hourly_loss >= h_limit * 0.8:
            status = '🟡'
        else:
            status = '✅'
        loss_str = '亏损' if hourly_loss > 0 else '盈利'
        lines.append(
            '  %s 小时%s $%.2f / $%.2f [%s] %.0f%%' % (status, loss_str, abs(hourly_loss), h_limit, bar, abs(pct))
        )

    # Daily
    daily_snap = state.get('daily_snapshot_equity')
    if daily_snap is not None:
        daily_loss = daily_snap - equity  # 正=亏损，负=盈利
        d_limit = dyn['daily_limit']
        pct = min(100, daily_loss / d_limit * 100) if d_limit > 0 else 0
        bar = '█' * int(max(0, pct) / 10) + '░' * (10 - int(max(0, pct) / 10))
        if daily_loss >= d_limit:
            status = '🚫'
        elif daily_loss >= d_limit * 0.8:
            status = '🟡'
        else:
            status = '✅'
        loss_str = '亏损' if daily_loss > 0 else '盈利'
        lines.append(
            '  %s 今日%s $%.2f / $%.2f [%s] %.0f%%' % (status, loss_str, abs(daily_loss), d_limit, bar, abs(pct))
        )

    # Reserve
    reserve_ok = equity >= dyn['reserve_balance']
    lines.append(
        '  %s 保留金 $%.2f >= $%.2f' % (
            '✅' if reserve_ok else '🚫',
            equity, dyn['reserve_balance']
        )
    )

    # Per-trade
    pt_limit = dyn['per_trade_limit']
    lines.append('  动态限制: 单笔$%.2f | 小时$%.2f | 日$%.2f | 保留金$%.2f' % (
        pt_limit, dyn['hourly_limit'], dyn['daily_limit'], dyn['reserve_balance']
    ))

    return '\n'.join(lines)


# ============ 五级生存层级系统 ============
# 参考: Automaton生存压力设计 - Kronos交易版
# 初始资金 = START_BALANCE，每次启动时记录
START_BALANCE = 20.00   # 固定基准（USD）

TIER_THRESHOLDS = {
    # tier_name: (min_equity_pct, description, allowed_actions)
    # min_equity_pct: 相对START_BALANCE的百分比
    'normal':       (0.90, '正常',    '全策略开仓+正常仓位'),
    'caution':      (0.80, '谨慎',    '仓位减半，只做最优策略'),
    'low_compute':  (0.70, '降级',    '最小仓位，只做ADA/DOGE'),
    'critical':     (0.60, '危急',    '只监控不开新仓'),
    'paused':       (0.00, '暂停',    '禁止所有交易，需人工解锁'),
}

# 降级恢复需要连续N次检测高于阈值
RECOVERY_CONSECUTIVE = 3

# 状态文件路径
def _get_state_path():
    return Path.home() / '.hermes' / 'cron' / 'output' / 'kronos_survival_state.json'


def load_survival_state():
    """加载生存状态（含历史连续计数）"""
    path = _get_state_path()
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {
            'tier': 'normal',
            'consecutive_above_count': 0,  # 连续高于阈值的次数
            'is_paused': False,            # 是否被人工暂停
            'pause_reason': '',
            'last_tier_change_time': '',
            'last_equity': START_BALANCE,
            'tier_change_log': [],         # 最近5次层级变化记录
        }


def save_survival_state(state):
    """保存生存状态到磁盘（原子写入，防断电损坏）"""
    path = _get_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state, indent=2)


def get_survival_tier(equity):
    """
    根据当前权益计算生存层级
    规则：
    - paused: 人工暂停 → 跳过检测，不自动恢复
    - 低于60%: critical
    - 低于70%: low_compute
    - 低于80%: caution
    - 高于80%: normal
    - 向上恢复需要连续3次检测高于阈值
    """
    # P1 Fix: equity=0通常是API失败，不是真实权益。用treasury快照作fallback
    if not equity or equity <= 0:
        treasury = load_treasury_state()
        equity = treasury.get('equity') or treasury.get('daily_snapshot_equity') or 0
        if equity <= 0:
            equity = treasury.get('session_start_equity') or 0

    state = load_survival_state()

    # 被人工暂停：不解锁不恢复
    if state.get('is_paused'):
        return 'paused', state

    prev_tier = state.get('tier', 'normal')
    prev_above_count = state.get('consecutive_above_count', 0)

    # 计算各层级的实际权益阈值
    thresholds = {
        tier: START_BALANCE * pct
        for tier, (pct, _, _) in TIER_THRESHOLDS.items()
    }

    # 当前权益对应的层级
    new_tier = 'normal'
    for tier in ['normal', 'caution', 'low_compute', 'critical']:
        min_eq = thresholds[tier]
        if equity < min_eq:
            new_tier = tier

    # 层级变化处理
    tier_changed = (new_tier != prev_tier)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if tier_changed:
        # 降级（恶化）：立即生效
        if _tier_rank(new_tier) > _tier_rank(prev_tier):
            state['tier'] = new_tier
            state['consecutive_above_count'] = 0
            state['last_tier_change_time'] = now_str
            state['tier_change_log'].append({
                'time': now_str,
                'from': prev_tier,
                'to': new_tier,
                'equity': round(equity, 2),
                'reason': 'equity_drop'
            })
            if len(state['tier_change_log']) > 5:
                state['tier_change_log'] = state['tier_change_log'][-5:]

        # 升级（恢复）：需要连续3次检测
        else:
            if equity >= thresholds.get(prev_tier, START_BALANCE):
                state['consecutive_above_count'] = prev_above_count + 1
                if state['consecutive_above_count'] >= RECOVERY_CONSECUTIVE:
                    state['tier'] = new_tier
                    state['consecutive_above_count'] = 0
                    state['last_tier_change_time'] = now_str
                    state['tier_change_log'].append({
                        'time': now_str,
                        'from': prev_tier,
                        'to': new_tier,
                        'equity': round(equity, 2),
                        'reason': 'recovery'
                    })
            else:
                state['consecutive_above_count'] = 0

    state['last_equity'] = equity
    save_survival_state(state)

    return state['tier'], state


def _tier_rank(tier):
    """层级数值越小越危险（数值越高）"""
    rank = {'normal': 1, 'caution': 2, 'low_compute': 3, 'critical': 4, 'paused': 5}
    return rank.get(tier, 5)


def can_open_new_position(tier, coin=None):
    """
    根据层级判断能否开新仓
    返回 (bool, reason)
    """
    if tier == 'paused':
        return False, '⛔ 系统已暂停，需人工解锁'
    if tier == 'critical':
        return False, '⚠️ 危急层级，禁止开新仓'
    if tier == 'low_compute':
        if coin and coin not in ['ADA', 'DOGE']:
            return False, '⚠️ 降级层，只允许ADA/DOGE'
        return True, '🟡 降级层，最小仓位'
    if tier == 'caution':
        return True, '🟡 谨慎层，仓位减半'
    return True, '✅ 正常层'


def get_position_multiplier(tier):
    """
    根据层级返回仓位倍数
    用于下单时调整仓位
    """
    multipliers = {
        'normal': 1.0,
        'caution': 0.5,
        'low_compute': 0.25,
        'critical': 0.0,   # 不开新仓
        'paused': 0.0,
    }
    return multipliers.get(tier, 0.0)


def pause_system(reason, state=None):
    """人工暂停系统"""
    if state is None:
        state = load_survival_state()
    state['is_paused'] = True
    state['tier'] = 'paused'
    state['pause_reason'] = reason
    state['last_tier_change_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_survival_state(state)
    return state


def resume_system():
    """人工恢复系统"""
    state = load_survival_state()
    state['is_paused'] = False
    state['tier'] = 'normal'
    state['consecutive_above_count'] = 0
    save_survival_state(state)
    return state


def format_tier_report(state, equity):
    """生成层级状态报告文本"""
    tier = state['tier']
    pct = equity / START_BALANCE * 100
    thresholds = {tier: round(START_BALANCE * p, 2) for tier, (p, _, _) in TIER_THRESHOLDS.items()}
    multiplier = get_position_multiplier(tier)

    desc_map = dict([(t, d) for t, (_, d, _) in TIER_THRESHOLDS.items()])
    can_open, reason = can_open_new_position(tier)

    lines = [
        '━━━ 生存层级 ━━━',
        '当前权益: $%.2f (初始$%.2f的%.0f%%)' % (equity, START_BALANCE, pct),
        '层级: %s — %s' % (tier.upper(), desc_map.get(tier, '?')),
        '仓位倍数: ×%.2f' % multiplier,
        '开仓权限: %s' % reason,
    ]

    if state.get('is_paused'):
        lines.append('⛔ 已人工暂停: %s' % state.get('pause_reason', ''))
        lines.append('→ 需调用 resume_system() 才能恢复')

    if state.get('consecutive_above_count', 0) > 0:
        next_tier = _get_next_tier_up(state['tier'])
        if next_tier:
            lines.append('进度: %d/%d次检测后可升级至%s' % (
                state['consecutive_above_count'], RECOVERY_CONSECUTIVE, next_tier))

    if state.get('tier_change_log'):
        last = state['tier_change_log'][-1]
        lines.append('最近变化: %s → %s ($%s %.0f%%) @ %s' % (
            last['from'], last['to'], last['equity'],
            last['equity']/START_BALANCE*100, last['time']))

    return '\n'.join(lines)


def _get_next_tier_up(current):
    order = ['normal', 'caution', 'low_compute', 'critical', 'paused']
    try:
        idx = order.index(current)
        return order[idx - 1] if idx > 0 else None
    except:
        return None


# ============ 核心查询 ============

def get_real_positions(include_closed=False):
    """
    获取OKX真实持仓
    include_closed=True: 返回所有有过持仓的记录（含size=0）
                        用于纸仓同步（检测持仓消失）
    """
    try:
        result = _req('GET', '/api/v5/account/positions?instId=')
        if result.get('code') != '0':
            return None, '查询持仓失败: %s %s' % (result.get('code'), result.get('msg', ''))

        positions = {}
        for pos in result.get('data', []):
            inst = pos.get('instId', '')
            if '-USDT-SWAP' not in inst:
                continue
            coin = inst.replace('-USDT-SWAP', '')
            size = float(pos.get('pos', 0))

            # 默认只返回有持仓的
            if size <= 0 and not include_closed:
                continue

            side = pos.get('side', 'buy').lower()
            positions[coin] = {
                'side': side,  # buy=long, sell=short
                'size': size,   # size=0 表示已平仓
                'entry': float(pos.get('avgPx', 0)),
                'unrealized_pnl': float(pos.get('upl', 0)),
                'leverage': int(pos.get('lever', 3)),
                'mgnMode': pos.get('mgnMode', 'isolated'),  # 持仓保证金模式
                'cTime': pos.get('cTime', ''),  # 开仓时间戳(毫秒)，用于超时检测
                'liqPx': float(pos.get('liqPx', 0)) or 0,  # 强平价格
            }
        return positions, None
    except Exception as e:
        # P0 Fix: e may be a UnicodeEncodeError whose str() itself contains Chinese chars
        # that would re-trigger the same encoding error. Extract safe attributes instead.
        try:
            err_type = type(e).__name__
            if isinstance(e, UnicodeEncodeError):
                # Safely extract from UnicodeEncodeError without triggering re-encoding
                err_msg = '%s at position %s: %s' % (
                    e.encoding, e.start, e.reason)
            else:
                # Try repr first (safer than str for non-ASCII)
                try:
                    err_msg = repr(str(e))
                except Exception:
                    err_msg = err_type
            return None, '网络异常: [%s] %s' % (err_type, err_msg)
        except Exception:
            # Absolute last resort
            return None, '网络异常: <encoding error in exception handler>'


def cancel_algo_orders(instId, algo_ids):
    """
    取消条件单（支持多个）
    OKX要求格式: [{"algoId": "...", "instId": "..."}]
    """
    if not algo_ids:
        return []
    body = json.dumps([{'algoId': str(aid), 'instId': instId} for aid in algo_ids])
    result = _req('POST', '/api/v5/trade/cancel-algos', body)
    if result.get('code') != '0':
        return ['❌ 取消失败: %s' % result.get('msg', '')]
    return []


def get_real_sl_tp_orders():
    """获取活跃的SL/TP条件单（含OCO和conditional）
    
    P0 Bug修复：必须同时查oco和conditional两种ordType，否则会把已有的OCO订单当成"没有订单"。
    """
    orders = {}
    for ordType in ['oco', 'conditional']:
        try:
            result = _req('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP&ordType=%s&limit=100' % ordType)
            if result.get('code') != '0':
                continue
            
            for o in result.get('data', []):
                inst = o.get('instId', '')
                if '-USDT-SWAP' not in inst:
                    continue
                coin = inst.replace('-USDT-SWAP', '')
                algo_id = o.get('algoId', '')
                sl_price = o.get('slTriggerPx', '')
                tp_price = o.get('tpTriggerPx', '')
                sz = o.get('sz', '')
                
                if coin not in orders:
                    orders[coin] = {}
                if sl_price:
                    orders[coin]['sl'] = {'price': sl_price, 'algoId': algo_id, 'sz': sz, 'ordType': ordType}
                if tp_price:
                    orders[coin]['tp'] = {'price': tp_price, 'algoId': algo_id, 'sz': sz, 'ordType': ordType}
        except Exception:
            continue
    return orders, None


def place_sl_tp(coin, side, size, entry_price, sl_pct, tp_pct):
    """为持仓挂OCO bracket订单（SL+TP合并为1个OCO订单）
    
    P0 Bug修复：
    1. OKX每个持仓只允许1个条件单，必须用ordType='oco'合并SL+TP
    2. 幂等性：挂单前先查是否已有活跃OCO订单，有则跳过，防止重复挂单
    
    正确方案：使用ordType='oco'，在单个订单中同时包含SL和TP，触发时自动互斥。
    """
    instId = '%s-USDT-SWAP' % coin
    
    # ── 幂等检查：先查是否已有活跃OCO订单，有则跳过 ──
    existing_orders = []
    for ordType in ['oco', 'conditional']:
        try:
            r = _req('GET', '/api/v5/trade/orders-algo-pending?instId=%s&ordType=%s&limit=50' % (instId, ordType))
            for o in r.get('data', []):
                if o.get('algoId'):
                    existing_orders.append({'algoId': o.get('algoId'), 'ordType': ordType,
                                           'sl': o.get('slTriggerPx'), 'tp': o.get('tpTriggerPx')})
        except Exception:
            continue
    
    if existing_orders:
        return {'bracket': '⏭️ 已存在%d个活跃订单，跳过: %s' % (
            len(existing_orders), ', '.join('%s/%s' % (o['ordType'], o['algoId'][:8]) for o in existing_orders))}
    
    # net_mode: buy=long, sell=short
    if side == 'buy':
        close_side = 'sell'  # 平多
    else:
        close_side = 'buy'   # 平空
    
    # 计算止损止盈价格（如果传入的是百分比，用入场价换算）
    # P0 Fix：对SHORT仓位，SL方向和TP方向与LONG相反
    try:
        sl_pct_f = float(sl_pct)
        tp_pct_f = float(tp_pct)
        if sl_pct_f < 1.0:  # 百分比模式
            if side == 'buy':  # LONG
                sl_price = round(entry_price * (1 - sl_pct_f), 4)
                tp_price = round(entry_price * (1 + tp_pct_f), 4)
            else:  # SHORT
                sl_price = round(entry_price * (1 + sl_pct_f), 4)  # SL在入场价上方
                tp_price = round(entry_price * (1 - tp_pct_f), 4)  # TP在入场价下方
        else:  # 绝对价格模式
            sl_price = sl_pct_f
            tp_price = tp_pct_f
    except:
        sl_price = sl_pct
        tp_price = tp_pct
    
    # OCO Bracket订单：1个订单同时包含SL和TP，触发时互斥
    body = {
        'instId': instId,
        'tdMode': 'isolated',
        'side': close_side,
        'ordType': 'oco',       # OCO = One-Cancels-Other，SL+TP互斥
        'sz': str(int(size)),
        'reduceOnly': 'true',
        'posSide': 'long',
        'slTriggerPx': str(sl_price),
        'slOrdPx': '-1',        # 市价触发
        'tpTriggerPx': str(tp_price),
        'tpOrdPx': '-1',        # 市价触发
    }
    
    results = {}
    try:
        r = _req('POST', '/api/v5/trade/order-algo', json.dumps(body))
        code = r.get('code', '?')
        if code == '0':
            algo_id = r['data'][0]['algoId']
            results['bracket'] = '✅ SL@$%s + TP@$%s [id:%s]' % (sl_price, tp_price, algo_id[:8])
        else:
            results['bracket'] = '❌ OCO失败: %s (code=%s)' % (r.get('msg', ''), code)
    except Exception as e:
        results['bracket'] = '❌ OCO异常: %s' % e
    
    return results


def sync_paper_log(real_pos, paper_path=None):
    """
    同步真实持仓到纸仓日志
    real_pos: 来自 get_real_positions(include_closed=True) 的完整持仓数据
    - 补录: 真实有持仓但纸仓没有 → 新增OPEN记录
    - 更新: 真实已平仓 → 标记纸仓为CLOSED + 计算PnL
    v1.4: 平仓时实时拉取OKX fills计算PnL，填补compute_trade_ic数据闭环
    """
    if paper_path is None:
        paper_path = Path.home() / '.hermes' / 'cron' / 'output' / 'paper_trades.json'

    try:
        with open(paper_path) as f:
            raw = json.load(f)
    except:
        raw = []

    # Handle both formats: {"trades": [...]} and plain [...]
    if isinstance(raw, dict) and 'trades' in raw:
        paper = raw['trades']
    else:
        paper = raw if isinstance(raw, list) else []

    existing = {t['coin']: t for t in paper}
    added = []
    updated = []

    # Step 1: 检测已平仓（纸仓OPEN但真实已无持仓）→ 计算PnL
    for t in paper:
        coin = t['coin']
        if t.get('status') == 'OPEN' and coin not in real_pos:
            t['status'] = 'CLOSED'
            t['close_time'] = datetime.now().isoformat()
            if 'close_reason' not in t:
                t['close_reason'] = 'SL/TP触发'
            # v1.4: 实时拉取PnL
            pnl_val = get_pnl_from_fills(coin)
            if pnl_val is not None:
                t['pnl'] = pnl_val
                # 同时计算 result_pct（向后兼容）
                if t.get('entry_price') and pnl_val != 0:
                    contracts = t.get('contracts', 100)
                    lev = t.get('leverage', 3)
                    t['result_pct'] = round(pnl_val / (contracts * lev) * 100, 2)
            updated.append(coin + '(已平)')
        elif t.get('status') == 'OPEN' and coin in real_pos and real_pos[coin]['size'] <= 0:
            t['status'] = 'CLOSED'
            t['close_time'] = datetime.now().isoformat()
            if 'close_reason' not in t:
                t['close_reason'] = 'unknown_sync'
            # v1.4: 实时拉取PnL
            pnl_val = get_pnl_from_fills(coin)
            if pnl_val is not None:
                t['pnl'] = pnl_val
                if t.get('entry_price') and pnl_val != 0:
                    contracts = t.get('contracts', 100)
                    lev = t.get('leverage', 3)
                    t['result_pct'] = round(pnl_val / (contracts * lev) * 100, 2)
            updated.append(coin + '(已平)')

    # Step 2: 补录真实持仓但纸仓没有的（仅限Kronos系统开仓）
    # 重要：best_factor='recovered'的OKX同步持仓不是Kronos开的，不录入paper_trades
    for coin, pos in real_pos.items():
        if pos['size'] <= 0:
            continue  # 跳过已平仓的
        if coin not in existing:
            # 只录入有正确best_factor的仓位（排除从OKX同步的第三方持仓）
            if pos.get('best_factor') in (None, '', 'recovered'):
                continue  # 跳过OKX同步的第三方持仓，不污染paper_trades
            trade = {
                'coin': coin,
                'direction': 'LONG' if pos['side'] == 'buy' else 'SHORT',
                'entry_price': pos['entry'],
                'open_time': datetime.now().isoformat(),
                'status': 'OPEN',
                'contracts': int(pos['size']),
                'leverage': pos['leverage'],
                'ic': 0.0,
                'confidence': 50,
                'best_factor': pos.get('best_factor', 'unknown'),
                'strategy': 'Kronos开仓',
            }
            paper.append(trade)
            added.append(coin)
        else:
            # 已存在：确保状态是OPEN
            for t in paper:
                if t['coin'] == coin and t.get('status') != 'OPEN':
                    t['status'] = 'OPEN'
                    t['open_time'] = datetime.now().isoformat()  # 重置开仓时间
                    updated.append(coin + '(状态更新)')

    if added or updated:
        atomic_write_json(paper_path, paper, indent=2)
        logger.info('  纸仓同步: 补录%s | 更新%s' % (
            ', '.join(added) if added else '-',
            ', '.join(updated) if updated else '-'))

    return added + updated


def load_paper_log():
    path = Path.home() / '.hermes' / 'cron' / 'output' / 'paper_trades.json'
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []


def get_atr_stop(coin):
    """返回(止损%, 止盈%)，基于入场价+4×日线ATR
    
    重要：止损从ENTRY PRICE算，不是当前价格。
    这样止损是固定的，不会随价格漂移。
    """
    try:
        import ccxt
        c = ccxt.okx({'enableRateLimit': True})
        bars = c.fetch_ohlcv('%s-USDT' % coin, '1d', limit=14)
        if len(bars) < 14:
            return 0.05, 0.20
        import numpy as np
        highs = np.array([b[2] for b in bars])
        lows = np.array([b[3] for b in bars])
        closes = np.array([b[4] for b in bars])
        trs = np.maximum(highs[1:] - lows[1:], np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
        atr14 = np.mean(trs)
        # 用日线ATR（持仓周期匹配）
        sl_pct = atr14 / closes[-1] * 4.0  # 4×日ATR
        return sl_pct, sl_pct * 3.0  # 3:1 R/R
    except:
        return 0.05, 0.20


def get_atr_stop_from_entry(entry_price, coin):
    """
    从入场价计算止损/止盈价格
    使用与kronos_pilot._get_volatility_stop一致的sqrt缩放公式
    """
    try:
        import ccxt
        c = ccxt.okx({'enableRateLimit': True})
        bars = c.fetch_ohlcv('%s-USDT' % coin, '1d', limit=14)
        if len(bars) < 14:
            return None, None
        import numpy as np
        highs  = np.array([b[2] for b in bars])
        lows   = np.array([b[3] for b in bars])
        closes = np.array([b[4] for b in bars])
        trs = np.maximum(highs[1:] - lows[1:], np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
        atr14 = np.mean(trs)  # 日线ATR（美元）
        price = closes[-1]
        atr_pct = atr14 / price  # 日ATR百分比

        # sqrt时间缩放：√(72h / 24h) = √3 ≈ 1.732
        # 与kronos_pilot._get_volatility_stop保持一致
        sqrt_factor = (72.0 / 24.0) ** 0.5
        stop_pct = atr_pct * sqrt_factor  # ≈ 10.9% for AVAX
        stop_pct = max(stop_pct, 0.03)  # 最小3%

        sl_price = round(entry_price * (1 - stop_pct), 4)
        tp_price = round(entry_price * (1 + stop_pct * 3), 4)  # 3:1 赔率
        return sl_price, tp_price
    except:
        return None, None


# ============================================================
# 职业级账户健康检查
# ============================================================

MAX_POSITIONS = 3        # 最多同时持仓数
MIN_BALANCE = 10         # 可用保证金低于这个值，停止开新仓（资金紧张提醒）
MARGIN_WARNING = 80      # 保证金使用率 >80% 警告
MARGIN_CRITICAL = 90   # 保证金使用率 >90% 强警告
CONCENTRATION_WARN = 50  # 单币种占总仓位 >50% 警告
DEEP_LOSS_PCT = 10      # 单笔浮亏超过总权益10% → 🔴强警告
MAX_HOLD_HOURS = 72      # 最大持仓小时数，超时强制检查（不自动平）


def check_account_health(real_pos, balance_info=None):
    """
    职业级账户健康检查
    - 总权益、可用保证金、已用保证金
    - 保证金使用率
    - 持仓数检查（最多3个）
    - 单币种集中度
    - 新开仓空间
    返回: (是否健康, 警告信息列表)
    """
    if balance_info is None:
        balance_info = get_account_balance()
    
    warnings = []
    is_healthy = True
    
    total_eq = balance_info.get('totalEq', 0)
    avail_eq = balance_info.get('availEq', 0)
    frozen = balance_info.get('frozenBal', 0)
    upl = balance_info.get('upl', 0)
    
    # 1. 基础余额
    logger.info('  账户: 总权益=$%.2f 可用=$%.2f 保证金=$%.2f 浮盈=$%+.2f' % (
        total_eq, avail_eq, frozen, upl))
    
    # 2. 保证金使用率
    if total_eq > 0:
        margin_ratio = (frozen / total_eq) * 100
    else:
        margin_ratio = 0
    
    if margin_ratio > MARGIN_CRITICAL:
        warnings.append('🚨 保证金使用率%.0f%%（危险！）' % margin_ratio)
        is_healthy = False
    elif margin_ratio > MARGIN_WARNING:
        warnings.append('⚠️ 保证金使用率%.0f%%（偏高）' % margin_ratio)
        is_healthy = False
    else:
        logger.info('  保证金使用率: %.0f%% ✅' % margin_ratio)
    
    # 3. 持仓数
    pos_count = len([c for c, p in real_pos.items() if p['size'] > 0])
    if pos_count >= MAX_POSITIONS:
        warnings.append('🚨 已达最大持仓数%d/%d，暂停新开仓' % (pos_count, MAX_POSITIONS))
        is_healthy = False
    else:
        logger.info('  持仓数: %d/%d ✅' % (pos_count, MAX_POSITIONS))
    
    # 4. 新开仓空间
    space = MAX_POSITIONS - pos_count
    logger.info('  新开仓空间: 最多%d个 | 可用保证金$%.2f' % (space, avail_eq))
    
    # 5. 每笔仓位的保证金占比（集中度）
    logger.info('  仓位明细:')
    for coin, pos in real_pos.items():
        if pos['size'] <= 0:
            continue
        # 估算保证金 = 仓位价值 / 杠杆
        pos_value = pos['size'] * pos['entry']
        margin_used = pos_value / pos['leverage']
        pct_of_eq = (margin_used / total_eq * 100) if total_eq > 0 else 0
        
        # 标记
        flag = ''
        if pct_of_eq > CONCENTRATION_WARN:
            flag = ' ⚠️集中'
            warnings.append('⚠️ %s保证金占总权益%.0f%%（单币过重）' % (coin, pct_of_eq))
        
        # 深亏检测：浮亏占总权益 >10% 才告警
        if total_eq > 0:
            loss_pct = abs(pos['unrealized_pnl']) / total_eq * 100 if pos['unrealized_pnl'] < 0 else 0
            if loss_pct > DEEP_LOSS_PCT:
                flag += ' 🔴深亏%.0f%%' % loss_pct
                warnings.append('🔴 %s浮亏$%+.2f(占总权益%.0f%%)' % (coin, pos['unrealized_pnl'], loss_pct))
        
        logger.info('  - %s: %s %s张 保证金$%.2f(%.0f%%) 浮盈$%+.2f%s' % (
            coin, pos['side'], pos['size'],
            margin_used, pct_of_eq,
            pos['unrealized_pnl'], flag))
    
    # 6. 可否开新仓（仅打印，不告警）
    new_possible = True
    if avail_eq < MIN_BALANCE:
        new_possible = False
        logger.info('  💡 可用保证金$%.2f（<$%d），当前无法开新仓' % (avail_eq, MIN_BALANCE))
    elif space <= 0:
        new_possible = False
        logger.info('  💡 已达持仓上限%d个' % MAX_POSITIONS)
    else:
        logger.info('  ✅ 可开新仓: %d个 | 可用$%.2f' % (space, avail_eq))
    
    return is_healthy, warnings


def get_account_balance():
    """获取账户余额和保证金信息

    P0 Fix: 遍历details找到ccy==USDT的记录，不假设details[0]是USDT
    """
    try:
        result = _req('GET', '/api/v5/account/balance?ccy=USDT')
        if result.get('code') != '0':
            return {'totalEq': 0, 'availEq': 0, 'frozenBal': 0, 'upl': 0}

        # P0 Fix: 遍历details找USDT，不假设第一个是USDT
        details = None
        for d in result.get('data', [{}])[0].get('details', []):
            if d.get('ccy') == 'USDT':
                details = d
                break
        if details is None:
            return {'totalEq': 0, 'availEq': 0, 'frozenBal': 0, 'upl': 0}

        return {
            'totalEq': float(details.get('eq', 0)),
            'availEq': float(details.get('availEq', 0)),
            'frozenBal': float(details.get('frozenBal', 0)),
            'upl': float(details.get('upl', 0)),
            'uplRatio': details.get('uplRatio', ''),
        }
    except:
        return {'totalEq': 0, 'availEq': 0, 'frozenBal': 0, 'upl': 0}


def check_hold_timeout(real_pos):
    """
    检查持仓是否超时
    从纸仓获取开仓时间，估算已持仓时长
    注意：OKX不直接提供持仓开仓时间，用纸仓记录作为近似
    """
    from datetime import datetime
    import zoneinfo

    paper = load_paper_log()
    open_trades = {t['coin']: t for t in paper if t.get('status') == 'OPEN'}
    now_cst = datetime.now(zoneinfo.ZoneInfo('Asia/Shanghai'))
    warnings = []

    for coin, pos in real_pos.items():
        if pos['size'] <= 0:
            continue
        if coin not in open_trades:
            # 没有记录，尝试从OKX持仓详情估算
            logger.info('  💡 %s: 无开仓记录，跳过超时检查' % coin)
            continue

        open_time_str = open_trades[coin].get('open_time', '')
        if not open_time_str:
            continue

        try:
            # 解析开仓时间，尝试多种格式
            open_time_str = open_time_str.replace('Z', '+00:00')
            if '+' not in open_time_str and '.' not in open_time_str.split('T')[1]:
                open_time = datetime.fromisoformat(open_time_str).replace(
                    tzinfo=zoneinfo.ZoneInfo('Asia/Shanghai'))
            else:
                open_time = datetime.fromisoformat(open_time_str)
                if open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=zoneinfo.ZoneInfo('Asia/Shanghai'))
            hours_elapsed = (now_cst - open_time).total_seconds() / 3600
        except Exception as e:
            logger.warning('  ⚠️ %s: 无法解析开仓时间 %s (%s)' % (coin, open_time_str, e))
            continue

        pct = hours_elapsed / MAX_HOLD_HOURS * 100
        flag = '🔴' if hours_elapsed >= MAX_HOLD_HOURS else '🟡'

        logger.info('  %s %s: 已持仓%.1f小时 / %d小时 (%.0f%%)' % (
            flag, coin, hours_elapsed, MAX_HOLD_HOURS, pct))

        if hours_elapsed >= MAX_HOLD_HOURS:
            warnings.append(
                '⏰ %s 持仓超时！已%.1f小时（>%dh）需要人工判断是否平仓' % (
                    coin, hours_elapsed, MAX_HOLD_HOURS))
        elif hours_elapsed >= MAX_HOLD_HOURS * 0.8:
            warnings.append(
                '🟡 %s 持仓接近超时：%.1f小时/%d小时' % (
                    coin, hours_elapsed, MAX_HOLD_HOURS))

    return warnings


def monitor_and_fix():
    """主监控函数：查询真实持仓，补录纸仓，修复SL/TP，生存层级检查"""
    logger.info('[%s] 真实持仓监控' % datetime.now().strftime('%H:%M:%S'))

    # 0. 生存层级检查
    balance_info = get_account_balance()
    equity = balance_info.get('totalEq', 0)
    # P1 Fix: totalEq=0通常是API失败，不是真实equity。用treasury历史快照作fallback
    if equity <= 0:
        prev_state = load_treasury_state()
        equity = prev_state.get('equity') or prev_state.get('daily_snapshot_equity') or 0
        if equity > 0:
            logger.warning('  ⚠️  API equity=0，使用 treasury 快照 $%.2f' % equity)
    tier, tier_state = get_survival_tier(equity)
    multiplier = get_position_multiplier(tier)
    can_open, open_reason = can_open_new_position(tier)

    logger.info('  ├─ 权益: $%.2f | 层级: %s | 仓位倍数: ×%.2f' % (equity, tier.upper(), multiplier))
    logger.info('  └─ 开仓权限: %s' % open_reason)

    # 0c. 排除币种检查：发现排除名单上的币有持仓 → 立即强制平仓
    from kronos_multi_coin import get_coin_strategy_map
    smap = get_coin_strategy_map()
    excluded = {c['symbol'] for c in smap.get('coins', []) if c.get('excluded')}
    if excluded:
        real_pos_temp, _ = get_real_positions()
        if real_pos_temp:
            for coin in list(real_pos_temp.keys()):
                for ex in excluded:
                    if ex in coin:  # DOGE,LINK等
                        pos = real_pos_temp[coin]
                        size = pos.get('size', 0)
                        if size > 0:
                            side = pos.get('side', 'buy')
                            logger.warning('  🚨 排除币种 %s 有持仓 %s张 → 强制平仓' % (coin, size))
                            from kronos_active_judgment import close_position as close_pos_action
                            close_result = close_pos_action(coin, side, size, reason='排除币种强制平仓')
                            logger.info('     平仓结果: %s' % close_result)
                            push_feishu('🚨 Kronos排除币种强制平仓\n%s %s张 平仓结果: %s' % (coin, size, close_result))

    # 0b. 财务政策检查（更新快照 + 检查限制）
    update_treasury_snapshots(equity)
    treasury_allowed, treasury_reason, treasury_warnings = check_treasury_limits(equity)
    if not treasury_allowed:
        logger.warning('  ⚠️  财务限制: %s' % treasury_reason.replace('\n', ' '))
        push_feishu('🚫 Kronos财务限制\n' + treasury_reason)

    # 层级降级时推送告警
    prev_tier = tier_state.get('tier')
    if tier_state.get('tier_change_log'):
        last_change = tier_state['tier_change_log'][-1]
        # 只在本次调用发生了降级时推送（last_change的to就是当前tier）
        if last_change.get('reason') == 'equity_drop' and last_change.get('to') == tier:
            push_feishu(
                '⏰ Kronos层级变化\n'
                '%s → %s\n'
                '权益: $%.2f (%.0f%%)\n'
                '原因: %s' % (
                    last_change['from'].upper(),
                    last_change['to'].upper(),
                    equity,
                    equity / START_BALANCE * 100,
                    '权益跌破阈值'
                )
            )

    # 1. 查真实持仓（含已平仓，用于纸仓同步）
    real_pos, err = get_real_positions()
    real_pos_all, err2 = get_real_positions(include_closed=True)  # 含已平仓

    if err:
        logger.error('  ❌ ' + err)
        return ['❌ 无法连接OKX: ' + err]

    if err2:
        logger.error('  ❌ ' + err2)
        return ['❌ 无法连接OKX(含已平仓): ' + err2]

    if not real_pos:
        logger.info('  ✅ OKX无持仓')
        # 无持仓时也要输出层级状态
        logger.info('  %s' % format_tier_report(tier_state, equity))
        return None

    # 2. 同步纸仓（用含已平仓的完整数据，检测持仓消失）
    sync_paper_log(real_pos_all)

    # 3. 账户健康检查（职业级）
    is_healthy, warnings = check_account_health(real_pos, balance_info)

    # 4. 查活跃SL/TP条件单
    real_orders, _ = get_real_sl_tp_orders()

    logger.info('  真实持仓:')
    alerts = []
    for coin, pos in real_pos.items():
        size = pos['size']
        if size <= 0:
            continue
        side = pos['side']  # buy/sell
        entry = pos['entry']
        pnl = pos['unrealized_pnl']
        lev = pos['leverage']
        has_sl_tp = coin in real_orders and (('sl' in real_orders[coin]) or ('tp' in real_orders[coin]))

        logger.info('  - %s: %s %s张 均价$%.4f 浮盈$%+.2f 杠杆%dx %s' % (
            coin, side, size, entry, pnl, lev,
            '✅ SL/TP已挂' if has_sl_tp else '⚠️ 无SL/TP'))

        if not has_sl_tp:
            alerts.append('⛔ %s 缺SL/TP' % coin)
            # 用入场价算止损/止盈
            sl_price_calc, tp_price_calc = get_atr_stop_from_entry(entry, coin)
            if sl_price_calc is None:
                sl_pct, tp_pct = 0.05, 0.20
            else:
                sl_pct, tp_pct = sl_price_calc, tp_price_calc
            results = place_sl_tp(coin, side, size, entry, sl_pct, tp_pct)
            for name, status in results.items():
                logger.info('    %s: %s' % (name, status))
            placed = [k for k, v in results.items() if v.startswith('✅')]
            if len(placed) == 2:
                alerts.append('✅ %s SL/TP已挂: 止损$%s 止盈$%s' % (coin, sl_price_calc, tp_price_calc))
            else:
                failed = [k for k, v in results.items() if v.startswith('❌')]
                alerts.append('❌ %s 挂单失败: %s' % (coin, ', '.join(failed)))
        else:
            # 有SL/TP但检查TP是否缺失
            has_tp = 'tp' in real_orders.get(coin, {})
            if not has_tp:
                alerts.append('⚠️ %s 有SL但无TP，立即补挂' % coin)
                sl_price_calc, tp_price_calc = get_atr_stop_from_entry(entry, coin)
                if tp_price_calc:
                    results = place_sl_tp(coin, side, size, entry, sl_price_calc, tp_price_calc)
                    for name, status in results.items():
                        if '止盈' in name or 'TP' in name:
                            logger.info('    %s: %s' % (name, status))
                            placed_tp = [k for k, v in results.items() if v.startswith('✅')]
                            if placed_tp:
                                alerts.append('✅ %s TP补挂成功: $%s' % (coin, tp_price_calc))


    # 5. 汇总：健康检查警告 + SL/TP问题
    all_alerts = warnings + alerts

    # 6. 持仓超时检查
    hold_warnings = check_hold_timeout(real_pos)
    if hold_warnings:
        all_alerts.extend(hold_warnings)

    # 7. 层级限制说明（不推飞书，正常静默）
    if tier in ('critical', 'paused'):
        logger.info('  %s' % format_tier_report(tier_state, equity).replace('\n', '\n  '))

    # v1.4: 飞书告警去重 - 相同内容30分钟内不重复推送
    ALERT_COOLDOWN_MINUTES = 30
    alert_state_file = Path.home() / '.hermes/cron/output/real_monitor_alert_state.json'

    if all_alerts:
        # 过滤：3/3正常状态不推飞书（内部正常静默）
        feishu_alerts = [a for a in all_alerts if '已达最大持仓数' not in a]
        # v1.4: 已知正常状态不重复推 - 单币过重是已知状态，改为静默不推送飞书
        # 原因：OKX模拟盘正常也会触发此告警，不需要重复推送
        persistent_warnings = [a for a in feishu_alerts if '保证金占总权益' in a]
        feishu_alerts = [a for a in feishu_alerts if '保证金占总权益' not in a]

        # 已在控制台打印，静默飞书推送
        if persistent_warnings:
            import re
            coin_list = []
            for w in persistent_warnings:
                key = re.sub(r'[\d.]+%?', '', w).strip().replace('⚠️ ', '')
                coin_list.append(key)
            coins_str = ', '.join(coin_list)
            logger.info(f'  飞书静默(已知状态): {coins_str}')
            persistent_warnings = []

        if feishu_alerts:
            msg = 'Kronos账户告警:\n' + '\n'.join(feishu_alerts)
            logger.info('  飞书推送: %s' % msg[:300])
            push_feishu(msg)

    return all_alerts if all_alerts else None


if __name__ == '__main__':
    # 显示系统启动信息（实时查权益）
    bal = get_account_balance()
    equity = bal.get('totalEq', 0)
    # P1 Fix: totalEq=0通常是API失败，不是真实equity。用treasury历史快照作fallback
    if equity <= 0:
        prev_state = load_treasury_state()
        equity = prev_state.get('equity') or prev_state.get('daily_snapshot_equity') or 0
        if equity > 0:
            logger.warning('  ⚠️  API equity=0，使用 treasury 快照 $%.2f' % equity)

    # 先更新财务快照（确保报告有数据）
    update_treasury_snapshots(equity)

    # 生存层级
    tier, state = get_survival_tier(equity)
    logger.info('━━━ Kronos生存层级系统 ━━━')
    logger.info(format_tier_report(state, equity))
    logger.info('')
    logger.info('━━━ Kronos财务政策 ━━━')
    logger.info(format_treasury_report(equity))
    logger.info('')

    result = monitor_and_fix()
    if result:
        logger.info('\n需要关注: %s' % result)
    else:
        logger.info('\n✅ 系统正常')
