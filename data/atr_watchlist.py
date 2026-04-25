"""
Kronos ATR Watchlist

ATR-based watchlist monitoring system.
Tracks assets with significant ATR breakouts and provides alerts.

Based on kronos_v2 WilderATR implementation.
"""

import os
import sys
import json
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import random

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


class SignalDirection(Enum):
    """Trade direction signals."""
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


@dataclass
class ATRConfig:
    """ATR calculation configuration."""
    period: int = 14
    alert_threshold: float = 1.5  # ATR multiplier for alerts
    breakout_threshold: float = 2.0  # ATR multiplier for breakouts


@dataclass
class ATRReading:
    """Single ATR reading for an asset."""
    symbol: str
    timestamp: datetime
    close: float
    high: float
    low: float
    atr: float
    atr_percent: float  # ATR as percentage of price
    prev_atr: float = 0.0
    atr_change_pct: float = 0.0


@dataclass
class WatchlistEntry:
    """Entry in the ATR watchlist."""
    symbol: str
    direction: SignalDirection
    atr_reading: ATRReading
    strength: float  # 0.0 to 1.0
    alert_level: str  # 'normal', 'elevated', 'breakout'
    reason: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'direction': self.direction.value,
            'atr': self.atr_reading.atr,
            'atr_percent': self.atr_reading.atr_percent,
            'strength': self.strength,
            'alert_level': self.alert_level,
            'reason': self.reason,
            'timestamp': self.atr_reading.timestamp.isoformat()
        }


class WilderATR:
    """Average True Range using Wilder smoothing.
    
    From kronos_v2/core/factor_calculator.py - P0-B4 fix.
    """
    
    def __init__(self, period: int = 14):
        self.period = period
        self.tr_list: List[float] = []
        self.atr: Optional[float] = None
    
    def update(self, high: float, low: float, close: float) -> float:
        """Update ATR with new candle data."""
        if len(self.tr_list) == 0:
            tr = high - low
        else:
            prev_close = self.tr_list[-1] if self.tr_list else close
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
        
        self.tr_list.append(tr)
        
        if len(self.tr_list) < self.period:
            return tr
        
        if self.atr is None:
            self.atr = sum(self.tr_list[-self.period:]) / self.period
        else:
            self.atr = (self.atr * (self.period - 1) + tr) / self.period
        
        return self.atr
    
    def get_value(self) -> float:
        """Get current ATR value."""
        if self.atr is None:
            return self.tr_list[-1] if self.tr_list else 0.0
        return self.atr


class ATRCalculator:
    """Calculate ATR and related metrics for assets."""
    
    def __init__(self, period: int = 14):
        self.period = period
        self.atr_engines: Dict[str, WilderATR] = {}
    
    def get_engine(self, symbol: str) -> WilderATR:
        """Get or create ATR engine for symbol."""
        if symbol not in self.atr_engines:
            self.atr_engines[symbol] = WilderATR(period=self.period)
        return self.atr_engines[symbol]
    
    def calculate(
        self,
        symbol: str,
        highs: List[float],
        lows: List[float],
        closes: List[float],
        timestamps: Optional[List[datetime]] = None
    ) -> List[ATRReading]:
        """Calculate ATR readings for historical data."""
        if timestamps is None:
            timestamps = [datetime.now() - timedelta(minutes=5 * i) for i in range(len(closes))]
        
        readings = []
        engine = WilderATR(period=self.period)
        prev_atr = 0.0
        
        for i in range(len(closes)):
            high = highs[i]
            low = lows[i]
            close = closes[i]
            
            atr = engine.update(high, low, close)
            atr_percent = (atr / close * 100) if close > 0 else 0.0
            atr_change_pct = ((atr - prev_atr) / prev_atr * 100) if prev_atr > 0 else 0.0
            
            reading = ATRReading(
                symbol=symbol,
                timestamp=timestamps[i],
                close=close,
                high=high,
                low=low,
                atr=atr,
                atr_percent=atr_percent,
                prev_atr=prev_atr,
                atr_change_pct=atr_change_pct
            )
            readings.append(reading)
            prev_atr = atr
        
        return readings
    
    def latest_reading(self, symbol: str) -> Optional[ATRReading]:
        """Get latest ATR reading for symbol."""
        engine = self.get_engine(symbol)
        if engine.atr is None and len(engine.tr_list) == 0:
            return None
        
        # Return mock reading - in real impl would track last price
        return None


class ATRWatchlist:
    """
    ATR-based watchlist monitoring system.
    
    Monitors assets for:
    - ATR breakouts (volatility expansion)
    - ATR contractions (low volatility)
    - Directional signals based on price vs ATR
    """
    
    def __init__(self, config: Optional[ATRConfig] = None):
        self.config = config or ATRConfig()
        self.calculator = ATRCalculator(period=self.config.period)
        self.watchlist: Dict[str, WatchlistEntry] = {}
        self.alert_history: List[Dict[str, Any]] = []
    
    def process_symbol(
        self,
        symbol: str,
        highs: List[float],
        lows: List[float],
        closes: List[float]
    ) -> Optional[WatchlistEntry]:
        """Process symbol and add to watchlist if significant."""
        if len(closes) < self.config.period:
            logger.warning(f"Insufficient data for {symbol}")
            return None
        
        # Calculate ATR
        readings = self.calculator.calculate(symbol, highs, lows, closes)
        latest = readings[-1]
        
        # Determine alert level
        atr_ratio = latest.atr / (sum(r.atr for r in readings[-self.config.period:]) / self.config.period)
        
        if atr_ratio >= self.config.breakout_threshold:
            alert_level = 'breakout'
            strength = min(1.0, (atr_ratio - self.config.breakout_threshold) / 2.0)
        elif atr_ratio >= self.config.alert_threshold:
            alert_level = 'elevated'
            strength = min(1.0, (atr_ratio - self.config.alert_threshold) / 1.5)
        else:
            alert_level = 'normal'
            strength = atr_ratio / self.config.alert_threshold
        
        # Determine direction
        recent_closes = closes[-10:]
        if len(recent_closes) >= 2:
            price_change = (recent_closes[-1] - recent_closes[0]) / recent_closes[0]
            if price_change > 0.02:  # >2% up
                direction = SignalDirection.LONG
                reason = f"Price up {price_change*100:.1f}%, ATR expanding"
            elif price_change < -0.02:  # >2% down
                direction = SignalDirection.SHORT
                reason = f"Price down {abs(price_change)*100:.1f}%, ATR expanding"
            else:
                direction = SignalDirection.NEUTRAL
                reason = f"ATR breakout detected (ratio={atr_ratio:.2f})"
        else:
            direction = SignalDirection.NEUTRAL
            reason = f"ATR ratio={atr_ratio:.2f}"
        
        entry = WatchlistEntry(
            symbol=symbol,
            direction=direction,
            atr_reading=latest,
            strength=strength,
            alert_level=alert_level,
            reason=reason
        )
        
        self.watchlist[symbol] = entry
        
        if alert_level in ('elevated', 'breakout'):
            self._record_alert(entry)
        
        return entry
    
    def _record_alert(self, entry: WatchlistEntry) -> None:
        """Record alert to history."""
        alert = {
            'timestamp': datetime.now().isoformat(),
            'symbol': entry.symbol,
            'alert_level': entry.alert_level,
            'strength': entry.strength,
            'atr': entry.atr_reading.atr,
            'atr_percent': entry.atr_reading.atr_percent
        }
        self.alert_history.append(alert)
        logger.info(f"ALERT [{entry.alert_level.upper()}] {entry.symbol}: {entry.reason}")
    
    def get_watchlist(self, alert_level: Optional[str] = None) -> List[WatchlistEntry]:
        """Get current watchlist, optionally filtered by alert level."""
        entries = list(self.watchlist.values())
        if alert_level:
            entries = [e for e in entries if e.alert_level == alert_level]
        return sorted(entries, key=lambda e: e.strength, reverse=True)
    
    def get_alerts(self, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Get alert history, optionally since a specific time."""
        if since is None:
            return self.alert_history
        return [
            a for a in self.alert_history
            if datetime.fromisoformat(a['timestamp']) > since
        ]
    
    def save_state(self, filepath: str) -> None:
        """Save watchlist state to file."""
        state = {
            'timestamp': datetime.now().isoformat(),
            'config': {
                'period': self.config.period,
                'alert_threshold': self.config.alert_threshold,
                'breakout_threshold': self.config.breakout_threshold
            },
            'watchlist': {symbol: entry.to_dict() for symbol, entry in self.watchlist.items()},
            'alert_count': len(self.alert_history)
        }
        with open(filepath, 'w') as f:
            json.dump(state, f, indent=2)
        logger.info(f"Saved watchlist state to {filepath}")
    
    def load_state(self, filepath: str) -> None:
        """Load watchlist state from file."""
        with open(filepath, 'r') as f:
            state = json.load(f)
        
        logger.info(f"Loaded watchlist state from {filepath}")


def generate_mock_data(symbol: str, base_price: float = 100.0, length: int = 50) -> Dict[str, List[float]]:
    """Generate mock OHLCV data for testing."""
    import random
    
    closes = [base_price]
    for _ in range(length - 1):
        change = random.gauss(0, base_price * 0.02)
        closes.append(closes[-1] + change)
    
    highs = [c + abs(random.gauss(0, base_price * 0.01)) for c in closes]
    lows = [c - abs(random.gauss(0, base_price * 0.01)) for c in closes]
    volumes = [random.uniform(1000, 5000) for _ in range(length)]
    
    return {
        'highs': highs,
        'lows': lows,
        'closes': closes,
        'volumes': volumes
    }


def run_demo():
    """Demonstrate ATR watchlist functionality."""
    logger.info("=" * 60)
    logger.info("Kronos ATR Watchlist - Demo")
    logger.info("=" * 60)
    
    # Create watchlist with custom config
    config = ATRConfig(
        period=14,
        alert_threshold=1.5,
        breakout_threshold=2.0
    )
    watchlist = ATRWatchlist(config=config)
    
    # Process multiple symbols
    symbols = ['BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'DOGE-USDT', 'AVAX-USDT']
    base_prices = [65000, 3500, 150, 0.15, 35]
    
    logger.info(f"Processing {len(symbols)} symbols...")
    
    for symbol, base_price in zip(symbols, base_prices):
        # Simulate volatility spike for some symbols
        if symbol in ('BTC-USDT', 'SOL-USDT'):
            # Generate with higher volatility (breakout)
            data = generate_mock_data(symbol, base_price, 50)
            # Add extra volatility
            data['highs'] = [h + abs(random.gauss(0, base_price * 0.03)) for h in data['highs']]
            data['lows'] = [l - abs(random.gauss(0, base_price * 0.03)) for l in data['lows']]
        else:
            data = generate_mock_data(symbol, base_price, 50)
        
        entry = watchlist.process_symbol(
            symbol,
            data['highs'],
            data['lows'],
            data['closes']
        )
        
        if entry:
            logger.info(
                f"  {symbol}: ATR={entry.atr_reading.atr:.2f} "
                f"({entry.atr_reading.atr_percent:.2f}%), "
                f"Level={entry.alert_level}, "
                f"Signal={entry.direction.value}"
            )
    
    # Report watchlist
    logger.info("=" * 60)
    logger.info("Current Watchlist (sorted by strength):")
    
    entries = watchlist.get_watchlist()
    for entry in entries:
        logger.info(
            f"  [{entry.alert_level.upper():9}] {entry.symbol:12} "
            f"strength={entry.strength:.2f} {entry.direction.value}"
        )
    
    # Show elevated/breakout alerts
    logger.info("-" * 60)
    logger.info("Active Alerts:")
    
    for level in ['breakout', 'elevated']:
        level_entries = watchlist.get_watchlist(alert_level=level)
        if level_entries:
            logger.info(f"  {level.upper()}:")
            for entry in level_entries:
                logger.info(f"    - {entry.symbol}: {entry.reason}")
    
    # Save state
    state_path = os.path.expanduser("~/kronos/data/atr_watchlist_state.json")
    watchlist.save_state(state_path)
    
    logger.info("=" * 60)
    logger.info("Demo complete!")
    
    return watchlist


if __name__ == '__main__':
    run_demo()
