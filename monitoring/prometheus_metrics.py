#!/usr/bin/env python3
"""
Prometheus Metrics Exporter for Kronos
=======================================
暴露Prometheus格式的监控指标，供Prometheus服务器抓取。
支持Grafana仪表盘可视化。

使用方法:
    # 启动HTTP服务器 (默认端口: 9090)
    python prometheus_metrics.py
    
    # 或在Gunicorn中运行
    gunicorn prometheus_metrics:app -b 0.0.0.0:9090

Metrics Exposed:
    - kronos_trades_total{status, coin, side}
    - kronos_trade_pnl{coin, side}
    - kronos_equity_current
    - kronos_equity_start
    - kronos_equity_pct_change
    - kronos_positions_open
    - kronos_position_pnl{coin, side}
    - kronos_circuit_breaker_tripped
    - kronos_circuit_consecutive_losses
    - kronos_last_trade_timestamp
    - kronos_last_heartbeat_timestamp
    - kronos_guard_runs_total
    - kronos_guard_dangers_detected{severity}
    - kronos_api_errors_total{endpoint, error_type}
    - kronos_treasury_hourly_loss
    - kronos_treasury_daily_loss

Version: 1.0.0
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps
import threading

# Flask for HTTP server
try:
    from flask import Flask, Response, jsonify
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    print("Warning: Flask not installed. Run: pip install flask")

# Prometheus client
try:
    from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    print("Warning: prometheus_client not installed. Run: pip install prometheus-client")

# Swagger support
try:
    from flasgger import Swagger
    SWAGGER_AVAILABLE = True
except ImportError:
    SWAGGER_AVAILABLE = False
    print("Warning: flasgger not installed. Run: pip install flasgger")

logger = logging.getLogger('kronos.prometheus')

# ============ Constants ============
ROOT = Path.home() / 'kronos'
STATE_DIR = Path.home() / '.hermes' / 'cron' / 'output'
PROMETHEUS_PORT = int(os.getenv('PROMETHEUS_PORT', '9090'))

# ============ Prometheus Metrics Definitions ============
if PROMETHEUS_AVAILABLE:
    # Trade metrics
    TRADES_TOTAL = Counter(
        'kronos_trades_total',
        'Total number of trades',
        ['status', 'coin', 'side']
    )
    TRADE_PNL = Gauge(
        'kronos_trade_pnl',
        'Realized PnL per trade',
        ['coin', 'side']
    )
    
    # Equity metrics
    EQUITY_CURRENT = Gauge(
        'kronos_equity_current',
        'Current account equity in USDT'
    )
    EQUITY_START = Gauge(
        'kronos_equity_start',
        'Starting account equity in USDT'
    )
    EQUITY_PCT_CHANGE = Gauge(
        'kronos_equity_pct_change',
        'Equity percentage change from start'
    )
    
    # Position metrics
    POSITIONS_OPEN = Gauge(
        'kronos_positions_open',
        'Number of currently open positions'
    )
    POSITION_PNL = Gauge(
        'kronos_position_pnl',
        'Unrealized PnL per position',
        ['coin', 'side']
    )
    POSITION_ENTRY_PRICE = Gauge(
        'kronos_position_entry_price',
        'Entry price per position',
        ['coin', 'side']
    )
    POSITION_SIZE = Gauge(
        'kronos_position_size',
        'Position size',
        ['coin', 'side']
    )
    
    # Circuit breaker metrics
    CIRCUIT_TRIPPED = Gauge(
        'kronos_circuit_breaker_tripped',
        'Whether circuit breaker is tripped (1=tripped, 0=normal)'
    )
    CIRCUIT_CONSECUTIVE_LOSSES = Gauge(
        'kronos_circuit_consecutive_losses',
        'Current consecutive losses count'
    )
    
    # Timing metrics
    LAST_TRADE_TIMESTAMP = Gauge(
        'kronos_last_trade_timestamp',
        'Unix timestamp of last trade'
    )
    LAST_HEARTBEAT_TIMESTAMP = Gauge(
        'kronos_last_heartbeat_timestamp',
        'Unix timestamp of last heartbeat'
    )
    
    # Guard metrics
    GUARD_RUNS_TOTAL = Counter(
        'kronos_guard_runs_total',
        'Total number of guard runs',
        ['result']
    )
    GUARD_DANGERS_DETECTED = Counter(
        'kronos_guard_dangers_detected',
        'Number of dangers detected by guard',
        ['severity', 'coin']
    )
    
    # API error metrics
    API_ERRORS_TOTAL = Counter(
        'kronos_api_errors_total',
        'Total API errors',
        ['endpoint', 'error_type']
    )
    
    # Treasury metrics
    TREASURY_HOURLY_LOSS = Gauge(
        'kronos_treasury_hourly_loss',
        'Hourly loss in USDT'
    )
    TREASURY_DAILY_LOSS = Gauge(
        'kronos_treasury_daily_loss',
        'Daily loss in USDT'
    )
    
    # System health metrics
    SYSTEM_UPTIME = Gauge(
        'kronos_system_uptime_seconds',
        'System uptime in seconds'
    )
    SYSTEM_VERSION = Info(
        'kronos_system',
        'Kronos system information'
    )

# ============ Data Loading Functions ============

def load_paper_trades():
    """Load paper trading records."""
    path = STATE_DIR / 'paper_trades.json'
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []


def load_circuit_state():
    """Load circuit breaker state."""
    path = STATE_DIR / 'kronos_circuit.json'
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {
            'consecutive_losses': 0,
            'last_outcome': None,
            'is_tripped': False,
            'trip_reason': '',
            'trip_time': '',
            'losses_log': [],
        }


def load_treasury_state():
    """Load treasury state."""
    path = ROOT / 'data' / 'treasury.json'
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}


def load_positions():
    """Load current positions from real_monitor."""
    try:
        sys.path.insert(0, str(ROOT))
        from real_monitor import get_real_positions
        positions, err = get_real_positions()
        return positions if not err else {}
    except:
        return {}


# ============ Metrics Collection ============

class MetricsCollector:
    """Collects and updates all Prometheus metrics."""
    
    def __init__(self):
        self.start_time = time.time()
        self.last_trade_time = 0
        self.last_heartbeat_time = 0
        self._lock = threading.Lock()
        
    def collect_all(self):
        """Collect all metrics from data sources."""
        with self._lock:
            self._collect_trade_metrics()
            self._collect_equity_metrics()
            self._collect_position_metrics()
            self._collect_circuit_metrics()
            self._collect_treasury_metrics()
            self._collect_system_metrics()
            
    def _collect_trade_metrics(self):
        """Collect trade-related metrics."""
        trades = load_paper_trades()
        
        # Count trades by status
        statuses = {'OPEN': 0, 'CLOSED': 0, 'FAILED': 0}
        coins = set()
        
        for t in trades:
            status = t.get('status', 'UNKNOWN')
            if status in statuses:
                statuses[status] += 1
            coin = t.get('coin', 'UNKNOWN')
            coins.add(coin)
            
            # Track last trade time
            open_time = t.get('open_time', '')
            if open_time:
                try:
                    ts = datetime.fromisoformat(open_time.replace('Z', '+00:00'))
                    ts = ts.timestamp()
                    if ts > self.last_trade_time:
                        self.last_trade_time = ts
                except:
                    pass
        
        # Update counters
        if PROMETHEUS_AVAILABLE:
            for status, count in statuses.items():
                for coin in coins:
                    TRADES_TOTAL.labels(status=status, coin=coin, side='unknown')._value.set(0)
            
            # This is a bit hacky - we just increment by status
            # Real implementation would track individual trades
            LAST_TRADE_TIMESTAMP.set(self.last_trade_time)
            
    def _collect_equity_metrics(self):
        """Collect equity metrics."""
        try:
            sys.path.insert(0, str(ROOT))
            from real_monitor import get_account_balance, START_BALANCE
            
            equity, err = get_account_balance()
            if not err and equity:
                if PROMETHEUS_AVAILABLE:
                    EQUITY_CURRENT.set(equity)
                    EQUITY_START.set(START_BALANCE)
                    pct_change = (equity - START_BALANCE) / START_BALANCE * 100
                    EQUITY_PCT_CHANGE.set(pct_change)
        except Exception as e:
            logger.warning(f"Failed to collect equity metrics: {e}")
            
    def _collect_position_metrics(self):
        """Collect position metrics."""
        positions = load_positions()
        
        if PROMETHEUS_AVAILABLE:
            POSITIONS_OPEN.set(len(positions))
            
            for coin, pos in positions.items():
                side = pos.get('side', 'unknown')
                pnl = pos.get('unrealized_pnl', 0)
                entry = pos.get('entry', 0)
                size = pos.get('size', 0)
                
                POSITION_PNL.labels(coin=coin, side=side).set(pnl)
                POSITION_ENTRY_PRICE.labels(coin=coin, side=side).set(entry)
                POSITION_SIZE.labels(coin=coin, side=side).set(size)
                
    def _collect_circuit_metrics(self):
        """Collect circuit breaker metrics."""
        state = load_circuit_state()
        
        if PROMETHEUS_AVAILABLE:
            CIRCUIT_TRIPPED.set(1 if state.get('is_tripped') else 0)
            CIRCUIT_CONSECUTIVE_LOSSES.set(state.get('consecutive_losses', 0))
            
    def _collect_treasury_metrics(self):
        """Collect treasury metrics."""
        treasury = load_treasury_state()
        
        if PROMETHEUS_AVAILABLE:
            hourly_snap = treasury.get('hourly_snapshot_equity', 0)
            TREASURY_HOURLY_LOSS.set(hourly_snap)
            TREASURY_DAILY_LOSS.set(treasury.get('daily_loss', 0))
            
    def _collect_system_metrics(self):
        """Collect system metrics."""
        if PROMETHEUS_AVAILABLE:
            SYSTEM_UPTIME.set(time.time() - self.start_time)
            SYSTEM_VERSION.info({
                'version': '5.0.0',
                'name': 'Kronos'
            })


# Global collector instance
collector = MetricsCollector()


# ============ HTTP Server ============

if FLASK_AVAILABLE and PROMETHEUS_AVAILABLE:
    app = Flask(__name__)
    
    # Swagger configuration for prometheus_metrics
    if SWAGGER_AVAILABLE:
        swagger_config = {
            "headers": [],
            "specs": [
                {
                    "endpoint": 'apispec',
                    "route": '/apispec.json',
                    "rule_filter": lambda rule: True,
                    "model_filter": lambda tag: True,
                }
            ],
            "static_url_path": "/flasgger_static",
            "swagger_ui": True,
            "specs_route": "/api/docs"
        }
        
        swagger_template = {
            "info": {
                "title": "Kronos Prometheus Metrics API",
                "description": "Prometheus metrics exporter for Kronos trading system. Exposes Prometheus-format metrics for Grafana dashboards.",
                "version": "1.0.0",
                "contact": {
                    "name": "Kronos Support",
                    "email": "support@kronos.trade"
                }
            },
            "basePath": "/",
            "schemes": ["http", "https"],
            "tags": [
                {"name": "Metrics", "description": "Prometheus metrics endpoints"},
                {"name": "Health", "description": "Health check endpoints"},
                {"name": "Debug", "description": "Debug and utility endpoints"}
            ]
        }
        
        Swagger(app, config=swagger_config, template=swagger_template)
    
    @app.route('/metrics')
    def metrics():
        """Expose Prometheus metrics."""
        collector.collect_all()
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
    
    @app.route('/health')
    def health():
        """Health check endpoint."""
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'service': 'kronos-prometheus-exporter'
        })
    
    @app.route('/metrics/positions')
    def positions_metric():
        """Detailed positions JSON for debugging."""
        positions = load_positions()
        return jsonify(positions)
    
    @app.route('/metrics/circuit')
    def circuit_metric():
        """Circuit breaker state JSON."""
        return jsonify(load_circuit_state())
    
    @app.route('/metrics/trades')
    def trades_metric():
        """Recent trades JSON."""
        return jsonify(load_paper_trades()[-20:])
    
    @app.route('/reload')
    def reload():
        """Force reload of all metrics."""
        collector.collect_all()
        return jsonify({'status': 'reloaded', 'timestamp': time.time()})


def create_standalone_server():
    """Create a simple HTTP server without Flask (fallback)."""
    
    class PrometheusHandler:
        def __init__(self):
            self.collector = collector
            
        def collect_and_format(self):
            """Collect metrics and format as Prometheus text."""
            self.collector.collect_all()
            
            output = []
            
            # Equity
            output.append('# HELP kronos_equity_current Current account equity')
            output.append('# TYPE kronos_equity_current gauge')
            output.append(f'kronos_equity_current {self.collector._get_equity()}')
            
            # Positions
            output.append('# HELP kronos_positions_open Number of open positions')
            output.append('# TYPE kronos_positions_open gauge')
            output.append(f'kronos_positions_open {len(load_positions())}')
            
            # Circuit breaker
            circuit = load_circuit_state()
            output.append('# HELP kronos_circuit_breaker_tripped Circuit breaker status')
            output.append('# TYPE kronos_circuit_breaker_tripped gauge')
            output.append(f'kronos_circuit_breaker_tripped {1 if circuit.get("is_tripped") else 0}')
            
            return '\n'.join(output)
        
        def _get_equity(self):
            try:
                sys.path.insert(0, str(ROOT))
                from real_monitor import get_account_balance
                equity, err = get_account_balance()
                return equity if not err else 0
            except:
                return 0
    
    return PrometheusHandler()


# ============ CLI Interface ============

def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Kronos Prometheus Metrics Exporter')
    parser.add_argument('--port', type=int, default=PROMETHEUS_PORT, help='Port to listen on')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--test', action='store_true', help='Test metrics collection')
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    if args.test:
        print("Testing metrics collection...")
        collector.collect_all()
        print("Metrics collection test complete.")
        return
    
    if FLASK_AVAILABLE and PROMETHEUS_AVAILABLE:
        print(f"Starting Kronos Prometheus Exporter on {args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=False)
    else:
        print("Flask or prometheus_client not available. Install with:")
        print("  pip install flask prometheus-client")
        sys.exit(1)


if __name__ == '__main__':
    main()
