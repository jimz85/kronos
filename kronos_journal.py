#!/usr/bin/env python3
"""
Kronos Trade Journal Analyzer
===============================
从 OKX 实时 API 读取真实持仓和成交记录，生成交易报告。

Usage:
    python3 kronos_journal.py              # 分析并推送飞书
    python3 kronos_journal.py --weekly   # 周报
    python3 kronos_journal.py --stats     # 仅输出统计
    python3 kronos_journal.py --push      # 推送当前报告
"""

import json
import sys
import argparse
import os
import hmac
import hashlib
import base64
import requests as _requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from enum import IntEnum
from kronos_utils import get_pnl_from_fills  # v1.4: FIFO PnL 计算

# ── OKX API ───────────────────────────────────────────────────────────────────
def _ts():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

def _req_okx(method, path, body=''):
    """OKX API 请求（模拟盘 key）"""
    from dotenv import load_dotenv
    load_dotenv(Path.home() / '.hermes' / '.env', override=True)

    key    = os.getenv('OKX_API_KEY', '')
    secret = os.getenv('OKX_SECRET', '')
    phrase = os.getenv('OKX_PASSPHRASE', '')

    ts = _ts()
    msg  = f'{ts}{method}{path}{body}'
    sign = base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    headers = {
        'OK-ACCESS-KEY':     key,
        'OK-ACCESS-SIGN':    sign,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': phrase,
        'Content-Type': 'application/json',
        'x-simulated-trading': '1',
    }
    url = 'https://www.okx.com' + path
    r = _requests.get(url, headers=headers, timeout=15) if method == 'GET' else \
        _requests.post(url, headers=headers, data=body, timeout=15)
    return r.json()

# ── Paths ──────────────────────────────────────────────────────────────────────
HERMES_BASE   = Path.home() / ".hermes/cron/output"
PAPER_TRADES  = HERMES_BASE / "paper_trades.json"
JOURNAL_FILE  = HERMES_BASE / "kronos_journal.json"
BLACKLIST_FILE= HERMES_BASE / "symbol_blacklist.json"
CIRCUIT_FILE  = HERMES_BASE / "kronos_circuit.json"
TREASURY_FILE = HERMES_BASE / "kronos_treasury.json"

TZ = timezone(timedelta(hours=8))  # CST

# ── Outcome Classification ─────────────────────────────────────────────────────
class Outcome(IntEnum):
    WIN          = 1   # Positive P&L, normal TP/SL/exit
    LOSS         = 2   # Negative P&L, normal trading loss
    BALANCE_FAIL = 3   # Insufficient balance (not a trading loss)
    FAILURE      = 4   # timestamp_error, open_failed, timeout_sync, system_error
    SYSTEM_FAIL  = 5   # [deprecated, use FAILURE]
    OPEN         = 6   # Still open
    MANUAL       = 7   # Manually closed (external intervention)
    CLOSED       = 8   # Closed by system sync (OKX position no longer exists)

def classify(trade: dict) -> Outcome:
    """Classify a trade outcome from its close_reason and pnl."""
    # journal entries: outcome directly; paper_trades: status field
    status = trade.get("status", trade.get("outcome", ""))
    if status == "OPEN" or trade.get("outcome") == "OPEN":
        return Outcome.OPEN
    reason = trade.get("close_reason", "")
    pnl    = trade.get("pnl")

    # 余额/系统失败（非真实交易亏损，不计入WLR）
    if reason in ("insufficient_balance", "balance_insufficient"):
        return Outcome.BALANCE_FAIL
    if reason in ("timestamp_error", "open_failed", "timeout_sync", "system_error"):
        return Outcome.FAILURE
    if reason in ("manual", "user_closed", "external_close"):
        return Outcome.MANUAL

    # OKX系统同步关闭（无可靠PnL）
    if reason == "OKX_sync_no_position":
        return Outcome.CLOSED

    # 有PnL的正常交易
    if pnl is not None and pnl > 0:
        return Outcome.WIN
    if pnl is not None and pnl < 0:
        return Outcome.LOSS

    # Fallback: check okx_result
    okx = trade.get("okx_result", {})
    if okx.get("code") == "0" and reason in ("tp_triggered", "sl_triggered", "sold"):
        return Outcome.WIN if (pnl or 0) > 0 else Outcome.LOSS

    return Outcome.LOSS  # Default to loss

def pnl_signed(trade: dict) -> float:
    """Return P&L or 0 for open/None."""
    v = trade.get("pnl")
    return v if v is not None else 0.0

# ── Journal Entry ──────────────────────────────────────────────────────────────
def build_journal_entry(trade: dict) -> dict:
    outcome = classify(trade)
    open_time  = trade.get("open_time")
    close_time = trade.get("close_time")
    hold_hours = None
    if open_time and close_time:
        try:
            ot = datetime.fromisoformat(open_time)
            ct = datetime.fromisoformat(close_time)
            hold_hours = (ct - ot).total_seconds() / 3600
        except Exception:
            pass
    return {
        "trade_id":   trade["id"],
        "coin":       trade["coin"],
        "direction":  trade["direction"],
        "entry_price":trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "size_usd":   trade.get("size_usd"),
        "pnl":        pnl_signed(trade),
        "outcome":    outcome.name,
        "close_reason": trade.get("close_reason"),
        "hold_hours": hold_hours,
        "open_time":  open_time,
        "close_time": close_time,
        "ic":         trade.get("ic"),
        "best_factor":trade.get("best_factor"),
        "confidence": trade.get("confidence"),
        # ── P2 Fix: 补充完整交易日志字段 ─────────────────────────
        "open_reason":    trade.get("open_reason"),
        "sl_price":      trade.get("sl_price"),
        "tp_price":      trade.get("tp_price"),
        "rsi_at_entry":  trade.get("rsi_at_entry"),
        "adx_at_entry":  trade.get("adx_at_entry"),
        "btc_price_at_entry": trade.get("btc_price_at_entry"),
    }

def load_trades() -> list:
    """从 OKX 实时 API 读取真实持仓和近期成交记录。
    
    数据源：
    - 持仓 → GET /api/v5/account/positions（实时权益、浮盈、入场价）
    - 近期成交 → GET /api/v5/trade/fills（最近24h内的所有成交）
    
    返回格式与原 paper_trades.json 兼容，供 compute_stats() 使用。
    """
    trades = []
    
    # ── 1. 当前真实持仓 ──────────────────────────────────────────────────────
    # ── 1. 当前 Kronos 管理的持仓（以 paper_trades 为主，OKX 补充实时 PnL）──
    # v1.4: 只显示 Kronos 管理的币（DOGE/SOL），OKX 数据仅补充实时浮动盈亏
    try:
        paper_trades = json.loads(PAPER_TRADES.read_text())
    except:
        paper_trades = []

    managed_coins = {t['coin'] for t in paper_trades if t.get('status') == 'OPEN'}
    if not managed_coins:
        managed_coins = {'DOGE', 'SOL'}  # fallback: 默认管理币种

    # 实时从 OKX 补充当前持仓的浮动盈亏
    okx_live = {}
    try:
        pos_resp = _req_okx('GET', '/api/v5/account/positions')
    except Exception as e:
        print(f"[journal] OKX positions API failed: {e}", flush=True)
        pos_resp = {}
    if pos_resp.get('code') == '0':
        for p in pos_resp.get('data', []):
            inst_id = p.get('instId', '')
            if '-USDT-SWAP' not in inst_id:
                continue
            coin = inst_id.replace('-USDT-SWAP', '')
            if coin not in managed_coins:
                continue
            pos_side = p.get('posSide', '').lower()
            if not pos_side:
                continue
            pos_amt = float(p.get('pos', '0'))
            if pos_amt <= 0:
                continue
            avg_px = float(p.get('avgPx', 0) or 0)
            if avg_px <= 0:
                continue
            okx_live[f'{coin}_{pos_side}'] = {
                'upl': float(p.get('upl', 0) or 0),
                'avg_px': avg_px,
                'pos_amt': pos_amt,
            }

    # 用 paper_trades 数据（可靠），用 OKX 补充实时 PnL
    for t in paper_trades:
        if t.get('status') != 'OPEN':
            continue
        coin = t['coin']
        direction = t.get('direction', 'long')
        key = f'{coin}_{direction}'
        upl_data = okx_live.get(key, {})
        upl = upl_data.get('upl', 0)
        # 如果 OKX 没有该持仓（可能已平），标记为 CLOSED
        if upl_data and upl_data.get('pos_amt', 0) <= 0:
            t['status'] = 'CLOSED'
            t['close_time'] = datetime.now(TZ).isoformat()
            t['close_reason'] = 'OKX_sync'
            upl = 0
        trades.append({
            'id':           t.get('id', f'paper_{coin}_{direction}'),
            'coin':         coin,
            'direction':    direction,
            'status':       t.get('status', 'OPEN'),
            'entry_price':  t.get('entry_price'),
            'exit_price':   None,
            'size_usd':     t.get('size_usd'),
            'pnl':          round(upl, 4),  # OKX 实时浮动盈亏
            'close_reason': t.get('close_reason'),
            'open_time':    t.get('open_time'),
            'close_time':   t.get('close_time'),
            'ic':           t.get('ic'),
            'best_factor':  t.get('best_factor'),
            'confidence':   t.get('confidence'),
        })
    
    # ── 2. 近期成交（最近48h）→ 找已平仓记录 ────────────────────────────────
    # v1.4: 用 FIFO 配对计算真实 PnL，不再设为 0
    # 策略：先计算coin级FIFO PnL，再分配给各笔成交
    after_ts = int((datetime.now(TZ) - timedelta(hours=48)).timestamp() * 1000)

    coins = ['AVAX', 'BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'DOT', 'LINK', 'XRP']
    closed_ids = {t['id'] for t in trades}   # 排除已在持仓中的
    
    for coin in coins:
        # v1.4: 先计算该币的 FIFO PnL（所有fills聚合），再分配给各笔成交
        try:
            coin_pnl = get_pnl_from_fills(coin) or 0.0
        except Exception as e:
            print(f"[journal] get_pnl_from_fills failed for {coin}: {e}", flush=True)
            coin_pnl = 0.0
        inst_id = f'{coin}-USDT-SWAP'
        try:
            resp = _req_okx('GET', f'/api/v5/trade/fills?instId={inst_id}&after={after_ts}&limit=100')
        except Exception as e:
            print(f"[journal] OKX fills API failed for {coin}: {e}", flush=True)
            continue
        if resp.get('code') != '0':
            continue
        
        # 按 ordId 分组合并成交（一次开仓/平仓可能有多笔成交）
        from collections import defaultdict as DD
        ord_map = DD(list)
        for f in resp.get('data', []):
            oid = f.get('ordId', '')
            if oid:
                ord_map[oid].append(f)
        
        for oid, fills in ord_map.items():
            # 找这个订单的完整信息
            inst_id_f = fills[0].get('instId', '')
            side = fills[0].get('side', '').lower()   # buy / sell
            # 取第一笔成交时间作为订单时间
            first_ts = min(int(f.get('ts', 0)) for f in fills)
            last_ts  = max(int(f.get('ts', 0)) for f in fills)
            
            open_dt  = datetime.utcfromtimestamp(first_ts / 1000).replace(tzinfo=timezone.utc).astimezone(TZ)
            close_dt = datetime.utcfromtimestamp(last_ts  / 1000).replace(tzinfo=timezone.utc).astimezone(TZ)
            
            # 方向判断：buy=做多(close sell), sell=做空(close buy)
            if side == 'buy':
                direction = 'long'
            elif side == 'sell':
                direction = 'short'
            else:
                continue
            
            coin_sym = inst_id_f.replace('-USDT-SWAP', '')
            trade_id = f'fill_{coin_sym}_{direction}_{first_ts}'
            
            if trade_id in closed_ids:
                continue
            
            # 计算平均成交价和手续费
            total_sz = sum(float(f.get('sz', 0)) for f in fills)
            if total_sz == 0:
                continue
            
            avg_px_fill = sum(float(f.get('sz', 0)) * float(f.get('px', 0) or 0) for f in fills) / total_sz
            
            # pnl：OKX fills 不直接提供，用持仓的 upl
            # 简化：取平均价差 × 数量估算
            pnl_val = coin_pnl  # v1.4: 用 coin 级 FIFO PnL（来自 get_pnl_from_fills）
            fee_val = sum(float(f.get('fee', 0) or 0) for f in fills)
            
            # 判断是开仓还是平仓：根据 side 和数量方向
            # buy + 有持仓 → 开仓（跳过）；sell + 有持仓 → 平仓
            # 这里简化为：所有 fills 都记为一次完整交易（开仓+平仓合并）
            trades.append({
                'id':           trade_id,
                'coin':         coin_sym,
                'direction':    direction,
                'status':       'CLOSED',
                'entry_price':  round(avg_px_fill, 6),
                'exit_price':   round(avg_px_fill, 6),   # 估算
                'size_usd':     round(avg_px_fill * total_sz, 2),
                'pnl':          round(pnl_val - abs(fee_val), 4),
                'close_reason': 'filled',   # 从成交记录
                'open_time':    open_dt.isoformat(),
                'close_time':   close_dt.isoformat(),
                'ic':           None,
                'best_factor':  None,
                'confidence':   None,
            })
            closed_ids.add(trade_id)
    
    print(f"[kronos_journal] OKX真实持仓: {len([t for t in trades if t['status']=='OPEN'])} 活跃, "
          f"{len([t for t in trades if t['status']=='CLOSED'])} 已平仓")
    return trades

def load_journal() -> dict:
    if JOURNAL_FILE.exists():
        with open(JOURNAL_FILE) as f:
            return json.load(f)
    return {"entries": [], "weekly_reports": [], "stats": {}}

def save_journal(journal: dict):
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_FILE, "w") as f:
        json.dump(journal, f, indent=2, ensure_ascii=False)

def _sync_open_entries_with_okx(journal: dict):
    """对journal中所有OPEN条目做OKX实时持仓验证，关闭幽灵仓位。
    
    每次update_journal()时调用，防止SL/TP触发后journal条目永久失联。
    工作逻辑：
    1. 收集所有outcome=OPEN的条目
    2. 从OKX API获取所有当前持仓
    3. 对每个OPEN条目，检查OKX是否还有对应持仓（coin+direction匹配）
    4. 如果OKX无持仓 → 标记为CLOSED，close_reason=OKX_sync_no_position
       同时更新paper_trades.json保持两边一致（Bug Fix: 之前只更新journal不更新paper_trades）
    """
    from collections import defaultdict
    import requests as _requests
    
    open_entries = [e for e in journal.get('entries', []) if e.get('outcome') == 'OPEN']
    if not open_entries:
        return  # 无OPEN条目，快速返回
    
    # 构建OKX当前持仓的快速查找表
    okx_pos = defaultdict(dict)  # okx_pos[coin][direction] = {upl, avgPx, pos}
    try:
        pos_resp = _req_okx('GET', '/api/v5/account/positions')
    except Exception as e:
        print(f"[kronos_journal] OKX持仓查询失败: {e}")
        return
    if pos_resp.get('code') != '0':
        print(f"[kronos_journal] OKX持仓查询失败: {pos_resp.get('msg', pos_resp)}")
        return
    
    for p in pos_resp.get('data', []):
        inst_id = p.get('instId', '')
        if '-USDT-SWAP' not in inst_id:
            continue
        coin = inst_id.replace('-USDT-SWAP', '')
        pos_side = p.get('posSide', '').lower()
        pos_amt = float(p.get('pos', '0'))
        if pos_amt <= 0:
            continue
        okx_pos[coin][pos_side] = {
            'upl': float(p.get('upl', 0) or 0),
            'avgPx': float(p.get('avgPx', 0) or 0),
            'pos': pos_amt,
        }
    
    # ── Bug Fix: 同时加载paper_trades.json用于同步更新 ─────────────────────
    paper_trades = []
    try:
        paper_trades = json.loads(PAPER_TRADES.read_text())
    except Exception:
        pass  # 无paper_trades文件时跳过

    closed_count = 0
    closed_trade_ids = []  # 记录已关闭的trade_id，用于同步paper_trades
    for e in journal.get('entries', []):
        if e.get('outcome') != 'OPEN':
            continue
        coin = e.get('coin', '')
        direction = e.get('direction', 'long').lower()
        trade_id = e.get('trade_id', '')
        
        if coin not in okx_pos or direction not in okx_pos[coin]:
            # OKX没有这个持仓了 → 幽灵仓位，标记关闭
            now_iso = datetime.now(TZ).isoformat()
            e['outcome'] = 'CLOSED'
            e['status'] = 'CLOSED'
            e['close_reason'] = 'OKX_sync_no_position'
            e['close_time'] = now_iso
            # pnl必须清零：OKX_sync_no_position意味着我们无法获取真实已实现盈亏，
            # 旧的浮动盈亏（floating UPL）对已平仓毫无意义，不能混入统计。
            e['pnl'] = 0.0
            closed_count += 1
            closed_trade_ids.append(trade_id)
            print(f"[kronos_journal] 幽灵仓位关闭: {trade_id} (OKX无{coin}_{direction}持仓)")

            # ── Bug Fix: 同步更新paper_trades.json ──────────────────────────
            # 匹配逻辑：用trade_id匹配paper_trades中的id字段
            for pt in paper_trades:
                if pt.get('id') == trade_id and pt.get('status') == 'OPEN':
                    pt['status'] = 'CLOSED'
                    pt['close_reason'] = 'OKX_sync_no_position'
                    pt['close_time'] = now_iso
                    pt['pnl'] = 0.0
                    print(f"[kronos_journal] 纸仓同步关闭: {trade_id}")
                    break
    
    # 保存更新后的paper_trades.json
    if closed_count > 0 and paper_trades:
        try:
            PAPER_TRADES.parent.mkdir(parents=True, exist_ok=True)
            with open(PAPER_TRADES, 'w') as f:
                json.dump(paper_trades, f, indent=2, ensure_ascii=False)
            print(f"[kronos_journal] paper_trades.json已同步更新（{closed_count}条）")
        except Exception as e:
            print(f"[kronos_journal] paper_trades.json写入失败: {e}")
    
    if closed_count > 0:
        print(f"[kronos_journal] 共关闭{closed_count}个幽灵仓位")

def update_journal(trades: list):
    """Sync closed trades into journal entries.
    
    PROTECTION: 每次更新前，对所有OPEN条目做OKX实时持仓验证，
    防止SL/TP触发后journal里的条目永远变成幽灵。
    """
    journal = load_journal()
    
    # ── P0 Fix: 直接对所有OPEN journal条目做OKX实时验证 ───────────────
    # 问题根源: load_trades()只处理paper_trades里的OPEN条目，
    # "live_"开头的条目（来自旧版初始化）永远不会被OKX同步检查。
    # 修复: 在任何其他操作之前，先查OKX当前持仓，逐条验证OPEN条目。
    _sync_open_entries_with_okx(journal)
    
    existing_ids = {e["trade_id"] for e in journal["entries"]}
    for t in trades:
        if "id" not in t:
            continue  # Skip OKX-synced trades without id field
        if t["id"] not in existing_ids:
            journal["entries"].append(build_journal_entry(t))  # Add all (OPEN/CLOSED/FAILED)
    # Update open trades in entries (in case they closed)
    open_ids = {t["id"] for t in trades if t.get("id") and t.get("status") == "OPEN"}
    for i, e in enumerate(journal["entries"]):
        if e["trade_id"] in open_ids:
            # Find current state
            for t in trades:
                if t.get("id") == e["trade_id"]:
                    if t["status"] in ("CLOSED", "FAILED"):
                        journal["entries"][i] = build_journal_entry(t)
                    break
    # Update consecutive_loss_hours based on closed trades
    # 规则：有WIN → 重置为0；有LOSS → +1
    # 这样连亏计数只受真实交易结果驱动，不受时间/快照影响
    new_entries = [build_journal_entry(t) for t in trades
                  if t.get("id") and t["id"] not in existing_ids
                  and t.get("status") in ("CLOSED", "FAILED")]
    if new_entries:
        _update_consecutive_loss_hours(new_entries)
    
    save_journal(journal)


def _update_consecutive_loss_hours(new_entries: list):
    """
    根据journal新平仓交易更新treasury的consecutive_loss_hours。

    规则：
    - 有WIN → consecutive_loss_hours = 0（盈利重置）
    - 有LOSS → consecutive_loss_hours += 1（连亏+1）
    - 持平 → 不改变计数
    """
    from real_monitor import load_treasury_state, save_treasury_state
    state = load_treasury_state()

    has_win = any(e['outcome'] == 'WIN' for e in new_entries)
    has_loss = any(e['outcome'] == 'LOSS' for e in new_entries)

    if has_win:
        state['consecutive_loss_hours'] = 0
        print(f"  ✅ 连亏重置（journal WIN）")
    elif has_loss:
        state['consecutive_loss_hours'] = state.get('consecutive_loss_hours', 0) + 1
        print(f"  ⚠️  连亏+1（journal LOSS）")
    # 持平 → 不改变

    save_treasury_state(state)


# ── Statistics ─────────────────────────────────────────────────────────────────
def compute_stats(journal: dict) -> dict:
    entries = journal["entries"]
    if not entries:
        return empty_stats()

    # Separate by outcome
    by_outcome = defaultdict(list)
    for e in entries:
        by_outcome[Outcome[e["outcome"]]].append(e)

    closed = [e for e in entries if e["outcome"] not in ("OPEN",)]
    wins   = by_outcome[Outcome.WIN]
    losses = by_outcome[Outcome.LOSS]
    bal_fails = by_outcome[Outcome.BALANCE_FAIL]
    sys_fails = by_outcome[Outcome.FAILURE]
    open_trades = by_outcome[Outcome.OPEN]

    # P&L
    total_pnl = sum(e["pnl"] for e in closed)
    win_pnl   = sum(e["pnl"] for e in wins)
    loss_pnl  = sum(e["pnl"] for e in losses)

    # Win rate (exclude balance failures and open from count)
    n_trading = len(wins) + len(losses)
    win_rate  = len(wins) / n_trading if n_trading > 0 else 0.0

    # Average win / loss
    avg_win  = win_pnl / len(wins) if wins else 0.0
    avg_loss = loss_pnl / len(losses) if losses else 0.0
    wlr      = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    # Holding hours
    with_duration = [e for e in closed if e["hold_hours"] is not None]
    avg_hold = sum(e["hold_hours"] for e in with_duration) / len(with_duration) if with_duration else 0

    # Per-coin stats
    coin_stats = {}
    for coin in set(e["coin"] for e in entries):
        ce = [e for e in entries if e["coin"] == coin]
        cw = [e for e in ce if e["outcome"] == "WIN"]
        cl = [e for e in ce if e["outcome"] == "LOSS"]
        cpnl = sum(e["pnl"] for e in ce if e["outcome"] not in ("OPEN",))
        coin_stats[coin] = {
            "trades":     len(ce),
            "wins":       len(cw),
            "losses":     len(cl),
            "total_pnl":  round(cpnl, 4),
            "win_rate":   round(len(cw) / (len(cw)+len(cl)), 4) if (len(cw)+len(cl)) > 0 else 0,
            "avg_win":    round(sum(e["pnl"] for e in cw)/len(cw), 4) if cw else 0,
            "avg_loss":   round(sum(e["pnl"] for e in cl)/len(cl), 4) if cl else 0,
            "open":       len([e for e in ce if e["outcome"] == "OPEN"]),
        }

    # Max drawdown (peak-to-trough equity decline)
    mdd, peak = 0, 0
    running = 0
    for e in sorted(closed, key=lambda x: x["close_time"] or ""):
        running += e["pnl"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > mdd:
            mdd = dd

    return {
        "total_trades":     len(entries),
        "closed_trades":    len(closed),
        "open_trades":      len(open_trades),
        "wins":             len(wins),
        "losses":           len(losses),
        "balance_fails":    len(bal_fails),
        "system_fails":     len(sys_fails),
        "win_rate":         round(win_rate, 4),
        "total_pnl":        round(total_pnl, 4),
        "win_pnl":          round(win_pnl, 4),
        "loss_pnl":         round(loss_pnl, 4),
        "avg_win":          round(avg_win, 4),
        "avg_loss":         round(avg_loss, 4),
        "wlr":              round(wlr, 2) if wlr != float('inf') else "∞",
        "avg_hold_hours":   round(avg_hold, 1),
        "max_drawdown":     round(mdd, 4),
        "coin_stats":       coin_stats,
    }

def empty_stats() -> dict:
    return {
        "total_trades": 0, "closed_trades": 0, "open_trades": 0,
        "wins": 0, "losses": 0, "balance_fails": 0, "system_fails": 0,
        "win_rate": 0, "total_pnl": 0, "win_pnl": 0, "loss_pnl": 0,
        "avg_win": 0, "avg_loss": 0, "wlr": "∞",
        "avg_hold_hours": 0, "max_drawdown": 0, "coin_stats": {},
    }

# ── Feishu Push ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from kronos_pilot import push_feishu as _pf

def push_feishu(msg: str):
    try:
        return _pf(msg)
    except Exception as e:
        return {"error": str(e)}

# ── Weekly Report Builder ──────────────────────────────────────────────────────
def build_weekly_report(stats: dict, circuit: dict, treasury: dict, blacklist: dict) -> str:
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"📊 Kronos Trade Journal | {now}",
        "=" * 40,
    ]

    # Summary
    s = stats
    pnl_emoji = "🟢" if s["total_pnl"] >= 0 else "🔴"
    lines += [
        "",
        "【账户概览】",
        f"  总交易次数: {s['total_trades']} (closed:{s['closed_trades']} open:{s['open_trades']})",
        f"  盈亏: {pnl_emoji} ${s['total_pnl']:.4f}",
        f"  胜率: {s['win_rate']:.1%} ({s['wins']}胜/{s['losses']}亏)",
        f"  WLR: {s['wlr']} | 平均持仓: {s['avg_hold_hours']:.1f}h",
    ]

    if s["balance_fails"] or s["system_fails"]:
        lines.append(f"  ⚠️ 系统异常: {s['balance_fails']}笔余额不足 / {s['system_fails']}笔同步异常")

    # Circuit breaker
    if circuit.get("is_tripped"):
        lines.append(f"  ⛔ 熔断器: 已触发 ({circuit.get('trip_reason', '')})")
    else:
        lines.append(f"  ✅ 熔断器: 正常")

    # Treasury
    eq = treasury.get("hourly_snapshot_equity", 0)
    lines.append(f"  权益: ${eq:.2f}")

    # Blacklist
    if blacklist:
        active = {k: v for k, v in blacklist.items()
                  if v.get("expires_at", 0) > datetime.now(TZ).timestamp()}
        if active:
            import time
            for coin, info in active.items():
                remaining = (info["expires_at"] - time.time()) / 86400
                lines.append(f"  🚫 {coin} 黑名单: {remaining:.1f}天后自动移除 ({info['reason']})")

    # Per-coin breakdown
    if s["coin_stats"]:
        lines.append("")
        lines.append("【各币种明细】")
        for coin, cs in sorted(s["coin_stats"].items()):
            wr = cs["win_rate"]
            wr_str = f"{wr:.0%}" if isinstance(wr, float) else str(wr)
            pnl_str = f"${cs['total_pnl']:.4f}"
            pnl_prefix = "🟢" if cs['total_pnl'] >= 0 else "🔴"
            lines.append(
                f"  {coin}: {pnl_prefix}{pnl_str} | "
                f"胜率{cs['wins']}/{cs['losses']}={wr_str} | "
                f"均盈${cs['avg_win']:.4f} 均亏${cs['avg_loss']:.4f} | "
                f"开仓{cs['open']}笔"
            )

    # Anomaly detection
    sys_fails = s.get("system_fails", 0)
    if sys_fails > 0:
        lines.append("")
        lines.append(f"⚠️ 异常记录: {sys_fails}笔系统同步异常，需人工检查paper_trades.json")

    lines.append("")
    lines.append("─────────────────────")
    lines.append("Kronos Journal | 全自动交易系统")

    return "\n".join(lines)

# ── Full Report Builder (detailed) ─────────────────────────────────────────────
def build_full_report(stats: dict, entries: list, circuit: dict) -> str:
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"📈 Kronos OKX真实交易报告 | {now}",
        "=" * 42,
    ]

    s = stats
    pnl_emoji = "🟢" if s["total_pnl"] >= 0 else "🔴"

    # Section 1: Summary
    lines += [
        "",
        "【业绩摘要】",
        f"  总交易: {s['total_trades']}笔",
        f"  已平仓: {s['closed_trades']}笔 (胜{s['wins']}/亏{s['losses']}/异常{s['balance_fails']+s['system_fails']})",
        f"  持仓中: {s['open_trades']}笔",
        f"  总盈亏: {pnl_emoji} ${s['total_pnl']:.4f}",
        f"  胜率: {s['win_rate']:.1%}",
        f"  WLR: {s['wlr']}",
        f"  均持仓: {s['avg_hold_hours']:.1f}h",
        f"  最大回撤: ${s['max_drawdown']:.4f}",
        f"  均盈: ${s['avg_win']:.4f} | 均亏: ${s['avg_loss']:.4f}",
    ]

    # Section 2: Per-coin closed stats（仅已平仓，open时显示0是正常）
    closed_coins = {e['coin'] for e in entries if e["outcome"] not in ("OPEN",)}
    if s["coin_stats"] and closed_coins:
        lines.append("")
        lines.append("【已平仓统计】")
        for coin in sorted(closed_coins):
            cs = s["coin_stats"].get(coin, {})
            pnl_prefix = "🟢" if cs.get('total_pnl', 0) >= 0 else "🔴"
            wr = cs.get("win_rate", 0)
            wr_str = f"{wr:.0%}" if isinstance(wr, float) else str(wr)
            lines.append(
                f"  {coin}: {pnl_prefix}${cs.get('total_pnl', 0):.4f} | "
                f"{cs.get('wins',0)}胜/{cs.get('losses',0)}亏 | "
                f"均盈${cs.get('avg_win',0):.4f} 均亏${cs.get('avg_loss',0):.4f}"
            )

    # Section 2b: 实时浮动盈亏（来自OKX持仓）
    open_entries = [e for e in entries if e["outcome"] == "OPEN"]
    if open_entries:
        total_upl = sum(float(e.get('pnl') or 0) for e in open_entries)
        upl_prefix = "🟢" if total_upl >= 0 else "🔴"
        lines.append("")
        lines.append("【实时浮动盈亏】")
        lines.append(f"  合计: {upl_prefix}${total_upl:.2f}")
        for e in open_entries:
            ep = e.get('entry_price')
            ep_str = f"${ep:.4f}" if ep is not None else "?"
            upl = float(e.get('pnl') or 0)
            upl_s = f"🟢+${upl:.2f}" if upl >= 0 else f"🔴${upl:.2f}"
            sz = float(e.get('size_usd') or 0)
            lines.append(f"  📍 {e['coin']} {e['direction']} @ {ep_str} | {upl_s} | 仓位${sz:.0f}")

    # Section 3: Closed trade log（仅显示有真实已平仓的记录）
    closed = [e for e in entries if e.get("close_reason") not in (None, "") and e["outcome"] not in ("OPEN",)]
    if closed:
        lines.append("")
        lines.append("【已平仓记录】")
        for e in sorted(closed, key=lambda x: x["close_time"] or "")[-10:]:  # Last 10
            outcome_icon = {
                "WIN": "✅", "LOSS": "❌", "BALANCE_FAIL": "🚫",
                "FAILURE": "⚠️", "MANUAL": "👤"
            }.get(e.get("outcome", ""), "❓")
            pnl_val = float(e.get("pnl") or 0)
            pnl_str = f"🟢${pnl_val:.4f}" if pnl_val >= 0 else f"🔴${pnl_val:.4f}"
            reason = e.get("close_reason") or "closed"
            hold = f"{e['hold_hours']:.1f}h" if e.get("hold_hours") else "?"
            lines.append(
                f"  {outcome_icon} {e['coin']} {e['direction']} | {pnl_str} | {hold} | {reason}"
            )
    else:
        lines.append("")
        lines.append("【已平仓记录】")
        lines.append("  （暂无已平仓记录）")

    # Section 4: Open positions
    open_entries = [e for e in entries if e["outcome"] == "OPEN"]
    if open_entries:
        lines.append("")
        lines.append("【当前持仓】")
        for e in open_entries:
            ep = e.get('entry_price')
            ep_str = f"${ep:.4f}" if ep is not None else "?"
            ic_str = f"IC={e['ic']:.3f}" if e.get('ic') is not None else ""
            bf_str = e.get('best_factor') or ""
            lines.append(
                f"  📍 {e['coin']} {e['direction']} @ {ep_str} | {ic_str} {bf_str}".strip()
            )

    # Section 5: Circuit breaker
    lines.append("")
    lines.append("【熔断状态】")
    if circuit.get("is_tripped"):
        lines.append(f"  ⛔ 已触发: {circuit.get('trip_reason')}")
        for l in circuit.get("losses_log", []):
            lines.append(f"     - {l['coin']}: ${l['pnl']:.4f} ({l['reason']})")
    else:
        lines.append(f"  ✅ 正常 ({circuit.get('consecutive_losses', 0)}连败)")

    lines.append("")
    lines.append("─────────────────────────────")
    lines.append("Kronos Journal | 职业套保系统")

    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Kronos Trade Journal Analyzer")
    parser.add_argument("--weekly", action="store_true", help="Push weekly summary")
    parser.add_argument("--stats",  action="store_true", help="Print stats only")
    parser.add_argument("--push",   action="store_true", help="Push full report to Feishu")
    parser.add_argument("--json",    action="store_true", help="Output JSON stats")
    args = parser.parse_args()

    trades = load_trades()
    update_journal(trades)
    journal = load_journal()
    stats   = compute_stats(journal)

    # Load supplementary
    def load_json(p):
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return {}

    circuit    = load_json(CIRCUIT_FILE)
    treasury   = load_json(TREASURY_FILE)
    blacklist  = load_json(BLACKLIST_FILE)

    if args.stats or args.json:
        output = json.dumps(stats, indent=2, ensure_ascii=False)
        print(output)
        return

    if args.weekly:
        report = build_weekly_report(stats, circuit, treasury, blacklist)
        print(report)
        result = push_feishu(report)
        print(f"\nFeishu push: {result}")
        return

    # Default: full report
    report = build_full_report(stats, journal["entries"], circuit)
    print(report)

    if args.push:
        result = push_feishu(report)
        print(f"\nFeishu push: {result}")

if __name__ == "__main__":
    main()
