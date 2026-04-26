#!/bin/bash
# Security Audit Script for Kronos
# Performs basic security checks on dependencies

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements.txt"

echo "=========================================="
echo "Kronos Security Audit"
echo "=========================================="
echo ""

# Check if requirements.txt exists
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "ERROR: requirements.txt not found at $REQUIREMENTS_FILE"
    exit 1
fi

echo "[1/5] Checking for known vulnerable packages..."
pip-audit --strict || true
echo ""

echo "[2/5] Checking for insecure dependencies..."
# Check for packages with known CVEs (basic check)
pip freeze | grep -iE "^(django|flask|requests|urllib3|jinja2|pillow)==" || echo "No critical packages found"
echo ""

echo "[3/5] Checking environment file security..."
ENV_FILE="$PROJECT_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    echo "WARNING: .env file exists - ensure it is in .gitignore"
    # Check for hardcoded secrets
    if grep -qE "(api_key|secret|password|token).*=" "$ENV_FILE" 2>/dev/null; then
        echo "WARNING: Potential secrets found in .env file"
    fi
else
    echo "OK: No .env file found (expected)"
fi
echo ""

echo "[4/5] Checking file permissions..."
# Check that sensitive files have restricted permissions
for file in .env requirements.txt; do
    if [ -f "$PROJECT_ROOT/$file" ]; then
        perms=$(stat -f "%Lp" "$PROJECT_ROOT/$file" 2>/dev/null || stat -c "%a" "$PROJECT_ROOT/$file" 2>/dev/null)
        if [ "$perms" = "644" ] || [ "$perms" = "600" ]; then
            echo "OK: $file has appropriate permissions ($perms)"
        else
            echo "WARNING: $file has permissions $perms (should be 600 or 644)"
        fi
    fi
done
echo ""

echo "[5/5] Checking for SQL injection risks in code..."
# Basic check for potential SQL injection patterns
find "$PROJECT_ROOT" -name "*.py" -exec grep -l "execute.*%" {} \; 2>/dev/null | head -5 || echo "No obvious SQL injection patterns found"
echo ""

echo "=========================================="
echo "Security Audit Complete"
echo "=========================================="
echo ""
echo "Recommendations:"
echo "1. Run 'pip-audit' regularly"
echo "2. Keep dependencies updated"
echo "3. Never commit .env files"
echo "4. Review logs for suspicious activity"
