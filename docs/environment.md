# Kronos Environment Configuration

**Version**: v5.0.0  
**Last Updated**: 2026-04-26

## Environment Overview

Kronos uses a layered configuration system to manage different deployment environments (development, simulation, production).

## Environment Variables

### Core Configuration

| Variable | Type | Required | Description |
|----------|------|----------|-------------|
| `OKX_API_KEY` | string | Yes | OKX exchange API key |
| `OKX_SECRET_KEY` | string | Yes | OKX exchange secret key |
| `OKX_PASSPHRASE` | string | Yes | OKX API passphrase |
| `OKX_FLAG` | string | No | Trading mode: `0`=live, `1`=sim (default: `1`) |
| `FEISHU_WEBHOOK` | string | No | Feishu webhook URL for notifications |
| `LOG_LEVEL` | string | No | Logging level: DEBUG, INFO, WARNING, ERROR |
| `ROOT_DIR` | string | No | Override root directory path |

### OKX_FLAG Values

| Value | Mode | Risk Level | Description |
|-------|------|------------|-------------|
| `0` | Live Trading | HIGH | Real money, real orders |
| `1` | Simulation | LOW | Paper trading, test mode (default) |

## Configuration Files

### State Files

| File | Location | Purpose |
|------|----------|---------|
| `treasury.json` | `data/` | Balance and treasury tracking |
| `circuit.json` | `data/` | Circuit breaker state |
| `paper_trades.json` | Root | Paper trading positions |
| `decision_journal.jsonl` | Root | AI decision audit log |
| `emergency_stop.json` | Root | Emergency stop flag |

### Configuration Schema

#### treasury.json

```json
{
  "balance": 10000.0,
  "reserve_pct": 0.20,
  "hourly_loss_pct": 0.02,
  "daily_loss_pct": 0.05,
  "per_trade_pct": 0.01,
  "last_updated": "2026-04-26T12:00:00Z"
}
```

#### circuit.json

```json
{
  "consecutive_losses": 0,
  "max_consecutive_losses": 5,
  "last_reset": "2026-04-26T08:00:00Z",
  "total_trades": 150,
  "win_rate": 0.65
}
```

## Path Configuration

All paths are absolute and rooted at the project directory:

```
kronos/
├── data/          # State and cache data
├── logs/          # Application logs
├── core/          # Core modules
├── strategies/    # Trading strategies
├── models/        # ML models
├── risk/          # Risk management
└── docs/          # Documentation
```

## Environment-Specific Settings

### Development

```bash
export OKX_FLAG=1
export LOG_LEVEL=DEBUG
```

### Simulation

```bash
export OKX_FLAG=1
export LOG_LEVEL=INFO
```

### Production

```bash
export OKX_FLAG=0
export LOG_LEVEL=WARNING
```

## Configuration Loading

The system loads configuration in the following order:

1. Environment variables (highest priority)
2. Configuration files (`constants.py`, `core/config.py`)
3. Default values (lowest priority)

### Example: Loading Configuration

```python
from core.config import load_config

config = load_config()
okx_key = config.get('OKX_API_KEY', os.getenv('OKX_API_KEY'))
```

## Safety Limits

| Limit | Value | Description |
|-------|-------|-------------|
| Max hourly loss | 2% | Maximum allowed hourly loss percentage |
| Max daily loss | 5% | Maximum allowed daily loss percentage |
| Per trade max | 1% | Maximum position size per trade |
| Reserve balance | 20% | Minimum balance to maintain |
| Consecutive losses | 5 | Circuit breaker trigger count |

## Secret Management

### Best Practices

1. **Never commit secrets** to version control
2. **Use `.env` files** for local development
3. **Use environment variables** in production
4. **Rotate API keys** regularly

### Local Development Setup

```bash
# Create .env file (never commit this)
cat > .env << EOF
OKX_API_KEY=your_api_key
OKX_SECRET_KEY=your_secret_key
OKX_PASSPHRASE=your_passphrase
OKX_FLAG=1
FEISHU_WEBHOOK=https://your webhook
EOF
```

### Production Setup

```bash
# Set environment variables
export OKX_API_KEY="your_production_key"
export OKX_SECRET_KEY="your_production_secret"
export OKX_PASSPHRASE="your_production_passphrase"
export OKX_FLAG="0"
```

## Validation

On startup, Kronos validates:

- [ ] OKX API credentials are present
- [ ] OKX_FLAG is set to valid value (0 or 1)
- [ ] Data directory exists and is writable
- [ ] State files have valid JSON format
- [ ] Treasury balance meets minimum requirements

## Environment Variables Reference

```bash
# Core OKX Configuration
export OKX_API_KEY="8aba4d..."       # Your OKX API key
export OKX_SECRET_KEY="..."          # Your OKX secret key
export OKX_PASSPHRASE="..."          # Your OKX passphrase

# Trading Mode
export OKX_FLAG="1"                  # 0=live, 1=simulation

# Notifications
export FEISHU_WEBHOOK="https://..."  # Feishu webhook URL

# Logging
export LOG_LEVEL="INFO"              # DEBUG, INFO, WARNING, ERROR

# Paths
export ROOT_DIR="/path/to/kronos"    # Override default root
```