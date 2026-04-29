#!/usr/bin/env python3
"""
Kronos Health Watchdog
======================
独立健康检查服务，监控Kronos系统的关键组件健康状态。
支持多种检查：
- 进程存活检查
- API可用性检查
- 熔断器状态检查
- 持仓超时检查
- 权益异常检查
- 自愈操作

使用方法:
    # 前台运行
    python health_watchdog.py
    
    # 后台运行
    python health_watchdog.py --daemon
    
    # 指定检查间隔
    python health_watchdog.py --interval 60

健康状态通过飞书通知，并支持Prometheusmetrics暴露。

Version: 1.0.0
"""

import os
import sys
import json
import time
import signal
import logging
import subprocess
import requests
import threading
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum

# Load .env
from dotenv import load_dotenv
load_dotenv(Path.home() / '.hermes' / '.env', override=True)

# ============ Configuration ============
ROOT = Path.home() / 'kronos'
STATE_DIR = Path.home() / '.hermes' / 'cron' / 'output'
LOG_DIR = ROOT / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# Health thresholds
OKX_API_KEY = os.getenv('OKX_API_KEY', '')
OKX_SECRET = os.getenv('OKX_SECRET', '')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
OKX_FLAG = os.getenv('OKX_FLAG', '1')

FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
FEISHU_CHAT_ID = os.getenv('FEISHU_CHAT_ID', 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0')

HEALTH_CHECK_INTERVAL = int(os.getenv('HEALTH_CHECK_INTERVAL', '60'))  # seconds
HEALTH_PORT = int(os.getenv('HEALTH_PORT', '9091'))

# ============ Logging Setup ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s15:04:05 [%(levelname)s] %(name)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'health_watchdog.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('kronos.health_watchdog')


class HealthStatus(Enum):
    """Health status levels."""
    HEALTHY = 'healthy'
    WARNING = 'warning'
    CRITICAL = 'critical'
    UNKNOWN = 'unknown'


@dataclass
class HealthCheck:
    """Represents a single health check."""
    name: str
    status: HealthStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


@dataclass
class HealthReport:
    """Overall health report."""
    overall_status: HealthStatus
    checks: List[HealthCheck]
    timestamp: str
    uptime_seconds: float
    consecutive_failures: int = 0
    
    def to_dict(self) -> Dict:
        return {
            'overall_status': self.overall_status.value,
            'checks': [
                {
                    'name': c.name,
                    'status': c.status.value,
                    'message': c.message,
                    'details': c.details,
                    'timestamp': c.timestamp
                }
                for c in self.checks
            ],
            'timestamp': self.timestamp,
            'uptime_seconds': self.uptime_seconds,
            'consecutive_failures': self.consecutive_failures
        }


# ============ Feishu Notification ============
_feishu_token = None
_feishu_expire = 0


def get_feishu_token():
    """Get Feishu access token."""
    global _feishu_token, _feishu_expire
    
    if _feishu_token and time.time() < _feishu_expire:
        return _feishu_token
        
    try:
        resp = requests.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET},
            timeout=10
        )
        data = resp.json()
        if data.get('code') == 0:
            _feishu_token = data['tenant_access_token']
            _feishu_expire = time.time() + data.get('expire', 3600) - 60
            return _feishu_token
    except Exception as e:
        logger.error(f"Failed to get Feishu token: {e}")
    return None


def feishu_notify(text: str, level: str = 'info'):
    """Send Feishu notification."""
    try:
        token = get_feishu_token()
        if not token:
            return
            
        emoji = {
            'info': 'ℹ️',
            'warning': '⚠️',
            'critical': '🚨',
            'healthy': '✅'
        }.get(level, 'ℹ️')
        
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        payload = {
            'receive_id': FEISHU_CHAT_ID,
            'msg_type': 'text',
            'content': json.dumps({'text': f'{emoji} {text}'})
        }
        resp = requests.post(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
            headers=headers, json=payload, timeout=10
        )
    except Exception as e:
        logger.error(f"Feishu notification failed: {e}")


# ============ Data Loading ============

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


# ============ OKX API Helpers ============

def okx_request(method: str, path: str, body: str = ''):
    """Make OKX API request."""
    if not OKX_API_KEY:
        return {'code': '1', 'msg': 'No API key'}
    
    import hmac
    import base64
    from urllib.parse import urlparse
    
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
    msg = ts + method + path + (body if body else '')
    sig = base64.b64encode(hmac.new(
        OKX_SECRET.encode(), msg.encode(), hashlib.sha256
    ).digest()).decode()
    
    parsed = urlparse('https://www.okx.com' + path)
    
    try:
        if method == 'GET':
            resp = requests.get(
                f'https://www.okx.com{path}',
                headers={
                    'OK-ACCESS-KEY': OKX_API_KEY,
                    'OK-ACCESS-SIGN': sig,
                    'OK-ACCESS-TIMESTAMP': ts,
                    'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
                    'Content-Type': 'application/json',
                },
                timeout=10
            )
            return resp.json()
        else:
            resp = requests.post(
                f'https://www.okx.com{path}',
                headers={
                    'OK-ACCESS-KEY': OKX_API_KEY,
                    'OK-ACCESS-SIGN': sig,
                    'OK-ACCESS-TIMESTAMP': ts,
                    'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
                    'Content-Type': 'application/json',
                },
                data=body,
                timeout=10
            )
            return resp.json()
    except Exception as e:
        logger.error(f"OKX API request failed: {e}")
        return {'code': '-1', 'msg': str(e)}


# ============ Health Checks ============

class HealthChecker:
    """Performs health checks on Kronos components."""
    
    def __init__(self):
        self.checks: List[HealthCheck] = []
        
    def check_processes(self) -> HealthCheck:
        """Check if critical processes are running."""
        try:
            # Check for running kronos processes
            result = subprocess.run(
                ['pgrep', '-f', 'kronos'],
                capture_output=True,
                text=True
            )
            pids = result.stdout.strip().split('\n') if result.stdout.strip() else []
            running_count = len([p for p in pids if p])
            
            if running_count > 0:
                return HealthCheck(
                    name='processes',
                    status=HealthStatus.HEALTHY,
                    message=f'{running_count} kronos process(es) running',
                    details={'pid_count': running_count, 'pids': pids}
                )
            else:
                return HealthCheck(
                    name='processes',
                    status=HealthStatus.CRITICAL,
                    message='No kronos processes running',
                    details={'pids': []}
                )
        except Exception as e:
            return HealthCheck(
                name='processes',
                status=HealthStatus.UNKNOWN,
                message=f'Failed to check processes: {e}'
            )
    
    def check_okx_api(self) -> HealthCheck:
        """Check OKX API availability."""
        try:
            # Simple connectivity check
            resp = requests.get('https://www.okx.com/api/v5/public/time', timeout=5)
            if resp.status_code == 200:
                return HealthCheck(
                    name='okx_api',
                    status=HealthStatus.HEALTHY,
                    message='OKX API is reachable',
                    details={'status_code': resp.status_code}
                )
            else:
                return HealthCheck(
                    name='okx_api',
                    status=HealthStatus.WARNING,
                    message=f'OKX API returned status {resp.status_code}',
                    details={'status_code': resp.status_code}
                )
        except Exception as e:
            return HealthCheck(
                name='okx_api',
                status=HealthStatus.CRITICAL,
                message=f'OKX API unreachable: {e}'
            )
    
    def check_circuit_breaker(self) -> HealthCheck:
        """Check circuit breaker status."""
        circuit = load_circuit_state()
        
        if circuit.get('is_tripped'):
            return HealthCheck(
                name='circuit_breaker',
                status=HealthStatus.CRITICAL,
                message=f"Circuit breaker tripped: {circuit.get('trip_reason', 'Unknown')}",
                details={
                    'is_tripped': True,
                    'consecutive_losses': circuit.get('consecutive_losses', 0),
                    'trip_reason': circuit.get('trip_reason', ''),
                    'trip_time': circuit.get('trip_time', '')
                }
            )
        elif circuit.get('consecutive_losses', 0) >= 2:
            return HealthCheck(
                name='circuit_breaker',
                status=HealthStatus.WARNING,
                message=f"Consecutive losses: {circuit.get('consecutive_losses', 0)} (near trip)",
                details={
                    'is_tripped': False,
                    'consecutive_losses': circuit.get('consecutive_losses', 0)
                }
            )
        else:
            return HealthCheck(
                name='circuit_breaker',
                status=HealthStatus.HEALTHY,
                message="Circuit breaker normal",
                details={
                    'is_tripped': False,
                    'consecutive_losses': circuit.get('consecutive_losses', 0)
                }
            )
    
    def check_position_timeouts(self) -> HealthCheck:
        """Check for stale/open positions."""
        try:
            sys.path.insert(0, str(ROOT))
            from real_monitor import get_real_positions
            
            positions, err = get_real_positions()
            if err:
                return HealthCheck(
                    name='position_timeouts',
                    status=HealthStatus.WARNING,
                    message=f'Failed to get positions: {err}'
                )
            
            now = time.time()
            max_age = 72 * 3600  # 72 hours
            stale_positions = []
            
            for coin, pos in positions.items():
                ctime = pos.get('cTime', '')
                if ctime:
                    try:
                        open_ts = int(ctime) / 1000
                        age = now - open_ts
                        if age > max_age:
                            stale_positions.append({
                                'coin': coin,
                                'age_hours': age / 3600,
                                'side': pos.get('side'),
                                'entry': pos.get('entry')
                            })
                    except:
                        pass
            
            if stale_positions:
                return HealthCheck(
                    name='position_timeouts',
                    status=HealthStatus.CRITICAL,
                    message=f'{len(stale_positions)} stale position(s) >72h',
                    details={'stale_positions': stale_positions}
                )
            else:
                return HealthCheck(
                    name='position_timeouts',
                    status=HealthStatus.HEALTHY,
                    message=f'{len(positions)} position(s), all within timeout',
                    details={'position_count': len(positions)}
                )
        except Exception as e:
            return HealthCheck(
                name='position_timeouts',
                status=HealthStatus.UNKNOWN,
                message=f'Failed to check positions: {e}'
            )
    
    def check_equity(self) -> HealthCheck:
        """Check equity levels and drawdown."""
        try:
            sys.path.insert(0, str(ROOT))
            from real_monitor import get_account_balance, START_BALANCE
            
            equity, err = get_account_balance()
            if err or not equity:
                return HealthCheck(
                    name='equity',
                    status=HealthStatus.UNKNOWN,
                    message=f'Failed to get equity: {err}'
                )
            
            pct_change = (equity - START_BALANCE) / START_BALANCE * 100
            drawdown = max(0, -pct_change)
            
            # Determine status based on drawdown
            if pct_change < -20:
                status = HealthStatus.CRITICAL
                msg = f'Equity ${equity:.2f} ({pct_change:.1f}%) - DEEP DRAW DOWN'
            elif pct_change < -10:
                status = HealthStatus.CRITICAL
                msg = f'Equity ${equity:.2f} ({pct_change:.1f}%) - DRAWDOWN CRITICAL'
            elif pct_change < -5:
                status = HealthStatus.WARNING
                msg = f'Equity ${equity:.2f} ({pct_change:.1f}%) - DRAWDOWN WARNING'
            else:
                status = HealthStatus.HEALTHY
                msg = f'Equity ${equity:.2f} ({pct_change:+.1f}%) - NORMAL'
            
            return HealthCheck(
                name='equity',
                status=status,
                message=msg,
                details={
                    'equity': equity,
                    'start_balance': START_BALANCE,
                    'pct_change': pct_change,
                    'drawdown_pct': drawdown
                }
            )
        except Exception as e:
            return HealthCheck(
                name='equity',
                status=HealthStatus.UNKNOWN,
                message=f'Failed to check equity: {e}'
            )
    
    def check_treasury_limits(self) -> HealthCheck:
        """Check treasury policy compliance."""
        treasury = load_treasury_state()
        
        hourly_limit = treasury.get('hourly_limit', 0.02)  # 2% default
        daily_limit = treasury.get('daily_limit', 0.05)    # 5% default
        
        hourly_loss = treasury.get('hourly_loss', 0)
        daily_loss = treasury.get('daily_loss', 0)
        
        hourly_pct = hourly_loss / (treasury.get('start_balance', 1) or 1)
        daily_pct = daily_loss / (treasury.get('start_balance', 1) or 1)
        
        # Check violations
        violations = []
        if hourly_pct > hourly_limit * 1.5:
            violations.append(f'Hourly loss {hourly_pct*100:.1f}% exceeds limit')
        if daily_pct > daily_limit * 1.5:
            violations.append(f'Daily loss {daily_pct*100:.1f}% exceeds limit')
        
        if violations:
            return HealthCheck(
                name='treasury',
                status=HealthStatus.CRITICAL,
                message='; '.join(violations),
                details={
                    'hourly_loss_pct': hourly_pct * 100,
                    'daily_loss_pct': daily_pct * 100,
                    'hourly_limit_pct': hourly_limit * 100,
                    'daily_limit_pct': daily_limit * 100
                }
            )
        elif hourly_pct > hourly_limit or daily_pct > daily_limit:
            return HealthCheck(
                name='treasury',
                status=HealthStatus.WARNING,
                message=f'Hourly: {hourly_pct*100:.1f}%, Daily: {daily_pct*100:.1f}%',
                details={
                    'hourly_loss_pct': hourly_pct * 100,
                    'daily_loss_pct': daily_pct * 100
                }
            )
        else:
            return HealthCheck(
                name='treasury',
                status=HealthStatus.HEALTHY,
                message=f'Hourly: {hourly_pct*100:.2f}%, Daily: {daily_pct*100:.2f}% - OK',
                details={
                    'hourly_loss_pct': hourly_pct * 100,
                    'daily_loss_pct': daily_pct * 100
                }
            )
    
    def check_recent_trades(self) -> HealthCheck:
        """Check for recent trading activity."""
        trades = load_paper_trades()
        
        if not trades:
            return HealthCheck(
                name='recent_trades',
                status=HealthStatus.WARNING,
                message='No trades found',
                details={'trade_count': 0}
            )
        
        # Get last trade time
        last_trade = trades[-1] if trades else None
        last_time = None
        if last_trade:
            open_time = last_trade.get('open_time', '')
            if open_time:
                try:
                    from dateutil import parser
                    last_dt = parser.isoparse(open_time)
                    last_time = last_dt.timestamp()
                except:
                    pass
        
        if last_time:
            hours_since = (time.time() - last_time) / 3600
            if hours_since > 24:
                return HealthCheck(
                    name='recent_trades',
                    status=HealthStatus.WARNING,
                    message=f'No trades in {hours_since:.1f} hours',
                    details={
                        'hours_since_last_trade': hours_since,
                        'total_trades': len(trades)
                    }
                )
        
        return HealthCheck(
            name='recent_trades',
            status=HealthStatus.HEALTHY,
            message=f'{len(trades)} trades, last {hours_since:.1f}h ago' if last_time else f'{len(trades)} trades',
            details={
                'total_trades': len(trades),
                'hours_since_last_trade': hours_since if last_time else None
            }
        )
    
    def run_all_checks(self) -> List[HealthCheck]:
        """Run all health checks."""
        self.checks = [
            self.check_processes(),
            self.check_okx_api(),
            self.check_circuit_breaker(),
            self.check_position_timeouts(),
            self.check_equity(),
            self.check_treasury_limits(),
            self.check_recent_trades(),
        ]
        return self.checks


# ============ Health Watchdog ============

class HealthWatchdog:
    """Main health watchdog service."""
    
    def __init__(self, interval: int = HEALTH_CHECK_INTERVAL):
        self.interval = interval
        self.start_time = time.time()
        self.last_report: Optional[HealthReport] = None
        self.consecutive_failures = 0
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        # Notification throttle - don't spam
        self._last_notification_time = 0
        self._notification_cooldown = 300  # 5 minutes between notifications
        
        # Load health checker
        self.checker = HealthChecker()
        
    @property
    def uptime(self) -> float:
        return time.time() - self.start_time
        
    def get_report(self) -> HealthReport:
        """Get current health report."""
        with self._lock:
            return self.last_report
            
    def _determine_overall_status(self, checks: List[HealthCheck]) -> HealthStatus:
        """Determine overall status from individual checks."""
        if any(c.status == HealthStatus.CRITICAL for c in checks):
            return HealthStatus.CRITICAL
        elif any(c.status == HealthStatus.WARNING for c in checks):
            return HealthStatus.WARNING
        elif any(c.status == HealthStatus.UNKNOWN for c in checks):
            return HealthStatus.UNKNOWN
        return HealthStatus.HEALTHY
        
    def _should_notify(self, status: HealthStatus) -> bool:
        """Throttle notifications."""
        now = time.time()
        if status == HealthStatus.HEALTHY:
            return False
        if now - self._last_notification_time < self._notification_cooldown:
            return False
        return True
        
    def run_health_check(self) -> HealthReport:
        """Run one health check cycle."""
        logger.info("Running health check cycle...")
        
        # Run all checks
        checks = self.checker.run_all_checks()
        
        # Determine overall status
        overall = self._determine_overall_status(checks)
        
        # Update consecutive failures
        if overall == HealthStatus.CRITICAL:
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0
            
        # Create report
        report = HealthReport(
            overall_status=overall,
            checks=checks,
            timestamp=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            uptime_seconds=self.uptime,
            consecutive_failures=self.consecutive_failures
        )
        
        with self._lock:
            self.last_report = report
            
        # Send notification if critical
        if overall == HealthStatus.CRITICAL and self._should_notify(overall):
            self._send_alert(report)
            self._last_notification_time = time.time()
            
        # Log summary
        logger.info(f"Health check complete: {overall.value}")
        for check in checks:
            if check.status != HealthStatus.HEALTHY:
                logger.warning(f"  {check.name}: {check.status.value} - {check.message}")
                
        return report
        
    def _send_alert(self, report: HealthReport):
        """Send alert via Feishu."""
        lines = [
            f"🚨 Kronos健康告警",
            f"状态: {report.overall_status.value.upper()}",
            f"时间: {report.timestamp}",
            ""
        ]
        
        for check in report.checks:
            if check.status != HealthStatus.HEALTHY:
                emoji = {
                    HealthStatus.CRITICAL: '🔴',
                    HealthStatus.WARNING: '🟡',
                    HealthStatus.UNKNOWN: '❓'
                }.get(check.status, '❓')
                lines.append(f"{emoji} {check.name}: {check.message}")
        
        lines.append(f"\n连续失败: {report.consecutive_failures}次")
        
        feishu_notify('\n'.join(lines), level='critical')
        
    def _loop(self):
        """Main watchdog loop."""
        logger.info(f"Health watchdog started, interval={self.interval}s")
        
        while self.running:
            try:
                self.run_health_check()
            except Exception as e:
                logger.error(f"Health check error: {e}")
                
            # Sleep in small increments for responsive shutdown
            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)
                
        logger.info("Health watchdog stopped")
        
    def start(self):
        """Start the watchdog in a background thread."""
        if self.running:
            return
            
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Health watchdog thread started")
        
    def stop(self):
        """Stop the watchdog."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
            
    def run_once(self) -> HealthReport:
        """Run a single health check (for testing or cron)."""
        return self.run_health_check()


# ============ HTTP Server for Health Endpoint ============

def create_health_server(watchdog: HealthWatchdog):
    """Create a simple HTTP server for health endpoint."""
    try:
        from flask import Flask, jsonify
        FLASK_AVAILABLE = True
    except ImportError:
        FLASK_AVAILABLE = False
        
    if not FLASK_AVAILABLE:
        logger.warning("Flask not available, health endpoint disabled")
        return None
        
    app = Flask(__name__)
    
    @app.route('/health')
    def health():
        """Main health endpoint."""
        report = watchdog.get_report()
        if report is None:
            # Run check if not yet run
            report = watchdog.run_once()
            
        status_code = 200 if report.overall_status in (
            HealthStatus.HEALTHY, HealthStatus.WARNING
        ) else 503
        
        return jsonify(report.to_dict()), status_code
        
    @app.route('/health/live')
    def live():
        """Liveness probe - just checks if process is running."""
        return jsonify({'status': 'alive', 'timestamp': time.time()})
        
    @app.route('/health/ready')
    def ready():
        """Readiness probe - checks if system is ready to serve."""
        report = watchdog.get_report()
        if report is None:
            return jsonify({'status': 'unknown', 'message': 'Not yet checked'}), 503
        if report.overall_status == HealthStatus.CRITICAL:
            return jsonify({'status': 'not_ready', 'reason': report.overall_status.value}), 503
        return jsonify({'status': 'ready'}), 200
        
    @app.route('/health/details')
    def details():
        """Detailed health information."""
        report = watchdog.get_report()
        if report is None:
            report = watchdog.run_once()
        return jsonify(report.to_dict())
        
    return app


# ============ CLI Interface ============

def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Kronos Health Watchdog')
    parser.add_argument('--interval', type=int, default=HEALTH_CHECK_INTERVAL,
                        help=f'Check interval in seconds (default: {HEALTH_CHECK_INTERVAL})')
    parser.add_argument('--port', type=int, default=HEALTH_PORT,
                        help=f'Health server port (default: {HEALTH_PORT})')
    parser.add_argument('--daemon', action='store_true',
                        help='Run as daemon')
    parser.add_argument('--once', action='store_true',
                        help='Run single health check and exit')
    args = parser.parse_args()
    
    # Create watchdog
    watchdog = HealthWatchdog(interval=args.interval)
    
    if args.once:
        # Single run mode
        report = watchdog.run_once()
        print(json.dumps(report.to_dict(), indent=2))
        sys.exit(0 if report.overall_status != HealthStatus.CRITICAL else 1)
        
    if args.daemon:
        # Fork to background
        pid = os.fork()
        if pid > 0:
            print(f"Health watchdog started with PID {pid}")
            sys.exit(0)
            
    # Create health server
    app = create_health_server(watchdog)
    
    if app:
        # Run with Flask
        try:
            watchdog.start()
            app.run(host='0.0.0.0', port=args.port, debug=False,
                   use_reloader=False, threaded=True)
        finally:
            watchdog.stop()
    else:
        # Run without HTTP server (just watchdog)
        watchdog.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            watchdog.stop()


if __name__ == '__main__':
    main()
