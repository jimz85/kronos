"""
Dynamic Trailing Stop Module
============================
Adaptive trailing stop that adjusts based on market volatility and regime.
"""
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Tuple
import math
import logging

logger = logging.getLogger(__name__)


class TrailingMode(Enum):
    FIXED = "fixed"              # Static percentage trail
    ATR_BASED = "atr_based"      # Trail using ATR multiples
    VOLATILITY = "volatility"    # Dynamic based on price std dev
    SUPERTREND = "supertrend"    # Supertrend-style trail


@dataclass
class TrailingConfig:
    mode: TrailingMode = TrailingMode.ATR_BASED
    trail_percent: float = 0.02       # 2% for fixed mode
    atr_period: int = 14              # ATR lookback period
    atr_multiplier: float = 2.5       # ATR multiple for trail
    volatility_window: int = 20       # Window for volatility calc
    volatility_multiplier: float = 2.0
    min_trail_percent: float = 0.01   # 1% absolute minimum trail
    max_trail_percent: float = 0.10   # 10% absolute maximum trail
    use_dynamic_multiplier: bool = True  # Adjust ATR mult based on regime


@dataclass
class TrailingState:
    active: bool = False
    trigger_price: float = 0.0        # Price that activated the trail
    current_stop: float = 0.0         # Current trailing stop level
    highest_price: float = 0.0       # Highest price since activation
    lowest_price: float = float('inf')  # Lowest since activation (for short)
    atr_value: float = 0.0            # Current ATR
    volatility: float = 0.0           # Current volatility measure
    direction: int = 0                # 1 for long, -1 for short
    trails_count: int = 0             # Number of times stop was raised/lowered


class DynamicTrailingStop:
    """
    Dynamic trailing stop that adapts to market conditions.
    
    Supports multiple trailing modes:
    - FIXED: Simple percentage-based trailing stop
    - ATR_BASED: Uses Average True Range for adaptive trailing
    - VOLATILITY: Uses rolling standard deviation
    - SUPERTREND: Supertrend-style based on ATR with upper/lower bands
    """
    
    def __init__(
        self,
        name: str = "default",
        config: Optional[TrailingConfig] = None
    ):
        self.name = name
        self.config = config or TrailingConfig()
        self._state = TrailingState()
        self._price_history = []
        self._atr_history = []
        
    @property
    def state(self) -> TrailingState:
        return self._state
    
    @property
    def current_stop_price(self) -> float:
        """Return the current stop price, 0 if inactive."""
        return self._state.current_stop if self._state.active else 0.0
    
    def _update_atr(self, high: float, low: float, prev_close: float) -> float:
        """Calculate True Range and update ATR."""
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        self._atr_history.append(tr)
        
        # Keep ATR period length
        if len(self._atr_history) > self.config.atr_period * 2:
            self._atr_history = self._atr_history[-self.config.atr_period:]
            
        # Calculate ATR using smoothed average
        if len(self._atr_history) >= self.config.atr_period:
            return sum(self._atr_history[-self.config.atr_period:]) / self.config.atr_period
        return tr
    
    def _calculate_atr(self, prices: list) -> float:
        """Calculate ATR from OHLC price list."""
        if len(prices) < 2:
            return 0.0
        tr_list = []
        for i in range(1, len(prices)):
            high = prices[i]['high'] if isinstance(prices[i], dict) else prices[i] * 1.001
            low = prices[i]['low'] if isinstance(prices[i], dict) else prices[i] * 0.999
            prev_close = prices[i-1]['close'] if isinstance(prices[i-1], dict) else prices[i-1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_list.append(tr)
        return sum(tr_list[-self.config.atr_period:]) / self.config.atr_period if tr_list else 0.0
    
    def _calculate_volatility(self, prices: list) -> float:
        """Calculate rolling standard deviation of returns."""
        if len(prices) < 3:
            return 0.0
        closes = [p['close'] if isinstance(p, dict) else p for p in prices]
        returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        window = returns[-self.config.volatility_window:]
        if len(window) < 2:
            return 0.0
        mean = sum(window) / len(window)
        variance = sum((r - mean) ** 2 for r in window) / len(window)
        return math.sqrt(variance)
    
    def activate(
        self,
        entry_price: float,
        direction: int,
        current_price: float,
        high_price: Optional[float] = None,
        low_price: Optional[float] = None,
        atr: Optional[float] = None
    ):
        """
        Activate the trailing stop after entry.
        
        Args:
            entry_price: The entry/execution price
            direction: 1 for long, -1 for short
            current_price: Current market price
            high_price: Period high (for ATR calc)
            low_price: Period low (for ATR calc)
            atr: Pre-calculated ATR value (optional)
        """
        if atr is not None:
            self._state.atr_value = atr
        elif high_price is not None and low_price is not None:
            self._state.atr_value = self._update_atr(
                high_price, low_price, entry_price
            )
            
        self._state.direction = direction
        self._state.active = True
        self._state.trigger_price = entry_price
        self._state.highest_price = max(entry_price, current_price, high_price or 0)
        self._state.lowest_price = min(entry_price, current_price, low_price or float('inf'))
        self._state.trails_count = 0
        
        # Calculate initial stop
        self._update_stop(current_price)
        
        logger.info(
            f"TrailingStop[{self.name}]: Activated at {entry_price}, "
            f"direction={'LONG' if direction == 1 else 'SHORT'}, "
            f"initial_stop={self._state.current_stop:.4f}"
        )
        
    def _get_trail_multiplier(self, current_price: float) -> float:
        """Get dynamic ATR multiplier based on conditions."""
        if not self.config.use_dynamic_multiplier:
            return self.config.atr_multiplier
            
        # Adjust based on how far price has moved
        if self._state.direction == 1:  # Long
            move_percent = (self._state.highest_price - self._state.trigger_price) / self._state.trigger_price
        else:  # Short
            move_percent = (self._state.trigger_price - self._state.lowest_price) / self._state.trigger_price
            
        # Increase multiplier as profit increases (more room to breathe)
        if move_percent > 0.10:  # >10% profit
            return self.config.atr_multiplier * 1.3
        elif move_percent > 0.05:  # >5% profit
            return self.config.atr_multiplier * 1.15
        return self.config.atr_multiplier
    
    def _calculate_fixed_trail(self, price: float) -> float:
        """Calculate fixed percentage trail."""
        trail = price * self.config.trail_percent
        trail = max(trail, price * self.config.min_trail_percent)
        trail = min(trail, price * self.config.max_trail_percent)
        return trail
    
    def _calculate_atr_trail(self, price: float) -> float:
        """Calculate ATR-based trail."""
        multiplier = self._get_trail_multiplier(price)
        trail = self._state.atr_value * multiplier
        
        # Convert to percentage for bounds checking
        trail_percent = trail / price if price > 0 else 0
        trail_percent = max(trail_percent, self.config.min_trail_percent)
        trail_percent = min(trail_percent, self.config.max_trail_percent)
        
        return price * trail_percent
    
    def _calculate_volatility_trail(self, price: float) -> float:
        """Calculate volatility-based trail."""
        trail = self._state.volatility * self.config.volatility_multiplier * price
        trail_percent = trail / price if price > 0 else 0
        trail_percent = max(trail_percent, self.config.min_trail_percent)
        trail_percent = min(trail_percent, self.config.max_trail_percent)
        return price * trail_percent
    
    def _update_stop(self, current_price: float):
        """Update the trailing stop based on current mode and price."""
        if self._state.direction == 1:  # Long
            if current_price > self._state.highest_price:
                self._state.highest_price = current_price
                
            if self.config.mode == TrailingMode.FIXED:
                trail_amount = self._calculate_fixed_trail(self._state.highest_price)
            elif self.config.mode == TrailingMode.ATR_BASED:
                trail_amount = self._calculate_atr_trail(self._state.highest_price)
            elif self.config.mode == TrailingMode.VOLATILITY:
                trail_amount = self._calculate_volatility_trail(self._state.highest_price)
            else:
                trail_amount = self._calculate_atr_trail(self._state.highest_price)
                
            new_stop = self._state.highest_price - trail_amount
            
            # Only raise stop, never lower (but first time always sets)
            if new_stop > self._state.current_stop or self._state.current_stop == 0:
                self._state.current_stop = new_stop
                self._state.trails_count += 1
                
        else:  # Short
            if current_price < self._state.lowest_price:
                self._state.lowest_price = current_price
                
            if self.config.mode == TrailingMode.FIXED:
                trail_amount = self._calculate_fixed_trail(self._state.lowest_price)
            elif self.config.mode == TrailingMode.ATR_BASED:
                trail_amount = self._calculate_atr_trail(self._state.lowest_price)
            elif self.config.mode == TrailingMode.VOLATILITY:
                trail_amount = self._calculate_volatility_trail(self._state.lowest_price)
            else:
                trail_amount = self._calculate_atr_trail(self._state.lowest_price)
                
            new_stop = self._state.lowest_price + trail_amount
            
            # Only lower stop for shorts (but first time always sets)
            if new_stop < self._state.current_stop or self._state.current_stop == 0:
                self._state.current_stop = new_stop
                self._state.trails_count += 1
    
    def update(
        self,
        current_price: float,
        high_price: Optional[float] = None,
        low_price: Optional[float] = None,
        atr: Optional[float] = None
    ) -> Tuple[float, bool]:
        """
        Update trailing stop with new price data.
        
        Returns:
            Tuple of (stop_price, was_raised/lowered)
        """
        if not self._state.active:
            return 0.0, False
            
        prev_stop = self._state.current_stop
        
        # Update ATR if provided
        if atr is not None:
            self._state.atr_value = atr
        elif high_price is not None and low_price is not None:
            self._state.atr_value = self._update_atr(high_price, low_price, current_price)
            
        # Update volatility
        self._price_history.append({'close': current_price, 'high': high_price or current_price, 'low': low_price or current_price})
        if len(self._price_history) > self.config.volatility_window * 2:
            self._price_history = self._price_history[-self.config.volatility_window:]
        self._state.volatility = self._calculate_volatility(self._price_history)
        
        # Update track price
        if self._state.direction == 1 and high_price and high_price > self._state.highest_price:
            self._state.highest_price = high_price
        elif self._state.direction == -1 and low_price and low_price < self._state.lowest_price:
            self._state.lowest_price = low_price
            
        self._update_stop(current_price)
        
        moved = abs(self._state.current_stop - prev_stop) > 0.0001
        return self._state.current_stop, moved
    
    def is_stop_hit(self, current_price: float) -> bool:
        """Check if the trailing stop has been hit."""
        if not self._state.active:
            return False
            
        if self._state.direction == 1:  # Long
            return current_price <= self._state.current_stop
        else:  # Short
            return current_price >= self._state.current_stop
    
    def deactivate(self):
        """Manually deactivate the trailing stop."""
        self._state = TrailingState()
        logger.info(f"TrailingStop[{self.name}]: Deactivated")
        
    def get_unrealized_pnl(
        self,
        entry_price: float,
        current_price: float
    ) -> float:
        """Calculate unrealized PnL in percent."""
        if self._state.direction == 0:
            return 0.0
        pnl = (current_price - entry_price) / entry_price * self._state.direction
        return pnl * 100  # percentage
    
    def __repr__(self):
        if not self._state.active:
            return f"DynamicTrailingStop({self.name}, INACTIVE)"
        return (f"DynamicTrailingStop({self.name}, "
                f"stop={self._state.current_stop:.4f}, "
                f"mode={self.config.mode.value}, "
                f"trails={self._state.trails_count}, "
                f"{'LONG' if self._state.direction == 1 else 'SHORT'})")


# =============================================================================
# DEMO
# =============================================================================

def demo():
    """Demonstrate dynamic trailing stop behavior."""
    print("=" * 60)
    print("DYNAMIC TRAILING STOP DEMO")
    print("=" * 60)
    
    # Create trailing stop with ATR mode
    config = TrailingConfig(
        mode=TrailingMode.ATR_BASED,
        atr_period=14,
        atr_multiplier=2.5,
        min_trail_percent=0.015,
        max_trail_percent=0.08
    )
    ts = DynamicTrailingStop("BTC_USD", config=config)
    
    # Simulate price sequence for a LONG position
    print("\n--- LONG POSITION SIMULATION ---")
    print(f"Mode: {config.mode.value}")
    print(f"ATR Multiplier: {config.atr_multiplier}")
    print(f"Trail Percent Range: {config.min_trail_percent*100}% - {config.max_trail_percent*100}%")
    print()
    
    entry_price = 50000.0
    atr_value = 500.0  # Simulated ATR
    
    print(f"Entry: ${entry_price:.2f}")
    print(f"ATR: ${atr_value:.2f}")
    print()
    
    # Simulate price movement
    price_sequence = [
        (50500, 50800, 50200),   # price, high, low
        (51000, 51500, 50500),
        (50800, 51200, 50600),  # slight pullback
        (52000, 52500, 51800),
        (53000, 53500, 52800),
        (54000, 54500, 53500),
        (53500, 54200, 53200),  # bigger pullback
        (55000, 55500, 54800),
        (54500, 55200, 54300),  # another pullback
    ]
    
    ts.activate(entry_price, 1, entry_price, entry_price, entry_price, atr_value)
    print(f"Activated: {ts}")
    print()
    
    print(f"{'Step':>4} | {'Price':>8} | {'High':>8} | {'Stop':>8} | {'Trail?':>6} | {'Hit?':>5}")
    print("-" * 55)
    
    for i, (price, high, low) in enumerate(price_sequence):
        stop, moved = ts.update(price, high, low)
        moved_str = "YES" if moved else "-"
        hit = "HIT!" if ts.is_stop_hit(price) else "-"
        print(f"{i+1:>4} | ${price:>7.0f} | ${high:>7.0f} | ${stop:>7.0f} | {moved_str:>6} | {hit:>5}")
        
    print()
    print(f"Final stop: ${ts.current_stop_price:.2f}")
    print(f"Price trails count: {ts.state.trails_count}")
    
    # Now test SHORT position
    print("\n--- SHORT POSITION SIMULATION ---")
    ts2 = DynamicTrailingStop("ETH_USD", config=config)
    entry_price = 3000.0
    atr_value = 50.0
    
    print(f"Entry: ${entry_price:.2f}")
    print()
    
    short_prices = [
        (2950, 3020, 2940),
        (2900, 2980, 2890),
        (2920, 2960, 2900),  # squeeze
        (2850, 2930, 2840),
        (2800, 2880, 2790),
        (2750, 2820, 2740),
        (2780, 2800, 2720),  # bear bounce
        (2700, 2780, 2690),
        (2650, 2720, 2640),
    ]
    
    ts2.activate(entry_price, -1, entry_price, entry_price, entry_price, atr_value)
    print(f"Activated: {ts2}")
    print()
    
    print(f"{'Step':>4} | {'Price':>8} | {'Low':>8} | {'Stop':>8} | {'Trail?':>6} | {'Hit?':>5}")
    print("-" * 55)
    
    for i, (price, high, low) in enumerate(short_prices):
        stop, moved = ts2.update(price, high, low)
        moved_str = "YES" if moved else "-"
        hit = "HIT!" if ts2.is_stop_hit(price) else "-"
        print(f"{i+1:>4} | ${price:>7.0f} | ${low:>7.0f} | ${stop:>7.0f} | {moved_str:>6} | {hit:>5}")
        
    print()
    print(f"Final stop: ${ts2.current_stop_price:.2f}")
    
    print("\n" + "=" * 60)
    print("DEMO COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    demo()
