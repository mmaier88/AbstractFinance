#!/usr/bin/env bash
# op_fetch_secret.sh - Fetch a single secret from 1Password
#
# Usage: ./op_fetch_secret.sh <vault> <item> [field]
#
# Arguments:
#   vault  - Vault name (e.g., "AF - Trading Infra - Staging")
#   item   - Item name (e.g., "ibkr.prod.username")
#   field  - Field name (default: "password" for Login items, "notesPlain" for Secure Notes)
#
# Required environment variables:
#   OP_SERVICE_ACCOUNT_TOKEN - 1Password service account token
#
# Examples:
#   ./op_fetch_secret.sh "AF - Trading Infra - Staging" "ibkr.staging.password" "password"
#   ./op_fetch_secret.sh "AF - Trading Infra - Staging" "telegram.bot-token" "credential"

set -euo pipefail

# Colors for output
RED='\033[0;31m'
NC='\033[0m'

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Validate arguments
if [[ $# -lt 2 ]]; then
    log_error "Missing required arguments"
    echo ""
    echo "Usage: $0 <vault> <item> [field]"
    echo ""
    echo "Arguments:"
    echo "  vault  - Vault name (e.g., 'AF - Trading Infra - Staging')"
    echo "  item   - Item name (e.g., 'ibkr.staging.password')"
    echo "  field  - Field name (default: 'password')"
    echo ""
    echo "Example:"
    echo "  $0 'AF - Trading Infra - Staging' 'ibkr.staging.password' 'password'"
    exit 1
fi

VAULT="$1"
ITEM="$2"
FIELD="${3:-password}"

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

# Fetch the secret using op read
# The format is: op://vault/item/field
SECRET_REF="op://${VAULT}/${ITEM}/${FIELD}"

if ! SECRET=$(op read "${SECRET_REF}" 2>&1); then
    log_error "Failed to fetch secret: ${SECRET_REF}"
    echo "Error: ${SECRET}" >&2
    exit 1
fi

# Output the secret (to stdout, can be captured by caller)
echo -n "${SECRET}"
