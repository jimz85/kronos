#!/usr/bin/env python3
"""
shadow_validator.py - Shadow Model Comparison System
=====================================================

Compares ML-based signals vs rule-based signals to validate model quality.
Tracks discrepancies and records trade outcomes for performance analysis.

Key Classes:
    - ShadowSignal: Signal data from both ML and rule-based systems
    - SignalDiscrepancy: Tracks differences between signal sources

Key Functions:
    - compare_signals(): Compare ML vs rule-based signals
    - record_outcome(): Record trade result for later analysis
    - get_performance_summary(): Get comparison statistics

Version: 1.0.0
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from collections import defaultdict

logger = logging.getLogger('kronos.shadow_validator')


class SignalSource(Enum):
    """Source of the trading signal."""
    ML = "ml"
    RULE_BASED = "rule_based"
    BOTH_AGREE = "both_agree"
    CONFLICT = "conflict"


class SignalDirection(Enum):
    """Direction of the signal."""
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class DiscrepancyType(Enum):
    """Type of discrepancy detected."""
    NONE = "none"
    DIRECTION_MISMATCH = "direction_mismatch"
    CONFIDENCE_DIVERGENCE = "confidence_divergence"
    TIMING_LAG = "timing_lag"
    MISSING_SIGNAL = "missing_signal"


@dataclass
class ShadowSignal:
    """
    Represents a signal from either ML or rule-based system.
    
    Attributes:
        symbol: Trading pair symbol (e.g., 'BTC-USDT')
        timestamp: When the signal was generated
        direction: Signal direction (long/short/neutral)
        confidence: Confidence score (0.0 to 1.0)
        source: Signal source (ML or RULE_BASED)
        features: Dict of features used for signal generation
    """
    symbol: str
    timestamp: datetime
    direction: SignalDirection
    confidence: float
    source: SignalSource
    features: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate signal data after initialization."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be between 0.0 and 1.0, got {self.confidence}")
        if not self.symbol:
            raise ValueError("Symbol cannot be empty")


@dataclass
class SignalDiscrepancy:
    """
    Tracks a discrepancy between ML and rule-based signals.
    
    Attributes:
        symbol: Trading pair symbol
        timestamp: When the discrepancy was detected
        ml_signal: The ML-generated signal
        rule_signal: The rule-based signal
        discrepancy_type: Type of discrepancy found
        severity: How significant is the discrepancy (0.0 to 1.0)
        description: Human-readable description of the discrepancy
    """
    symbol: str
    timestamp: datetime
    ml_signal: Optional[ShadowSignal]
    rule_signal: Optional[ShadowSignal]
    discrepancy_type: DiscrepancyType
    severity: float = 0.0
    description: str = ""
    
    def __post_init__(self):
        """Calculate severity and description if not provided."""
        if not self.description:
            self.description = self._generate_description()
        if self.severity == 0.0:
            self.severity = self._calculate_severity()
    
    def _generate_description(self) -> str:
        """Generate a human-readable description of the discrepancy."""
        if self.discrepancy_type == DiscrepancyType.NONE:
            return "No discrepancy - signals agree"
        
        parts = [f"Discrepancy on {self.symbol} at {self.timestamp.isoformat()}"]
        parts.append(f"Type: {self.discrepancy_type.value}")
        
        if self.ml_signal and self.rule_signal:
            parts.append(
                f"ML: {self.ml_signal.direction.value} ({self.ml_signal.confidence:.2f}), "
                f"Rule: {self.rule_signal.direction.value} ({self.rule_signal.confidence:.2f})"
            )
        elif self.ml_signal:
            parts.append(f"Rule signal missing, ML: {self.ml_signal.direction.value}")
        elif self.rule_signal:
            parts.append(f"ML signal missing, Rule: {self.rule_signal.direction.value}")
        
        return "; ".join(parts)
    
    def _calculate_severity(self) -> float:
        """Calculate severity score based on discrepancy type and magnitude."""
        if self.discrepancy_type == DiscrepancyType.NONE:
            return 0.0
        
        base_severity = {
            DiscrepancyType.DIRECTION_MISMATCH: 1.0,
            DiscrepancyType.CONFIDENCE_DIVERGENCE: 0.5,
            DiscrepancyType.TIMING_LAG: 0.3,
            DiscrepancyType.MISSING_SIGNAL: 0.7,
        }.get(self.discrepancy_type, 0.5)
        
        # Adjust severity based on confidence difference if both signals exist
        if self.ml_signal and self.rule_signal:
            conf_diff = abs(self.ml_signal.confidence - self.rule_signal.confidence)
            adjusted_severity = base_severity * (0.5 + conf_diff)
            return min(1.0, adjusted_severity)
        
        return base_severity


@dataclass
class TradeOutcome:
    """
    Records the outcome of a trade for performance tracking.
    
    Attributes:
        symbol: Trading pair symbol
        entry_time: When the position was entered
        exit_time: When the position was exited
        signal_source: Which signal source was used (ML or RULE_BASED)
        direction: Trade direction
        entry_price: Price at entry
        exit_price: Price at exit
        pnl_pct: Profit/loss percentage
        pnl_absolute: Absolute profit/loss
        position_size: Size of the position
        discrepancy: Any discrepancy that existed at signal time
        notes: Additional notes about the trade
    """
    symbol: str
    entry_time: datetime
    exit_time: Optional[datetime]
    signal_source: SignalSource
    direction: SignalDirection
    entry_price: float
    exit_price: Optional[float]
    pnl_pct: Optional[float] = None
    pnl_absolute: Optional[float] = None
    position_size: float = 0.0
    discrepancy: Optional[SignalDiscrepancy] = None
    notes: str = ""
    
    def __post_init__(self):
        """Calculate PnL if exit price is provided."""
        if self.exit_price is not None and self.pnl_pct is None:
            if self.direction == SignalDirection.LONG:
                self.pnl_pct = ((self.exit_price - self.entry_price) / self.entry_price) * 100
                self.pnl_absolute = (self.exit_price - self.entry_price) * self.position_size
            elif self.direction == SignalDirection.SHORT:
                self.pnl_pct = ((self.entry_price - self.exit_price) / self.entry_price) * 100
                self.pnl_absolute = (self.entry_price - self.exit_price) * self.position_size
            else:
                self.pnl_pct = 0.0
                self.pnl_absolute = 0.0


class ShadowValidator:
    """
    Shadow model comparison system for validating ML vs rule-based signals.
    
    This class tracks signals from both ML and rule-based systems,
    identifies discrepancies, and records trade outcomes for performance analysis.
    
    Example:
        >>> validator = ShadowValidator()
        >>> 
        >>> # Record signals
        >>> validator.record_signal(ml_signal)
        >>> validator.record_signal(rule_signal)
        >>> 
        >>> # Compare signals
        >>> discrepancy = validator.compare_signals('BTC-USDT')
        >>> 
        >>> # Record outcome
        >>> outcome = validator.record_outcome(
        ...     symbol='BTC-USDT',
        ...     entry_time=datetime.now(),
        ...     exit_time=datetime.now(),
        ...     signal_source=SignalSource.ML,
        ...     direction=SignalDirection.LONG,
        ...     entry_price=50000.0,
        ...     exit_price=51000.0,
        ...     position_size=0.01
        ... )
        >>> 
        >>> # Get performance summary
        >>> summary = validator.get_performance_summary()
    """
    
    def __init__(self):
        """Initialize the shadow validator."""
        self._ml_signals: dict[str, list[ShadowSignal]] = defaultdict(list)
        self._rule_signals: dict[str, list[ShadowSignal]] = defaultdict(list)
        self._discrepancies: list[SignalDiscrepancy] = []
        self._outcomes: list[TradeOutcome] = []
        logger.info("ShadowValidator initialized")
    
    def record_signal(self, signal: ShadowSignal) -> None:
        """
        Record a signal from either ML or rule-based system.
        
        Args:
            signal: The signal to record
        """
        if signal.source == SignalSource.ML:
            self._ml_signals[signal.symbol].append(signal)
            logger.debug(f"Recorded ML signal for {signal.symbol}: {signal.direction.value}")
        elif signal.source == SignalSource.RULE_BASED:
            self._rule_signals[signal.symbol].append(signal)
            logger.debug(f"Recorded rule-based signal for {signal.symbol}: {signal.direction.value}")
        else:
            logger.warning(f"Unknown signal source: {signal.source}")
    
    def compare_signals(self, symbol: str) -> SignalDiscrepancy:
        """
        Compare the most recent ML vs rule-based signals for a symbol.
        
        Args:
            symbol: Trading pair symbol to compare
            
        Returns:
            SignalDiscrepancy describing the difference between signals
        """
        ml_signals = self._ml_signals.get(symbol, [])
        rule_signals = self._rule_signals.get(symbol, [])
        
        ml_signal = ml_signals[-1] if ml_signals else None
        rule_signal = rule_signals[-1] if rule_signals else None
        
        timestamp = datetime.now()
        if ml_signal:
            timestamp = ml_signal.timestamp
        elif rule_signal:
            timestamp = rule_signal.timestamp
        
        # Determine discrepancy type
        discrepancy_type = self._determine_discrepancy_type(ml_signal, rule_signal)
        
        discrepancy = SignalDiscrepancy(
            symbol=symbol,
            timestamp=timestamp,
            ml_signal=ml_signal,
            rule_signal=rule_signal,
            discrepancy_type=discrepancy_type
        )
        
        if discrepancy_type != DiscrepancyType.NONE:
            self._discrepancies.append(discrepancy)
            logger.info(f"Discrepancy detected for {symbol}: {discrepancy.description}")
        
        return discrepancy
    
    def _determine_discrepancy_type(
        self,
        ml_signal: Optional[ShadowSignal],
        rule_signal: Optional[ShadowSignal]
    ) -> DiscrepancyType:
        """Determine the type of discrepancy between two signals."""
        # Both signals missing
        if ml_signal is None and rule_signal is None:
            return DiscrepancyType.NONE
        
        # One signal missing
        if ml_signal is None or rule_signal is None:
            return DiscrepancyType.MISSING_SIGNAL
        
        # Direction mismatch
        if ml_signal.direction != rule_signal.direction:
            if ml_signal.direction == SignalDirection.NEUTRAL or rule_signal.direction == SignalDirection.NEUTRAL:
                return DiscrepancyType.MISSING_SIGNAL
            return DiscrepancyType.DIRECTION_MISMATCH
        
        # Both neutral
        if ml_signal.direction == SignalDirection.NEUTRAL and rule_signal.direction == SignalDirection.NEUTRAL:
            return DiscrepancyType.NONE
        
        # Confidence divergence (significant difference in confidence)
        conf_diff = abs(ml_signal.confidence - rule_signal.confidence)
        if conf_diff > 0.3:  # Threshold for significant divergence
            return DiscrepancyType.CONFIDENCE_DIVERGENCE
        
        return DiscrepancyType.NONE
    
    def record_outcome(
        self,
        symbol: str,
        entry_time: datetime,
        exit_time: Optional[datetime],
        signal_source: SignalSource,
        direction: SignalDirection,
        entry_price: float,
        exit_price: Optional[float] = None,
        position_size: float = 0.0,
        discrepancy: Optional[SignalDiscrepancy] = None,
        notes: str = ""
    ) -> TradeOutcome:
        """
        Record the outcome of a trade.
        
        Args:
            symbol: Trading pair symbol
            entry_time: When the position was entered
            exit_time: When the position was exited
            signal_source: Which signal source was used
            direction: Trade direction
            entry_price: Price at entry
            exit_price: Price at exit
            position_size: Size of the position
            discrepancy: Any discrepancy that existed at signal time
            notes: Additional notes
            
        Returns:
            TradeOutcome object representing the trade result
        """
        outcome = TradeOutcome(
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            signal_source=signal_source,
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            position_size=position_size,
            discrepancy=discrepancy,
            notes=notes
        )
        
        self._outcomes.append(outcome)
        
        pnl_str = f"{outcome.pnl_pct:.2f}%" if outcome.pnl_pct is not None else "pending"
        logger.info(
            f"Trade outcome recorded for {symbol}: {outcome.signal_source.value}, "
            f"direction={outcome.direction.value}, PnL={pnl_str}"
        )
        
        return outcome
    
    def get_performance_summary(self) -> dict:
        """
        Get performance summary comparing ML vs rule-based signals.
        
        Returns:
            Dictionary containing:
            - total_trades: Total number of trades recorded
            - ml_trades: Trades using ML signals
            - rule_trades: Trades using rule-based signals
            - ml_win_rate: Win rate for ML-sourced trades
            - rule_win_rate: Win rate for rule-based trades
            - ml_avg_pnl: Average PnL for ML trades
            - rule_avg_pnl: Average PnL for rule trades
            - discrepancy_count: Number of discrepancies detected
            - discrepancy_rate: Percentage of signals with discrepancies
            - by_symbol: Per-symbol breakdown
        """
        total_trades = len(self._outcomes)
        
        if total_trades == 0:
            return {
                "total_trades": 0,
                "ml_trades": 0,
                "rule_trades": 0,
                "ml_win_rate": 0.0,
                "rule_win_rate": 0.0,
                "ml_avg_pnl": 0.0,
                "rule_avg_pnl": 0.0,
                "discrepancy_count": 0,
                "discrepancy_rate": 0.0,
                "by_symbol": {}
            }
        
        # Separate ML and rule trades
        ml_outcomes = [o for o in self._outcomes if o.signal_source == SignalSource.ML]
        rule_outcomes = [o for o in self._outcomes if o.signal_source == SignalSource.RULE_BASED]
        
        # Calculate win rates
        ml_wins = sum(1 for o in ml_outcomes if o.pnl_pct is not None and o.pnl_pct > 0)
        rule_wins = sum(1 for o in rule_outcomes if o.pnl_pct is not None and o.pnl_pct > 0)
        
        ml_win_rate = ml_wins / len(ml_outcomes) if ml_outcomes else 0.0
        rule_win_rate = rule_wins / len(rule_outcomes) if rule_outcomes else 0.0
        
        # Calculate average PnL
        ml_pnls = [o.pnl_pct for o in ml_outcomes if o.pnl_pct is not None]
        rule_pnls = [o.pnl_pct for o in rule_outcomes if o.pnl_pct is not None]
        
        ml_avg_pnl = sum(ml_pnls) / len(ml_pnls) if ml_pnls else 0.0
        rule_avg_pnl = sum(rule_pnls) / len(rule_pnls) if rule_pnls else 0.0
        
        # Calculate discrepancy rate
        total_signals = sum(len(v) for v in self._ml_signals.values()) + \
                        sum(len(v) for v in self._rule_signals.values())
        discrepancy_rate = len(self._discrepancies) / total_signals if total_signals > 0 else 0.0
        
        # Per-symbol breakdown
        by_symbol = {}
        for outcome in self._outcomes:
            if outcome.symbol not in by_symbol:
                by_symbol[outcome.symbol] = {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "ml_trades": 0,
                    "rule_trades": 0,
                    "total_pnl": 0.0
                }
            
            sym_stats = by_symbol[outcome.symbol]
            sym_stats["trades"] += 1
            
            if outcome.signal_source == SignalSource.ML:
                sym_stats["ml_trades"] += 1
            else:
                sym_stats["rule_trades"] += 1
            
            if outcome.pnl_pct is not None:
                sym_stats["total_pnl"] += outcome.pnl_pct
                if outcome.pnl_pct > 0:
                    sym_stats["wins"] += 1
                elif outcome.pnl_pct < 0:
                    sym_stats["losses"] += 1
        
        # Calculate win rates per symbol
        for sym_stats in by_symbol.values():
            total = sym_stats["wins"] + sym_stats["losses"]
            sym_stats["win_rate"] = sym_stats["wins"] / total if total > 0 else 0.0
        
        return {
            "total_trades": total_trades,
            "ml_trades": len(ml_outcomes),
            "rule_trades": len(rule_outcomes),
            "ml_win_rate": ml_win_rate,
            "rule_win_rate": rule_win_rate,
            "ml_avg_pnl": ml_avg_pnl,
            "rule_avg_pnl": rule_avg_pnl,
            "discrepancy_count": len(self._discrepancies),
            "discrepancy_rate": discrepancy_rate,
            "by_symbol": by_symbol
        }
    
    def get_discrepancies(self) -> list[SignalDiscrepancy]:
        """
        Get all recorded discrepancies.
        
        Returns:
            List of all SignalDiscrepancy objects
        """
        return self._discrepancies.copy()
    
    def get_outcomes(self) -> list[TradeOutcome]:
        """
        Get all recorded trade outcomes.
        
        Returns:
            List of all TradeOutcome objects
        """
        return self._outcomes.copy()
    
    def clear_history(self) -> None:
        """Clear all recorded signals, discrepancies, and outcomes."""
        self._ml_signals.clear()
        self._rule_signals.clear()
        self._discrepancies.clear()
        self._outcomes.clear()
        logger.info("ShadowValidator history cleared")


# Convenience function for quick comparisons
def compare_signals(
    ml_signal: Optional[ShadowSignal],
    rule_signal: Optional[ShadowSignal]
) -> SignalDiscrepancy:
    """
    Compare two signals directly without needing a ShadowValidator instance.
    
    Args:
        ml_signal: The ML-generated signal
        rule_signal: The rule-based signal
        
    Returns:
        SignalDiscrepancy describing the difference
    """
    if ml_signal is None and rule_signal is None:
        raise ValueError("At least one signal must be provided")
    
    timestamp = datetime.now()
    symbol = ""
    
    if ml_signal:
        timestamp = ml_signal.timestamp
        symbol = ml_signal.symbol
    elif rule_signal:
        timestamp = rule_signal.timestamp
        symbol = rule_signal.symbol
    
    validator = ShadowValidator()
    
    if ml_signal:
        validator.record_signal(ml_signal)
    if rule_signal:
        validator.record_signal(rule_signal)
    
    return validator.compare_signals(symbol)


if __name__ == "__main__":
    # Simple test/demo
    print("ShadowValidator Demo")
    print("=" * 50)
    
    validator = ShadowValidator()
    
    # Create test signals
    ml_signal = ShadowSignal(
        symbol="BTC-USDT",
        timestamp=datetime.now(),
        direction=SignalDirection.LONG,
        confidence=0.85,
        source=SignalSource.ML,
        features={"rsi": 45, "adx": 35}
    )
    
    rule_signal = ShadowSignal(
        symbol="BTC-USDT",
        timestamp=datetime.now(),
        direction=SignalDirection.LONG,
        confidence=0.72,
        source=SignalSource.RULE_BASED,
        features={"rsi": 42, "adx": 38}
    )
    
    # Record and compare
    validator.record_signal(ml_signal)
    validator.record_signal(rule_signal)
    
    discrepancy = validator.compare_signals("BTC-USDT")
    print(f"Discrepancy: {discrepancy.description}")
    print(f"Type: {discrepancy.discrepancy_type.value}")
    print(f"Severity: {discrepancy.severity:.2f}")
    
    # Record a trade outcome
    outcome = validator.record_outcome(
        symbol="BTC-USDT",
        entry_time=datetime.now(),
        exit_time=datetime.now(),
        signal_source=SignalSource.ML,
        direction=SignalDirection.LONG,
        entry_price=50000.0,
        exit_price=51000.0,
        position_size=0.01
    )
    print(f"\nTrade PnL: {outcome.pnl_pct:.2f}%")
    
    # Get performance summary
    summary = validator.get_performance_summary()
    print(f"\nPerformance Summary:")
    print(f"  Total trades: {summary['total_trades']}")
    print(f"  ML win rate: {summary['ml_win_rate']:.2%}")
    print(f"  ML avg PnL: {summary['ml_avg_pnl']:.2f}%")
    print(f"  Discrepancy rate: {summary['discrepancy_rate']:.2%}")
