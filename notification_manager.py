#!/usr/bin/env python3
"""
notification_manager.py — 中央通知管理与噪声过滤
=================================================

统一管理所有飞书通知的去重、冷却和模式感知。
避免"连接正常"每5分钟推送、无变化时报持仓等噪音问题。

关键功能:
  - should_notify(key, cooldown): 带冷却时间的去重检查
  - send_feishu(message, category): 统一发送接口，自动过滤
  - check_connectivity_change(): 连接状态变化检测
  - check_position_change(): 持仓变化检测

Version: 1.0.0
"""

import os
import json
import time
import hashlib
from pathlib import Path

# ─── 状态文件 ───────────────────────────────────────────────
STATE_DIR = Path.home() / "kronos" / "data"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DEDUP_FILE = STATE_DIR / "notification_dedup.json"
CONNECTIVITY_FILE = STATE_DIR / "last_connectivity.json"
POSITION_SNAPSHOT_FILE = STATE_DIR / "last_position_snapshot.json"

# ─── 常量 ───────────────────────────────────────────────────
DEFAULT_COOLDOWN = 300  # 默认5分钟冷却

# 告警类别 — 定义哪些类别的告警在模拟盘模式下也发送
CATEGORY_CRITICAL = "critical"     # 熔断/网络断连/严重错误 → 始终发送
CATEGORY_OPERATION = "operation"   # 实际交易操作 → 模拟盘静默，实盘发送
CATEGORY_STATUS = "status"         # 状态报告（持仓/连接）→ 只在变化时发送
CATEGORY_INFO = "info"             # 一般信息（日报等）→ 按计划发送

# 模拟盘模式判断
def is_simulation():
    """检查是否模拟盘模式"""
    return os.getenv('OKX_FLAG', '1') == '1'

# ─── 去重 / 冷却 ──────────────────────────────────────────

def _load_dedup():
    """加载去重状态"""
    try:
        if DEDUP_FILE.exists():
            return json.loads(DEDUP_FILE.read_text())
    except:
        pass
    return {}

def _save_dedup(state):
    """保存去重状态"""
    try:
        DEDUP_FILE.write_text(json.dumps(state))
    except:
        pass

def _make_key(message, category=None):
    """生成去重key：对消息内容hash，或使用类别+核心标识"""
    if not message:
        return None
    # 截取前100字符作为标识，避免长消息产生过多不同key
    text = message.strip()[:100]
    return hashlib.md5(text.encode()).hexdigest()

def should_notify(key, cooldown=DEFAULT_COOLDOWN):
    """检查是否应该发送通知（去重+冷却）
    
    Args:
        key: 通知唯一标识（字符串）
        cooldown: 冷却时间（秒）
    
    Returns:
        True=应该发送, False=冷却中跳过
    """
    if not key:
        return True
    
    state = _load_dedup()
    now = time.time()
    
    last_time = state.get(key, 0)
    if last_time > 0 and (now - last_time) < cooldown:
        return False  # 冷却中
    
    # 更新时间戳
    state[key] = now
    _save_dedup(state)
    return True

# ─── 发送接口 ──────────────────────────────────────────────

def send_feishu(message, category=CATEGORY_INFO):
    """统一的飞书发送接口，带类别感知和去重
    
    Args:
        message: 通知文本
        category: 通知类别（CATEGORY_*）
    
    Returns:
        True=已发送, False=被过滤或发送失败
    """
    if not message or not message.strip():
        return False
    
    sim_mode = is_simulation()
    
    # ── 模拟盘模式过滤规则 ─────────────────────────────────
    if sim_mode:
        if category == CATEGORY_OPERATION:
            # 模拟盘：操作通知静默（不推送）
            return False
        elif category == CATEGORY_STATUS:
            # 状态通知：只在有变化时推送（调用方自行判断）
            pass  # 由调用方决定
        elif category == CATEGORY_INFO:
            # 信息类：仅在日报配置中发送
            pass
        # CRITICAL → 始终发送
    
    # ── 去重（对ROUTINE/STATUS类做冷却，CRITICAL不过滤） ──
    if category != CATEGORY_CRITICAL:
        key = _make_key(message, category)
        if not should_notify(key, cooldown=DEFAULT_COOLDOWN if category != CATEGORY_STATUS else 3600):
            return False
    
    # ── 实际发送 ──────────────────────────────────────────
    try:
        import requests as _req
        app_id = os.environ.get('FEISHU_APP_ID', '')
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')
        if not app_id or not app_secret:
            return False
        
        # 获取token
        tr = _req.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': app_id, 'app_secret': app_secret},
            timeout=10
        )
        td = tr.json()
        if td.get('code') != 0:
            return False
        token = td.get('tenant_access_token')
        
        # 发送消息
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        payload = {
            'receive_id': 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0',
            'msg_type': 'text',
            'content': json.dumps({'text': message[:4000]}),
        }
        rr = _req.post(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
            headers=headers, json=payload, timeout=10
        )
        return rr.json().get('code') == 0
    except:
        return False

# ─── 连接状态变化检测 ──────────────────────────────────────

def check_connectivity_change(current_status):
    """检测连接状态是否变化
    
    Args:
        current_status: 'ok' 或 错误描述字符串
    
    Returns:
        (changed, previous_status): 
            changed=True=状态变化了，previous_status=之前的状态
    """
    previous = "unknown"
    try:
        if CONNECTIVITY_FILE.exists():
            data = json.loads(CONNECTIVITY_FILE.read_text())
            previous = data.get('status', 'unknown')
    except:
        pass
    
    changed = (previous != current_status)
    
    # 保存当前状态
    try:
        CONNECTIVITY_FILE.write_text(json.dumps({
            'status': current_status,
            'timestamp': time.time()
        }))
    except:
        pass
    
    return changed, previous

# ─── 持仓变化检测 ──────────────────────────────────────────

def get_positions_hash(positions):
    """生成持仓的快照哈希（用于检测变化）"""
    if not positions:
        return "empty"
    
    # 按币种排序，生成稳定的hash
    items = []
    for coin in sorted(positions.keys()):
        pos = positions[coin]
        side = pos.get('side', '')
        size = pos.get('size', 0)
        entry = pos.get('entry', 0)
        items.append(f"{coin}:{side}:{size}:{entry}")
    
    return hashlib.md5("|".join(items).encode()).hexdigest()

def check_position_change(positions, positions_err=None):
    """检测持仓是否发生变化
    
    Args:
        positions: 当前持仓dict
        positions_err: 获取持仓时的错误（None=成功）
    
    Returns:
        (changed, hash_before, hash_after):
            changed=True=持仓变化了
    """
    if positions_err:
        # API错误不视为变化，不覆盖快照
        return False, None, None
    
    current_hash = get_positions_hash(positions)
    
    previous_hash = None
    try:
        if POSITION_SNAPSHOT_FILE.exists():
            data = json.loads(POSITION_SNAPSHOT_FILE.read_text())
            previous_hash = data.get('hash')
    except:
        pass
    
    changed = (previous_hash != current_hash)
    
    # 保存当前快照
    try:
        POSITION_SNAPSHOT_FILE.write_text(json.dumps({
            'hash': current_hash,
            'timestamp': time.time()
        }))
    except:
        pass
    
    return changed, previous_hash, current_hash

# ─── 快捷发送函数（兼容现有调用） ──────────────────────────

def notify_operation(message):
    """快捷发送操作通知（模拟盘静默）"""
    return send_feishu(message, CATEGORY_OPERATION)

def notify_critical(message):
    """快捷发送关键告警（始终发送）"""
    return send_feishu(message, CATEGORY_CRITICAL)

def notify_status(message):
    """快捷发送状态通知（变化时发送）"""
    return send_feishu(message, CATEGORY_STATUS)

# ─── 测试入口 ──────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    
    sim = is_simulation()
    print(f"模式: {'模拟盘' if sim else '实盘'}")
    print(f"去重文件: {DEDUP_FILE}")
    print(f"连接状态文件: {CONNECTIVITY_FILE}")
    print(f"持仓快照文件: {POSITION_SNAPSHOT_FILE}")
    
    if len(sys.argv) > 1 and sys.argv[1] == '--test-connectivity':
        changed, prev = check_connectivity_change('ok')
        print(f"连接状态变化: {changed} (之前={prev})")
        changed2, prev2 = check_connectivity_change('ok')
        print(f"再次检查(相同): {changed2} (之前={prev2})")
        changed3, prev3 = check_connectivity_change('FAIL: timeout')
        print(f"再次检查(变化): {changed3} (之前={prev3})")
    
    elif len(sys.argv) > 1 and sys.argv[1] == '--test-dedup':
        msg = "测试消息: 连接正常"
        r1 = send_feishu(msg, CATEGORY_STATUS)
        print(f"第一次发送: {'成功' if r1 else '被过滤'}")
        r2 = send_feishu(msg, CATEGORY_STATUS)
        print(f"第二次发送(应在冷却内): {'成功' if r2 else '被过滤'}")
        r3 = send_feishu("🔴 严重错误: API断连", CATEGORY_CRITICAL)
        print(f"严重告警(不过滤): {'成功' if r3 else '被过滤'}")
