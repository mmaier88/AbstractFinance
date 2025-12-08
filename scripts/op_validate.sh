#!/usr/bin/env bash
# op_validate.sh - Validate 1Password CLI access with service account
#
# Usage: ./op_validate.sh
#
# Required environment variables:
#   OP_SERVICE_ACCOUNT_TOKEN - 1Password service account token
#
# This script validates that:
#   1. op CLI is installed
#   2. Service account token is configured
#   3. We can successfully authenticate and list vaults

set -euo pipefail

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
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Check for required environment variable
if [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
    log_error "OP_SERVICE_ACCOUNT_TOKEN environment variable is not set"
    echo ""
    echo "Set it with:"
    echo "  export OP_SERVICE_ACCOUNT_TOKEN='ops_eyJ...'"
    exit 1
fi

# Check if op CLI is installed
if ! command -v op &> /dev/null; then
    log_error "1Password CLI 'op' is not installed or not in PATH"
    echo ""
    echo "Install it with:"
    echo "  # macOS"
    echo "  brew install 1password-cli"
    echo ""
    echo "  # Ubuntu/Debian"
    echo "  curl -sS https://downloads.1password.com/linux/keys/1password.asc | sudo gpg --dearmor --output /usr/share/keyrings/1password-archive-keyring.gpg"
    echo "  echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/amd64 stable main' | sudo tee /etc/apt/sources.list.d/1password.list"
    echo "  sudo apt update && sudo apt install 1password-cli"
    exit 1
fi

# Check op version
OP_VERSION=$(op --version 2>/dev/null || echo "unknown")
log_info "1Password CLI version: ${OP_VERSION}"

# Validate service account access by listing vaults
log_info "Validating service account access..."

if op vault list --format=json > /dev/null 2>&1; then
    log_info "Service account authentication successful!"

    # List accessible vaults
    echo ""
    log_info "Accessible vaults:"
    op vault list --format=json | jq -r '.[] | "  - \(.name) (\(.id))"'

    # Count items (optional, for verification)
    VAULT_COUNT=$(op vault list --format=json | jq '. | length')
    log_info "Total vaults accessible: ${VAULT_COUNT}"

    exit 0
else
    log_error "Failed to authenticate with 1Password"
    echo ""
    echo "Check that:"
    echo "  1. OP_SERVICE_ACCOUNT_TOKEN is set correctly"
    echo "  2. The token hasn't been revoked"
    echo "  3. The service account has vault access configured"
    exit 1
fi
