#!/usr/bin/env bash
#
# backup.sh - Kronos Backup Script
#
# Purpose: Automated backup of critical Kronos system files
# Usage: ./backup.sh [OPTIONS]
#
# Options:
#   --full       Full system backup (default)
#   --state      State files only
#   --code       Code repository only
#   --verify     Verify existing backup
#   --clean      Remove old backups (retention policy)
#   --dry-run    Show what would be done
#

set -euo pipefail

# Configuration
KRONOS_ROOT="${HOME}/kronos"
BACKUP_ROOT="${HOME}/kronos_backups"
RETENTION_DAYS=30
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logging
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Create backup directory
create_backup_dir() {
    local backup_dir="${BACKUP_ROOT}/${TIMESTAMP}"
    mkdir -p "$backup_dir"
    echo "$backup_dir"
}

# Backup state files (critical data)
backup_state_files() {
    log_info "Backing up state files..."
    
    local backup_dir="$1"
    local state_dir="${backup_dir}/state"
    mkdir -p "$state_dir"
    
    # Critical state files
    local state_files=(
        "${KRONOS_ROOT}/decision_journal.jsonl"
        "${KRONOS_ROOT}/data/treasury.json"
        "${KRONOS_ROOT}/data/circuit.json"
        "${KRONOS_ROOT}/paper_trades.json"
        "${KRONOS_ROOT}/dual_strategy_state.json"
        "${KRONOS_ROOT}/multi_direction_state.json"
        "${KRONOS_ROOT}/emergency_stop.json"
        "${KRONOS_ROOT}/factor_context.json"
    )
    
    for file in "${state_files[@]}"; do
        if [[ -f "$file" ]]; then
            cp "$file" "${state_dir}/"
            log_info "  Backed up: $(basename "$file")"
        else
            log_warn "  Not found: $file"
        fi
    done
    
    # Backup JSON state files from root
    for file in "${KRONOS_ROOT}"/*.json; do
        if [[ -f "$file" ]] && [[ "$(basename "$file")" != *"kronos"* ]]; then
            cp "$file" "${state_dir}/"
        fi
    done
    
    log_info "State files backup complete"
}

# Backup configuration files
backup_config() {
    log_info "Backing up configuration files..."
    
    local backup_dir="$1"
    local config_dir="${backup_dir}/config"
    mkdir -p "$config_dir"
    
    local config_files=(
        "${KRONOS_ROOT}/constants.py"
        "${KRONOS_ROOT}/requirements.txt"
        "${KRONOS_ROOT}/requirements_locked.txt"
        "${KRONOS_ROOT}/.cooldown.json"
    )
    
    for file in "${config_files[@]}"; do
        if [[ -f "$file" ]]; then
            cp "$file" "${config_dir}/"
            log_info "  Backed up: $(basename "$file")"
        fi
    done
    
    log_info "Configuration backup complete"
}

# Backup code repository
backup_code() {
    log_info "Backing up code repository..."
    
    local backup_dir="$1"
    local repo_dir="${backup_dir}/repo"
    mkdir -p "$repo_dir"
    
    # Backup git repository
    if [[ -d "${KRONOS_ROOT}/.git" ]]; then
        cp -r "${KRONOS_ROOT}/.git" "${repo_dir}/"
        cp "${KRONOS_ROOT}/.gitignore" "${repo_dir}/"
        log_info "  Backed up: .git"
    fi
    
    # Backup key Python files
    local py_files=(
        "kronos_pilot.py"
        "kronos_auto_guard.py"
        "kronos_heartbeat.py"
        "real_monitor.py"
        "kronos_journal.py"
    )
    
    for file in "${py_files[@]}"; do
        if [[ -f "${KRONOS_ROOT}/$file" ]]; then
            cp "${KRONOS_ROOT}/$file" "${repo_dir}/"
        fi
    done
    
    # Backup core directories structure
    local core_dirs=("core" "strategies" "models" "risk" "data" "execution")
    for dir in "${core_dirs[@]}"; do
        if [[ -d "${KRONOS_ROOT}/$dir" ]]; then
            cp -r "${KRONOS_ROOT}/$dir" "${repo_dir}/"
        fi
    done
    
    log_info "Code repository backup complete"
}

# Backup data files
backup_data() {
    log_info "Backing up data files..."
    
    local backup_dir="$1"
    local data_backup="${backup_dir}/data"
    mkdir -p "$data_backup"
    
    # Backup performance database
    if [[ -f "${KRONOS_ROOT}/performance.db" ]]; then
        cp "${KRONOS_ROOT}/performance.db" "${data_backup}/"
        log_info "  Backed up: performance.db"
    fi
    
    # Backup logs directory
    if [[ -d "${KRONOS_ROOT}/logs" ]]; then
        cp -r "${KRONOS_ROOT}/logs" "${data_backup}/"
        log_info "  Backed up: logs/"
    fi
    
    # Backup audit logs
    if [[ -f "${KRONOS_ROOT}/audit_log.jsonl" ]]; then
        cp "${KRONOS_ROOT}/audit_log.jsonl" "${data_backup}/"
        log_info "  Backed up: audit_log.jsonl"
    fi
    
    log_info "Data files backup complete"
}

# Create backup manifest
create_manifest() {
    local backup_dir="$1"
    cat > "${backup_dir}/MANIFEST.txt" << EOF
Kronos Backup Manifest
=======================
Backup Date: ${TIMESTAMP}
Hostname: $(hostname)
Kronos Root: ${KRONOS_ROOT}

Backup Contents:
$(ls -la "$backup_dir")

Disk Usage:
$(du -sh "$backup_dir" 2>/dev/null || echo "N/A")

EOF
    log_info "Created backup manifest"
}

# Verify backup integrity
verify_backup() {
    local backup_dir="${BACKUP_ROOT}/${1:-latest}"
    
    if [[ ! -d "$backup_dir" ]]; then
        log_error "Backup directory not found: $backup_dir"
        return 1
    fi
    
    log_info "Verifying backup: $backup_dir"
    
    local errors=0
    
    # Check manifest exists
    if [[ ! -f "${backup_dir}/MANIFEST.txt" ]]; then
        log_warn "  MANIFEST.txt not found"
        ((errors++))
    fi
    
    # Check state files
    if [[ -d "${backup_dir}/state" ]]; then
        for file in "${backup_dir}/state"/*.json "${backup_dir}/state"/*.jsonl; do
            if [[ -f "$file" ]]; then
                if ! python3 -c "import json; json.load(open('$file'))" 2>/dev/null; then
                    if [[ "$file" == *.jsonl ]]; then
                        # JSONL files may have multiple JSON objects
                        continue
                    fi
                    log_warn "  Invalid JSON: $(basename "$file")"
                    ((errors++))
                fi
            fi
        done
    fi
    
    # Check database integrity
    if [[ -f "${backup_dir}/data/performance.db" ]]; then
        if ! python3 -c "import sqlite3; conn=sqlite3.connect('$file'); conn.close()" 2>/dev/null; then
            log_warn "  Database may be corrupted: performance.db"
            ((errors++))
        fi
    fi
    
    if [[ $errors -eq 0 ]]; then
        log_info "Backup verification passed"
        return 0
    else
        log_error "Backup verification failed with $errors errors"
        return 1
    fi
}

# Clean old backups
clean_old_backups() {
    log_info "Cleaning backups older than ${RETENTION_DAYS} days..."
    
    if [[ ! -d "${BACKUP_ROOT}" ]]; then
        log_warn "No backup directory found"
        return
    fi
    
    local count=0
    while IFS= read -r dir; do
        rm -rf "$dir"
        log_info "  Removed: $(basename "$dir")"
        ((count++))
    done < <(find "${BACKUP_ROOT}" -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -name "????????_??????")
    
    log_info "Cleaned $count old backup(s)"
}

# Main backup function
do_full_backup() {
    log_info "Starting full backup..."
    local backup_dir
    backup_dir=$(create_backup_dir)
    
    backup_state_files "$backup_dir"
    backup_config "$backup_dir"
    backup_code "$backup_dir"
    backup_data "$backup_dir"
    create_manifest "$backup_dir"
    
    log_info "Full backup complete: $backup_dir"
    echo "$backup_dir"
}

# Main script
main() {
    case "${1:-}" in
        --full|"")
            do_full_backup
            ;;
        --state)
            local backup_dir
            backup_dir=$(create_backup_dir)
            backup_state_files "$backup_dir"
            backup_config "$backup_dir"
            create_manifest "$backup_dir"
            log_info "State backup complete: $backup_dir"
            ;;
        --code)
            local backup_dir
            backup_dir=$(create_backup_dir)
            backup_code "$backup_dir"
            create_manifest "$backup_dir"
            log_info "Code backup complete: $backup_dir"
            ;;
        --verify)
            verify_backup "${2:-}"
            ;;
        --clean)
            clean_old_backups
            ;;
        --dry-run)
            log_info "Dry run - would perform full backup"
            log_info "Backup root: ${BACKUP_ROOT}"
            log_info "Timestamp: ${TIMESTAMP}"
            ;;
        --help)
            echo "Kronos Backup Script"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --full       Full system backup (default)"
            echo "  --state      State files only"
            echo "  --code       Code repository only"
            echo "  --verify     Verify existing backup"
            echo "  --clean      Remove old backups"
            echo "  --dry-run    Show what would be done"
            echo "  --help       Show this help message"
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
}

main "$@"