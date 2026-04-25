"""
Tests for Kronos State Reconciliation Module.
Uses unittest.mock to inject fake OKX/local state without touching real APIs.

Tests cover the 5 required scenarios:
  A — Orphan Order (dangling on OKX, local thinks IDLE)
  B — Phantom Position (local has DOGE long, OKX has 0)
  C — Zombie Position (OKX has ETH long, local thinks IDLE)
  D — Normal State (perfect match)
  E — Multiple Issues Simultaneously
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
from reconcile_state import (
    ExchangeState, LocalState, ReconcileResult,
    OrphanOrder, PhantomPosition, ZombiePosition,
    PositionInfo, OrderInfo,
    reconcile_state,
)


def make_okx_state(positions=None, open_orders=None, algo_orders=None):
    return ExchangeState(
        account_balance=0.0,
        positions=positions or {},
        open_orders=open_orders or {},
        pending_algo_orders=algo_orders or {},
    )


def make_local_state(positions=None, orders=None):
    return LocalState(
        timestamp="2026-04-25T00:00:00",
        positions=positions or {},
        orders=orders or {},
    )


# ── Helper: call reconcile_state() with mocked OKX and local data ──────────────
def run_with_mocks(okx_state, local_state):
    with patch('reconcile_state.fetch_exchange_state', return_value=okx_state), \
         patch('reconcile_state.load_local_state', return_value=local_state):
        return reconcile_state()


class TestOrphanOrder:
    """Scenario A — Dangling order on OKX, local thinks IDLE."""

    def test_orphan_order_detected(self):
        """
        Risk: Local thinks IDLE but OKX has a pending limit order.
        Kronos might place a duplicate order → double exposure.
        Expected: orphan_orders list contains this order.
        """
        okx = make_okx_state(
            open_orders={
                "ORD-999": OrderInfo(
                    order_id="ORD-999", inst_id="DOGE-USDT-SWAP",
                    side="buy", sz=1000, price=0.1821,
                    algo_id=None, sl_trigger=None, tp_trigger=None,
                    ord_type="limit", state="live", created_at="2026-04-25T00:00:00",
                )
            }
        )
        local = make_local_state(orders={})  # local knows nothing

        result = run_with_mocks(okx, local)

        assert len(result.orphan_orders) == 1, \
            f"Expected 1 orphan, got {len(result.orphan_orders)}: {[o.order_id for o in result.orphan_orders]}"
        assert result.orphan_orders[0].order_id == "ORD-999"
        assert result.orphan_orders[0].inst_id == "DOGE-USDT-SWAP"
        assert result.is_healthy is False

    def test_order_with_local_record_not_orphan(self):
        """Order tracked in local → not orphan."""
        okx = make_okx_state(
            open_orders={
                "ORD-A": OrderInfo(
                    order_id="ORD-A", inst_id="BTC-USDT-SWAP",
                    side="buy", sz=10, price=60000,
                    algo_id=None, sl_trigger=None, tp_trigger=None,
                    ord_type="limit", state="live", created_at="2026-04-25T00:00:00",
                )
            }
        )
        local = make_local_state(orders={"ORD-A": MagicMock()})

        result = run_with_mocks(okx, local)

        assert len(result.orphan_orders) == 0


class TestPhantomPosition:
    """Scenario B — Local thinks in trade, OKX shows zero holdings."""

    def test_phantom_position_detected(self):
        """
        Risk: Local thinks we're in a DOGE long but OKX shows zero.
        Kronos might send a buy order thinking we need to add.
        Expected: phantom_positions list contains DOGE.
        """
        okx = make_okx_state(positions={})   # OKX: no positions
        local = make_local_state(
            positions={
                "DOGE-USDT-SWAP": PositionInfo(
                    inst_id="DOGE-USDT-SWAP",
                    pos=2000, direction="long",
                    entry_price=0.1821,
                    mark_price=0.0, upl=0.0,
                    notional=0.0, leverage=3.0,
                    liq_price=0.0,
                )
            }
        )

        result = run_with_mocks(okx, local)

        assert len(result.phantom_positions) == 1, \
            f"Expected 1 phantom, got {len(result.phantom_positions)}"
        assert result.phantom_positions[0].inst_id == "DOGE-USDT-SWAP"
        assert result.is_healthy is False

    def test_position_on_both_sides_not_phantom(self):
        """Position exists on both OKX and local → not phantom."""
        okx = make_okx_state(
            positions={
                "BTC-USDT-SWAP": PositionInfo(
                    inst_id="BTC-USDT-SWAP",
                    pos=100, direction="long",
                    entry_price=60000.0,
                    mark_price=60500.0, upl=500.0,
                    notional=6000000.0, leverage=3.0,
                    liq_price=50000.0,
                )
            }
        )
        local = make_local_state(
            positions={
                "BTC-USDT-SWAP": MagicMock()  # local tracks it
            }
        )

        result = run_with_mocks(okx, local)

        assert len(result.phantom_positions) == 0


class TestZombiePosition:
    """Scenario C — OKX has forgotten position, local thinks IDLE."""

    def test_zombie_position_detected(self):
        """
        Risk: A forgotten ETH long from 6 months ago is still open on OKX.
        Kronos restarts thinking no positions, but ETH is bleeding.
        Expected: zombie_positions list contains ETH long.
        """
        okx = make_okx_state(
            positions={
                "ETH-USDT-SWAP": PositionInfo(
                    inst_id="ETH-USDT-SWAP",
                    pos=500, direction="long",
                    entry_price=1800.0,
                    mark_price=1750.0, upl=-25.0,
                    notional=875000.0, leverage=3.0,
                    liq_price=1700.0,   # 2.9% away from liq — manageable
                )
            }
        )
        local = make_local_state(positions={})  # local thinks IDLE

        result = run_with_mocks(okx, local)

        assert len(result.zombie_positions) == 1, \
            f"Expected 1 zombie, got {len(result.zombie_positions)}"
        zp = result.zombie_positions[0]
        assert zp.inst_id == "ETH-USDT-SWAP"
        assert zp.pos == 500
        assert zp.entry_price == 1800.0
        assert result.is_healthy is False

    def test_zombie_liquidation_imminent(self):
        """
        Risk: Zombie position is about to be liquidated (liq_price very close).
        Expected: zombie_positions list detects it.
        """
        okx = make_okx_state(
            positions={
                "SOL-USDT-SWAP": PositionInfo(
                    inst_id="SOL-USDT-SWAP",
                    pos=100, direction="long",
                    entry_price=100.0,
                    mark_price=99.8, upl=-20.0,
                    notional=9980.0, leverage=5.0,
                    liq_price=99.5,   # 0.3% away → imminent liquidation
                )
            }
        )
        local = make_local_state(positions={})

        result = run_with_mocks(okx, local)

        assert len(result.zombie_positions) == 1
        zp = result.zombie_positions[0]
        assert zp.inst_id == "SOL-USDT-SWAP"


class TestNormalState:
    """Scenario D — Perfect consistency, no issues."""

    def test_perfect_match_is_healthy(self):
        """
        Risk: None. Both OKX and local agree on DOGE long.
        Expected: is_healthy=True, all issue lists empty.
        """
        pos = {
            "DOGE-USDT-SWAP": PositionInfo(
                inst_id="DOGE-USDT-SWAP",
                pos=2000, direction="long",
                entry_price=0.1821,
                mark_price=0.1850, upl=5.8,
                notional=370.0, leverage=3.0,
                liq_price=0.15,
            )
        }
        okx = make_okx_state(positions=pos)
        local = make_local_state(positions={"DOGE-USDT-SWAP": MagicMock()})

        result = run_with_mocks(okx, local)

        assert result.is_healthy is True
        assert len(result.orphan_orders) == 0
        assert len(result.phantom_positions) == 0
        assert len(result.zombie_positions) == 0


class TestMultiIssue:
    """Scenario E — Multiple simultaneous inconsistencies."""

    def test_all_three_issue_types_categorized(self):
        """
        Risk: 3 independent failures at once.
          - Orphan: BTC limit order on OKX, local doesn't know
          - Phantom: SOL long in local, OKX shows zero
          - Zombie: AVAX long on OKX, local is IDLE
        Expected: all three types correctly detected.
        """
        okx = make_okx_state(
            positions={
                "AVAX-USDT-SWAP": PositionInfo(
                    inst_id="AVAX-USDT-SWAP",
                    pos=100, direction="long",
                    entry_price=35.0,
                    mark_price=34.0, upl=-10.0,
                    notional=3400.0, leverage=2.0,
                    liq_price=30.0,
                )
            },
            open_orders={
                "ORD-BTC-001": OrderInfo(
                    order_id="ORD-BTC-001", inst_id="BTC-USDT-SWAP",
                    side="sell", sz=10, price=61000,
                    algo_id=None, sl_trigger=None, tp_trigger=None,
                    ord_type="limit", state="live", created_at="2026-04-25T00:00:00",
                )
            },
        )
        local = make_local_state(
            positions={
                "SOL-USDT-SWAP": PositionInfo(
                    inst_id="SOL-USDT-SWAP",
                    pos=50, direction="long",
                    entry_price=95.0,
                    mark_price=0.0, upl=0.0,
                    notional=0.0, leverage=3.0,
                    liq_price=0.0,
                )
            },
            orders={},   # no record of ORD-BTC-001
        )

        result = run_with_mocks(okx, local)

        assert len(result.orphan_orders) == 1, \
            f"Expected 1 orphan, got {len(result.orphan_orders)}"
        assert len(result.phantom_positions) == 1, \
            f"Expected 1 phantom, got {len(result.phantom_positions)}"
        assert len(result.zombie_positions) == 1, \
            f"Expected 1 zombie, got {len(result.zombie_positions)}"
        assert result.is_healthy is False

        # Verify correct items flagged
        assert result.orphan_orders[0].order_id == "ORD-BTC-001"
        assert result.phantom_positions[0].inst_id == "SOL-USDT-SWAP"
        assert result.zombie_positions[0].inst_id == "AVAX-USDT-SWAP"
