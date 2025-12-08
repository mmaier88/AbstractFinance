#!/usr/bin/env bash
# op_inject_env.sh - Inject secrets from 1Password into environment variables
#
# Usage: source ./op_inject_env.sh <vault>
#        OR
#        eval $(./op_inject_env.sh <vault>)
#
# This script fetches individual secrets from 1Password and exports them
# as environment variables. Useful for CI/CD pipelines.
#
# Arguments:
#   vault - Vault name (e.g., "AF - Trading Infra - Staging")
#
# Required environment variables:
#   OP_SERVICE_ACCOUNT_TOKEN - 1Password service account token
#
# Items expected in vault:
#   - ibkr.staging.username   (username field)
#   - ibkr.staging.password   (password field)
#   - ibkr.staging.totp-key   (password field)
#   - db.staging.password     (password field)
#   - telegram.bot-token      (credential field)
#   - grafana.admin.password  (password field)
#
# Example:
#   export OP_SERVICE_ACCOUNT_TOKEN="ops_eyJ..."
#   source ./op_inject_env.sh "AF - Trading Infra - Staging"
#   echo $IBKR_USERNAME

set -euo pipefail

# Colors for output (only for stderr)
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1" >&2
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Validate arguments
if [[ $# -lt 1 ]]; then
    log_error "Missing vault argument"
    echo "Usage: source $0 <vault>" >&2
    echo "       eval \$($0 <vault>)" >&2
    exit 1
fi

VAULT="$1"

# Check for required environment variable
if [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
    log_error "OP_SERVICE_ACCOUNT_TOKEN environment variable is not set"
    exit 1
fi

# Check if op CLI is installed
if ! command -v op &> /dev/null; then
    log_error "1Password CLI 'op' is not installed"
    exit 1
fi

# Function to safely fetch a secret
fetch_secret() {
    local item="$1"
    local field="${2:-password}"
    local ref="op://${VAULT}/${item}/${field}"

    if op read "${ref}" 2>/dev/null; then
        return 0
    else
        log_error "Failed to fetch: ${ref}" >&2
        return 1
    fi
}

log_info "Fetching secrets from vault: ${VAULT}"

# Fetch and export each secret
# Output export statements that can be eval'd

# IBKR credentials
if IBKR_USERNAME=$(fetch_secret "ibkr.staging.username" "username"); then
    echo "export IBKR_USERNAME='${IBKR_USERNAME}'"
fi

if IBKR_PASSWORD=$(fetch_secret "ibkr.staging.password" "password"); then
    echo "export IBKR_PASSWORD='${IBKR_PASSWORD}'"
fi

if IBKR_TOTP_KEY=$(fetch_secret "ibkr.staging.totp-key" "password"); then
    echo "export IBKR_TOTP_KEY='${IBKR_TOTP_KEY}'"
fi

# Database
if DB_PASSWORD=$(fetch_secret "db.staging.password" "password"); then
    echo "export DB_PASSWORD='${DB_PASSWORD}'"
fi

# Telegram
if TELEGRAM_BOT_TOKEN=$(fetch_secret "telegram.bot-token" "credential"); then
    echo "export TELEGRAM_BOT_TOKEN='${TELEGRAM_BOT_TOKEN}'"
fi

# Grafana
if GRAFANA_PASSWORD=$(fetch_secret "grafana.admin.password" "password"); then
    echo "export GRAFANA_PASSWORD='${GRAFANA_PASSWORD}'"
fi

log_info "Secret injection complete"
