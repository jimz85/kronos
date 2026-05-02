#!/usr/bin/env python3
"""
Kronos State Reconciliation Module
Detects orphan orders, phantom positions, and zombie positions.
"""
import os, json, sys, time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from kronos_utils import okx_req

# Paths
KRONOS_DIR = Path.home() / "kronos"
LOCAL_STATE_FILE = KRONOS_DIR / "local_state.json"
PAPER_TRADES_FILE = Path.home() / ".hermes" / "cron" / "output" / "paper_trades.json"

# ══════════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class OrderInfo:
    order_id: str
    inst_id: str
    side: str
    sz: float
    price: Optional[float] = None
    algo_id: Optional[str] = None
    sl_trigger: Optional[float] = None
    tp_trigger: Optional[float] = None
    ord_type: str = "market"
    state: str = "live"
    created_at: Optional[str] = None

@dataclass
class PositionInfo:
    inst_id: str
    pos: float
    direction: str
    entry_price: float = 0.0
    mark_price: float = 0.0
    upl: float = 0.0
    notional: float = 0.0
    leverage: float = 1.0
    liq_price: float = 0.0

@dataclass
class LocalOrderRecord:
    order_id: str
    inst_id: str
    side: str
    sz: float
    created_at: str
    status: str = "submitted"
    algo_id: Optional[str] = None

@dataclass
class LocalPositionRecord:
    inst_id: str
    direction: str
    entry_price: float
    contracts: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    opened_at: Optional[str] = None
    status: str = "open"

@dataclass
class ExchangeState:
    timestamp: str = ""
    account_balance: float = 0.0
    positions: dict = field(default_factory=dict)
    open_orders: dict = field(default_factory=dict)
    pending_algo_orders: dict = field(default_factory=dict)

@dataclass
class LocalState:
    timestamp: str = ""
    positions: dict = field(default_factory=dict)
    orders: dict = field(default_factory=dict)

@dataclass
class OrphanOrder:
    order_id: str
    inst_id: str
    side: str
    sz: float
    algo_id: Optional[str] = None
    order_type: str = "unknown"
    note: str = ""

@dataclass
class PhantomPosition:
    inst_id: str
    direction: str
    entry_price: float
    contracts: float
    note: str = ""

@dataclass
class ZombiePosition:
    inst_id: str
    direction: str
    pos: float
    entry_price: float
    mark_price: float
    upl: float
    notional: float
    note: str = ""

@dataclass
class ReconcileResult:
    timestamp: str = ""
    duration_ms: float = 0.0
    orphan_orders: list = field(default_factory=list)
    phantom_positions: list = field(default_factory=list)
    zombie_positions: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    total_local_positions: int = 0
    total_exchange_positions: int = 0
    total_local_orders: int = 0
    total_exchange_orders: int = 0
    account_balance: float = 0.0
    is_healthy: bool = True

# ══════════════════════════════════════════════════════════════════════
# State Fetching
# ══════════════════════════════════════════════════════════════════════

def fetch_exchange_state() -> ExchangeState:
    """Fetch current state from OKX exchange."""
    state = ExchangeState()
    state.timestamp = datetime.now().isoformat()

    try:
        bal = okx_req('GET', '/api/v5/account/balance')
        if bal.get('code') == '0' and bal.get('data'):
            state.account_balance = float(bal['data'][0].get('totalEq', 0))
    except: pass

    try:
        pos_data = okx_req('GET', '/api/v5/account/positions?instType=SWAP')
        if pos_data.get('code') == '0':
            for p in pos_data.get('data', []):
                inst_id = p.get('instId', '')
                pos = float(p.get('pos', 0))
                if pos == 0: continue
                state.positions[inst_id] = PositionInfo(
                    inst_id=inst_id, pos=pos, direction='long' if pos > 0 else 'short',
                    entry_price=float(p.get('avgPx', 0)), mark_price=float(p.get('markPx', 0)),
                    upl=float(p.get('upl', 0)), notional=float(p.get('notionalUsd', 0)),
                    leverage=float(p.get('lever', 1)), liq_price=float(p.get('liqPx', 0)))
    except: pass

    try:
        orders_data = okx_req('GET', '/api/v5/trade/orders-pending?instType=SWAP')
        if orders_data.get('code') == '0':
            for o in orders_data.get('data', []):
                oid = o.get('ordId', '')
                if not oid or float(o.get('sz', 0)) == 0: continue
                state.open_orders[oid] = OrderInfo(
                    order_id=oid, inst_id=o.get('instId', ''), side=o.get('side', ''),
                    sz=float(o.get('sz', 0)), price=float(o.get('px', 0)) if o.get('px') else None,
                    ord_type=o.get('ordType', 'market'), state=o.get('state', 'live'))
    except: pass

    try:
        algo_data = okx_req('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP')
        if algo_data.get('code') == '0':
            for a in algo_data.get('data', []):
                aid = a.get('algoId', '')
                if not aid or float(a.get('sz', 0)) == 0: continue
                state.pending_algo_orders[aid] = OrderInfo(
                    order_id=aid, inst_id=a.get('instId', ''), side=a.get('side', ''),
                    sz=float(a.get('sz', 0)), algo_id=aid,
                    sl_trigger=float(a.get('slTriggerPx', 0)) if a.get('slTriggerPx') else None,
                    tp_trigger=float(a.get('tpTriggerPx', 0)) if a.get('tpTriggerPx') else None,
                    ord_type=a.get('ordType', 'conditional'), state='pending')
    except: pass

    return state

def load_local_state() -> LocalState:
    """Load locally tracked state from disk.

    ✅ P2 Fix: 正确读取paper_trades.json（修复原实现只看order_id/ordId的问题）。
    paper_trades使用: id(字符串), trade_id(UUID), coin, direction, status, contracts。
    """
    state = LocalState()
    state.timestamp = datetime.now().isoformat()

    # Load positions from local_state.json (legacy)
    if LOCAL_STATE_FILE.exists():
        try:
            with open(LOCAL_STATE_FILE) as f:
                data = json.load(f)
            for inst_id, pos_data in data.items():
                if isinstance(pos_data, dict) and 'position' in str(pos_data):
                    for coin, info in pos_data.items():
                        if isinstance(info, dict) and info.get('position', 0) != 0:
                            state.positions[f"{coin}-USDT-SWAP"] = LocalPositionRecord(
                                inst_id=f"{coin}-USDT-SWAP",
                                direction='long' if info['position'] > 0 else 'short',
                                entry_price=info.get('entry', 0),
                                contracts=abs(info['position']),
                                stop_loss=info.get('stop'))
        except: pass

    # Load paper_trades.json (primary source)
    # paper_trades格式: id, trade_id(UUID), coin, direction, status, contracts, entry_price等
    if PAPER_TRADES_FILE.exists():
        try:
            with open(PAPER_TRADES_FILE) as f:
                trades = json.load(f)
            for t in trades:
                if not isinstance(t, dict):
                    continue
                status = t.get('status', '')
                coin = t.get('coin', '')
                direction = t.get('direction', '')
                # 只追踪OPEN的paper_trades持仓
                if status == 'OPEN' and coin:
                    inst_id = f"{coin}-USDT-SWAP"
                    # 转换direction格式
                    dir_lower = direction.lower()
                    if dir_lower in ('long', '做多'):
                        dir_normalized = 'long'
                    elif dir_lower in ('short', '做空'):
                        dir_normalized = 'short'
                    else:
                        dir_normalized = dir_lower
                    state.positions[inst_id] = LocalPositionRecord(
                        inst_id=inst_id,
                        direction=dir_normalized,
                        entry_price=t.get('entry_price', 0),
                        contracts=t.get('contracts', 0),
                        stop_loss=t.get('sl_price'),
                        opened_at=t.get('open_time', ''))
                    result_key = t.get('trade_id') or t.get('id', '')
                    state.orders[str(result_key)] = LocalOrderRecord(
                        order_id=str(result_key),
                        inst_id=inst_id,
                        side=dir_normalized,
                        sz=t.get('contracts', 0),
                        created_at=t.get('open_time', ''),
                        status='open')
        except Exception as e:
            pass  # 不阻断

    return state

# ══════════════════════════════════════════════════════════════════════
# Reconciliation
# ══════════════════════════════════════════════════════════════════════

def reconcile_state() -> ReconcileResult:
    """Compare local state against exchange state. Returns ReconcileResult."""
    start = time.time()
    result = ReconcileResult()
    result.timestamp = datetime.now().isoformat()

    try:
        exchange = fetch_exchange_state()
        local = load_local_state()

        result.total_exchange_positions = len(exchange.positions)
        result.total_local_positions = len(local.positions)
        result.total_exchange_orders = len(exchange.open_orders) + len(exchange.pending_algo_orders)
        result.total_local_orders = len(local.orders)
        result.account_balance = exchange.account_balance

        # Detect Orphan Orders (on exchange, no local record)
        for oid, o in exchange.open_orders.items():
            if oid not in local.orders:
                result.orphan_orders.append(OrphanOrder(
                    order_id=oid, inst_id=o.inst_id, side=o.side, sz=o.sz,
                    order_type=o.ord_type, note="Order on exchange but no local record"))

        for aid, a in exchange.pending_algo_orders.items():
            if aid not in local.orders:
                result.orphan_orders.append(OrphanOrder(
                    order_id=aid, inst_id=a.inst_id, side=a.side, sz=a.sz,
                    algo_id=aid, order_type=f"algo_{a.ord_type}", note="Algo order on exchange, no local record"))

        # Detect Phantom Positions (local open, exchange zero)
        for inst_id, lpos in local.positions.items():
            if inst_id not in exchange.positions:
                contracts = getattr(lpos, 'contracts', None) or getattr(lpos, 'pos', 0)
                result.phantom_positions.append(PhantomPosition(
                    inst_id=inst_id, direction=getattr(lpos, 'direction', 'unknown'),
                    entry_price=getattr(lpos, 'entry_price', 0.0),
                    contracts=contracts, note="Local position has zero size on exchange - PHANTOM!"))

        # Detect Zombie Positions (on exchange, not tracked locally)
        for inst_id, epos in exchange.positions.items():
            if epos.pos != 0 and inst_id not in local.positions:
                result.zombie_positions.append(ZombiePosition(
                    inst_id=inst_id, direction=epos.direction, pos=epos.pos,
                    entry_price=epos.entry_price, mark_price=epos.mark_price,
                    upl=epos.upl, notional=epos.notional,
                    note="Position on exchange but no local tracking record"))

        # Health check
        if result.orphan_orders:
            result.warnings.append(f"Found {len(result.orphan_orders)} orphan order(s)")
            result.is_healthy = False
        if result.phantom_positions:
            result.errors.append(f"Found {len(result.phantom_positions)} phantom position(s) - local open but exchange has none!")
            result.is_healthy = False
        if result.zombie_positions:
            result.warnings.append(f"Found {len(result.zombie_positions)} zombie position(s) - on exchange not tracked locally")
            result.is_healthy = False

    except Exception as e:
        result.errors.append(f"Reconciliation failed: {e}")
        result.is_healthy = False

    result.duration_ms = (time.time() - start) * 1000
    return result

# ══════════════════════════════════════════════════════════════════════
# Human-Readable Report
# ══════════════════════════════════════════════════════════════════════

def print_startup_report(result: ReconcileResult) -> None:
    """Print human-readable startup report to console."""
    print()
    print("═" * 70)
    print("  KRONOS STATE RECONCILIATION REPORT")
    print("═" * 70)
    print(f"  Generated: {result.timestamp}")
    print(f"  Duration:  {result.duration_ms:.1f}ms")
    print()

    print("┌─ EXCHANGE STATE (OKX) ──────────────────────────────────────")
    print(f"│  Account Balance: ${result.account_balance:,.2f}")
    print(f"│  Open Positions:  {result.total_exchange_positions}")
    print(f"│  Open Orders:     {result.total_exchange_orders}")
    print("└─────────────────────────────────────────────────────────────")
    print()
    print("┌─ LOCAL STATE ───────────────────────────────────────────────")
    print(f"│  Tracked Positions: {result.total_local_positions}")
    print(f"│  Tracked Orders:    {result.total_local_orders}")
    print("└─────────────────────────────────────────────────────────────")

    print()
    if result.orphan_orders:
        print("┌─ ⚠️  ORPHAN ORDERS (on exchange, no local record) ──────────")
        for o in result.orphan_orders:
            print(f"│  [{o.order_id}] {o.inst_id} {o.side} sz={o.sz} type={o.order_type}")
        print("└─────────────────────────────────────────────────────────────")
    else:
        print("┌─ ✓ NO ORPHAN ORDERS ───────────────────────────────────────")

    print()
    if result.phantom_positions:
        print("┌─ 🚨 PHANTOM POSITIONS (local open, exchange zero) ───────────")
        for p in result.phantom_positions:
            print(f"│  {p.inst_id} | {p.direction} | entry={p.entry_price} | contracts={p.contracts}")
            print(f"│    → {p.note}")
        print("└─────────────────────────────────────────────────────────────")
    else:
        print("┌─ ✓ NO PHANTOM POSITIONS ────────────────────────────────────")

    print()
    if result.zombie_positions:
        print("┌─ ⚡ ZOMBIE POSITIONS (on exchange, not tracked locally) ─────")
        for z in result.zombie_positions:
            print(f"│  {z.inst_id} | {z.direction} | pos={z.pos} | entry={z.entry_price:.4f} | mark={z.mark_price:.4f}")
            print(f"│    UPL=${z.upl:.2f} | notional=${z.notional:.2f}")
        print("└─────────────────────────────────────────────────────────────")
    else:
        print("┌─ ✓ NO ZOMBIE POSITIONS ─────────────────────────────────────")

    if result.errors:
        print()
        print("┌─ ❌ ERRORS ──────────────────────────────────────────────────")
        for e in result.errors: print(f"│  {e}")
        print("└─────────────────────────────────────────────────────────────")

    if result.warnings:
        print()
        print("┌─ ⚠️  WARNINGS ───────────────────────────────────────────────")
        for w in result.warnings: print(f"│  {w}")
        print("└─────────────────────────────────────────────────────────────")

    print()
    print("═" * 70)
    print("  ✅ STATE IS HEALTHY - Ready to operate" if result.is_healthy else "  ❌ STATE HAS ISSUES - Review above before trading")
    print("═" * 70)
    print()

# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    result = reconcile_state()
    print_startup_report(result)
    sys.exit(0 if result.is_healthy else 1)
