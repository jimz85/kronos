#!/usr/bin/env python3
"""OKX Reconciliation Data Fetcher - Async module for fetching live assets from OKX.
Uses @async_api_retry decorator. Fetches positions, orders, algo orders concurrently.
"""
import asyncio, os, sys, hmac, base64, hashlib, time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
import aiohttp

# Add parent of kronos directory to sys.path
_kronos_parent = Path(__file__).parent.parent
if str(_kronos_parent) not in sys.path:
    sys.path.insert(0, str(_kronos_parent))

try:
    from kronos.okx_api_retry import async_api_retry, APIExhaustedError
except ImportError:
    async_api_retry = None
    APIExhaustedError = Exception

BASE_URL = "https://www.okx.com"

# ══════════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class PositionInfo:
    inst_id: str = ""; pos: float = 0.0; direction: str = ""
    entry_price: float = 0.0; mark_price: float = 0.0; upl: float = 0.0
    notional: float = 0.0; leverage: float = 1.0; liq_price: float = 0.0

@dataclass
class OrderInfo:
    order_id: str = ""; inst_id: str = ""; side: str = ""; sz: float = 0.0
    price: Optional[float] = None; algo_id: Optional[str] = None
    sl_trigger: Optional[float] = None; tp_trigger: Optional[float] = None
    ord_type: str = "market"; state: str = "live"; created_at: Optional[str] = None

@dataclass
class ExchangeState:
    timestamp: str = ""; account_balance: float = 0.0
    positions: dict = field(default_factory=dict)
    open_orders: dict = field(default_factory=dict)
    pending_algo_orders: dict = field(default_factory=dict)

    def to_dict(self) -> dict: return asdict(self)
    def __len__(self) -> int:
        return len(self.positions) + len(self.open_orders) + len(self.pending_algo_orders)

# ══════════════════════════════════════════════════════════════════════
# Async OKX HTTP Client
# ══════════════════════════════════════════════════════════════════════

async def _sign(ts: str, method: str, path: str, body: str = "") -> str:
    msg = ts + method + path + body
    mac = hmac.new(os.environ.get("OKX_SECRET", "").encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

async def _get_headers(method: str, path: str, body: str = "") -> dict:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(time.time() * 1000) % 1000)
    return {"OK-ACCESS-KEY": os.environ.get("OKX_API_KEY", ""),
            "OK-ACCESS-SIGN": await _sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": os.environ.get("OKX_PASSPHRASE", ""),
            "Content-Type": "application/json",
            "x-simulated-trading": "1" if os.environ.get("OKX_FLAG", "1") == "1" else "0"}

async def _async_request(method: str, path: str, body: str = "") -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.request(method, BASE_URL + path, headers=await _get_headers(method, path, body),
                                  data=body, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()

# ══════════════════════════════════════════════════════════════════════
# Decorated API Functions
# ══════════════════════════════════════════════════════════════════════

@async_api_retry(max_retries=5, base_delay=1.0, jitter=True)
async def fetch_balance() -> dict: return await _async_request("GET", "/api/v5/account/balance")

@async_api_retry(max_retries=5, base_delay=1.0, jitter=True)
async def fetch_positions() -> dict: return await _async_request("GET", "/api/v5/account/positions?instType=SWAP")

@async_api_retry(max_retries=5, base_delay=1.0, jitter=True)
async def fetch_open_orders() -> dict: return await _async_request("GET", "/api/v5/trade/orders-pending?instType=SWAP")

@async_api_retry(max_retries=5, base_delay=1.0, jitter=True)
async def fetch_algo_orders() -> dict: return await _async_request("GET", "/api/v5/trade/orders-algo-pending?instType=SWAP")

# ══════════════════════════════════════════════════════════════════════
# Main Entry Point
# ══════════════════════════════════════════════════════════════════════

async def get_all_live_assets() -> ExchangeState:
    """Fetch all live assets from OKX exchange concurrently. Returns ExchangeState."""
    state = ExchangeState(timestamp=datetime.now().isoformat())

    # Fetch all data concurrently
    balance_raw, positions_raw, orders_raw, algos_raw = await asyncio.gather(
        fetch_balance(), fetch_positions(), fetch_open_orders(), fetch_algo_orders())

    # Parse balance
    try:
        if balance_raw.get("code") == "0" and balance_raw.get("data"):
            state.account_balance = float(balance_raw["data"][0].get("totalEq", 0))
    except: pass

    # Parse positions
    try:
        if positions_raw.get("code") == "0":
            for p in positions_raw.get("data", []):
                inst_id = p.get("instId", ""); pos = float(p.get("pos", 0))
                if pos != 0:
                    state.positions[inst_id] = PositionInfo(
                        inst_id=inst_id, pos=pos, direction="long" if pos > 0 else "short",
                        entry_price=float(p.get("avgPx", 0)), mark_price=float(p.get("markPx", 0)),
                        upl=float(p.get("upl", 0)), notional=float(p.get("notionalUsd", 0)),
                        leverage=float(p.get("lever", 1)), liq_price=float(p.get("liqPx", 0)))
    except: pass

    # Parse orders
    try:
        if orders_raw.get("code") == "0":
            for o in orders_raw.get("data", []):
                oid = o.get("ordId", "")
                if oid and float(o.get("sz", 0)) != 0:
                    state.open_orders[oid] = OrderInfo(
                        order_id=oid, inst_id=o.get("instId", ""), side=o.get("side", ""),
                        sz=float(o.get("sz", 0)), price=float(o.get("px", 0)) if o.get("px") else None,
                        ord_type=o.get("ordType", "market"), state=o.get("state", "live"),
                        created_at=o.get("cTime", ""))
    except: pass

    # Parse algo orders
    try:
        if algos_raw.get("code") == "0":
            for a in algos_raw.get("data", []):
                aid = a.get("algoId", "")
                if aid and float(a.get("sz", 0)) != 0:
                    state.pending_algo_orders[aid] = OrderInfo(
                        order_id=aid, inst_id=a.get("instId", ""), side=a.get("side", ""),
                        sz=float(a.get("sz", 0)), algo_id=aid,
                        sl_trigger=float(a.get("slTriggerPx", 0)) if a.get("slTriggerPx") else None,
                        tp_trigger=float(a.get("tpTriggerPx", 0)) if a.get("tpTriggerPx") else None,
                        ord_type=a.get("ordType", "conditional"), state="pending",
                        created_at=a.get("cTime", ""))
    except: pass

    return state

# ══════════════════════════════════════════════════════════════════════
# Verification
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    state = ExchangeState()
    assert state.timestamp == "" and len(state) == 0
    pos = PositionInfo(inst_id="BTC-USDT-SWAP", pos=1.5, direction="long")
    assert pos.pos == 1.5 and pos.direction == "long"
    order = OrderInfo(order_id="12345", inst_id="ETH-USDT-SWAP", side="buy", sz=0.5)
    assert order.sz == 0.5
    print("✓ All verifications passed!")
