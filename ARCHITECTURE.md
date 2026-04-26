# Kronos Architecture Documentation

## Table of Contents
1. [5-Layer Architecture](#5-layer-architecture)
2. [Core Components](#core-components)
3. [Cron Schedule](#cron-schedule)
4. [Data Flow](#data-flow)
5. [State Files](#state-files)

---

## 5-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              KRONOS 5-LAYER ARCHITECTURE                        │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  PHASE 0: CORE (core/)                                                    │ │
│  │  ───────────────────────────────────────────────────────────────────────  │ │
│  │  • constants.py    - System enums, risk configs, version                   │ │
│  │  • config.py        - Environment variable loader                          │ │
│  │  • indicators.py   - RSI, ATR, ADX, EMA calculations                      │ │
│  │  • logging_config.py - Centralized logging setup                           │ │
│  │  • gemma4_parser.py - Gemma4 model output parser                          │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  PHASE 1: STRATEGIES (strategies/)                                         │ │
│  │  ───────────────────────────────────────────────────────────────────────  │ │
│  │  • regime_classifier.py  - Market regime detection (BULL/BEAR/RANGE)      │ │
│  │  • engine_alpha.py       - Alpha engine (CHOP range → mean reversion)      │ │
│  │  • engine_beta.py        - Beta engine (TREND → trend following)           │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  PHASE 2: MODELS (models/)                                                │ │
│  │  ───────────────────────────────────────────────────────────────────────  │ │
│  │  • confidence_scorer.py   - Signal confidence scoring (0-1)                │ │
│  │  • position_sizer.py      - Dynamic position sizing based on confidence    │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  PHASE 3: RISK (risk/)                                                    │ │
│  │  ───────────────────────────────────────────────────────────────────────  │ │
│  │  • circuit_breaker.py     - Consecutive loss circuit breaker               │ │
│  │  • dynamic_trailing.py    - ADX-adaptive trailing stop                     │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  PHASE 4: DATA (data/)                                                    │ │
│  │  ───────────────────────────────────────────────────────────────────────  │ │
│  │  • atr_watchlist.py       - ATR-based volatility watchlist                 │ │
│  │  • evolution_engine.py    - Strategy parameter evolution                   │ │
│  │  • daily_review.py        - End-of-day analysis                            │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  PHASE 5: EXECUTION (execution/)                                           │ │
│  │  ───────────────────────────────────────────────────────────────────────  │ │
│  │  • Order execution layer (reserved for future)                              │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. PILOT (`kronos_pilot.py`)

**Role**: Main trading loop and signal generation

```
┌─────────────────────────────────────────────────────────────────┐
│                        PILOT COMPONENT                           │
├─────────────────────────────────────────────────────────────────┤
│  Purpose: Generate trading signals, manage paper trades          │
│  Run Frequency: Every 5 minutes (via cron)                     │
│  Mode: --full for complete report, default for quick signal     │
│                                                                 │
│  Flow:                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  IC Analysis │ -> │ Multi-Timeframe│ -> │ Signal Gen   │      │
│  │  (Info Coef) │    │  Confirmation │    │ + Paper Trade│      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│         │                                       │               │
│         v                                       v               │
│  ┌──────────────┐                      ┌──────────────┐        │
│  │ Update       │                      │ Track Position│        │
│  │ decision_journal│                   │ + PnL        │        │
│  └──────────────┘                      └──────────────┘        │
│                                                │               │
│                                                v               │
│                                       ┌──────────────┐        │
│                                       │ Feishu Daily │        │
│                                       │ Report       │        │
│                                       └──────────────┘        │
└─────────────────────────────────────────────────────────────────┘
```

**Key Functions**:
- `generate_signal()` - IC analysis + multi-timeframe confirmation
- `paper_trade()` - Record simulated trades
- `push_feishu()` - Send daily reports

**State Files**: `paper_trades.json`, `decision_journal.jsonl`

---

### 2. AUTO_GUARD (`kronos_auto_guard.py`)

**Role**: Safety monitor, runs every 3 minutes

```
┌─────────────────────────────────────────────────────────────────┐
│                      AUTO_GUARD COMPONENT                        │
├─────────────────────────────────────────────────────────────────┤
│  Purpose: Detect dangerous conditions, block bad trades         │
│  Run Frequency: Every 3 minutes (via cron)                     │
│                                                                 │
│  Danger Detection:                                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ 1. CIRCUIT BREAKER TRIPPED                                │  │
│  │    → Block all new positions                              │  │
│  │    → Send Feishu alert                                    │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 2. SL DANGER (<2% from current price)                    │  │
│  │    → Trigger AI judgment (MiniMax)                        │  │
│  │    → May auto-close position                              │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 3. LIQUIDATION DANGER (<3% from liq price)               │  │
│  │    → Trigger AI judgment                                  │  │
│  │    → May reduce position                                  │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 4. POSITION TIMEOUT (>90% of max hold time)               │  │
│  │    → Trigger AI judgment                                  │  │
│  │    → May force close                                      │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  Actions:                                                       │
│  - Only sends Feishu when actual intervention occurs            │
│  - Silent when conditions are normal                            │
└─────────────────────────────────────────────────────────────────┘
```

**Key Thresholds**:
| Condition | Warning | Danger |
|-----------|---------|--------|
| Stop Loss Distance | <4% | <2% |
| Liquidation Distance | - | <3% |
| Position Timeout | 80% of max | 90% of max |

---

### 3. HEARTBEAT (`kronos_heartbeat.py`)

**Role**: Hourly health check, circuit breaker management, PnL reporting

```
┌─────────────────────────────────────────────────────────────────┐
│                     HEARTBEAT COMPONENT                         │
├─────────────────────────────────────────────────────────────────┤
│  Purpose: Hourly system health, trade outcome tracking          │
│  Run Frequency: Every hour (via cron)                           │
│                                                                 │
│  Functions:                                                     │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ 1. LOAD CIRCUIT STATE                                     │  │
│  │    - consecutive_losses count                              │  │
│  │    - last_outcome (win/loss/failure)                       │  │
│  │    - is_tripped status                                     │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 2. RECORD TRADE OUTCOME                                    │  │
│  │    - On position close: record win/loss                    │  │
│  │    - System failures: don't count toward circuit          │  │
│  │    - Update consecutive_loss counter                       │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 3. CHECK CIRCUIT BREAKER                                  │  │
│  │    - 3 consecutive losses → trip breaker                  │  │
│  │    - 1 win → reset counter                                │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 4. GENERATE HOURLY REPORT                                 │  │
│  │    - Account balance                                       │  │
│  │    - Open positions                                       │  │
│  │    - Circuit breaker status                               │  │
│  │    - Treasury health                                      │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  Circuit Breaker Rules:                                         │
│  - 3 consecutive losses → STOP TRADING                         │
│  - Only real losses count (PnL <= 0, not system failures)       │
│  - Blacklisted failures (timestamp, insufficient balance)        │
│    are handled separately, don't affect circuit                 │
└─────────────────────────────────────────────────────────────────┘
```

**Key State Variables**:
```python
{
    'consecutive_losses': 0,   # Current consecutive loss count
    'last_outcome': None,       # 'win'/'loss'/'failure'/None
    'is_tripped': False,        # Circuit breaker triggered
    'trip_reason': '',          # Why it tripped
    'trip_time': '',            # When it tripped
    'losses_log': []            # Last 10 trade outcomes
}
```

---

### 4. REAL_MONITOR (`real_monitor.py`)

**Role**: Real-time OKX position synchronization and monitoring

```
┌─────────────────────────────────────────────────────────────────┐
│                    REAL_MONITOR COMPONENT                        │
├─────────────────────────────────────────────────────────────────┤
│  Purpose: Sync with OKX, detect position discrepancies         │
│  Run Frequency: Every 1 minute (via cron)                      │
│                                                                 │
│  Key Functions:                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ 1. GET REAL POSITIONS                                     │  │
│  │    - Query OKX positions via API                          │  │
│  │    - Parse SL/TP orders                                   │  │
│  │    - Get account balance                                  │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 2. COMPARE WITH PAPER_TRADES                             │  │
│  │    - Find discrepancies between system and exchange        │  │
│  │    - Alert on unexpected positions                         │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 3. SET SL/TP ORDERS                                      │  │
│  │    - Ensure all positions have SL/TP                      │  │
│  │    - Use separate SL/TP order endpoint                    │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ 4. DYNAMIC TREASURY LIMITS                                │  │
│  │    - Hourly loss limit: 2% of account                     │  │
│  │    - Daily loss limit: 5% of account                       │  │
│  │    - Per-trade limit: 1% of account                       │  │
│  │    - Reserve requirement: 20% minimum                     │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  OKX Header (Simulation vs Live):                               │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  x-simulated-trading: OKX_FLAG env var                  │  │
│  │    - '0' = live trading                                 │  │
│  │    - '1' = simulation (paper trading)                    │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**Dynamic Treasury System**:
```python
TREASURY_BASE = {
    'hourly_loss_pct': 0.02,   # Max 2% per hour
    'daily_loss_pct': 0.05,    # Max 5% per day
    'per_trade_pct': 0.01,     # Max 1% per trade
    'reserve_pct': 0.20,       # Min 20% reserve
}

# ADX-based adjustment (higher volatility = tighter limits)
ADX_THRESHOLDS = [
    (15, 1.00),   # ADX<15: normal
    (25, 0.80),   # ADX 15-25: 20% tighter
    (35, 0.60),   # ADX 25-35: 40% tighter
    (50, 0.40),   # ADX 35-50: 60% tighter
    (999, 0.25),  # ADX>50: 75% tighter
]
```

---

## Cron Schedule

### Production Cron Jobs

```bash
# ┌───────────── minute (0 - 59)
# │ ┌───────────── hour (0 - 23)
# │ │ ┌───────────── day of month (1 - 31)
# │ │ │ ┌───────────── month (1 - 12)
# │ │ │ │ ┌───────────── day of week (0 - 6) (Sunday=0)
# │ │ │ │ │
# │ │ │ │ │
# * * * * * command

# Real-time position monitor (every minute)
*/1 * * * * cd ~/kronos && python3 real_monitor.py >> logs/real_monitor.log 2>&1

# Auto guard safety check (every 3 minutes)
*/3 * * * * cd ~/kronos && python3 kronos_auto_guard.py >> logs/auto_guard.log 2>&1

# Hourly heartbeat health check
0 * * * * cd ~/kronos && python3 kronos_heartbeat.py >> logs/heartbeat.log 2>&1

# Main trading pilot (every 5 minutes)
*/5 * * * * cd ~/kronos && python3 kronos_pilot.py >> logs/pilot.log 2>&1

# Daily data update (2 AM UTC)
0 2 * * * cd ~/kronos && python3 update_local_data.py >> logs/update_data.log 2>&1
```

### Component Timing Summary

| Component | Frequency | Duration | Cron Expression |
|-----------|-----------|---------|-----------------|
| real_monitor | 1 min | <30s | `*/1 * * * *` |
| auto_guard | 3 min | <10s | `*/3 * * * *` |
| heartbeat | 1 hour | <60s | `0 * * * *` |
| pilot | 5 min | <120s | `*/5 * * * *` |

---

## Data Flow

### Complete Trading Flow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              KRONOS DATA FLOW                                    │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  1. MARKET DATA INGESTION                                                        │
│  ════════════════════════                                                        │
│                                                                                  │
│      OKX API  ────>  fetch_okx_candles()  ───>  pandas DataFrame               │
│                                    │                                             │
│                                    v                                             │
│                        ┌───────────────────────┐                               │
│                        │   Data Validation     │                               │
│                        │   (1h, 4h, 1d bars)   │                               │
│                        └───────────────────────┘                               │
│                                                                                  │
│  2. REGIME CLASSIFICATION (Phase 1)                                             │
│  ════════════════════════════════                                                │
│                                                                                  │
│      DataFrame  ───>  RegimeClassifier  ───>  RegimeType                       │
│                                                   (BULL_TREND /                  │
│                                                    BEAR_TREND /                  │
│                                                    RANGE_BOUND /                 │
│                                                    VOLATILE)                      │
│                                                                                  │
│                                    │                                             │
│                    ┌───────────────┴───────────────┐                            │
│                    v                               v                             │
│           CHOP <= 50                        CHOP > 50                          │
│           (Alpha Engine)                   (Beta Engine)                        │
│                                                                                  │
│  3. SIGNAL GENERATION (Phase 1-2)                                              │
│  ══════════════════════════════════════                                          │
│                                                                                  │
│      ┌──────────────────┐         ┌──────────────────┐                            │
│      │   AlphaEngine   │         │   BetaEngine    │                            │
│      │  (Mean Reversion)│         │ (Trend Follow)  │                            │
│      └────────┬─────────┘         └────────┬─────────┘                            │
│               │                            │                                     │
│               v                            v                                     │
│      SignalType.LONG/SHORT          SignalType.LONG/SHORT                        │
│               │                            │                                     │
│               └────────────┬───────────────┘                                     │
│                            v                                                     │
│                   ConfidenceScorer                                              │
│                   (Score 0.0 - 1.0)                                             │
│                                                                                  │
│  4. RISK CHECK (Phase 3)                                                        │
│  ══════════════════════                                                         │
│                                                                                  │
│      Signal + Score  ──>  CircuitBreaker.is_allowed()                           │
│                                   │                                             │
│                                   v                                             │
│                         ┌─────────────────┐                                     │
│                         │ CANCEL if:      │                                     │
│                         │ - is_tripped    │                                     │
│                         │ - 3 consec loss │                                     │
│                         │ - blocked coin │                                     │
│                         └─────────────────┘                                     │
│                                   │                                             │
│                                   v (if allowed)                                │
│                         PositionSizer.calculate()                               │
│                         (Position size from confidence)                         │
│                                                                                  │
│  5. EXECUTION (Phase 5)                                                         │
│  ══════════════════════                                                         │
│                                                                                  │
│      Sized Order  ──>  OKX API (paper trade mode)                              │
│                              │                                                  │
│                              v                                                  │
│                      paper_trades.json                                          │
│                      (via atomic_write_json)                                    │
│                                                                                  │
│  6. MONITORING LOOP                                                            │
│  ══════════════════════                                                        │
│                                                                                  │
│      ┌─────────────────────────────────────────────────────┐                    │
│      │                 real_monitor (1 min)                 │                    │
│      │  - Sync with OKX positions                          │                    │
│      │  - Check SL/TP orders                              │                    │
│      │  - Alert on discrepancies                          │                    │
│      └─────────────────────────────────────────────────────┘                    │
│                              │                                                  │
│                              v                                                  │
│      ┌─────────────────────────────────────────────────────┐                    │
│      │                auto_guard (3 min)                    │                    │
│      │  - Check SL distance (<2% danger)                   │                    │
│      │  - Check liq distance (<3% danger)                  │                    │
│      │  - Trigger AI judgment if needed                    │                    │
│      └─────────────────────────────────────────────────────┘                    │
│                              │                                                  │
│                              v                                                  │
│      ┌─────────────────────────────────────────────────────┐                    │
│      │                heartbeat (1 hour)                    │                    │
│      │  - Record trade outcomes                           │                    │
│      │  - Update circuit breaker                          │                    │
│      │  - Generate health report                          │                    │
│      └─────────────────────────────────────────────────────┘                    │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## State Files

### State File Locations

| File | Path | Purpose |
|------|------|---------|
| `paper_trades.json` | `~/.hermes/cron/output/` | Paper trading records |
| `treasury.json` | `~/kronos/data/` | Account treasury tracking |
| `journal.json` | `~/kronos/data/` | Trade journal entries |
| `circuit.json` | `~/kronos/data/` | Circuit breaker state |
| `daily_dd.json` | `~/kronos/data/` | Daily drawdown records |
| `atr_watchlist_state.json` | `~/kronos/data/` | ATR watchlist state |
| `evolution_state.json` | `~/kronos/data/` | Strategy evolution state |
| `factor_context.json` | `~/kronos/` | IC factor weights |
| `decision_journal.jsonl` | `~/kronos/` | AI decision audit log |
| `dual_strategy_state.json` | `~/kronos/` | Dual strategy state |
| `multi_direction_state.json` | `~/kronos/` | Multi-direction state |
| `chaos_state.json` | `~/kronos/` | Chaos testing state |

### State File Formats

#### paper_trades.json
```json
[
  {
    "coin": "BTC",
    "direction": "long",
    "entry_price": 65000.0,
    "entry_time": "2024-04-26T10:00:00Z",
    "quantity": 0.01,
    "sl_price": 64200.0,
    "tp_price": 66500.0,
    "status": "OPEN",
    "exit_price": null,
    "exit_time": null,
    "pnl": null,
    "close_reason": null
  }
]
```

#### circuit.json
```json
{
  "consecutive_losses": 0,
  "last_outcome": "win",
  "is_tripped": false,
  "trip_reason": "",
  "trip_time": "",
  "losses_log": ["win", "win", "loss", "win"]
}
```

#### treasury.json
```json
{
  "balance": 10000.0,
  "reserved": 2000.0,
  "available": 8000.0,
  "last_update": "2024-04-26T10:00:00Z",
  "daily_loss": 0.0,
  "hourly_loss": 0.0
}
```

---

## Error Handling

### Failure Categories

| Category | Examples | Circuit Count? | Action |
|----------|----------|----------------|--------|
| **System Failure** | timestamp_error, insufficient_balance, timeout_sync | NO | Blacklist handled separately |
| **Trading Failure** | open_failed, close_failed | NO | Retry with backoff |
| **Real Loss** | TP triggered, SL triggered, manual close at loss | YES | Increment consecutive losses |

### Blacklisted Failures
```python
FAILURE_REASONS = {
    'insufficient_balance',
    'balance_insufficient', 
    'timestamp_error',
    'timeout_sync',
    'system_error',
    'open_failed'
}
```

---

## API Integration

### OKX API Headers
```python
{
    'OK-ACCESS-KEY': OKX_API_KEY,
    'OK-ACCESS-SIGN': signature,
    'OK-ACCESS-TIMESTAMP': timestamp,  # ISO8601 format
    'OK-ACCESS-PASSPHRASE': passphrase,
    'Content-Type': 'application/json',
    'x-simulated-trading': OKX_FLAG  # '0'=live, '1'=sim
}
```

### Feishu API
```python
# Get token
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
{
    "app_id": FEISHU_APP_ID,
    "app_secret": FEISHU_APP_SECRET
}

# Send message
POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id
{
    "receive_id": FEISHU_CHAT_ID,
    "msg_type": "text",
    "content": json.dumps({"text": "message"})
}
```

---

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| v5.0.0 | 2024-04-25 | 5-layer architecture, simulation mode |
| v4.x | 2024-04 | Multi-coin support, dual strategy |
| v3.x | 2024-03 | AI judgment integration |
| v2.x | 2024-02 | OKX native API |
| v1.x | 2024-01 | Initial version |
