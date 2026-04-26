#!/usr/bin/env make -f

# Kronos - Autonomous Crypto Trading System
# Common Makefile Commands

.PHONY: help test lint backup deploy clean run-pilot run-guard docker-build docker-up docker-down

# Default target
help:
	@echo "Kronos Trading System - Available Commands"
	@echo "=========================================="
	@echo "  make test          - Run all tests"
	@echo "  make test TESTS=<f> - Run specific test file"
	@echo "  make lint          - Run code linting"
	@echo "  make format        - Format code"
	@echo "  make backup        - Create timestamped backup"
	@echo "  make deploy        - Deploy using Docker"
	@echo "  make docker-build  - Build Docker image"
	@echo "  make docker-up     - Start Docker containers"
	@echo "  make docker-down   - Stop Docker containers"
	@echo "  make clean         - Clean cache files"
	@echo "  make run-pilot     - Run kronos_pilot.py"
	@echo "  make run-guard     - Run kronos_auto_guard.py"
	@echo "  make run-monitor   - Run real_monitor.py"
	@echo "  make run-heartbeat - Run kronos_heartbeat.py"
	@echo "  make install       - Install dependencies"
	@echo "  make journal       - Run trade journal"
	@echo "  make status        - Show system status"

# Test commands
test:
	@echo "Running tests..."
	@if [ -n "$(TESTS)" ]; then \
		python -m pytest $(TESTS) -v; \
	else \
		python -m pytest tests/ -v; \
	fi

test-stress:
	@echo "Running stress tests..."
	python -m pytest tests/stress_test_atomic_write.py -v

# Linting
lint:
	@echo "Running pylint..."
	@pylint kronos_*.py core/ strategies/ models/ risk/ data/ --disable=C0111,R0913,R0914 2>/dev/null || \
		echo "pylint not available, skipping..."

lint-flake:
	@echo "Running flake8..."
	@flake8 kronos_*.py core/ strategies/ models/ risk/ data/ --max-line-length=120 2>/dev/null || \
		echo "flake8 not available, skipping..."

format:
	@echo "Formatting code with black..."
	@black kronos_*.py core/ strategies/ models/ risk/ data/ 2>/dev/null || \
		echo "black not available, skipping..."

# Backup commands
backup:
	@echo "Creating backup..."
	@DATE=$(shell date +%Y%m%d_%H%M%S); \
	mkdir -p backups; \
	tar -czf backups/kronos_backup_$$DATE.tar.gz \
		--exclude='*.pyc' \
		--exclude='__pycache__' \
		--exclude='.git' \
		--exclude='backups' \
		--exclude='venv' \
		. && \
	echo "Backup created: backups/kronos_backup_$$DATE.tar.gz"

backup-state:
	@echo "Backing up state files..."
	@DATE=$(shell date +%Y%m%d_%H%M%S); \
	mkdir -p backups; \
	cp -r data/ backups/data_$$DATE 2>/dev/null || true; \
	cp *.json backups/ 2>/dev/null || true; \
	cp *.jsonl backups/ 2>/dev/null || true; \
	echo "State backup complete"

# Docker commands
docker-build:
	@echo "Building Docker image..."
	docker build -t kronos:latest .

docker-up:
	@echo "Starting Docker containers..."
	docker-compose up -d

docker-down:
	@echo "Stopping Docker containers..."
	docker-compose down

docker-logs:
	@echo "Showing Docker logs..."
	docker-compose logs -f

# Deploy
deploy: docker-build docker-up
	@echo "Deployment complete!"

# Run commands
run-pilot:
	@echo "Starting Kronos Pilot..."
	python kronos_pilot.py

run-guard:
	@echo "Starting Auto Guard..."
	python kronos_auto_guard.py

run-monitor:
	@echo "Starting Real Monitor..."
	python real_monitor.py

run-heartbeat:
	@echo "Starting Heartbeat..."
	python kronos_heartbeat.py

run-journal:
	@echo "Starting Trade Journal..."
	python kronos_journal.py

# System status
status:
	@echo "=== Kronos System Status ==="
	@echo ""
	@echo "Version:"
	@grep "SYSTEM_VERSION" core/constants.py 2>/dev/null || echo "  Unknown"
	@echo ""
	@echo "Simulation Mode:"
	@python -c "import os; print('  OKX_FLAG =', os.getenv('OKX_FLAG', '1'))" 2>/dev/null || echo "  Unknown"
	@echo ""
	@echo "Recent Trades:"
	@tail -5 decision_journal.jsonl 2>/dev/null | python -c "import sys,json; [print(' ', json.loads(l).get('action','?'), json.loads(l).get('coin','?')) for l in sys.stdin]" 2>/dev/null || echo "  No recent trades"
	@echo ""
	@echo "Circuit Breaker:"
	@cat data/circuit.json 2>/dev/null | python -m json.tool 2>/dev/null || echo "  No circuit state"

# Install dependencies
install:
	@echo "Installing dependencies..."
	pip install -r requirements.txt

install-dev:
	@echo "Installing dev dependencies..."
	pip install -r requirements.txt
	pip install pytest pylint black flake8

# Clean
clean:
	@echo "Cleaning cache files..."
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Clean complete"

clean-logs:
	@echo "Cleaning log files..."
	@rm -f *.log 2>/dev/null || true
	@rm -f logs/*.log 2>/dev/null || true
	@echo "Log files cleaned"

# Development helpers
dev-setup: install-dev
	@echo "Setting up development environment..."
	@mkdir -p data logs backups

git-status:
	@echo "=== Git Status ==="
	@git status --short
	@echo ""
	@echo "=== Recent Commits ==="
	@git log --oneline -5
