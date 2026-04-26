# Kronos Backup and Disaster Recovery Documentation

## Overview

This document outlines the backup and disaster recovery procedures for the Kronos autonomous trading system. Following these procedures ensures minimal data loss and rapid recovery in case of system failures.

---

## 1. Critical Data Assets

### 1.1 State Files
| File | Path | Description | Criticality |
|------|------|-------------|-------------|
| `decision_journal.jsonl` | `~/kronos/decision_journal.jsonl` | AI decision audit log | **CRITICAL** |
| `paper_trades.json` | `~/.hermes/cron/output/paper_trades.json` | Paper trading records | **CRITICAL** |
| `treasury.json` | `~/kronos/data/treasury.json` | Balance tracking | **CRITICAL** |
| `circuit.json` | `~/kronos/data/circuit.json` | Circuit breaker state | **HIGH** |
| `performance.db` | `~/kronos/performance.db` | Performance metrics database | **HIGH** |

### 1.2 Configuration Files
| File | Path | Description |
|------|------|-------------|
| `constants.py` | `~/kronos/constants.py` | System-wide constants |
| `requirements.txt` | `~/kronos/requirements.txt` | Python dependencies |
| `*.json` state files | Various | Strategy states, positions |

### 1.3 Code Repository
- Git repository at `~/kronos/.git`
- Contains all trading strategies and system code

---

## 2. Backup Schedule

### 2.1 Automated Backups
| Frequency | Scope | Retention |
|-----------|-------|-----------|
| Every 6 hours | State files, journals | 7 days |
| Daily (00:00 UTC) | Full data backup | 30 days |
| Weekly (Sunday) | Complete system snapshot | 90 days |

### 2.2 Backup Script
Use `scripts/backup.sh` for automated backups:
```bash
./scripts/backup.sh          # Full backup
./scripts/backup.sh --state  # State files only
./scripts/backup.sh --verify # Verify backup integrity
```

---

## 3. Backup Procedures

### 3.1 Full System Backup
1. Stop all running Kronos processes
2. Execute backup script
3. Verify backup integrity
4. Restart processes

### 3.2 State Files Backup
Critical files to backup:
```bash
KRONOS_ROOT=~/kronos
BACKUP_DIR=~/kronos_backups

# State files
cp $KRONOS_ROOT/decision_journal.jsonl $BACKUP_DIR/
cp $KRONOS_ROOT/data/treasury.json $BACKUP_DIR/
cp $KRONOS_ROOT/data/circuit.json $BACKUP_DIR/
cp $KRONOS_ROOT/paper_trades.json $BACKUP_DIR/

# Configuration
cp $KRONOS_ROOT/constants.py $BACKUP_DIR/
cp $KRONOS_ROOT/requirements.txt $BACKUP_DIR/
```

### 3.3 Off-Site Backup
For disaster recovery, sync backups to remote storage:
```bash
rsync -avz ~/kronos_backups/ remote-server:/backups/kronos/
```

---

## 4. Disaster Recovery Procedures

### 4.1 System Failure Recovery

#### Scenario A: Single File Corruption
1. Identify corrupted file from error logs
2. Restore from latest backup:
   ```bash
   cp ~/kronos_backups/YYYYMMDD_HHMMSS/<filename> <destination>
   ```
3. Verify file integrity
4. Restart affected service

#### Scenario B: Complete System Failure
1. Assess damage extent
2. Retrieve backup from off-site storage if local backups unavailable
3. Restore in order:
   - System dependencies (`requirements.txt`)
   - Configuration files
   - State files
   - Code repository
4. Run health check: `./scripts/health_check.sh`
5. Verify trading system functionality in simulation mode
6. Gradually return to production

### 4.2 Recovery Time Objectives (RTO)

| Component | RTO | Priority |
|-----------|-----|----------|
| State files recovery | < 15 minutes | P1 |
| Full system restore | < 2 hours | P2 |
| Off-site backup retrieval | < 4 hours | P3 |

### 4.3 Recovery Point Objectives (RPO)

| Data Type | RPO | Backup Frequency |
|-----------|-----|-----------------|
| Decision journal | 6 hours | Every 6 hours |
| Trade positions | Real-time | Every trade |
| Treasury state | 6 hours | Every 6 hours |
| Performance data | 24 hours | Daily |

---

## 5. Health Monitoring

### 5.1 Health Check Script
Run `./scripts/health_check.sh` to verify system integrity:

```bash
./scripts/health_check.sh              # Full health check
./scripts/health_check.sh --quick      # Quick verification
./scripts/health_check.sh --components # Check individual components
```

### 5.2 Health Check Components
- **Disk Space**: Verify adequate storage
- **Process Status**: Check running Kronos processes
- **State Files**: Validate JSON integrity
- **API Connectivity**: Test OKX API connectivity
- **Log Rotation**: Verify logs are being written
- **Backup Status**: Confirm recent backups exist

### 5.3 Alerting Thresholds
| Metric | Warning | Critical |
|--------|---------|----------|
| Disk usage | > 70% | > 85% |
| Failed API calls (1h) | > 5 | > 20 |
| Missing backups | 1 day | > 2 days |
| Process downtime | > 5 min | > 15 min |

---

## 6. Restoration Procedures

### 6.1 Restoring State Files
```bash
# Stop system first
pkill -f kronos

# Restore from backup
BACKUP_DATE=20260426_120000
cp ~/kronos_backups/$BACKUP_DATE/decision_journal.jsonl ~/kronos/
cp ~/kronos_backups/$BACKUP_DATE/treasury.json ~/kronos/data/
cp ~/kronos_backups/$BACKUP_DATE/circuit.json ~/kronos/data/

# Verify and restart
./scripts/health_check.sh
python3 kronos_pilot.py
```

### 6.2 Restoring Code Repository
```bash
cd ~/kronos
git stash  # Save any uncommitted changes
git pull origin main
git stash pop  # Apply any saved changes
```

---

## 7. Backup Verification

### 7.1 Verification Checklist
- [ ] Backup completed without errors
- [ ] Backup file size is reasonable
- [ ] Files can be decrypted/extracted
- [ ] State files are valid JSON
- [ ] Database integrity check passes

### 7.2 Test Restore Procedure
Quarterly, perform a test restore to verify backup validity:
1. Create isolated test environment
2. Restore backup to test environment
3. Run health check on restored system
4. Verify data integrity
5. Document any issues found

---

## 8. Emergency Contacts

| Role | Contact | Responsibility |
|------|---------|-----------------|
| System Admin | (Configure) | Backup execution, restore |
| Trading Lead | (Configure) | Trading decisions during recovery |
| DevOps | (Configure) | Infrastructure support |

---

## 9. Appendix: Backup Script Usage

### 9.1 backup.sh Options
```bash
./scripts/backup.sh --help

Options:
  --full       Full system backup (default)
  --state      State files only
  --code       Code repository only
  --verify     Verify existing backup
  --clean      Remove old backups
  --dry-run    Show what would be done
```

### 9.2 health_check.sh Options
```bash
./scripts/health_check.sh --help

Options:
  --quick       Quick health check (30 seconds)
  --full        Comprehensive health check
  --components  Check individual components
  --report      Generate HTML report
```

---

*Document Version: 1.0.0*
*Last Updated: 2026-04-26*
*Maintainer: Kronos DevOps Team*