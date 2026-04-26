# Kronos - AI Coding Assistant Guide

This document is for AI coding assistants (like Claude, Codex, etc.) working on the Kronos project.

## Project Overview

**Kronos** is an autonomous cryptocurrency trading system with:
- **Version**: v5.0.0
- **Architecture**: 5-layer system (Core → Strategies → Models → Risk → Data)
- **Exchange**: OKX (currently in simulation mode)
- **Language**: Python 3.11+

---

## Project Structure

```
kronos/
├── # Entry Points
├── kronos_pilot.py         # Main trading loop
├── kronos_auto_guard.py    # Safety monitor
├── kronos_heartbeat.py     # Hourly health
├── real_monitor.py        # Position monitor
├── kronos_journal.py      # Trade journal
│
├── # 5-Layer Architecture
├── core/                  # Phase 0: Constants, config, indicators
├── strategies/            # Phase 1: Regime, Alpha, Beta engines
├── models/                # Phase 2: Confidence, position sizing
├── risk/                  # Phase 3: Circuit breaker, trailing
├── data/                  # Phase 4: ATR watchlist, evolution
├── execution/             # Phase 5: Execution (reserved)
│
├── # Configuration
├── constants.py           # System-wide constants
├── core/config.py         # Environment config
│
├── # State Files (CRITICAL - understand before modifying)
├── paper_trades.json      # Paper trading records
├── data/treasury.json     # Balance tracking
├── data/circuit.json      # Circuit breaker state
└── decision_journal.jsonl # AI decision audit
```

---

## Key Files and Their Purpose

### Entry Points

| File | Purpose | Key Functions |
|------|---------|---------------|
| `kronos_pilot.py` | Main signal generation | `generate_signal()`, `paper_trade()`, `push_feishu()` |
| `kronos_auto_guard.py` | Danger detection | `check_sl_danger()`, `check_liq_distance()`, `feishu_notify()` |
| `kronos_heartbeat.py` | Hourly health | `load_circuit_state()`, `record_trade_outcome()`, `check_circuit_breaker()` |
| `real_monitor.py` | Position sync | `get_real_positions()`, `get_real_sl_tp_orders()`, `get_account_balance()` |
| `kronos_journal.py` | Trade journal | `update_journal()`, `compute_stats()` |

### Core Modules

| File | Purpose | Key Classes/Functions |
|------|---------|----------------------|
| `core/constants.py` | System constants | `SYSTEM_VERSION`, `RiskConfig`, `MarketRegime`, `SignalType` |
| `core/config.py` | Config loader | `OKXConfig`, `TradingConfig`, `FeishuConfig` |
| `core/engine.py` | Main engine | `fetch_okx_candles()`, 5-layer orchestration |
| `strategies/regime_classifier.py` | Market regime | `RegimeClassifier`, `RegimeType` |
| `strategies/engine_alpha.py` | Alpha signals | `AlphaEngine`, `SignalType` |
| `strategies/engine_beta.py` | Beta signals | `BetaEngine`, `BetaSignalType` |
| `models/confidence_scorer.py` | Signal confidence | `ConfidenceScorer` |
| `models/position_sizer.py` | Position sizing | `PositionSizer` |
| `risk/circuit_breaker.py` | Loss circuit | `CircuitBreaker` |
| `risk/dynamic_trailing.py` | Trailing stop | `DynamicTrailingStop` |
| `data/atr_watchlist.py` | Volatility tracking | `ATRWatchlist` |

---

## Critical Constants

### OKX_FLAG (Simulation vs Live)

**Location**: Environment variable `OKX_FLAG` or `os.getenv('OKX_FLAG', '1')`

```python
# In real_monitor.py line 42:
'x-simulated-trading': os.getenv('OKX_FLAG', '1'),

# In kronos_pilot.py line 87-88:
_is_sim_key = OKX_API_KEY.startswith('8aba4d') if OKX_API_KEY else False
DEMO_MODE = (not OKX_API_KEY) or _is_sim_key
```

| Value | Mode | Description |
|-------|------|-------------|
| `'0'` | Live | Real trading, real money |
| `'1'` | Simulation | Paper trading, test mode |

**IMPORTANT**: Always check `OKX_FLAG` before making any trading recommendations. The system should NEVER suggest live trading modifications without explicit user confirmation.

### Paper Trades Path

```python
PAPER_TRADES = Path.home() / '.hermes' / 'cron' / 'output' / 'paper_trades.json'
```

This is the **authoritative source** of truth for paper trading positions.

---

## Coding Conventions

### File Headers

Every new Python file should have:

```python
#!/usr/bin/env python3
"""
FileName.py - Brief Description
================================

Longer description of what this file does.

Key Functions:
    - function1(): What it does
    - function2(): What it does

Version: x.x.x
"""

import os, sys, json, time
# ... other imports
```

### Logging

Use the centralized logging system:

```python
# At top of file
import logging
logger = logging.getLogger('kronos.module_name')

# Use appropriate levels
logger.info("Normal operation message")
logger.warning("Warning condition")
logger.error("Error condition")
```

### Path Handling

**ALWAYS use absolute paths** - never `os.chdir()`:

```python
# CORRECT
ROOT = Path.home() / "kronos"
STATE_DIR = ROOT / "data"
STATE_DIR.mkdir(exist_ok=True)

# WRONG (never do this)
os.chdir('/some/path')
```

### State File Operations

Use atomic writes to prevent corruption:

```python
from kronos_utils import atomic_write_json

# CORRECT
atomic_write_json(state_file, new_state)

# WRONG (can corrupt file on crash)
with open(state_file, 'w') as f:
    json.dump(state, f)
```

### API Request Handling

OKX API requires specific timestamp format:

```python
# OKX requires ISO8601 format (NOT Unix milliseconds)
def _ts():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

# Sign the request correctly
def _req(method, path, body=''):
    ts = _ts()
    msg = ts + method + path + (body if body else '')
    sig = base64.b64encode(hmac.new(
        OKX_SECRET.encode(), msg.encode(), hashlib.sha256
    ).digest()).decode()
    # ... continue with request
```

---

## Important Notes

### 1. Simulation Mode First

**All new features should be developed and tested in simulation mode first.**

```python
# Check simulation status
if os.getenv('OKX_FLAG', '1') == '1':
    # Simulation mode - OK for experiments
    pass
else:
    # LIVE MODE - Be extra careful
    pass
```

### 2. Circuit Breaker Logic

The circuit breaker tracks **consecutive losses** to prevent cascading failures:

```python
# In kronos_heartbeat.py
def record_trade_outcome(coin, pnl, close_reason=''):
    """
    Key principle: Only record TRUE trading losses
    - System failures (timestamp, balance) → NOT counted
    - Real losses (PnL <= 0) → COUNTED
    """
    is_failure = close_reason in FAILURE_REASONS
    if is_failure:
        outcome = 'failure'  # Does NOT affect circuit
    elif pnl > 0:
        outcome = 'win'      # Resets counter
    else:
        outcome = 'loss'     # Increments counter
```

### 3. Treasury System

The treasury system prevents account destruction:

```python
TREASURY_BASE = {
    'hourly_loss_pct': 0.02,   # 2% per hour max
    'daily_loss_pct': 0.05,    # 5% per day max
    'per_trade_pct': 0.01,     # 1% per trade max
    'reserve_pct': 0.20,       # Keep 20% reserve
}
```

Never suggest trades that would exceed these limits without clear user approval.

### 4. Multi-Timeframe Confirmation

Signals require confirmation across timeframes:

```python
# Example from kronos_pilot.py
if timeframe == '1h':
    # Confirm with 4h and 1d trends
    if 4h_trend != 1h_trend:
        return None  # No trade on disagreement
```

### 5. Pattern Whitelist Learning

The system learns from trade history:

```python
# Pattern whitelist structure
PATTERN_WHITELIST = {
    "long_RSI35-45_ADX30-100": {"win_rate": 0.667, "count": 3},
    "long_RSI55-65_ADX30-40": {"win_rate": 0.588, "count": 17},
}
```

---

## File Modification Checklist

Before modifying any file, verify:

- [ ] **OKX_FLAG check**: Does your change work in simulation mode?
- [ ] **Path handling**: Are you using absolute paths?
- [ ] **State files**: Are you using atomic writes?
- [ ] **Logging**: Is proper logging added?
- [ ] **Error handling**: Are API failures handled gracefully?
- [ ] **Circuit breaker**: Does your change affect loss tracking?
- [ ] **Treasury**: Does your change respect treasury limits?

---

## Common Patterns

### Pattern 1: Adding a New Signal Type

```python
# 1. Add to constants.py
class SignalType(Enum):
    # ... existing types ...
    NEW_SIGNAL = "new_signal"

# 2. Add to engine
def _generate_new_signal(self, data):
    # Generate signal logic
    return {"type": SignalType.NEW_SIGNAL, "confidence": score}
```

### Pattern 2: Adding a New State File

```python
# 1. Define path
NEW_STATE_FILE = ROOT / "data" / "new_state.json"

# 2. Load function
def load_new_state():
    try:
        return json.loads(NEW_STATE_FILE.read_text())
    except:
        return default_state

# 3. Save function
def save_new_state(state):
    atomic_write_json(NEW_STATE_FILE, state)
```

### Pattern 3: Adding a New Cron Job

```python
# 1. Add lock file check
LOCK_FILE = ".new_cron.lock"

def _acquire_lock():
    if os.path.exists(LOCK_FILE):
        return False
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True

# 2. Add timeout cleanup
def _cleanup_stale_locks(max_age_seconds=300):
    # Remove locks older than max_age_seconds
    pass

# 3. Document in ARCHITECTURE.md cron schedule
```

---

## Testing Guidelines

### Unit Tests
```bash
# Run specific test file
python3 -m pytest tests/test_circuit_breaker.py -v

# Run with coverage
python3 -m pytest tests/ --cov=. --cov-report=html
```

### Simulation Testing
```bash
# Always test in simulation first
OKX_FLAG=1 python3 kronos_pilot.py --full

# Check paper trades
python3 kronos_pilot.py --status
```

### Manual Testing Checklist
```bash
# 1. Check circuit state
cat ~/kronos/data/circuit.json

# 2. Check paper trades
cat ~/.hermes/cron/output/paper_trades.json

# 3. Check logs
tail -100 logs/kronos_pilot.log
```

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `OKX_API_KEY` | (none) | OKX API key |
| `OKX_SECRET` | (none) | OKX API secret |
| `OKX_PASSPHRASE` | (none) | OKX API passphrase |
| `OKX_FLAG` | `1` | `0`=live, `1`=simulation |
| `FEISHU_APP_ID` | (none) | Feishu app ID |
| `FEISHU_APP_SECRET` | (none) | Feishu app secret |
| `FEISHU_CHAT_ID` | (none) | Feishu chat ID |
| `MINIMAX_API_KEY` | (none) | MiniMax AI API key |
| `MAX_HOLD_HOURS` | `72` | Max position hold time |
| `SL_PCT` | `1.0` | Stop loss percentage |
| `TP_PCT` | `2.0` | Take profit percentage |

---

## Documentation

When modifying code:
1. Update docstrings for new/changed functions
2. Update this CLAUDE.md if adding new conventions
3. Update ARCHITECTURE.md if changing architecture
4. Update README.md if changing user-facing features

---

## Getting Help

If uncertain about:
- **Architecture**: See ARCHITECTURE.md
- **Strategy**: See README.md (Current Strategy section)
- **Code patterns**: See this CLAUDE.md
- **State files**: See ARCHITECTURE.md (State Files section)

---

## Version Notes

- **v5.0.0**: Current version with 5-layer architecture
- **Simulation mode**: Paper trades stored in `~/.hermes/cron/output/paper_trades.json`
- **Key components**: pilot, auto_guard, heartbeat, real_monitor run on cron schedule
