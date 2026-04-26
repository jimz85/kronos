# Changelog

All notable changes to the Kronos autonomous crypto trading system will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [5.0.0] - 2026-04-26

### Added
- **Phase 0-4 Architecture Refactor**: Complete 5-layer system architecture (Core → Strategies → Models → Risk → Data)
- **OKX Automated Trading System**: Full integration with OKX exchange for automated crypto trading
- **Kronos Pilot**: Main trading loop with signal generation and paper trading
- **Kronos Auto Guard**: Safety monitor with danger detection and Feishu notifications
- **Kronos Heartbeat**: Hourly health checks and circuit breaker management
- **Real Monitor**: Position synchronization with OKX exchange
- **Trade Journal**: Comprehensive trade logging and statistics
- **Gemma4 Integration**: ML-based signal generation with Gemma4 heretic model
- **Multi-timeframe Analysis**: Support for 1m, 5m, 15m, 1h, 4h, 1d timeframes
- **Pattern Library**: Whitelist-based pattern recognition system
- **Voting System**: Multi-signal consensus for trade decisions
- **Chaos Drill**: Stress testing framework for system resilience
- **Slippage Shield**: Advanced slippage protection mechanisms
- **Signal Factory**: Centralized signal generation and processing
- **Docker Support**: Containerized deployment with docker-compose

### Changed
- **Print to Logging Refactor**: Migrated from print statements to proper logging infrastructure
- **Backtest Modularization**: Separated backtest engine into reusable modules
- **ML Training Pipeline**: Improved machine learning training workflow
- **Unified Logging Configuration**: Centralized logging setup across all modules
- **IC Threshold Adjustment**: Set to 2 trades minimum for signal confidence
- **Dynamic Coin Filtering**: Improved filtering mechanism with XRP/BNB support

### Fixed
- **OKX Clock Synchronization**: Fixed timestamp issues with OKX API
- **Circuit Breaker Logic**: Corrected loss tracking and circuit breaker triggers
- **Field Naming**: Standardized field names across modules
- **Path Handling**: Fixed absolute path usage throughout codebase
- **reduceOnly Orders**: Fixed order type for position reduction
- **Signal Expiration**: Proper handling of stale signals
- **Duplicate OCO Prevention**: Added idempotency checks for SL/TP orders
- **Feishu APP_ID Environment Variable**: Fixed configuration loading
- **Log Rotation**: Added proper log file rotation
- **Autonomous Hardcoding**: Removed hardcoded values in autonomous mode

### Removed
- **Legacy v1-v3 Research Files**: Cleaned up deprecated research files (-5589 lines)

## [4.0.0] - Earlier

### Previous Versions
- See git history for full changelog of earlier releases
- Commit history available via `git log`

---

## Version History

| Version | Date | Status |
|---------|------|--------|
| 5.0.0 | 2026-04-26 | Current |
| 4.0.0 | Earlier | Legacy |

---

## How to Generate This Changelog

This changelog is auto-generated from git commit history. To regenerate:

```bash
# Show all commits since last tag
git log --oneline --decorate

# Show commits for a specific version
git log v5.0.0..HEAD --oneline

# Show commits with full details
git log --format="%h %s" --graph
```

## Contribution Guidelines

When contributing to Kronos:
1. Use semantic commit messages (e.g., `feat:`, `fix:`, `docs:`)
2. Add entry to this changelog for notable changes
3. Update version number in `core/constants.py`
4. Ensure all tests pass before submitting

---

*Generated on: 2026-04-26*
