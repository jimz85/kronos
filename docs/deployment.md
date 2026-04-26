# Kronos Deployment Guide

**Version**: v5.0.0  
**Last Updated**: 2026-04-26

## Overview

Kronos is an autonomous cryptocurrency trading system with a 5-layer architecture. This guide covers deployment procedures for production environments.

## Prerequisites

- Python 3.11+
- OKX Exchange account (Simulation or Live)
- Docker (optional, for containerized deployment)
- Access to Feishu webhook for notifications

## Installation

### Standard Installation

```bash
# Clone the repository
git clone https://github.com/your-repo/kronos.git
cd kronos

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Docker Deployment

```bash
# Build the Docker image
docker build -t kronos:latest .

# Run with docker-compose
docker-compose up -d
```

## Configuration

### Environment Variables

Set the following environment variables before running:

| Variable | Required | Description | Default |
|----------|----------|-------------|---------|
| `OKX_API_KEY` | Yes | OKX API key | - |
| `OKX_SECRET_KEY` | Yes | OKX secret key | - |
| `OKX_PASSPHRASE` | Yes | OKX passphrase | - |
| `OKX_FLAG` | No | Trading mode (0=live, 1=sim) | `1` |
| `FEISHU_WEBHOOK` | No | Feishu notification webhook | - |
| `LOG_LEVEL` | No | Logging level | `INFO` |

### Required Configuration Files

```bash
# Create data directory
mkdir -p data

# Initialize state files
touch data/treasury.json
touch data/circuit.json
touch paper_trades.json
```

## Running the System

### 1. Main Trading Loop

```bash
python kronos_pilot.py
```

### 2. Safety Monitor

```bash
python kronos_auto_guard.py
```

### 3. Hourly Heartbeat

```bash
python kronos_heartbeat.py
```

### 4. Real Position Monitor

```bash
python real_monitor.py
```

## Deployment Modes

### Simulation Mode (Default)

```bash
export OKX_FLAG=1
python kronos_pilot.py
```

Paper trading with no real capital at risk.

### Live Trading Mode

```bash
export OKX_FLAG=0
python kronos_pilot.py
```

**WARNING**: Live trading involves real financial risk. Ensure all safety systems are operational before enabling.

## Docker Compose Configuration

```yaml
version: '3.8'
services:
  kronos:
    build: .
    environment:
      - OKX_API_KEY=${OKX_API_KEY}
      - OKX_SECRET_KEY=${OKX_SECRET_KEY}
      - OKX_PASSPHRASE=${OKX_PASSPHRASE}
      - OKX_FLAG=1
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    restart: unless-stopped
```

## Health Checks

### System Health

```bash
# Check if trading loop is running
ps aux | grep kronos_pilot

# Check recent logs
tail -n 100 logs/kronos.log
```

### Circuit Breaker Status

Monitor `data/circuit.json` for consecutive loss tracking:

```json
{
  "consecutive_losses": 0,
  "last_reset": "2026-04-26T12:00:00Z",
  "total_trades": 150
}
```

## Troubleshooting

### Common Issues

1. **API Connection Failed**
   - Verify OKX API credentials
   - Check network connectivity
   - Ensure IP whitelist is configured on OKX

2. **State File Corruption**
   - Use `atomic_write_json()` for state updates
   - Keep backups of `paper_trades.json` and `data/` directory

3. **High Memory Usage**
   - Clear old log files in `logs/`
   - Archive old decision journal entries

## Maintenance

### Regular Tasks

- **Daily**: Review decision journal (`decision_journal.jsonl`)
- **Weekly**: Check circuit breaker state and treasury balance
- **Monthly**: Backup state files and audit trading performance

### Backup Procedure

```bash
# Backup critical files
tar -czf kronos_backup_$(date +%Y%m%d).tar.gz \
  data/ \
  paper_trades.json \
  decision_journal.jsonl \
  *.json
```

## Security Considerations

1. **Never expose API keys** in version control
2. **Use environment variables** for sensitive configuration
3. **Enable 2FA** on OKX account
4. **Set IP whitelist** on OKX API settings
5. **Monitor unauthorized access** in OKX activity log

## Support

For issues or questions, review:
- `ARCHITECTURE.md` - System architecture details
- `CLAUDE.md` - Developer guide
- `MEMORY.md` - System memory and state management