#!/usr/bin/env python3
"""
Kronos 自动守护系统
===============
每3分钟运行，检测危险条件：
1. 熔断器触发 → 立即通知+阻断开仓
2. SL极度危险（<2%）→ 自动触发 judgment + AI审查
3. 强平距离过近（<3%）→ 自动触发 judgment + AI审查
4. 持仓超时（>90%时间）→ 自动触发 judgment + AI审查

有实际操作才发飞书，其余静默。
"""
import sys
import os
import json
import time
import signal
import subprocess
import requests
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger('kronos_auto_guard')

# 加载.env
from dotenv import load_dotenv
load_dotenv(Path.home() / '.hermes' / '.env', override=True)

sys.path.insert(0, str(Path(__file__).parent))

from real_monitor import (
    get_real_positions, get_real_sl_tp_orders, get_account_balance,
    _req, get_position_multiplier
)
from kronos_heartbeat import (
    load_circuit_state, check_circuit_breaker, sync_circuit_from_positions
)
from kronos_multi_coin import SL_DANGER_PCT  # P1统一SL危险阈值(0.5%)

# ============ 配置 ============
FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
FEISHU_CHAT_ID = os.getenv('FEISHU_CHAT_ID', 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0')
MINIMAX_API_KEY = os.getenv('MINIMAX_API_KEY', '')
MINIMAX_BASE_URL = os.getenv('MINIMAX_BASE_URL', 'https://api.minimaxi.com/v1')

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.kronos_dispatch.lock')
LOCK_TIMEOUT = 30

# 危险阈值(从kronos_multi_coin导入统一值SL_DANGER_PCT=0.5%)
# 注意：kronos_auto_guard使用0.5%作为SL极度危险阈值（原值2.0%已废弃）
SL_WARN_PCT = 4.0      # SL距现价<4%为警告
LIQ_DANGER_PCT = 3.0   # 强平距离<3%为危险
TIMEOUT_WARN_PCT = 80   # 持仓时间>80%为警告
TIMEOUT_DANGER_PCT = 90  # 持仓时间>90%为极度危险（通知MiniMax审查）

_f_token = None
_f_expire = 0

# ============ 飞书通知 ============
def get_feishu_token():
    global _f_token, _f_expire
    if _f_token and time.time() < _f_expire:
        return _f_token
    try:
        resp = requests.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET},
            timeout=10
        )
        data = resp.json()
        if data.get('code') == 0:
            _f_token = data['tenant_access_token']
            _f_expire = time.time() + data.get('expire', 3600) - 60
            return _f_token
    except:
        pass
    return None

def feishu_notify(text):
    """发送飞书消息 — 通过notification_manager（去重+冷却+模式感知）

    在模拟盘模式下：🚨告警始终发送，其他静默
    """
    try:
        from notification_manager import send_feishu
        # auto-classify: 🚨/🚫 → critical, 其他 → info
        if text.startswith(('🚨', '🚫')):
            category = 'critical'
        else:
            category = 'info'
        send_feishu(text, category)
    except Exception as e:
        logger.error(f"飞书通知失败: {e}")

# ============ 价格获取 ============
def get_price(coin):
    try:
        r = _req('GET', f'/api/v5/market/ticker?instId={coin}-USDT-SWAP')
        if r.get('code') == '0' and r.get('data'):
            return float(r['data'][0]['last'])
    except:
        pass
    return None

# ============ 撤销指定币种的pending条件单 ============
def cancel_orders_for_coin(coin, sl_tp_orders=None):
    """平仓前撤销该币的所有pending SL/TP/OCO条件单（用algoId取消OCO/conditional）"""
    if sl_tp_orders is None:
        sl_tp_orders, _ = get_real_sl_tp_orders()

    cancelled = []
    if coin not in sl_tp_orders:
        return cancelled

    # 取消OCO/conditional订单用cancel-algos（需要algoId）
    algo_ids = []
    for order_type in ['sl', 'tp', 'oco']:
        if order_type in sl_tp_orders[coin]:
            oid = sl_tp_orders[coin][order_type].get('algoId') or sl_tp_orders[coin][order_type].get('orderId')
            if oid:
                algo_ids.append(str(oid))

    if algo_ids:
        body = json.dumps([{'algoId': str(aid), 'instId': f'{coin}-USDT-SWAP'} for aid in algo_ids])
        r = _req('POST', '/api/v5/trade/cancel-algos', body)
        if r.get('code') == '0':
            cancelled.append(f'algoIds={algo_ids}')
            logger.info(f'  ✅ 撤销{coin} OCO/条件单: {algo_ids}')
        else:
            logger.error(f'  ❌ 撤销{coin} OCO失败: {r.get("msg")}')
            # 回退到逐个cancel-order
            for aid in algo_ids:
                body2 = json.dumps({'instId': f'{coin}-USDT-SWAP', 'ordId': str(aid)})
                r2 = _req('POST', '/api/v5/trade/cancel-order', body2)
                if r2.get('code') == '0':
                    cancelled.append(f'ordId={aid}')
    return cancelled
def analyze_positions():
    """分析所有持仓，返回(positions_dict, 危险项列表)"""
    positions, err = get_real_positions()
    if err or not positions:
        return {}, []

    sl_tp_orders, _ = get_real_sl_tp_orders()
    dangers = []

    for coin, pos in positions.items():
        side = pos['side']  # 'buy'=long, 'sell'=short
        size = pos['size']
        entry = pos['entry']
        instId = pos.get('instId', f'{coin}-USDT-SWAP')

        current = get_price(coin)
        if not current:
            continue

        # 计算盈亏
        if side == 'buy':
            profit_pct = (current - entry) / entry
            liq_price = pos.get('liqPx')
            liq_distance = ((current - liq_price) / current * 100) if liq_price else None
        else:
            profit_pct = (entry - current) / entry
            liq_price = pos.get('liqPx')
            liq_distance = ((liq_price - current) / current * 100) if liq_price else None

        # SL距离（始终用绝对值，方向由持仓方向决定）
        current_sl = None
        current_tp = None
        if coin in sl_tp_orders:
            if 'sl' in sl_tp_orders[coin]:
                current_sl = float(sl_tp_orders[coin]['sl']['price'])
            if 'tp' in sl_tp_orders[coin]:
                current_tp = float(sl_tp_orders[coin]['tp']['price'])
        sl_distance = None
        if current_sl:
            # 使用绝对值百分比距离
            sl_distance = abs(current - current_sl) / current * 100
            # 额外检查：做多时SL应低于现价，做空时SL应高于现价
            # 如果方向错误（做多但SL>现价），标记为配置错误危险
            if side == 'buy' and current_sl > current:
                # SL高于现价 = 反弹会触发止损 = 配置错误
                sl_distance = -sl_distance  # 负数表示配置错误
            elif side == 'sell' and current_sl < current:
                sl_distance = -sl_distance

        # 持仓时间
        ctime = pos.get('cTime', '')
        if ctime:
            try:
                open_ts = int(ctime) / 1000
                hours_elapsed = (time.time() - open_ts) / 3600
                timeout_pct = hours_elapsed / 72 * 100  # 假设最大72h
            except:
                timeout_pct = 0
        else:
            timeout_pct = 0

        # 检查危险项
        reason = []
        severity = 'warn'

        # P0: SL完全缺失 → 立即补SL
        if current_sl is None:
            reason.append('❌无SL保护（最高危险）')
            severity = 'danger'

        elif sl_distance is not None and sl_distance < SL_DANGER_PCT:
            reason.append(f'SL距现价仅{sl_distance:.1f}%')
            severity = 'danger'

        if liq_distance is not None and liq_distance < LIQ_DANGER_PCT:
            reason.append(f'距强平仅{liq_distance:.1f}%')
            severity = 'danger'

        if timeout_pct > TIMEOUT_DANGER_PCT:
            reason.append(f'持仓超时{int(timeout_pct)}%')
            severity = 'danger'
        elif timeout_pct > TIMEOUT_WARN_PCT:
            reason.append(f'持仓接近超时{int(timeout_pct)}%')
            severity = 'warn'

        if severity == 'danger':
            dangers.append({
                'coin': coin,
                'side': side,
                'size': size,
                'entry': entry,
                'current': current,
                'profit_pct': profit_pct,
                'current_sl': current_sl,
                'current_tp': current_tp,
                'sl_distance': sl_distance,
                'liq_distance': liq_distance,
                'timeout_pct': timeout_pct,
                'reason': ' | '.join(reason),
                'severity': severity,
            })
        elif severity == 'warn':
            dangers.append({
                'coin': coin,
                'side': side,
                'size': size,
                'entry': entry,
                'current': current,
                'profit_pct': profit_pct,
                'current_sl': current_sl,
                'current_tp': current_tp,
                'sl_distance': sl_distance,
                'liq_distance': liq_distance,
                'timeout_pct': timeout_pct,
                'reason': ' | '.join(reason),
                'severity': severity,
            })

    return positions, dangers

# ============ AI快速判断（本地规则，无API依赖） ============
def quick_ai_judgment(dangers, circuit_tripped):
    """快速AI判断 - 规则引擎，不依赖外部API"""
    actions = []

    for d in dangers:
        coin = d['coin']
        profit_pct = d['profit_pct']
        sl_distance = d['sl_distance']
        liq_distance = d['liq_distance']
        severity = d['severity']

        if circuit_tripped:
            # 熔断时只允许平仓，不允许开新仓
            actions.append({
                'type': 'force_close',
                'coin': coin,
                'reason': '熔断期间强制平仓',
                'priority': 'high'
            })
            continue

        # 注：SL/TP修补由kronos_multi_coin.py统一管理，每3分钟扫描。
        # 本函数只处理紧急情况：熔断平仓、极度危险强平。
        # 常规SL/TP缺失请查看kronos_multi_coin.py的decide_for_position。

        # 极度危险：SL<2% 或 强平<3%
        if severity == 'danger':
            if profit_pct < -0.03:
                # 亏损中 + 极度危险 → 止损
                actions.append({
                    'type': 'force_close',
                    'coin': coin,
                    'reason': f'浮亏{profit_pct*100:.1f}%+极度危险',
                    'priority': 'high'
                })
            else:
                # 盈利中但危险 → 收紧SL
                if sl_distance and sl_distance < 2.0:
                    # 尝试收紧SL到距现价2.5%
                    new_sl = d['current'] * 0.975 if d['side'] == 'buy' else d['current'] * 1.025
                    actions.append({
                        'type': 'tighten_sl',
                        'coin': coin,
                        'new_sl': new_sl,
                        'reason': f'SL收紧防触及',
                        'priority': 'medium'
                    })

    return actions

# ============ MiniMax AI 深度审查 ============
def minimax_review(positions_data, dangers, circuit_state):
    """调用MiniMax M2.7进行深度审查"""
    try:
        # 构建持仓摘要
        pos_summary = ""
        for d in positions_data:
            pos_summary += f"\n{d['coin']}: {d['side']} {d['size']}张 @{d['entry']:.4f} | 现价${d['current']:.4f} | 盈亏{d['profit_pct']*100:.1f}%"
            if d.get('sl_distance'):
                pos_summary += f" | SL距{d['sl_distance']:.1f}%"
            if d.get('liq_distance'):
                pos_summary += f" | 强平距{d['liq_distance']:.1f}%"

        danger_summary = ""
        for dd in dangers:
            danger_summary += f"\n🚨 {dd['coin']}: {dd['reason']}"

        circuit_info = ""
        if circuit_state.get('is_tripped'):
            circuit_info = f"\n⚠️ 熔断已触发！连续亏损{circuit_state['consecutive_losses']}次，禁止开新仓"

        prompt = f"""你是Kronos量化交易系统的风控AI。
【当前危险持仓】
{danger_summary if danger_summary else '无危险持仓'}

【所有持仓】
{pos_summary if pos_summary else '无持仓'}

【熔断状态】
{circuit_info if circuit_info else '熔断器正常'}

【紧急任务】
检测到危险条件，需要你立即做出风控决策：
1. 哪些持仓必须立即平仓？
2. 哪些需要收紧止损？
3. 是否有反向操作机会？

输出格式（严格遵守）：
```
风控决策:
- [币种] [操作] [原因]

执行优先级:
- 高: [说明]
- 中: [说明]
- 低: [说明]
```
"""
        import requests
        headers = {
            'Authorization': f'Bearer {MINIMAX_API_KEY}',
            'Content-Type': 'application/json',
        }
        data = {
            'model': 'MiniMax-M2.7',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.3,
            'max_tokens': 1500,
        }
        resp = requests.post(
            f'{MINIMAX_BASE_URL}/text/chatcompletion_v2',
            headers=headers, json=data, timeout=60
        )
        result = resp.json()
        if 'choices' in result:
            return result['choices'][0]['message']['content']
        else:
            err_msg = result.get('base_resp', {}).get('status_msg', str(result))
            return f"MiniMax API错误: {err_msg}"
    except Exception as e:
        return f"MiniMax调用失败: {str(e)}"

# ============ 执行动作 ============
def execute_actions(actions, positions):
    """执行判断动作"""
    results = []
    for action in actions:
        coin = action['coin']
        pos = positions.get(coin)
        if not pos:
            continue

        size = pos['size']
        side = pos['side']
        instId = f'{coin}-USDT-SWAP'

        if action['type'] == 'force_close':
            # 模拟盘格式：close long = buy+posSide=long；close short = sell+posSide=short
            if side == 'buy':
                close_side = 'buy'
                pos_side = 'long'
            else:
                close_side = 'sell'
                pos_side = 'short'
            body = {
                'instId': instId,
                'tdMode': 'isolated',
                'side': close_side,
                'ordType': 'market',
                'sz': str(int(size)),
                'posSide': pos_side,
                'reduceOnly': True,
            }
            result = _req('POST', '/api/v5/trade/order', json.dumps(body))
            if result.get('code') == '0':
                results.append(f"✅ {coin} 市价平仓成功")
            else:
                results.append(f"❌ {coin} 平仓失败: {result.get('msg')}")

        elif action['type'] == 'tighten_sl':
            new_sl = action['new_sl']
            # 取消当前SL
            sl_orders, _ = get_real_sl_tp_orders()
            if coin in sl_orders and 'sl' in sl_orders[coin]:
                algo_id = sl_orders[coin]['sl']['algoId']
                from real_monitor import cancel_algo_orders
                cancel_algo_orders(instId, [algo_id])

            # 挂新SL（全量）
            body = [{
                'instId': instId,
                'tdMode': 'isolated',
                'side': 'sell' if pos['side'] == 'buy' else 'buy',
                'ordType': 'conditional',
                'sz': str(int(size)),  # 全量止损，不是1张
                'slTriggerPx': str(new_sl),
                'slTriggerCond': 'last',
            }]
            result = _req('POST', '/api/v5/trade/order-algo', json.dumps(body))
            if result.get('code') == '0':
                results.append(f"✅ {coin} SL收紧到${new_sl:.4f}")
            else:
                results.append(f"❌ {coin} SL收紧失败: {result.get('msg')}")

        elif action['type'] == 'place_sl':
            # P0: 补缺失的SL
            new_sl = action['new_sl']
            sz = action.get('sz', size)
            body = [{
                'instId': instId,
                'tdMode': 'isolated',
                'side': 'sell' if pos['side'] == 'buy' else 'buy',
                'ordType': 'conditional',
                'sz': str(int(sz)),
                'slTriggerPx': str(new_sl),
                'slTriggerCond': 'last',
            }]
            result = _req('POST', '/api/v5/trade/order-algo', json.dumps(body))
            if result.get('code') == '0':
                results.append(f"✅ {coin} 补SL@{new_sl:.4f}({sz}张)")
            else:
                results.append(f"❌ {coin} 补SL失败: {result.get('msg')}")

        elif action['type'] == 'place_tp':
            # P0: 补缺失的TP
            new_tp = action.get('new_tp')
            sz = action.get('sz', size)
            body = [{
                'instId': instId,
                'tdMode': 'isolated',
                'side': 'sell' if pos['side'] == 'buy' else 'buy',
                'ordType': 'conditional',
                'sz': str(int(sz)),  # 全量止盈
                'tpTriggerPx': str(round(new_tp, 4)),
                'tpTriggerCond': 'last',
            }]
            result = _req('POST', '/api/v5/trade/order-algo', json.dumps(body))
            if result.get('code') == '0':
                results.append(f"✅ {coin} 补TP@{new_tp:.4f}({sz}张)")
            else:
                results.append(f"❌ {coin} 补TP失败: {result.get('msg')}")

    return results

# ============ 主守护逻辑 ============
def guard_cycle():
    """
    Kronos全自动守护 - 每次运行完整决策+执行

    不再只是发飞书预警，而是：
    1. 同步真实持仓盈亏到熔断器（修复幽灵仓位假阳性）
    2. 调用 kronos_multi_coin.full_scan() 做 gemma4 中央决策
    3. 自动执行（收紧SL/止盈/平仓/开仓）
    4. 有实际操作才飞书通知，否则静默
    """
    logger.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] === Kronos全自动守护 ===")

    # ── Step 0: 同步真实持仓盈亏到熔断器 ──────────────────────────────
    sync_circuit_from_positions()

    # ── Step 1: 检查熔断状态 ─────────────────────────────────────────
    circuit_tripped, circuit_reason, circuit_state = check_circuit_breaker()
    if circuit_tripped:
        logger.warning(f"🚨 熔断触发: {circuit_reason}")
        feishu_notify(f"🚨 Kronos熔断触发 | {circuit_reason} | 禁止开新仓")

    # ── Step 2: 全自动决策+执行（kronos_multi_coin gemma4中央决策）────
    # 延迟导入避免循环依赖
    from kronos_multi_coin import full_scan as _full_scan
    try:
        result = _full_scan(notify=True)
        executed = True
        logger.info("✅ full_scan执行完毕")
    except Exception as e:
        logger.error(f"❌ full_scan异常: {e}")
        import traceback
        traceback.print_exc()
        feishu_notify(f"🚨 Kronos全自动守护异常: {e}")
        executed = False
        result = None

    return {
        'circuit_tripped': circuit_tripped,
        'autonomous_scan': result,
        'executed': executed,
    }

# ============ 主入口 ============
if __name__ == '__main__':
    from filelock import FileLock

    # 全局超时：100秒后强制退出，防止LLM调用超时导致cron任务堆积
    # full_scan正常约97秒，冷启动可能更长
    _CRON_TIMEOUT = 100
    def _timeout_handler(signum, frame):
        logger.error(f"⏰ 全局超时({_CRON_TIMEOUT}s)触发，强制退出")
        raise SystemExit(1)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(_CRON_TIMEOUT)

    COOLDOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cooldown.json')
    COOLDOWN_SEC = 900  # 15分钟cooldown
    
    # Cooldown检查
    # 注意: 必须用os._exit(0)而不是exit(0)，因为bare except:会catch SystemExit
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE) as f:
                cd = json.load(f)
            last_action = cd.get('last_emergency_action_ts', 0)
            if time.time() - last_action < COOLDOWN_SEC:
                msg = f'⏭️ Cooldown生效（{COOLDOWN_SEC//60}分钟），距上次紧急操作{time.time()-last_action:.0f}秒，跳过'
                logger.info(msg)
                print(msg)  # visible in cron output
                logging.shutdown()
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(0)
    except:
        pass

    lock = FileLock(LOCK_FILE, timeout=LOCK_TIMEOUT)
    try:
        lock.acquire()
        result = guard_cycle()
        logger.info(f"\n结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
        if result and result.get('executed'):
            try:
                with open(COOLDOWN_FILE, 'w') as f:
                    json.dump({'last_emergency_action_ts': time.time(), 'reason': 'guard_emergency'}, f)
                logger.warning(f'⚠️ 紧急操作已执行，写入{COOLDOWN_SEC//60}分钟cooldown')
            except:
                pass
    except Exception as e:
        logger.error(f"执行异常: {e}")
        import traceback
        traceback.print_exc()
        feishu_notify(f"🚨 Kronos自动守护异常: {e}")
    finally:
        try:
            lock.release()
        except:
            pass
        signal.alarm(0)  # 取消全局alarm
