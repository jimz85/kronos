#!/bin/bash
# Security Audit Script for Kronos
# Comprehensive security scanning for dependencies, secrets, and file permissions

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements.txt"
REPORT_FILE="$PROJECT_ROOT/security_report_$(date +%Y%m%d_%H%M%S).txt"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "=========================================="
echo "Kronos Security Audit"
echo "=========================================="
echo ""

# Initialize report
echo "Kronos Security Report - $(date)" > "$REPORT_FILE"
echo "==========================================" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"

# Track findings
VULN_COUNT=0
SECRET_COUNT=0
PERM_ISSUES=0

# Function to log to report
log_report() {
    echo "$1" >> "$REPORT_FILE"
}

# Function to print status
print_status() {
    echo -e "${GREEN}[PASS]${NC} $1"
    log_report "[PASS] $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
    log_report "[WARN] $1"
}

print_error() {
    echo -e "${RED}[FAIL]${NC} $1"
    log_report "[FAIL] $1"
}

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
    log_report "[INFO] $1"
}

# ==============================================================================
# 1. PIP-AUDIT: Dependency Vulnerability Scanning
# ==============================================================================
echo -e "${BLUE}[1/5] Running pip-audit for dependency vulnerabilities...${NC}"
echo "" >> "$REPORT_FILE"
echo "=== 1. DEPENDENCY VULNERABILITY SCAN (pip-audit) ===" >> "$REPORT_FILE"

if python3 -m pip show pip-audit &>/dev/null; then
    echo "Running pip-audit..."
    PIP_AUDIT_OUTPUT=$(python3 -m pip_audit --format=json 2>&1 || python3 -m pip_audit 2>&1 || true)
    
    if echo "$PIP_AUDIT_OUTPUT" | grep -q "No known vulnerabilities found\|No vulnerabilities found\|::-.*{" || [ -z "$(echo "$PIP_AUDIT_OUTPUT" | grep -E '漏洞|vuln|VULN|CVE')" ]; then
        print_status "No known vulnerabilities found by pip-audit"
    else
        VULN_COUNT=$(echo "$PIP_AUDIT_OUTPUT" | grep -oiE "CVE-[0-9]{4}-[0-9]+" | sort -u | wc -l || echo "0")
        if [ "$VULN_COUNT" -gt 0 ]; then
            print_error "Found approximately $VULN_COUNT CVEs in dependencies"
            echo "$PIP_AUDIT_OUTPUT" | head -50 >> "$REPORT_FILE"
        else
            print_status "No vulnerabilities found by pip-audit"
        fi
    fi
else
    print_warning "pip-audit not installed, skipping (run: pip install pip-audit)"
    echo "pip-audit not available - install with: pip install pip-audit" >> "$REPORT_FILE"
fi
echo ""

# ==============================================================================
# 2. SAFETY CHECK: Additional Vulnerability Database
# ==============================================================================
echo -e "${BLUE}[2/5] Running safety check for known vulnerabilities...${NC}"
echo "" >> "$REPORT_FILE"
echo "=== 2. SAFETY CHECK (additional vulnerability database) ===" >> "$REPORT_FILE"

if python3 -m pip show safety &>/dev/null; then
    echo "Running safety check..."
    SAFETY_OUTPUT=$(python3 -m safety check --json 2>&1 || python3 -m safety check 2>&1 || true)
    
    if echo "$SAFETY_OUTPUT" | grep -qi "No known vulnerabilities\|No vulnerabilities\|0 vulnerabilities"; then
        print_status "No known vulnerabilities found by safety"
    elif echo "$SAFETY_OUTPUT" | grep -qi "Vulnerabilities found\|vulnerability"; then
        print_error "Safety found vulnerabilities in dependencies"
        echo "$SAFETY_OUTPUT" | head -30 >> "$REPORT_FILE"
    else
        print_info "Safety check completed"
    fi
else
    print_warning "safety not installed (run: pip install safety)"
    echo "safety not available - install with: pip install safety" >> "$REPORT_FILE"
fi
echo ""

# ==============================================================================
# 3. HARDCODED SECRETS DETECTION
# ==============================================================================
echo -e "${BLUE}[3/5] Scanning for hardcoded secrets in code...${NC}"
echo "" >> "$REPORT_FILE"
echo "=== 3. HARDCODED SECRETS SCAN ===" >> "$REPORT_FILE"

# Limit search to key directories (exclude archive, venv, __pycache__)
SEARCH_DIRS="$PROJECT_ROOT/core $PROJECT_ROOT/strategies $PROJECT_ROOT/models $PROJECT_ROOT/risk $PROJECT_ROOT/data $PROJECT_ROOT/execution $PROJECT_ROOT/scripts"

# Patterns for common secrets (simplified regex)
SECRET_PATTERNS=(
    "api[_-]?key.*=.*['\"][A-Za-z0-9]{20,}"
    "secret[_-]?key.*=.*['\"][A-Za-z0-9]{20,}"
    "password.*=.*['\"][^'\"]{8,}"
    "token.*=.*['\"][A-Za-z0-9_\-]{30,}"
    "sk-[A-Za-z0-9]{48}"
    "ghp_[A-Za-z0-9]{36}"
    "xox[baprs]-[A-Za-z0-9]{10,}"
)

SECRET_FILES_FOUND=()

for dir in $SEARCH_DIRS; do
    if [ -d "$dir" ]; then
        for pattern in "${SECRET_PATTERNS[@]}"; do
            while IFS= read -r file; do
                if [ -f "$file" ] && [[ ! "$file" =~ \.env ]] && [[ ! "$file" =~ credentials ]] && [[ ! "$file" =~ secret ]]; then
                    MATCHES=$(grep -n -E "$pattern" "$file" 2>/dev/null || true)
                    if [ -n "$MATCHES" ]; then
                        SECRET_FILES_FOUND+=("$file")
                        print_warning "Potential secret found in: $file"
                        log_report "Potential secret in $file:"
                        echo "$MATCHES" | head -2 >> "$REPORT_FILE"
                        ((SECRET_COUNT++)) || true
                    fi
                fi
            done < <(find "$dir" -name "*.py" -type f 2>/dev/null | head -100)
        done
    fi
done

if [ ${#SECRET_FILES_FOUND[@]} -eq 0 ] || [ -z "${SECRET_FILES_FOUND[0]}" ]; then
    print_status "No hardcoded secrets detected in code"
else
    UNIQUE_FILES=($(printf '%s\n' "${SECRET_FILES_FOUND[@]}" | sort -u))
    print_warning "Found potential secrets in ${#UNIQUE_FILES[@]} files"
fi
echo ""

# ==============================================================================
# 4. FILE PERMISSIONS CHECK
# ==============================================================================
echo -e "${BLUE}[4/5] Checking file permissions...${NC}"
echo "" >> "$REPORT_FILE"
echo "=== 4. FILE PERMISSIONS CHECK ===" >> "$REPORT_FILE"

PERM_ISSUES_FOUND=()

# Check for sensitive files with bad permissions
for pattern in ".env" "*.pem" "*.key" "credentials*" "*secret*" ".aws*"; do
    while IFS= read -r file; do
        if [ -f "$file" ]; then
            perms=$(stat -f "%Lp" "$file" 2>/dev/null || stat -c "%a" "$file" 2>/dev/null || echo "unknown")
            # Sensitive files should not be 644 or world-readable
            if [ "$perms" = "644" ] || [ "$perms" = "666" ] || [ "$perms" = "755" ] || [ "$perms" = "777" ]; then
                print_warning "Sensitive file $file has permissions $perms (should be 600 or 400)"
                log_report "Perm issue: $file is $perms"
                PERM_ISSUES_FOUND+=("$file")
                ((PERM_ISSUES++)) || true
            fi
        fi
    done < <(find "$PROJECT_ROOT" -maxdepth 2 -type f -name "$pattern" 2>/dev/null)
done

if [ ${#PERM_ISSUES_FOUND[@]} -eq 0 ]; then
    print_status "No sensitive file permission issues found"
fi
echo ""

# ==============================================================================
# 5. GENERATE SECURITY REPORT
# ==============================================================================
echo -e "${BLUE}[5/5] Generating security report...${NC}"
echo "" >> "$REPORT_FILE"
echo "=== 5. SECURITY SUMMARY ===" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"

# Check for requirements.txt
if [ -f "$REQUIREMENTS_FILE" ]; then
    REQ_COUNT=$(wc -l < "$REQUIREMENTS_FILE" 2>/dev/null || echo "0")
    print_info "Project has $REQ_COUNT dependencies in requirements.txt"
    log_report "Dependencies tracked: $REQ_COUNT"
fi

# Count Python files in main dirs
PY_COUNT=$(find "$PROJECT_ROOT/core" "$PROJECT_ROOT/strategies" "$PROJECT_ROOT/models" "$PROJECT_ROOT/risk" "$PROJECT_ROOT/data" -name "*.py" -type f 2>/dev/null | wc -l | tr -d ' ')
print_info "Project has $PY_COUNT core Python files"
log_report "Core Python files scanned: $PY_COUNT"

echo "" >> "$REPORT_FILE"
echo "=== FINDINGS SUMMARY ===" >> "$REPORT_FILE"
echo "Vulnerabilities found (pip-audit/safety): $VULN_COUNT" >> "$REPORT_FILE"
echo "Potential hardcoded secrets: $SECRET_COUNT" >> "$REPORT_FILE"
echo "File permission issues: $PERM_ISSUES" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"

# Security score
TOTAL_ISSUES=$((VULN_COUNT + SECRET_COUNT + PERM_ISSUES))
if [ "$TOTAL_ISSUES" -eq 0 ]; then
    SECURITY_SCORE="A (Excellent)"
    print_status "Security Score: $SECURITY_SCORE"
elif [ "$TOTAL_ISSUES" -lt 3 ]; then
    SECURITY_SCORE="B (Good)"
    print_warning "Security Score: $SECURITY_SCORE"
elif [ "$TOTAL_ISSUES" -lt 10 ]; then
    SECURITY_SCORE="C (Fair)"
    print_error "Security Score: $SECURITY_SCORE"
else
    SECURITY_SCORE="F (Poor)"
    print_error "Security Score: $SECURITY_SCORE - Immediate attention required"
fi

log_report "Security Score: $SECURITY_SCORE"
log_report "Total issues: $TOTAL_ISSUES"

echo "" >> "$REPORT_FILE"
echo "=== RECOMMENDATIONS ===" >> "$REPORT_FILE"
echo "1. Install and run 'pip-audit' to check dependencies: pip install pip-audit" >> "$REPORT_FILE"
echo "2. Install and run 'safety' for additional checks: pip install safety" >> "$REPORT_FILE"
echo "3. Use environment variables for all secrets instead of hardcoding" >> "$REPORT_FILE"
echo "4. Ensure sensitive files have 600 or 400 permissions" >> "$REPORT_FILE"
echo "5. Run this security check weekly" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "Report saved to: $REPORT_FILE" >> "$REPORT_FILE"

echo ""
echo "=========================================="
echo "Security Audit Complete"
echo "=========================================="
echo ""
echo -e "${GREEN}Summary:${NC}"
echo "  Vulnerabilities found: $VULN_COUNT"
echo "  Hardcoded secrets: $SECRET_COUNT"
echo "  Permission issues: $PERM_ISSUES"
echo "  Security Score: $SECURITY_SCORE"
echo ""
echo -e "${BLUE}Full report saved to: $REPORT_FILE${NC}"
echo ""
echo "Recommendations:"
echo "1. Install pip-audit: pip install pip-audit"
echo "2. Install safety: pip install safety"
echo "3. Use environment variables for all secrets"
echo "4. Ensure sensitive files have restricted permissions (600 or 400)"
echo "5. Run this security check regularly"
