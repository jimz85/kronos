# Contributing to Kronos

Thank you for your interest in contributing to Kronos!

## Project Overview

Kronos is an autonomous cryptocurrency trading system built with a 5-layer architecture:
- **Core** - Constants, configuration, indicators
- **Strategies** - Regime, Alpha, Beta engines
- **Models** - Confidence scoring, position sizing
- **Risk** - Circuit breakers, trailing stops
- **Data** - ATR watchlist, data evolution

## Getting Started

### Prerequisites

- Python 3.11+
- OKX exchange account (for live trading)
- Understanding of cryptocurrency trading concepts

### Setup

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd kronos
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Review `CLAUDE.md` for architecture details

## Development Guidelines

### Coding Standards

- **File Headers**: Every Python file must include:
  - Shebang line: `#!/usr/bin/env python3`
  - Module docstring with description and key functions
  - Version information

- **Path Handling**: Always use absolute paths:
  ```python
  from pathlib import Path
  ROOT = Path.home() / "kronos"
  ```

- **State Files**: Use atomic writes to prevent corruption:
  ```python
  from kronos_utils import atomic_write_json
  atomic_write_json(state_file, new_state)
  ```

### Logging Guidelines

Kronos supports two logging modes: **standard text logging** and **JSON structured logging**.

#### Standard Logging (Human-Readable)

Use for general application logs, debugging, and console output:

```python
from core.logging_config import get_logger

logger = get_logger('kronos.module_name')
logger.info("Normal operation message")
logger.warning("Warning condition - something needs attention")
logger.error("Error condition - operation failed")
logger.debug("Detailed debugging information")
```

#### JSON Structured Logging (Machine-Readable)

Use for:
- Trade events (signals, orders, positions)
- Audit events (config changes, risk breaches)
- Integration with log aggregation systems (ELK, Loki, Datadog)

```python
from core.logging_config import get_json_logger

# General JSON logger
logger = get_json_logger('kronos.module_name')

# Specialized trade logger
trade_logger = get_json_logger('kronos.trades', extra_fields={
    'log_type': 'trade_event'
})

# Audit logger
audit_logger = get_json_logger('kronos.audit', extra_fields={
    'log_type': 'audit_event'
})
```

##### JSON Log Output Example

```python
logger = get_json_logger('kronos.trades')
logger.info("Signal generated", extra={
    "coin": "BTC",
    "side": "long",
    "confidence": 0.85,
    "price": 50000.0,
    " timeframe": "1h"
})
```

Output:
```json
{
    "timestamp": "2026-04-27T20:30:00.000Z",
    "level": "INFO",
    "logger": "kronos.trades",
    "message": "Signal generated",
    "app": "kronos",
    "log_type": "trade_event",
    "coin": "BTC",
    "side": "long",
    "confidence": 0.85,
    "price": 50000.0,
    "timeframe": "1h"
}
```

##### Using Specialized Loggers

```python
from core.logging_config import get_trade_logger, get_audit_logger

# For trade events
trade_logger = get_trade_logger()
trade_logger.info("Order placed", extra={
    "order_id": "abc123",
    "inst_id": "BTC-USDT-SWAP",
    "side": "buy",
    "sz": 0.01,
    "px": 50000.0
})

# For audit events
audit_logger = get_audit_logger()
audit_logger.warning("Risk limit approached", extra={
    "hourly_loss_pct": 0.018,
    "limit": 0.02,
    "action": "circuit_breaker_warning"
})
```

##### Best Practices

1. **Use descriptive logger names**: Include module path
   ```python
   # Good
   logger = get_logger('kronos.strategies.regime_classifier')
   logger = get_json_logger('kronos.orders')

   # Avoid
   logger = get_logger('log')
   ```

2. **Include relevant context in extra fields**:
   ```python
   # Good
   logger.info("Trade executed", extra={
       "coin": "ETH",
       "side": "long",
       "size": 1.5,
       "price": 3000.0,
       "pnl_realized": 50.0
   })

   # Avoid - too generic
   logger.info("Trade happened")
   ```

3. **Use appropriate log levels**:
   - `DEBUG`: Detailed debugging info (not in production JSON logs by default)
   - `INFO`: Normal operations, state changes
   - `WARNING`: Attention needed, approaching limits
   - `ERROR`: Operations that failed
   - `CRITICAL`: System-level failures

4. **Include exception tracebacks**:
   ```python
   try:
       risky_operation()
   except Exception as e:
       logger.error(f"Operation failed: {e}", extra={
           "operation": "order_placement",
           "coin": "BTC"
       })
       # The JSON formatter automatically captures exc_info if present
       raise
   ```

### Architecture Principles

1. **Simulation First**: All features must be tested in simulation mode (`OKX_FLAG='1'`) before any live trading discussion

2. **Circuit Breaker**: Respect loss limits:
   - 2% per hour maximum
   - 5% per day maximum
   - 1% per trade maximum

3. **Reserve Maintenance**: Keep 20% reserve in treasury

4. **Multi-Timeframe Confirmation**: Signals should be confirmed across timeframes

### Critical Safety Rules

- **NEVER** modify `OKX_FLAG` to `'0'` without explicit user confirmation
- **ALWAYS** use atomic writes for state files
- **NEVER** use `os.chdir()` - always use absolute paths
- **ALWAYS** handle API failures gracefully

## Pull Request Process

1. **Branch Naming**: Use descriptive branch names:
   - `feature/description`
   - `fix/issue-description`
   - `refactor/module-name`
   - `logging/json-structured` (for logging improvements)

2. **Commit Messages**: Use clear, descriptive messages:
   - `Add: new feature description`
   - `Fix: bug description`
   - `Refactor: module improvement`
   - `Docs: update documentation`

3. **Testing**:
   - Test in simulation mode first
   - Verify no breaking changes to existing functionality
   - Check circuit breaker logic remains intact

4. **Documentation**:
   - Update `CLAUDE.md` if introducing new patterns
   - Add docstrings to new functions
   - Update README if adding new features
   - Add entries to CHANGELOG.md for notable changes

## Areas for Contribution

### High Priority
- Risk management improvements
- Circuit breaker refinements
- Backtesting engine enhancements
- Signal generation algorithms

### Medium Priority
- Additional exchange connectors
- UI/UX improvements
- Performance optimizations
- Documentation improvements

### Experimental
- New trading strategies
- Machine learning integration
- Multi-timeframe analysis

## Reporting Issues

When reporting issues, include:
- Python version
- Kronos version (from `constants.py`)
- OKX_FLAG setting
- Error logs (include JSON logs if available)
- Steps to reproduce

## License

By contributing to Kronos, you agree that your contributions will be licensed under the project's license.

## Questions?

Review `CLAUDE.md` for detailed architecture information, or refer to `README.md` for project overview.
