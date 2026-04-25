"""
Circuit Breaker Module
======================
Protects against cascade failures and abnormal market conditions.
Implements a sliding window circuit breaker pattern.
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable
from collections import deque
import time
import logging

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"    # Normal operation
    OPEN = "open"       # Blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5      # Failures before opening
    recovery_timeout: float = 60.0  # Seconds before half-open
    half_open_max_calls: int = 3    # Max test calls in half-open
    success_threshold: int = 2      # Successes to close from half-open
    window_size: int = 60           # Sliding window in seconds


@dataclass
class CircuitMetrics:
    failures: int = 0
    successes: int = 0
    consecutive_failures: int = 0
    last_failure_time: Optional[float] = None
    state: CircuitState = CircuitState.CLOSED
    half_open_calls: int = 0
    total_opens: int = 0


class CircuitBreaker:
    """
    Circuit breaker that trips after repeated failures and auto-recovers.
    
    State Machine:
        CLOSED -> (failure_threshold reached) -> OPEN
        OPEN -> (recovery_timeout elapsed) -> HALF_OPEN
        HALF_OPEN -> (success_threshold successes) -> CLOSED
        HALF_OPEN -> (any failure) -> OPEN
    """
    
    def __init__(
        self,
        name: str = "default",
        config: Optional[CircuitBreakerConfig] = None,
        on_state_change: Optional[Callable[[str, CircuitState, CircuitState], None]] = None
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.on_state_change = on_state_change
        self._events = deque(maxlen=self.config.window_size * 10)  # ~10 events/sec capacity
        self._metrics = CircuitMetrics()
        self._last_state_change = time.time()
        
    @property
    def state(self) -> CircuitState:
        self._check_state_transition()
        return self._metrics.state
    
    @property
    def metrics(self) -> CircuitMetrics:
        return self._metrics
    
    def _check_state_transition(self):
        """Check if automatic state transition should occur."""
        now = time.time()
        
        if self._metrics.state == CircuitState.OPEN:
            if now - self._last_state_change >= self.config.recovery_timeout:
                self._transition_to(CircuitState.HALF_OPEN)
                
        elif self._metrics.state == CircuitState.HALF_OPEN:
            if self._metrics.half_open_calls >= self.config.half_open_max_calls:
                # Reset for another recovery attempt
                self._metrics.half_open_calls = 0
                if self._metrics.consecutive_failures >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
                    
    def _transition_to(self, new_state: CircuitState):
        old_state = self._metrics.state
        self._metrics.state = new_state
        self._last_state_change = time.time()
        
        if new_state == CircuitState.OPEN:
            self._metrics.total_opens += 1
            
        logger.info(f"CircuitBreaker[{self.name}]: {old_state.value} -> {new_state.value}")
        
        if self.on_state_change:
            self.on_state_change(self.name, old_state, new_state)
            
    def _record_success(self):
        self._metrics.successes += 1
        self._metrics.consecutive_failures = 0
        
    def _record_failure(self):
        self._metrics.failures += 1
        self._metrics.consecutive_failures += 1
        self._metrics.last_failure_time = time.time()
        
    def is_allowed(self) -> bool:
        """Check if a request is currently allowed."""
        return self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)
    
    def record_success(self):
        """Record a successful call."""
        if self._metrics.state == CircuitState.HALF_OPEN:
            if self._metrics.consecutive_failures < self.config.success_threshold:
                self._record_success()
                self._metrics.half_open_calls += 1
                if self._metrics.consecutive_failures >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            else:
                self._transition_to(CircuitState.CLOSED)
        else:
            self._record_success()
            
    def record_failure(self):
        """Record a failed call."""
        self._record_failure()
        
        if self._metrics.state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.OPEN)
        elif self._metrics.consecutive_failures >= self.config.failure_threshold:
            self._transition_to(CircuitState.OPEN)
            
    def execute(self, func: Callable, *args, **kwargs):
        """
        Execute a function through the circuit breaker.
        Raises CircuitOpenError if blocked.
        """
        if not self.is_allowed():
            raise CircuitOpenError(f"Circuit {self.name} is {self.state.value}")
            
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure()
            raise
            
    def reset(self):
        """Manually reset the circuit breaker to closed state."""
        self._metrics = CircuitMetrics()
        self._transition_to(CircuitState.CLOSED)
        
    def __repr__(self):
        return (f"CircuitBreaker(name={self.name}, state={self.state.value}, "
                f"failures={self._metrics.failures}, consecutive={self._metrics.consecutive_failures})")


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open and request is blocked."""
    pass


# =============================================================================
# DEMO
# =============================================================================

def demo():
    """Demonstrate circuit breaker behavior."""
    print("=" * 60)
    print("CIRCUIT BREAKER DEMO")
    print("=" * 60)
    
    # Track state changes
    state_log = []
    def on_state_change(name, old, new):
        state_log.append((name, old.value, new.value))
        print(f"  [STATE] {name}: {old.value} -> {new.value}")
    
    # Create circuit breaker with tight settings for demo
    config = CircuitBreakerConfig(
        failure_threshold=3,
        recovery_timeout=2.0,
        half_open_max_calls=2,
        success_threshold=2
    )
    cb = CircuitBreaker("test", config=config, on_state_change=on_state_change)
    
    print(f"\nInitial state: {cb.state.value}")
    print(f"Config: failure_threshold={config.failure_threshold}, recovery_timeout={config.recovery_timeout}s")
    print()
    
    # Simulate function that sometimes fails
    call_count = [0]
    def flaky_function():
        call_count[0] += 1
        # Fail first 4 calls, then succeed
        if call_count[0] <= 4:
            raise RuntimeError(f"Simulated failure #{call_count[0]}")
        return "success"
    
    # Test 1: Accumulate failures to trip circuit
    print("Test 1: Accumulate failures to trip circuit")
    for i in range(5):
        try:
            if cb.is_allowed():
                result = cb.execute(flaky_function)
                print(f"  Call {i+1}: {result}")
            else:
                print(f"  Call {i+1}: BLOCKED (circuit {cb.state.value})")
        except CircuitOpenError as e:
            print(f"  Call {i+1}: {e}")
        except Exception as e:
            print(f"  Call {i+1}: Exception recorded")
            
    print(f"\n  After failures: {cb}")
    print(f"  State log: {state_log}")
    
    # Test 2: Wait for recovery timeout
    print("\nTest 2: Wait for recovery timeout (2 seconds)...")
    time.sleep(2.5)
    print(f"  After wait: {cb}")
    
    # Test 3: Half-open test calls
    print("\nTest 3: Test recovery in half-open state")
    for i in range(4):
        try:
            if cb.is_allowed():
                result = cb.execute(lambda: "recovery_success")
                print(f"  Call {i+1}: {result}")
            else:
                print(f"  Call {i+1}: BLOCKED")
        except Exception as e:
            print(f"  Call {i+1}: {e}")
            
    print(f"\n  Final state: {cb}")
    print(f"  Total times circuit opened: {cb.metrics.total_opens}")
    print("\n" + "=" * 60)
    print("DEMO COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    demo()
