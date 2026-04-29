#!/bin/bash
# Kronos Monitoring Stack Startup Script
# Starts Prometheus, Grafana, and Kronos metric exporters

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if Docker is running
check_docker() {
    if ! docker info > /dev/null 2>&1; then
        log_error "Docker is not running. Please start Docker first."
        exit 1
    fi
}

# Start Prometheus and Grafana
start_monitoring_stack() {
    log_info "Starting Prometheus and Grafana..."
    cd "$ROOT_DIR/grafana"
    docker-compose -f docker-compose.monitoring.yml up -d
    log_info "Monitoring stack started!"
    log_info "  - Prometheus: http://localhost:9090"
    log_info "  - Grafana:   http://localhost:3000 (admin/admin)"
}

# Stop monitoring stack
stop_monitoring_stack() {
    log_info "Stopping monitoring stack..."
    cd "$ROOT_DIR/grafana"
    docker-compose -f docker-compose.monitoring.yml down
    log_info "Monitoring stack stopped."
}

# Start Kronos Prometheus exporter
start_prometheus_exporter() {
    log_info "Starting Kronos Prometheus exporter on port 9090..."
    cd "$ROOT_DIR"
    python3 monitoring/prometheus_metrics.py &
    log_info "Prometheus exporter started on http://localhost:9090"
}

# Start Kronos Health Watchdog
start_health_watchdog() {
    log_info "Starting Kronos Health Watchdog on port 9091..."
    cd "$ROOT_DIR"
    python3 monitoring/health_watchdog.py &
    log_info "Health watchdog started on http://localhost:9091"
}

# Stop all Kronos monitoring processes
stop_kronos_services() {
    log_info "Stopping Kronos monitoring services..."
    pkill -f "prometheus_metrics.py" || true
    pkill -f "health_watchdog.py" || true
    log_info "Kronos monitoring services stopped."
}

# Show status
show_status() {
    echo ""
    echo "=== Kronos Monitoring Status ==="
    echo ""
    
    # Check Prometheus
    if curl -s http://localhost:9090/-/healthy > /dev/null 2>&1; then
        log_info "Prometheus: Running (http://localhost:9090)"
    else
        log_warn "Prometheus: Not running"
    fi
    
    # Check Grafana
    if curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
        log_info "Grafana: Running (http://localhost:3000)"
    else
        log_warn "Grafana: Not running"
    fi
    
    # Check Kronos exporter
    if curl -s http://localhost:9090/metrics > /dev/null 2>&1; then
        log_info "Kronos Exporter: Running (http://localhost:9090/metrics)"
    else
        log_warn "Kronos Exporter: Not running"
    fi
    
    # Check Health watchdog
    if curl -s http://localhost:9091/health/live > /dev/null 2>&1; then
        log_info "Health Watchdog: Running (http://localhost:9091)"
    else
        log_warn "Health Watchdog: Not running"
    fi
    
    echo ""
}

# Main command
case "${1:-start}" in
    start)
        check_docker
        start_monitoring_stack
        start_prometheus_exporter
        start_health_watchdog
        echo ""
        show_status
        ;;
    stop)
        stop_kronos_services
        stop_monitoring_stack
        ;;
    restart)
        stop
        sleep 2
        start
        ;;
    status)
        show_status
        ;;
    logs-prometheus)
        docker logs -f kronos-prometheus
        ;;
    logs-grafana)
        docker logs -f kronos-grafana
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs-prometheus|logs-grafana}"
        exit 1
        ;;
esac
