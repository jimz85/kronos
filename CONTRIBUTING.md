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

- **Logging**: Use the centralized logging system:
  ```python
  import logging
  logger = logging.getLogger('kronos.module_name')
  ```

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

2. **Commit Messages**: Use clear, descriptive messages:
   - `Add: new feature description`
   - `Fix: bug description`
   - `Refactor: module improvement`

3. **Testing**:
   - Test in simulation mode first
   - Verify no breaking changes to existing functionality
   - Check circuit breaker logic remains intact

4. **Documentation**:
   - Update `CLAUDE.md` if introducing new patterns
   - Add docstrings to new functions
   - Update README if adding new features

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
- Error logs
- Steps to reproduce

## License

By contributing to Kronos, you agree that your contributions will be licensed under the project's license.

## Questions?

Review `CLAUDE.md` for detailed architecture information, or refer to `README.md` for project overview.
