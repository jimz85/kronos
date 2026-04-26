# Kronos System Health Dashboard

**Version**: v5.0.0  
**Last Updated**: 2026-04-26

---

## 1. System Architecture Overview

Kronos is an autonomous cryptocurrency trading system with a 5-layer architecture:

```
┌─────────────────────────────────────────────────────────────┐
│                    ENTRY POINTS                             │
│  kronos_pilot.py │ kronos_auto_guard.py │ kronos_heartbeat  │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                   5-LAYER ARCHITECTURE                       │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: Core     │ Constants, config, indicators           │
│  Layer 2: Strategies│ Regime, Alpha, Beta engines            │
│  Layer 3: Models   │ Confidence scorer, position sizer        │
│  Layer 4: Risk     │ Circuit breaker, trailing stop          │
│  Layer 5: Data     │ ATR watchlist, evolution                │
│  Layer 6: Execution│ Execution (reserved)                    │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                    EXTERNAL SERVICES                         │
│         OKX Exchange (Simulation/Live) │ Feishu │           │
└─────────────────────────────────────────────────────────────┘
```

### Key Components

| Component | File | Purpose |
|-----------|------|---------|
| Main Trading Loop | `kronos_pilot.py` | Signal generation, paper trade execution |
| Safety Monitor | `kronos_auto_guard.py` | Danger detection (SL, liquidation) |
| Hourly Health | `kronos_heartbeat.py` | Health checks, circuit breaker updates |
| Position Monitor | `real_monitor.py` | Real/fake position synchronization |
| Trade Journal | `kronos_journal.py` | Trade audit and statistics |

### State Files (Critical)

| File | Purpose |
|------|---------|
| `paper_trades.json` | Paper trading records (source of truth) |
| `data/treasury.json` | Balance and treasury tracking |
| `data/circuit.json` | Circuit breaker state |
| `decision_journal.jsonl` | AI decision audit log |

---

## 2. Cron Job Status Table

| Cron Job | Schedule | Script | Last Check | Status | Notes |
|----------|----------|--------|------------|--------|-------|
| Health Beat | Hourly | `kronos_heartbeat.py` | - | ⏳ Pending | Updates circuit breaker, records trade outcomes |
| Pilot Run | Every 15min | `kronos_pilot.py` | - | ⏳ Pending | Main signal generation |
| Auto Guard | Every 5min | `kronos_auto_guard.py` | - | ⏳ Pending | Safety monitoring |
| Position Sync | Every 5min | `real_monitor.py` | - | ⏳ Pending | Position synchronization |
| Journal Update | On trade | `kronos_journal.py` | - | ⏳ Pending | Trade logging |

### Cron Status Legend

- ✅ **Healthy**: Running normally
- ⚠️ **Warning**: Delayed or intermittent issues
- ❌ **Critical**: Failed or not running
- ⏳ **Pending**: Awaiting execution

---

## 3. Key Metrics to Monitor

### Trading Metrics

| Metric | Description | Good | Warning | Critical |
|--------|-------------|------|---------|----------|
| Win Rate | Percentage of profitable trades | > 55% | 45-55% | < 45% |
| PnL Ratio | Average win/loss ratio | > 1.5 | 1.0-1.5 | < 1.0 |
| Daily Return | Daily profit/loss percentage | > 0% | -2% to 0% | < -2% |
| Hourly Loss | Hourly loss vs treasury limit (2%) | < 1% | 1-2% | > 2% |
| Consecutive Losses | Circuit breaker loss streak | 0-2 | 3-4 | ≥ 5 |

### System Metrics

| Metric | Description | Good | Warning | Critical |
|--------|-------------|------|---------|----------|
| API Latency | OKX API response time | < 500ms | 500-1000ms | > 1000ms |
| Heartbeat Age | Time since last heartbeat | < 2hrs | 2-4hrs | > 4hrs |
| Position Sync | Fence position vs real position | < 1% | 1-5% | > 5% |
| Treasury Reserve | Available reserve percentage | > 30% | 20-30% | < 20% |

### Risk Metrics

| Metric | Description | Limit |
|--------|-------------|-------|
| Per Trade Risk | Maximum risk per trade | 1% |
| Hourly Loss Limit | Maximum hourly loss | 2% |
| Daily Loss Limit | Maximum daily loss | 5% |
| Reserve Keep | Minimum reserve to maintain | 20% |

---

## 4. Health Check Commands

### Basic System Checks

```bash
# Check if Kronos is running
ps aux | grep kronos | grep -v grep

# Check recent logs
tail -100 ~/.hermes/cron/logs/kronos_*.log

# Verify OKX connection
python3 -c "from data.okx_client import OKXClient; c = OKXClient(); print(c.ping())"
```

### State File Checks

```bash
# Check paper trades
cat ~/.hermes/cron/output/paper_trades.json | python3 -m json.tool | head -50

# Check treasury status
cat ~/kronos/data/treasury.json | python3 -m json.tool

# Check circuit breaker state
cat ~/kronos/data/circuit.json | python3 -m json.tool

# Verify circuit breaker not tripped
python3 -c "import json; c=json.load(open('data/circuit.json')); print('Circuit:', c.get('consecutive_losses', 0), 'losses')"
```

### Position and Balance Checks

```bash
# Check current positions
python3 -c "from real_monitor import get_real_positions; print(get_real_positions())"

# Check account balance
python3 -c "from real_monitor import get_account_balance; print(get_account_balance())"

# Verify simulation mode
echo "OKX_FLAG: $OKX_FLAG"
```

### Cron Job Checks

```bash
# Check cron is running
ps aux | grep cron | grep -v grep

# List Kronos cron jobs
crontab -l | grep kronos

# Check cron execution log
grep -i kronos /var/log/syslog | tail -20
```

### Pattern Whitelist Check

```bash
# View pattern whitelist
cat ~/kronos/data/pattern_whitelist.json 2>/dev/null | python3 -m json.tool | head -30
```

---

## 5. Alert Thresholds

### 🚨 Critical Alerts (Immediate Action Required)

| Alert | Threshold | Action |
|-------|-----------|--------|
| Circuit Breaker Tripped | consecutive_losses ≥ 5 | Stop trading, review losses |
| Daily Loss Exceeded | daily_loss_pct ≥ 5% | Halt all trading |
| Hourly Loss Exceeded | hourly_loss_pct ≥ 2% | Pause for 1 hour |
| API Timeout | latency > 5000ms | Check OKX status |
| Treasury Reserve Low | reserve_pct < 10% | Emergency stop |
| Position Mismatch | sync_diff > 10% | Force sync, verify trades |

### ⚠️ Warning Alerts (Investigate Soon)

| Alert | Threshold | Action |
|-------|-----------|--------|
| Consecutive Losses | consecutive_losses ≥ 3 | Review strategy |
| Hourly Loss High | hourly_loss_pct ≥ 1.5% | Monitor closely |
| API Latency High | latency > 1000ms | Check connection |
| Win Rate Low | win_rate < 50% | Review signal quality |
| Heartbeat Delayed | age > 2 hours | Check cron/health |
| Reserve Low | reserve_pct < 20% | Reduce position size |

### 📊 Informational Alerts (Monitor)

| Alert | Threshold | Action |
|-------|-----------|--------|
| New Pattern Learned | pattern added | Log for review |
| Regime Change | regime != previous | Update strategy |
| High Volatility | ATR > 2x normal | Increase caution |
| Large Position | size > 50% of max | Monitor risk |

---

## Quick Health Check Script

```bash
#!/bin/bash
# Quick health check for Kronos

echo "=== Kronos Health Check ==="
echo ""

echo "1. Process Check:"
ps aux | grep -E "kronos_(pilot|heartbeat|autoguard|monitor)" | grep -v grep || echo "   ⚠️ No Kronos processes found"
echo ""

echo "2. OKX Mode:"
echo "   OKX_FLAG: ${OKX_FLAG:-1} (1=sim, 0=live)"
echo ""

echo "3. Circuit Breaker:"
CB=$(cat ~/kronos/data/circuit.json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('consecutive_losses', 'N/A'))" 2>/dev/null || echo "N/A")
echo "   Consecutive Losses: $CB"
[ "$CB" != "N/A" ] && [ "$CB" -ge 5 ] && echo "   🚨 CRITICAL: Circuit breaker tripped!"
echo ""

echo "4. Treasury:"
python3 -c "import json; t=json.load(open('${HOME}/kronos/data/treasury.json')); print(f'   Reserve: {t.get(\"reserve_pct\", 0)*100:.1f}%')" 2>/dev/null || echo "   ⚠️ Treasury file not found"
echo ""

echo "5. Recent Heartbeat:"
find ~/kronos -name "*.log" -mmin -60 2>/dev/null | head -3 | xargs tail -1 2>/dev/null || echo "   ⚠️ No recent logs"
echo ""

echo "=== Check Complete ==="
```

---

## Contact & Escalation

- **System Owner**: Kronos Trading Team
- **On-Call**: Check Feishu alert channels
- **Documentation**: See also `deployment.md`, `high_availability.md`

---

*End of System Health Dashboard*