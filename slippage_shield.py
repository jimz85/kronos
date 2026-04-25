"""
Slippage Shield Module
LiquidationShield: Manages liquidation price and enforces MAX_SL_DISTANCE constraints
SlippageCalculator: Computes slippage costs for trade execution
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

# ── Constants ────────────────────────────────────────────────────────────────────
MAX_SL_DISTANCE: float = 0.80  # SL must be within 80% of distance to liquidation
MAINTENANCE_MARGIN: float = 0.005  # 0.5% for USDT-margined perpetual
TAKER_FEE: float = 0.0005       # 0.05% taker fee
MAKER_FEE: float = 0.0002        # 0.02% maker fee
EXTREME_SLIPPAGE: float = 0.005  # 0.5% extreme slippage for trend-following orders


class PositionSide(Enum):
    LONG = "long"
    SHORT = "short"


class SLValidationError(Exception):
    """Raised when stop-loss validation fails"""
    pass


class SlippageError(Exception):
    """Raised when slippage calculation fails"""
    pass


@dataclass
class LiquidationResult:
    """Result of liquidation price computation"""
    liquidation_price: float
    margin_of_safety: float  # Distance from SL to liquidation
    distance_to_liquidation: float  # Distance from entry to liquidation
    is_safe: bool
    message: str


@dataclass
class SLValidationResult:
    """Result of stop-loss distance validation"""
    is_valid: bool
    sl_distance: float  # Actual SL distance from entry
    max_allowed_distance: float
    message: str


@dataclass
class SlippageResult:
    """Result of slippage cost calculation"""
    expected_price: float
    executed_price: float
    slippage_bps: float  # Basis points
    slippage_cost: float  # In quote currency
    slippage_pct: float  # Percentage


class LiquidationShield:
    """
    Protects positions from premature liquidation by validating
    stop-loss placement against MAX_SL_DISTANCE constraints.
    """
    
    def __init__(self, max_sl_distance: float = MAX_SL_DISTANCE):
        self.max_sl_distance = max_sl_distance
    
    def compute_liquidation_price(
        self,
        entry_price: float,
        leverage: float,
        side: PositionSide = PositionSide.LONG
    ) -> float:
        """
        Calculate liquidation price for a leveraged position (OKX USDT-margined perpetual).

        Formula (OKX official):
        - Long:  liquidation = entry_price * (1 - 1/leverage + maintenance_margin)
        - Short: liquidation = entry_price * (1 + 1/leverage - maintenance_margin)

        where maintenance_margin = 0.005 (0.5% for USDT-margined).

        Example: entry=$100,000, 10x LONG
          = 100000 * (1 - 0.10 + 0.005)
          = 100000 * 0.905
          = $90,500
        """
        if leverage <= 0:
            raise ValueError("Leverage must be positive")
        if entry_price <= 0:
            raise ValueError("Entry price must be positive")

        margin_ratio = 1.0 / leverage

        if side == PositionSide.LONG:
            # Long: price drops to (1/leverage - maintenance_margin) below entry
            return entry_price * (1.0 - margin_ratio + MAINTENANCE_MARGIN)
        else:  # SHORT
            # Short: price rises to (1/leverage - maintenance_margin) above entry
            return entry_price * (1.0 + margin_ratio - MAINTENANCE_MARGIN)
    
    def compute_liquidation_with_details(
        self,
        entry_price: float,
        leverage: float,
        stop_loss: float,
        side: PositionSide = PositionSide.LONG
    ) -> LiquidationResult:
        """
        Compute liquidation price with safety analysis.
        
        Args:
            entry_price: Position entry price
            leverage: Leverage multiplier
            stop_loss: Proposed stop-loss price
            side: Position side
            
        Returns:
            LiquidationResult with full details
        """
        liq_price = self.compute_liquidation_price(entry_price, leverage, side)
        
        if side == PositionSide.LONG:
            margin_of_safety = liq_price - stop_loss
            distance_to_liquidation = (entry_price - liq_price) / entry_price
            is_safe = stop_loss < liq_price
            message = f"LONG liquidation at ${liq_price:,.2f}, SL at ${stop_loss:,.2f}"
        else:
            margin_of_safety = stop_loss - liq_price
            distance_to_liquidation = (liq_price - entry_price) / entry_price
            is_safe = stop_loss > liq_price
            message = f"SHORT liquidation at ${liq_price:,.2f}, SL at ${stop_loss:,.2f}"
        
        return LiquidationResult(
            liquidation_price=liq_price,
            margin_of_safety=margin_of_safety,
            distance_to_liquidation=distance_to_liquidation,
            is_safe=is_safe,
            message=message
        )
    
    def validate_stop_loss_distance(
        self,
        entry_price: float,
        stop_loss: float,
        side: PositionSide = PositionSide.LONG
    ) -> SLValidationResult:
        """
        Validate that stop-loss is within MAX_SL_DISTANCE from entry.
        
        Args:
            entry_price: Position entry price
            stop_loss: Proposed stop-loss price
            side: Position side
            
        Returns:
            SLValidationResult with validation details
        """
        if entry_price <= 0 or stop_loss <= 0:
            raise ValueError("Prices must be positive")
        
        # Calculate actual distance
        if side == PositionSide.LONG:
            if stop_loss >= entry_price:
                return SLValidationResult(
                    is_valid=False,
                    sl_distance=0.0,
                    max_allowed_distance=self.max_sl_distance,
                    message="SL must be below entry for LONG positions"
                )
            sl_distance = (entry_price - stop_loss) / entry_price
        else:  # SHORT
            if stop_loss <= entry_price:
                return SLValidationResult(
                    is_valid=False,
                    sl_distance=0.0,
                    max_allowed_distance=self.max_sl_distance,
                    message="SL must be above entry for SHORT positions"
                )
            sl_distance = (stop_loss - entry_price) / entry_price
        
        is_valid = sl_distance <= self.max_sl_distance
        
        if is_valid:
            message = f"SL distance {sl_distance:.2%} is valid (max: {self.max_sl_distance:.2%})"
        else:
            message = f"SL distance {sl_distance:.2%} exceeds max {self.max_sl_distance:.2%}"
        
        return SLValidationResult(
            is_valid=is_valid,
            sl_distance=sl_distance,
            max_allowed_distance=self.max_sl_distance,
            message=message
        )
    
    def get_safe_stop_loss(
        self,
        entry_price: float,
        leverage: float,
        side: PositionSide = PositionSide.LONG,
        safety_buffer: float = 0.05  # 5% buffer above liquidation
    ) -> float:
        """
        Calculate a safe stop-loss that avoids premature liquidation.
        
        Args:
            entry_price: Position entry price
            leverage: Leverage multiplier
            side: Position side
            safety_buffer: Buffer between SL and liquidation (default 5%)
            
        Returns:
            Recommended stop-loss price
        """
        liq_price = self.compute_liquidation_price(entry_price, leverage, side)
        
        if side == PositionSide.LONG:
            # SL should be below liquidation but above it
            return liq_price * (1 - safety_buffer)
        else:
            return liq_price * (1 + safety_buffer)

    def enforce_sl(
        self,
        proposed_sl: float,
        entry_price: float,
        leverage: float,
        side: PositionSide = PositionSide.LONG,
    ) -> dict:
        """
        PRIMARY ENTRY POINT — validate and enforce the Liquidation Shield.

        Takes the strategy-computed SL and either approves or forcibly corrects it.

        MAX_SL_DISTANCE = 80% constraint:
          The SL must be within 80% of the distance from entry to liquidation.
          If proposed SL is beyond this, we move it to the 80% line.

        Returns dict:
          {
            'enforced_sl':      final SL to use,
            'was_adjusted':      True if original was overridden,
            'original_sl':       what strategy proposed,
            'liq_price':        exchange liquidation price,
            'max_safe_sl':      max SL allowed by shield,
            'adjustment_reason': human-readable reason,
            'safety_ratio':      actual margin above liq (fraction),
          }
        """
        liq_price = self.compute_liquidation_price(entry_price, leverage, side)

        if side == PositionSide.LONG:
            # Distance from entry down to liquidation
            liq_distance_dollar = entry_price - liq_price
            # Maximum SL distance from entry (80% of the full distance)
            max_sl_distance_dollar = liq_distance_dollar * self.max_sl_distance
            # Max safe SL: entry minus that amount
            max_safe_sl = entry_price - max_sl_distance_dollar

            was_unsafe = proposed_sl < max_safe_sl
            enforced_sl = max_safe_sl if was_unsafe else proposed_sl
            reason = (
                f"SL ${proposed_sl:,.0f} is {proposed_sl:.2f}m "
                f"beyond safe zone (${max_safe_sl:,.0f}). "
                f"Would be triggered BEFORE exchange liquidation at ${liq_price:,.0f}."
            ) if was_unsafe else "Within safe range."
            # Actual margin: (enforced_sl - liq_price) / liq_distance_dollar
            safety_ratio = (
                (enforced_sl - liq_price) / liq_distance_dollar
                if liq_distance_dollar > 0 else 1.0
            )

        else:  # SHORT
            liq_distance_dollar = liq_price - entry_price
            max_sl_distance_dollar = liq_distance_dollar * self.max_sl_distance
            max_safe_sl = entry_price + max_sl_distance_dollar

            was_unsafe = proposed_sl > max_safe_sl
            enforced_sl = max_safe_sl if was_unsafe else proposed_sl
            reason = (
                f"SL ${proposed_sl:,.0f} exceeds safe zone (${max_safe_sl:,.0f}). "
                f"Would be triggered BEFORE exchange liquidation at ${liq_price:,.0f}."
            ) if was_unsafe else "Within safe range."
            safety_ratio = (
                (liq_price - enforced_sl) / liq_distance_dollar
                if liq_distance_dollar > 0 else 1.0
            )

        return {
            "enforced_sl": round(enforced_sl, 2),
            "was_adjusted": was_unsafe,
            "original_sl": round(proposed_sl, 2),
            "liq_price": round(liq_price, 2),
            "max_safe_sl": round(max_safe_sl, 2),
            "adjustment_reason": reason,
            "safety_ratio": round(safety_ratio, 4),
        }


class SlippageCalculator:
    """
    Calculates slippage costs for trade execution.
    For Engine_Beta trend-following market orders: 0.5% extreme slippage + Taker fee.
    """

    def __init__(
        self,
        extreme_slippage: float = EXTREME_SLIPPAGE,
        taker_fee: float = TAKER_FEE,
        maker_fee: float = MAKER_FEE,
    ):
        self.extreme_slippage = extreme_slippage  # 0.5% for volatile breakouts
        self.taker_fee = taker_fee                # 0.05%
        self.maker_fee = maker_fee                 # 0.02%

    def execution_price(
        self, mid_price: float, side: str, order_type: str = "market"
    ) -> float:
        """
        Slippage-adjusted execution price.
        Market orders get extreme_slippage; limit orders get none.
        """
        slip = 0.0
        if order_type == "market":
            slip = self.extreme_slippage
        if side in ("buy", "long", "long_market"):
            return mid_price * (1 + slip)
        else:
            return mid_price * (1 - slip)

    def total_cost(self, mid_price: float, size: float, side: str) -> dict:
        """
        Full friction cost breakdown for a position.

        For a BUY (long) market order:
          entry_cost     = mid * (1 + slippage)     ← slippage ADDS to cost
          fee_cost       = mid * size * taker_fee
          slippage_cost  = mid * size * slippage

        Returns:
          {
            'entry_cost':    executed price,
            'slippage_cost': slippage in USDT,
            'fee_cost':      fee in USDT,
            'total_cost':    slippage + fee in USDT,
          }
        """
        entry_cost = self.execution_price(mid_price, side, "market")
        slippage_cost = abs(entry_cost - mid_price) * size
        fee_cost = mid_price * size * self.taker_fee
        return {
            "entry_cost": round(entry_cost, 4),
            "slippage_cost": round(slippage_cost, 4),
            "fee_cost": round(fee_cost, 4),
            "total_cost": round(slippage_cost + fee_cost, 4),
        }

    def expected_return_after_costs(
        self, entry_px: float, exit_px: float, size: float, side: str
    ) -> dict:
        """
        Net PnL after slippage and fees.
        If net_pnl is negative even for a 1% move, flag skip_trade=True.
        """
        # Gross PnL (before slippage, using mid prices)
        if side in ("buy", "long", "long_market"):
            gross_pnl = (exit_px - entry_px) * size
        else:
            gross_pnl = (entry_px - exit_px) * size

        # Friction
        slip = self.extreme_slippage
        fee = self.taker_fee
        slippage_cost = entry_px * size * slip
        fee_cost = entry_px * size * fee
        total_friction = slippage_cost + fee_cost

        net_pnl = gross_pnl - total_friction

        return {
            "gross_pnl": round(gross_pnl, 4),
            "slippage_cost": round(slippage_cost, 4),
            "fee_cost": round(fee_cost, 4),
            "net_pnl": round(net_pnl, 4),
            "skip_trade": net_pnl < 0,
        }
    
    def calculate_slippage(
        self,
        expected_price: float,
        position_size: float,
        market_impact_factor: float = 1.0,
        volatility_adjustment: float = 1.0
    ) -> SlippageResult:
        """
        Calculate slippage cost for a trade.
        
        Args:
            expected_price: Expected execution price
            position_size: Size of the position in quote currency
            market_impact_factor: Multiplier for market impact (1.0 = normal)
            volatility_adjustment: Multiplier based on volatility (1.0 = normal)
            
        Returns:
            SlippageResult with cost details
        """
        if expected_price <= 0:
            raise SlippageError("Expected price must be positive")
        if position_size <= 0:
            raise SlippageError("Position size must be positive")
        
        # Total slippage in bps = base * market_impact * volatility
        total_slippage_bps = self.base_slippage_bps * market_impact_factor * volatility_adjustment
        
        # Convert bps to decimal
        slippage_decimal = total_slippage_bps / 10000
        slippage_pct = total_slippage_bps / 100  # Convert to percentage
        
        # Calculate slippage cost
        slippage_cost = position_size * slippage_decimal
        
        # Simulate actual execution price (worst case for slippage)
        executed_price = expected_price * (1 + slippage_decimal)
        
        return SlippageResult(
            expected_price=expected_price,
            executed_price=executed_price,
            slippage_bps=total_slippage_bps,
            slippage_cost=slippage_cost,
            slippage_pct=slippage_pct
        )
    
    def estimate_slippage_for_order_book_depth(
        self,
        expected_price: float,
        position_size: float,
        order_book_imbalance: float = 0.0,
        depth_factor: float = 1.0
    ) -> SlippageResult:
        """
        Estimate slippage considering order book depth.
        
        Args:
            expected_price: Expected execution price
            position_size: Size of the position
            order_book_imbalance: -1 (heavy sell pressure) to +1 (heavy buy pressure)
            depth_factor: 1.0 = deep book, >1 = shallow book
            
        Returns:
            SlippageResult with estimated slippage
        """
        # Market impact increases with order book imbalance
        imbalance_impact = abs(order_book_imbalance) * 0.5
        volatility = depth_factor + imbalance_impact
        
        return self.calculate_slippage(
            expected_price=expected_price,
            position_size=position_size,
            market_impact_factor=depth_factor,
            volatility_adjustment=volatility
        )


def demo_slippage_shield():
    """
    Demonstration with:
    - Entry: $100,000
    - Leverage: 10x
    - Proposed SL: $85,000
    """
    print("=" * 60)
    print("SLIPPAGE SHIELD DEMONSTRATION")
    print("=" * 60)
    print()
    
    # Parameters
    entry_price = 100_000.0
    leverage = 10.0
    proposed_sl = 85_000.0
    position_size = 10_000.0  # $10k margin position
    
    print(f"SCENARIO:")
    print(f"  Entry Price:      ${entry_price:,.2f}")
    print(f"  Leverage:         {leverage:.0f}x")
    print(f"  Proposed SL:      ${proposed_sl:,.2f}")
    print(f"  Position Size:    ${position_size:,.2f}")
    print(f"  MAX_SL_DISTANCE:  {MAX_SL_DISTANCE:.0%}")
    print()
    
    # Initialize shields
    shield = LiquidationShield()
    slippage_calc = SlippageCalculator(base_slippage_bps=10.0)
    
    # === LIQUIDATION ANALYSIS ===
    print("-" * 60)
    print("LIQUIDATION PRICE ANALYSIS")
    print("-" * 60)
    
    liq_result = shield.compute_liquidation_with_details(
        entry_price=entry_price,
        leverage=leverage,
        stop_loss=proposed_sl,
        side=PositionSide.LONG
    )
    
    print(f"  Liquidation Price:    ${liq_result.liquidation_price:,.2f}")
    print(f"  Margin of Safety:     ${liq_result.margin_of_safety:,.2f}")
    print(f"  Distance to Liq:       {liq_result.distance_to_liquidation:.2%}")
    print(f"  Status:               {'✓ SAFE' if liq_result.is_safe else '✗ DANGER'}")
    print(f"  Message:              {liq_result.message}")
    print()
    
    # === SL VALIDATION ===
    print("-" * 60)
    print("STOP-LOSS VALIDATION")
    print("-" * 60)
    
    sl_validation = shield.validate_stop_loss_distance(
        entry_price=entry_price,
        stop_loss=proposed_sl,
        side=PositionSide.LONG
    )
    
    print(f"  SL Distance:          {sl_validation.sl_distance:.2%}")
    print(f"  Max Allowed:          {sl_validation.max_allowed_distance:.2%}")
    print(f"  Validation:           {'✓ PASSED' if sl_validation.is_valid else '✗ FAILED'}")
    print(f"  Message:              {sl_validation.message}")
    print()
    
    # === SAFE SL RECOMMENDATION ===
    print("-" * 60)
    print("SAFE STOP-LOSS RECOMMENDATION")
    print("-" * 60)
    
    safe_sl = shield.get_safe_stop_loss(
        entry_price=entry_price,
        leverage=leverage,
        side=PositionSide.LONG,
        safety_buffer=0.05
    )
    
    print(f"  Recommended Safe SL:  ${safe_sl:,.2f}")
    
    # Validate the safe SL
    safe_sl_validation = shield.validate_stop_loss_distance(
        entry_price=entry_price,
        stop_loss=safe_sl,
        side=PositionSide.LONG
    )
    print(f"  Safe SL Validation:   {'✓ PASSED' if safe_sl_validation.is_valid else '✗ FAILED'}")
    print()
    
    # === SLIPPAGE COST ANALYSIS ===
    print("-" * 60)
    print("SLIPPAGE COST ANALYSIS")
    print("-" * 60)
    
    # Normal conditions
    slippage_normal = slippage_calc.calculate_slippage(
        expected_price=entry_price,
        position_size=position_size,
        market_impact_factor=1.0,
        volatility_adjustment=1.0
    )
    
    print(f"NORMAL CONDITIONS:")
    print(f"  Expected Price:       ${slippage_normal.expected_price:,.2f}")
    print(f"  Executed Price:       ${slippage_normal.executed_price:,.2f}")
    print(f"  Slippage:             {slippage_normal.slippage_bps:.1f} bps ({slippage_normal.slippage_pct:.3f}%)")
    print(f"  Slippage Cost:        ${slippage_normal.slippage_cost:,.2f}")
    print()
    
    # High volatility conditions
    slippage_volatile = slippage_calc.calculate_slippage(
        expected_price=entry_price,
        position_size=position_size,
        market_impact_factor=1.5,
        volatility_adjustment=2.0
    )
    
    print(f"HIGH VOLATILITY CONDITIONS:")
    print(f"  Expected Price:       ${slippage_volatile.expected_price:,.2f}")
    print(f"  Executed Price:       ${slippage_volatile.executed_price:,.2f}")
    print(f"  Slippage:             {slippage_volatile.slippage_bps:.1f} bps ({slippage_volatile.slippage_pct:.3f}%)")
    print(f"  Slippage Cost:        ${slippage_volatile.slippage_cost:,.2f}")
    print()
    
    # === SUMMARY ===
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    issues = []
    
    if not sl_validation.is_valid:
        issues.append(f"SL distance {sl_validation.sl_distance:.2%} exceeds MAX_SL_DISTANCE {MAX_SL_DISTANCE:.0%}")
    
    if not liq_result.is_safe:
        issues.append("Stop-loss is at or beyond liquidation price - HIGH RISK!")
    
    if issues:
        print("⚠️  WARNINGS:")
        for issue in issues:
            print(f"    - {issue}")
    else:
        print("✓ All validations passed")
        print(f"  - Proposed SL ${proposed_sl:,.2f} is within safe limits")
        print(f"  - Liquidation at ${liq_result.liquidation_price:,.2f} provides ${liq_result.margin_of_safety:,.2f} buffer")
    
    print()
    print(f"RECOMMENDATION:")
    if sl_validation.sl_distance < 0.20:
        print(f"  Consider wider SL (closer to ${safe_sl:,.2f}) for better risk/reward")
    else:
        print(f"  Current SL placement is acceptable")
    
    print()
    print("=" * 60)
    
    return {
        "liquidation_result": liq_result,
        "sl_validation": sl_validation,
        "safe_sl": safe_sl,
        "slippage_normal": slippage_normal,
        "slippage_volatile": slippage_volatile
    }


if __name__ == "__main__":
    results = demo_slippage_shield()
