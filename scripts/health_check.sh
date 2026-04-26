#!/usr/bin/env bash
#
# health_check.sh - Kronos Health Check Script
#
# Purpose: Verify system integrity and operational status
# Usage: ./health_check.sh [OPTIONS]
#
# Options:
#   --quick       Quick health check (30 seconds)
#   --full        Comprehensive health check
#   --components  Check individual components
#   --report      Generate HTML report
#

set -uo pipefail

# Configuration
KRONOS_ROOT="${HOME}/kronos"
BACKUP_ROOT="${HOME}/kronos_backups"
TIMEOUT=5

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Counters
CHECKS_PASSED=0
CHECKS_FAILED=0
CHECKS_WARNED=0

# Logging functions
log_pass() {
    echo -e "${GREEN}[PASS]${NC} $1"
    ((CHECKS_PASSED++))
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    ((CHECKS_FAILED++))
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
    ((CHECKS_WARNED++))
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# Check: Disk space
check_disk_space() {
    log_info "Checking disk space..."
    
    local disk_usage
    disk_usage=$(df "$KRONOS_ROOT" | awk 'NR==2 {print $5}' | sed 's/%//')
    
    if [[ $disk_usage -gt 85 ]]; then
        log_fail "Disk usage critical: ${disk_usage}%"
        return 1
    elif [[ $disk_usage -gt 70 ]]; then
        log_warn "Disk usage high: ${disk_usage}%"
        return 0
    else
        log_pass "Disk space OK: ${disk_usage}% used"
        return 0
    fi
}

# Check: Critical files exist
check_critical_files() {
    log_info "Checking critical files..."
    
    local critical_files=(
        "${KRONOS_ROOT}/decision_journal.jsonl"
        "${KRONOS_ROOT}/constants.py"
        "${KRONOS_ROOT}/kronos_pilot.py"
        "${KRONOS_ROOT}/requirements.txt"
    )
    
    local all_ok=true
    for file in "${critical_files[@]}"; do
        if [[ -f "$file" ]]; then
            log_pass "Found: $(basename "$file")"
        else
            log_fail "Missing: $file"
            all_ok=false
        fi
    done
    
    [[ "$all_ok" == "true" ]]
}

# Check: State files integrity
check_state_files() {
    log_info "Checking state file integrity..."
    
    local state_files=(
        "${KRONOS_ROOT}/data/treasury.json"
        "${KRONOS_ROOT}/data/circuit.json"
    )
    
    local all_ok=true
    for file in "${state_files[@]}"; do
        if [[ -f "$file" ]]; then
            if python3 -c "import json; json.load(open('$file'))" 2>/dev/null; then
                log_pass "Valid JSON: $(basename "$file")"
            else
                log_fail "Invalid JSON: $file"
                all_ok=false
            fi
        else
            log_warn "Not found: $(basename "$file")"
        fi
    done
    
    [[ "$all_ok" == "true" ]]
}

# Check: Process status
check_processes() {
    log_info "Checking running processes..."
    
    local kronos_processes=(
        "kronos_pilot"
        "kronos_auto_guard"
        "kronos_heartbeat"
        "real_monitor"
    )
    
    local any_running=false
    for proc in "${kronos_processes[@]}"; do
        if pgrep -f "$proc" > /dev/null 2>&1; then
            log_pass "Process running: $proc"
            any_running=true
        fi
    done
    
    if [[ "$any_running" == "false" ]]; then
        log_warn "No Kronos processes currently running"
    fi
}

# Check: Recent backup exists
check_backup_status() {
    log_info "Checking backup status..."
    
    if [[ ! -d "${BACKUP_ROOT}" ]]; then
        log_warn "No backup directory found"
        return
    fi
    
    # Find most recent backup
    local latest_backup
    latest_backup=$(find "${BACKUP_ROOT}" -maxdepth 1 -type d -name "????????_??????" | sort -r | head -1)
    
    if [[ -z "$latest_backup" ]]; then
        log_fail "No backups found"
        return
    fi
    
    # Check if backup is recent (within 24 hours)
    local backup_age
    backup_age=$(find "${BACKUP_ROOT}" -maxdepth 1 -type d -name "????????_??????" -mtime -1 | wc -l)
    
    if [[ $backup_age -ge 1 ]]; then
        log_pass "Recent backup exists: $(basename "$latest_backup")"
    else
        log_fail "Backup is older than 24 hours"
    fi
    
    # Check backup manifest
    if [[ -f "${latest_backup}/MANIFEST.txt" ]]; then
        log_pass "Backup manifest exists"
    else
        log_warn "Backup manifest missing"
    fi
}

# Check: API connectivity (OKX)
check_api_connectivity() {
    log_info "Checking API connectivity..."
    
    # Check if network is available
    if ! ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1; then
        log_fail "Network unavailable"
        return 1
    fi
    log_pass "Network connectivity OK"
    
    # Check OKX API (simplified check)
    if command -v curl > /dev/null 2>&1; then
        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "https://www.okx.com" 2>/dev/null || echo "000")
        
        if [[ "$http_code" =~ ^[23] ]]; then
            log_pass "OKX API reachable (HTTP $http_code)"
        elif [[ "$http_code" == "000" ]]; then
            log_warn "OKX API timeout"
        else
            log_warn "OKX API returned HTTP $http_code"
        fi
    else
        log_warn "curl not available, skipping API check"
    fi
}

# Check: Log file status
check_log_files() {
    log_info "Checking log files..."
    
    local log_dir="${KRONOS_ROOT}/logs"
    
    if [[ ! -d "$log_dir" ]]; then
        log_warn "Log directory not found"
        return
    fi
    
    # Check for recent log activity
    local recent_logs
    recent_logs=$(find "$log_dir" -type f -mtime -1 2>/dev/null | wc -l)
    
    if [[ $recent_logs -gt 0 ]]; then
        log_pass "Recent log activity: $recent_logs file(s) updated today"
    else
        log_warn "No recent log activity"
    fi
    
    # Check log file sizes
    local total_size
    total_size=$(du -sh "$log_dir" 2>/dev/null | cut -f1 || echo "0")
    log_info "Log directory size: $total_size"
}

# Check: Python environment
check_python_env() {
    log_info "Checking Python environment..."
    
    # Check Python version
    local python_version
    python_version=$(python3 --version 2>/dev/null | cut -d' ' -f2)
    
    if [[ -n "$python_version" ]]; then
        log_pass "Python version: $python_version"
    else
        log_fail "Python not available"
        return 1
    fi
    
    # Check virtual environment
    if [[ -d "${KRONOS_ROOT}/venv" ]]; then
        log_pass "Virtual environment exists"
    else
        log_warn "No virtual environment found"
    fi
    
    # Check key imports
    local required_modules=("pandas" "numpy" "requests" "sqlite3")
    for module in "${required_modules[@]}"; do
        if python3 -c "import $module" 2>/dev/null; then
            log_pass "Module available: $module"
        else
            log_warn "Module not available: $module"
        fi
    done
}

# Check: Git repository status
check_git_status() {
    log_info "Checking git repository..."
    
    if [[ ! -d "${KRONOS_ROOT}/.git" ]]; then
        log_warn "Not a git repository"
        return
    fi
    
    cd "$KRONOS_ROOT"
    
    # Check for uncommitted changes
    if git diff --quiet 2>/dev/null; then
        log_pass "No uncommitted changes"
    else
        log_warn "Uncommitted changes exist"
    fi
    
    # Check current branch
    local branch
    branch=$(git branch --show-current 2>/dev/null || echo "unknown")
    log_info "Current branch: $branch"
    
    # Check if behind remote
    if command -v git remote > /dev/null 2>&1; then
        if git fetch --quiet 2>/dev/null; then
            local behind
            behind=$(git rev-list --count HEAD..@{upstream} 2>/dev/null || echo "0")
            if [[ "$behind" -gt 0 ]]; then
                log_warn "$behind commit(s) behind remote"
            else
                log_pass "Repository is up to date"
            fi
        fi
    fi
}

# Check: Memory usage
check_memory() {
    log_info "Checking memory usage..."
    
    if [[ -f /proc/meminfo ]]; then
        # Linux
        local mem_available
        mem_available=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
        local mem_total
        mem_total=$(grep MemTotal /proc/meminfo | awk '{print $2}')
        local mem_used_pct=$(( (mem_total - mem_available) * 100 / mem_total ))
        
        if [[ $mem_used_pct -gt 90 ]]; then
            log_fail "Memory usage critical: ${mem_used_pct}%"
        elif [[ $mem_used_pct -gt 75 ]]; then
            log_warn "Memory usage high: ${mem_used_pct}%"
        else
            log_pass "Memory usage OK: ${mem_used_pct}%"
        fi
    elif [[ "$(uname)" == "Darwin" ]]; then
        # macOS
        local mem_usage
        mem_usage=$(vm_stat | grep "Pages active" | awk '{print $3}' | sed 's/%//')
        log_info "Memory pages active (approximate)"
    fi
}

# Quick health check (fast subset)
quick_check() {
    log_info "Running quick health check..."
    echo ""
    
    check_disk_space
    check_critical_files
    check_processes
    check_python_env
    check_backup_status
    
    echo ""
    print_summary
}

# Full health check (comprehensive)
full_check() {
    log_info "Running comprehensive health check..."
    echo ""
    
    check_disk_space
    check_memory
    check_critical_files
    check_state_files
    check_processes
    check_backup_status
    check_api_connectivity
    check_log_files
    check_python_env
    check_git_status
    
    echo ""
    print_summary
}

# Component check
component_check() {
    log_info "Checking individual components..."
    echo ""
    
    local component="${1:-}"
    
    case "$component" in
        disk)
            check_disk_space
            ;;
        memory)
            check_memory
            ;;
        files)
            check_critical_files
            check_state_files
            ;;
        processes)
            check_processes
            ;;
        backup)
            check_backup_status
            ;;
        api)
            check_api_connectivity
            ;;
        logs)
            check_log_files
            ;;
        python)
            check_python_env
            ;;
        git)
            check_git_status
            ;;
        *)
            log_error "Unknown component: $component"
            echo "Use: $0 --components [disk|memory|files|processes|backup|api|logs|python|git]"
            exit 1
            ;;
    esac
    
    echo ""
    print_summary
}

# Print summary
print_summary() {
    log_info "========================================"
    log_info "Health Check Summary"
    log_info "========================================"
    echo ""
    echo -e "  ${GREEN}Passed:${NC}  $CHECKS_PASSED"
    echo -e "  ${YELLOW}Warnings:${NC} $CHECKS_WARNED"
    echo -e "  ${RED}Failed:${NC}  $CHECKS_FAILED"
    echo ""
    
    if [[ $CHECKS_FAILED -gt 0 ]]; then
        echo -e "${RED}System health: CRITICAL${NC}"
        return 2
    elif [[ $CHECKS_WARNED -gt 0 ]]; then
        echo -e "${YELLOW}System health: DEGRADED${NC}"
        return 1
    else
        echo -e "${GREEN}System health: OK${NC}"
        return 0
    fi
}

# Generate HTML report
generate_report() {
    local report_file="${KRONOS_ROOT}/health_report_$(date +%Y%m%d_%H%M%S).html"
    
    log_info "Generating HTML report: $report_file"
    
    cat > "$report_file" << 'EOF'
<!DOCTYPE html>
<html>
<head>
    <title>Kronos Health Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; }
        .pass { color: green; }
        .warn { color: orange; }
        .fail { color: red; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #4CAF50; color: white; }
    </style>
</head>
<body>
    <h1>Kronos Health Report</h1>
    <p>Generated: TIMESTAMP_PLACEHOLDER</p>
EOF
    
    log_info "Report generated: $report_file"
}

# Main script
main() {
    case "${1:-}" in
        --quick)
            quick_check
            ;;
        --full|"")
            full_check
            ;;
        --components)
            component_check "${2:-}"
            ;;
        --report)
            generate_report
            ;;
        --help)
            echo "Kronos Health Check Script"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --quick       Quick health check (30 seconds)"
            echo "  --full        Comprehensive health check"
            echo "  --components  Check individual components"
            echo "  --report      Generate HTML report"
            echo "  --help        Show this help message"
            echo ""
            echo "Components for --components:"
            echo "  disk, memory, files, processes, backup, api, logs, python, git"
            ;;
        *)
            if [[ -n "${1:-}" ]]; then
                log_error "Unknown option: $1"
                echo "Use --help for usage information"
                exit 1
            fi
            full_check
            ;;
    esac
}

main "$@"